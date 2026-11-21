"""
Gıda Dedektifi - Ürün Puanlama Motoru (Scoring Engine)

Puan Ağırlıkları (Toplam 100 Puan):
- Besin Değerleri: %60 (Maksimum 60 puan)
- Katkı Maddeleri (E-Kodları): %30 (Maksimum 30 puan)
- Doğal / Temiz İçerik: %10 (Maksimum 10 puan)

Özel Kural:
- Kamuya açık resmi duyuru / bildirim kaydı (is_adulterated=True) varsa
  toplam puan en fazla 10 ile sınırlandırılır ve bilgilendirme notu eklenir.
"""

from typing import Any, Dict, List, Optional

DEFAULT_DISCLAIMER = (
    "Bu skor yalnızca genel bilgilendirme amaçlıdır; tıbbi, diyetetik veya hukuki tavsiye niteliği taşımaz."
)

# 1. Grup: Dikkat edilmesi önerilen katkı maddeleri (Bilgilendirici fonksiyonel tanımlar)
PRIORITY_WATCH_ADDITIVES: Dict[str, str] = {
    "E102": "Tartrazin (Sentetik renklendirici)",
    "E110": "Sunset Yellow (Sentetik renklendirici)",
    "E122": "Azorubin (Sentetik renklendirici)",
    "E124": "Ponceau 4R (Sentetik renklendirici)",
    "E129": "Allura Red (Sentetik renklendirici)",
    "E171": "Titanyum Dioksit (Renklendirici)",
    "E211": "Sodyum Benzoat (Koruyucu)",
    "E220": "Kükürt Dioksit (Koruyucu / antioksidan)",
    "E249": "Potasyum Nitrit (Koruyucu)",
    "E250": "Sodyum Nitrit (Koruyucu)",
    "E251": "Sodyum Nitrat (Koruyucu)",
    "E252": "Potasyum Nitrat (Koruyucu)",
    "E320": "BHA - Bütillenmiş Hidroksianisol (Antioksidan)",
    "E321": "BHT - Bütillenmiş Hidroksitoluen (Antioksidan)",
    "E621": "Monosodyum Glutamat / MSG (Aroma artırıcı)",
    "E950": "Asesülfam K (Tatlandırıcı)",
    "E951": "Aspartam (Tatlandırıcı)",
    "E954": "Sakarin (Tatlandırıcı)",
}

# 2. Grup: Sınırlı tüketimi önerilen bileşenler
MODERATE_WATCH_ADDITIVES: Dict[str, str] = {
    "E202": "Potasyum Sorbat (Koruyucu)",
    "E338": "Fosforik Asit (Asitlik düzenleyici)",
    "E407": "Karragenan (Kıvam artırıcı / stabilizör)",
    "E412": "Guar Gum (Kıvam artırıcı)",
    "E450": "Difosfatlar (Emülgatör / stabilizör)",
    "E451": "Trifosfatlar (Emülgatör / stabilizör)",
    "E452": "Polifosfatlar (Emülgatör / stabilizör)",
    "E466": "Karboksimetil Selüloz (Kıvam artırıcı)",
    "E471": "Yağ Asitlerinin Mono ve Digliseritleri (Emülgatör)",
    "E955": "Sukraloz (Tatlandırıcı)",
}


def _normalize_e_code(code: str) -> str:
    """E-kodunu standart büyük harf formatına dönüştürür (örn: 'e-621' -> 'E621')."""
    cleaned = code.strip().upper().replace("-", "").replace(" ", "")
    if cleaned and not cleaned.startswith("E") and cleaned[0].isdigit():
        cleaned = f"E{cleaned}"
    return cleaned


def _determine_grade(score: int) -> str:
    """0-100 arası puana göre nötr ve bilgilendirici Türkçe derece (grade) döndürür."""
    if score >= 80:
        return "Çok İyi"
    if score >= 60:
        return "İyi"
    if score >= 40:
        return "Orta"
    return "Düşük Skor"


def _opt_float(value: Any) -> Optional[float]:
    """Değeri güvenli şekilde pozitif float'a çevirir; None veya boşsa None döndürür."""
    if value is None or value == "":
        return None
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        return None


def calculate_product_score(
    nutrition: Optional[Dict[str, Optional[float]]] = None,
    additives: Optional[List[str]] = None,
    is_adulterated: bool = False,
    has_palm_oil: bool = False,
    has_glucose_syrup: bool = False,
) -> Dict[str, Any]:
    """
    Ürün besin değerleri, katkı maddeleri, temiz içerik kriterleri ve
    kamuya açık resmi duyuru durumuna göre 0-100 arası puan hesaplar.

    Ağırlıklar:
    - Besin değerleri: %60 (0 - 60 puan)
    - Katkı maddeleri: %30 (0 - 30 puan)
    - Doğal/temiz içerik: %10 (0 - 10 puan)
    """
    nutrition = nutrition or {}
    additives = additives or []
    warnings: List[str] = []

    # -------------------------------------------------------------------------
    # 1. BESİN DEĞERLERİ PUANI (%60 -> Maksimum 60 Puan)
    # -------------------------------------------------------------------------
    nutrition_score = 60.0
    missing_core_nutrients: List[str] = []

    sugar_g = _opt_float(nutrition.get("sugar_g"))
    saturated_fat_g = _opt_float(nutrition.get("saturated_fat_g"))
    salt_g = _opt_float(nutrition.get("salt_g"))
    sodium_mg = _opt_float(nutrition.get("sodium_mg"))
    calories_kcal = _opt_float(nutrition.get("calories_kcal"))
    fiber_g = _opt_float(nutrition.get("fiber_g"))
    protein_g = _opt_float(nutrition.get("protein_g"))

    # Tuz verilmemiş ancak sodyum (mg) verilmişse tuza (g) çevir (Tuz = Sodyum * 2.5)
    effective_salt_g: Optional[float]
    if salt_g is not None and salt_g > 0:
        effective_salt_g = salt_g
    elif sodium_mg is not None and sodium_mg > 0:
        effective_salt_g = (sodium_mg / 1000.0) * 2.5
    elif salt_g is not None:
        effective_salt_g = salt_g
    elif sodium_mg is not None:
        effective_salt_g = 0.0
    else:
        effective_salt_g = None

    # Şeker değerlendirmesi (100g başına)
    if sugar_g is None:
        nutrition_score -= 9.0
        missing_core_nutrients.append("şeker")
    elif sugar_g > 22.5:
        nutrition_score -= 18.0
        warnings.append(f"Yüksek şeker içeriği ({sugar_g:g}g / 100g)")
    elif sugar_g > 10.0:
        nutrition_score -= 10.0
        warnings.append(f"Orta-yüksek şeker içeriği ({sugar_g:g}g / 100g)")
    elif sugar_g > 5.0:
        nutrition_score -= 4.0

    # Doymuş yağ değerlendirmesi (100g başına)
    if saturated_fat_g is None:
        nutrition_score -= 7.5
        missing_core_nutrients.append("doymuş yağ")
    elif saturated_fat_g > 10.0:
        nutrition_score -= 15.0
        warnings.append(f"Yüksek doymuş yağ oranı ({saturated_fat_g:g}g / 100g)")
    elif saturated_fat_g > 5.0:
        nutrition_score -= 8.0
        warnings.append(f"Orta-yüksek doymuş yağ oranı ({saturated_fat_g:g}g / 100g)")
    elif saturated_fat_g > 2.0:
        nutrition_score -= 3.0

    # Tuz / Sodyum değerlendirmesi (100g başına)
    if effective_salt_g is None:
        nutrition_score -= 7.5
        missing_core_nutrients.append("tuz/sodyum")
    elif effective_salt_g > 1.5:
        nutrition_score -= 15.0
        warnings.append(f"Yüksek tuz/sodyum içeriği ({effective_salt_g:.2f}g / 100g)")
    elif effective_salt_g > 0.9:
        nutrition_score -= 8.0
        warnings.append(f"Orta-yüksek tuz/sodyum içeriği ({effective_salt_g:.2f}g / 100g)")
    elif effective_salt_g > 0.3:
        nutrition_score -= 3.0

    # Kalori değerlendirmesi (100g başına)
    if calories_kcal is None:
        nutrition_score -= 6.0
        missing_core_nutrients.append("enerji/kalori")
    elif calories_kcal > 450.0:
        nutrition_score -= 12.0
        warnings.append(f"Yüksek enerji/kalori yoğunluğu ({calories_kcal:g} kcal / 100g)")
    elif calories_kcal > 300.0:
        nutrition_score -= 6.0
    elif calories_kcal > 150.0:
        nutrition_score -= 2.0

    # Pozitif besin öğeleri: Lif ve Protein ödül puanları (yalnızca değer biliniyorsa)
    if fiber_g is not None:
        if fiber_g >= 6.0:
            nutrition_score += 6.0
        elif fiber_g >= 3.0:
            nutrition_score += 3.0

    if protein_g is not None:
        if protein_g >= 10.0:
            nutrition_score += 6.0
        elif protein_g >= 5.0:
            nutrition_score += 3.0

    # Eksik temel besin verisi varsa pozitif bonusların eksik veriyi telafi etmesini engelle ve uyarı ekle
    if missing_core_nutrients:
        max_allowed_nutrition = max(24.0, 60.0 - (7.5 * len(missing_core_nutrients)))
        nutrition_score = min(nutrition_score, max_allowed_nutrition)
        warnings.append(
            "Bazı besin değerleri (örn. şeker, doymuş yağ) okunamadığı/bulunamadığı için puan sınırlı veriye göre hesaplandı."
        )

    nutrition_score = max(0.0, min(60.0, round(nutrition_score, 1)))

    # -------------------------------------------------------------------------
    # 2. KATKI MADDELERİ PUANI (%30 -> Maksimum 30 Puan)
    # -------------------------------------------------------------------------
    additives_score = 30.0
    normalized_codes: List[str] = []

    for raw_code in additives:
        code = _normalize_e_code(raw_code)
        if not code or code in normalized_codes:
            continue
        normalized_codes.append(code)

        if code in PRIORITY_WATCH_ADDITIVES:
            additives_score -= 12.0
            warnings.append(
                f"Dikkat edilmesi önerilen katkı maddesi: {code} - {PRIORITY_WATCH_ADDITIVES[code]}"
            )
        elif code in MODERATE_WATCH_ADDITIVES:
            additives_score -= 7.0
            warnings.append(
                f"Sınırlı tüketimi önerilen bileşen: {code} - {MODERATE_WATCH_ADDITIVES[code]}"
            )
        else:
            additives_score -= 4.0

    if len(normalized_codes) >= 3:
        warnings.append(
            f"Birden fazla katkı maddesi içeriyor ({len(normalized_codes)} adet E-kodu)"
        )

    additives_score = max(0.0, min(30.0, round(additives_score, 1)))

    # -------------------------------------------------------------------------
    # 3. DOĞAL / TEMİZ İÇERİK PUANI (%10 -> Maksimum 10 Puan)
    # -------------------------------------------------------------------------
    clean_content_score = 10.0

    if has_palm_oil:
        clean_content_score -= 5.0
        warnings.append("İçerikte palm yağı yer alıyor")

    if has_glucose_syrup:
        clean_content_score -= 5.0
        warnings.append("İçerikte glikoz/fruktoz şurubu yer alıyor")

    # Katkı maddesi varlığı doğal/temiz içerik puanını da etkiler
    if len(normalized_codes) >= 3:
        clean_content_score -= 4.0
    elif len(normalized_codes) >= 1:
        clean_content_score -= 2.0

    clean_content_score = max(0.0, min(10.0, round(clean_content_score, 1)))

    # -------------------------------------------------------------------------
    # 4. TOPLAM PUAN VE RESMİ DUYURU BİLGİ NOTU KONTROLÜ
    # -------------------------------------------------------------------------
    raw_total = round(nutrition_score + additives_score + clean_content_score)
    final_score = max(0, min(100, int(raw_total)))

    if is_adulterated:
        final_score = min(final_score, 10)
        warnings.insert(
            0,
            "Bilgi Notu: Bu ürün/parti ile ilgili kamuya açık resmi duyuru kaydı bulunmaktadır. "
            "Güncel ve kesin detaylar için T.C. Tarım ve Orman Bakanlığı resmi kayıtlarını kontrol ediniz.",
        )

    grade = _determine_grade(final_score)

    return {
        "score": final_score,
        "grade": grade,
        "warnings": warnings,
        "breakdown": {
            "nutrition_score": nutrition_score,
            "nutrition_max": 60,
            "additives_score": additives_score,
            "additives_max": 30,
            "clean_content_score": clean_content_score,
            "clean_content_max": 10,
            "raw_total_score": max(0, min(100, int(raw_total))),
            "adulteration_penalty_applied": is_adulterated,
        },
        "disclaimer": DEFAULT_DISCLAIMER,
    }
