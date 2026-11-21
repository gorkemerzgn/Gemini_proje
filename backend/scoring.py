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
    "Bu puan yalnızca paylaşılan 100g etiket verilerine dayalı otomatik bir hesaplamadır; "
    "tıbbi tavsiye veya marka değerlendirmesi niteliği taşımaz. Kesin bilgi için ürün ambalajını kontrol ediniz."
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
    kamuya açık resmi duyuru durumuna göre 0-100 arası puan ve şeffaf puan
    kırılım listesi (`score_breakdown`) hesaplar.

    Ağırlıklar:
    - Besin değerleri: %60 (0 - 60 puan)
    - Katkı maddeleri: %30 (0 - 30 puan)
    - Doğal/temiz içerik: %10 (0 - 10 puan)
    """
    nutrition = nutrition or {}
    additives = additives or []
    warnings: List[str] = []
    score_breakdown: List[Dict[str, Any]] = []

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

    missing_fields_count = sum(
        1
        for v in (
            calories_kcal,
            sugar_g,
            saturated_fat_g,
            effective_salt_g,
            fiber_g,
            protein_g,
        )
        if v is None
    )

    known_core_count = sum(
        1
        for v in (sugar_g, saturated_fat_g, effective_salt_g, calories_kcal)
        if v is not None
    )
    known_risk_count = sum(
        1 for v in (sugar_g, saturated_fat_g, effective_salt_g) if v is not None
    )

    # Temel 4 besin değerinden hiçbiri yoksa veya 3 ana risk besininden (şeker, doymuş yağ, tuz) hiçbiri yoksa
    # yanıltıcı 70 gibi varsayılan sayısal puan üretme; "Yetersiz Veri" döndür.
    if not is_adulterated and (
        known_core_count == 0 or (known_risk_count == 0 and known_core_count <= 1)
    ):
        insufficient_msg = (
            "Bu ürün için besin değerleri veritabanında bulunmadığından puan hesaplanamadı."
        )
        return {
            "insufficient_data": True,
            "score": None,
            "grade": "Yetersiz Veri",
            "warnings": [insufficient_msg],
            "score_breakdown": [
                {
                    "label": "Besin Değerleri (100g)",
                    "value_text": "Veri Yok",
                    "impact": 0.0,
                    "reason": insufficient_msg,
                }
            ],
            "cap_applied": False,
            "cap_reason": insufficient_msg,
            "missing_fields_count": missing_fields_count,
            "breakdown": {
                "nutrition_score": 0.0,
                "nutrition_max": 60,
                "additives_score": 0.0,
                "additives_max": 30,
                "clean_content_score": 0.0,
                "clean_content_max": 10,
                "raw_total_score": 0,
                "adulteration_penalty_applied": False,
            },
            "disclaimer": DEFAULT_DISCLAIMER,
        }

    # Şeker değerlendirmesi (100g başına)
    if sugar_g is None:
        nutrition_score -= 9.0
        missing_core_nutrients.append("şeker")
        score_breakdown.append(
            {
                "label": "Şeker (100g)",
                "value_text": "—",
                "impact": -9.0,
                "reason": "100g şeker verisi paylaşılmadığı için nötr varsayılan kesinti uygulandı",
            }
        )
    elif sugar_g > 22.5:
        nutrition_score -= 18.0
        warnings.append(f"Yüksek şeker içeriği ({sugar_g:g}g / 100g)")
        score_breakdown.append(
            {
                "label": "Şeker (100g)",
                "value_text": f"{sugar_g:g} g",
                "impact": -18.0,
                "reason": "100g başına 22.5 g referans eşiğinin üzerinde",
            }
        )
    elif sugar_g > 10.0:
        nutrition_score -= 10.0
        warnings.append(f"Orta-yüksek şeker içeriği ({sugar_g:g}g / 100g)")
        score_breakdown.append(
            {
                "label": "Şeker (100g)",
                "value_text": f"{sugar_g:g} g",
                "impact": -10.0,
                "reason": "100g başına 10 g – 22.5 g orta-üst referans aralığında",
            }
        )
    elif sugar_g > 5.0:
        nutrition_score -= 4.0
        score_breakdown.append(
            {
                "label": "Şeker (100g)",
                "value_text": f"{sugar_g:g} g",
                "impact": -4.0,
                "reason": "100g başına 5 g – 10 g orta referans aralığında",
            }
        )
    else:
        score_breakdown.append(
            {
                "label": "Şeker (100g)",
                "value_text": f"{sugar_g:g} g",
                "impact": 0.0,
                "reason": "100g başına 5 g referans eşiğinin altında",
            }
        )

    # Doymuş yağ değerlendirmesi (100g başına)
    if saturated_fat_g is None:
        nutrition_score -= 7.5
        missing_core_nutrients.append("doymuş yağ")
        score_breakdown.append(
            {
                "label": "Doymuş Yağ (100g)",
                "value_text": "—",
                "impact": -7.5,
                "reason": "100g doymuş yağ verisi paylaşılmadığı için nötr varsayılan kesinti uygulandı",
            }
        )
    elif saturated_fat_g > 10.0:
        nutrition_score -= 15.0
        warnings.append(f"Yüksek doymuş yağ oranı ({saturated_fat_g:g}g / 100g)")
        score_breakdown.append(
            {
                "label": "Doymuş Yağ (100g)",
                "value_text": f"{saturated_fat_g:g} g",
                "impact": -15.0,
                "reason": "100g başına 10 g referans eşiğinin üzerinde",
            }
        )
    elif saturated_fat_g > 5.0:
        nutrition_score -= 8.0
        warnings.append(f"Orta-yüksek doymuş yağ oranı ({saturated_fat_g:g}g / 100g)")
        score_breakdown.append(
            {
                "label": "Doymuş Yağ (100g)",
                "value_text": f"{saturated_fat_g:g} g",
                "impact": -8.0,
                "reason": "100g başına 5 g – 10 g orta-üst referans aralığında",
            }
        )
    elif saturated_fat_g > 2.0:
        nutrition_score -= 3.0
        score_breakdown.append(
            {
                "label": "Doymuş Yağ (100g)",
                "value_text": f"{saturated_fat_g:g} g",
                "impact": -3.0,
                "reason": "100g başına 2 g – 5 g orta referans aralığında",
            }
        )
    else:
        score_breakdown.append(
            {
                "label": "Doymuş Yağ (100g)",
                "value_text": f"{saturated_fat_g:g} g",
                "impact": 0.0,
                "reason": "100g başına 2 g referans eşiğinin altında",
            }
        )

    # Tuz / Sodyum değerlendirmesi (100g başına)
    if effective_salt_g is None:
        nutrition_score -= 7.5
        missing_core_nutrients.append("tuz/sodyum")
        score_breakdown.append(
            {
                "label": "Tuz / Sodyum (100g)",
                "value_text": "—",
                "impact": -7.5,
                "reason": "100g tuz/sodyum verisi paylaşılmadığı için nötr varsayılan kesinti uygulandı",
            }
        )
    elif effective_salt_g > 1.5:
        nutrition_score -= 15.0
        warnings.append(f"Yüksek tuz/sodyum içeriği ({effective_salt_g:.2f}g / 100g)")
        score_breakdown.append(
            {
                "label": "Tuz / Sodyum (100g)",
                "value_text": f"{effective_salt_g:.2f} g",
                "impact": -15.0,
                "reason": "100g başına 1.5 g referans eşiğinin üzerinde",
            }
        )
    elif effective_salt_g > 0.9:
        nutrition_score -= 8.0
        warnings.append(f"Orta-yüksek tuz/sodyum içeriği ({effective_salt_g:.2f}g / 100g)")
        score_breakdown.append(
            {
                "label": "Tuz / Sodyum (100g)",
                "value_text": f"{effective_salt_g:.2f} g",
                "impact": -8.0,
                "reason": "100g başına 0.9 g – 1.5 g orta-üst referans aralığında",
            }
        )
    elif effective_salt_g > 0.3:
        nutrition_score -= 3.0
        score_breakdown.append(
            {
                "label": "Tuz / Sodyum (100g)",
                "value_text": f"{effective_salt_g:.2f} g",
                "impact": -3.0,
                "reason": "100g başına 0.3 g – 0.9 g orta referans aralığında",
            }
        )
    else:
        score_breakdown.append(
            {
                "label": "Tuz / Sodyum (100g)",
                "value_text": f"{effective_salt_g:.2f} g",
                "impact": 0.0,
                "reason": "100g başına 0.3 g referans eşiğinin altında",
            }
        )

    # Kalori değerlendirmesi (100g başına)
    if calories_kcal is None:
        nutrition_score -= 6.0
        missing_core_nutrients.append("enerji/kalori")
        score_breakdown.append(
            {
                "label": "Enerji (100g)",
                "value_text": "—",
                "impact": -6.0,
                "reason": "100g enerji (kcal) verisi paylaşılmadığı için nötr varsayılan kesinti uygulandı",
            }
        )
    elif calories_kcal > 450.0:
        nutrition_score -= 12.0
        warnings.append(f"Yüksek enerji/kalori yoğunluğu ({calories_kcal:g} kcal / 100g)")
        score_breakdown.append(
            {
                "label": "Enerji (100g)",
                "value_text": f"{calories_kcal:g} kcal",
                "impact": -12.0,
                "reason": "100g başına 450 kcal referans eşiğinin üzerinde",
            }
        )
    elif calories_kcal > 300.0:
        nutrition_score -= 6.0
        score_breakdown.append(
            {
                "label": "Enerji (100g)",
                "value_text": f"{calories_kcal:g} kcal",
                "impact": -6.0,
                "reason": "100g başına 300 – 450 kcal orta-üst referans aralığında",
            }
        )
    elif calories_kcal > 150.0:
        nutrition_score -= 2.0
        score_breakdown.append(
            {
                "label": "Enerji (100g)",
                "value_text": f"{calories_kcal:g} kcal",
                "impact": -2.0,
                "reason": "100g başına 150 – 300 kcal orta referans aralığında",
            }
        )
    else:
        score_breakdown.append(
            {
                "label": "Enerji (100g)",
                "value_text": f"{calories_kcal:g} kcal",
                "impact": 0.0,
                "reason": "100g başına 150 kcal referans eşiğinin altında",
            }
        )

    # Pozitif besin öğeleri: Lif ve Protein ödül puanları (yalnızca değer biliniyorsa)
    if fiber_g is None:
        score_breakdown.append(
            {
                "label": "Lif (100g)",
                "value_text": "—",
                "impact": 0.0,
                "reason": "Etikette lif verisi belirtilmemiş (ek puan uygulanmadı)",
            }
        )
    elif fiber_g >= 6.0:
        nutrition_score += 6.0
        score_breakdown.append(
            {
                "label": "Lif (100g)",
                "value_text": f"{fiber_g:g} g",
                "impact": 6.0,
                "reason": "100g başına 6 g ve üzeri lif katkısı",
            }
        )
    elif fiber_g >= 3.0:
        nutrition_score += 3.0
        score_breakdown.append(
            {
                "label": "Lif (100g)",
                "value_text": f"{fiber_g:g} g",
                "impact": 3.0,
                "reason": "100g başına 3 g – 6 g lif katkısı",
            }
        )
    else:
        score_breakdown.append(
            {
                "label": "Lif (100g)",
                "value_text": f"{fiber_g:g} g",
                "impact": 0.0,
                "reason": "100g başına 3 g ek puan eşiğinin altında",
            }
        )

    if protein_g is None:
        score_breakdown.append(
            {
                "label": "Protein (100g)",
                "value_text": "—",
                "impact": 0.0,
                "reason": "Etikette protein verisi belirtilmemiş (ek puan uygulanmadı)",
            }
        )
    elif protein_g >= 10.0:
        nutrition_score += 6.0
        score_breakdown.append(
            {
                "label": "Protein (100g)",
                "value_text": f"{protein_g:g} g",
                "impact": 6.0,
                "reason": "100g başına 10 g ve üzeri protein katkısı",
            }
        )
    elif protein_g >= 5.0:
        nutrition_score += 3.0
        score_breakdown.append(
            {
                "label": "Protein (100g)",
                "value_text": f"{protein_g:g} g",
                "impact": 3.0,
                "reason": "100g başına 5 g – 10 g protein katkısı",
            }
        )
    else:
        score_breakdown.append(
            {
                "label": "Protein (100g)",
                "value_text": f"{protein_g:g} g",
                "impact": 0.0,
                "reason": "100g başına 5 g ek puan eşiğinin altında",
            }
        )

    # Eksik temel besin verisi varsa pozitif bonusların eksik veriyi telafi etmesini engelle ve uyarı ekle
    cap_applied = False
    cap_reason: Optional[str] = None

    if missing_core_nutrients:
        max_allowed_nutrition = max(24.0, 60.0 - (7.5 * len(missing_core_nutrients)))
        nutrition_score = min(nutrition_score, max_allowed_nutrition)
        cap_applied = True
        cap_reason = (
            f"Eksik temel besin verisi ({', '.join(missing_core_nutrients)}) nedeniyle "
            f"besin kategorisi puanı en fazla {max_allowed_nutrition:g}/60 ile sınırlandırıldı."
        )
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
    additives_impact = round(additives_score - 30.0, 1)

    if not normalized_codes:
        score_breakdown.append(
            {
                "label": "Katkı Maddeleri (E-Kodları)",
                "value_text": "Tespit edilmedi",
                "impact": 0.0,
                "reason": "Etiket verisinde E-kodu katkı maddesi yer almıyor",
            }
        )
    else:
        preview_codes = ", ".join(normalized_codes[:4]) + (
            "..." if len(normalized_codes) > 4 else ""
        )
        score_breakdown.append(
            {
                "label": "Katkı Maddeleri (E-Kodları)",
                "value_text": f"{preview_codes} ({len(normalized_codes)} adet)",
                "impact": additives_impact,
                "reason": f"Etikette yer alan {len(normalized_codes)} adet E-kodunun fonksiyonel grup ağırlığına göre hesaplandı",
            }
        )

    # -------------------------------------------------------------------------
    # 3. DOĞAL / TEMİZ İÇERİK PUANI (%10 -> Maksimum 10 Puan)
    # -------------------------------------------------------------------------
    clean_content_score = 10.0
    clean_flags: List[str] = []

    if has_palm_oil:
        clean_content_score -= 5.0
        clean_flags.append("Palm yağı")
        warnings.append("İçerikte palm yağı yer alıyor")

    if has_glucose_syrup:
        clean_content_score -= 5.0
        clean_flags.append("Glikoz/fruktoz şurubu")
        warnings.append("İçerikte glikoz/fruktoz şurubu yer alıyor")

    # Katkı maddesi varlığı doğal/temiz içerik puanını da etkiler
    if len(normalized_codes) >= 3:
        clean_content_score -= 4.0
        clean_flags.append(f"{len(normalized_codes)} adet E-kodu")
    elif len(normalized_codes) >= 1:
        clean_content_score -= 2.0
        clean_flags.append(f"{len(normalized_codes)} adet E-kodu")

    clean_content_score = max(0.0, min(10.0, round(clean_content_score, 1)))
    clean_impact = round(clean_content_score - 10.0, 1)

    if clean_impact == 0.0:
        score_breakdown.append(
            {
                "label": "İçerik Profili",
                "value_text": "Sade içerik",
                "impact": 0.0,
                "reason": "Palm yağı, glikoz/fruktoz şurubu veya katkı maddesi tespit edilmedi",
            }
        )
    else:
        score_breakdown.append(
            {
                "label": "İçerik Profili",
                "value_text": ", ".join(clean_flags),
                "impact": clean_impact,
                "reason": "Palm yağı, glikoz/fruktoz şurubu ve katkı sayısı kriterlerine göre hesaplandı",
            }
        )

    # -------------------------------------------------------------------------
    # 4. TOPLAM PUAN VE RESMİ DUYURU BİLGİ NOTU KONTROLÜ
    # -------------------------------------------------------------------------
    raw_total = round(nutrition_score + additives_score + clean_content_score)
    final_score = max(0, min(100, int(raw_total)))

    if is_adulterated:
        final_score = min(final_score, 10)
        cap_applied = True
        cap_reason = (
            "Kamuya açık resmi duyuru kaydı nedeniyle toplam puan en fazla 10/100 ile sınırlandırıldı."
        )
        warnings.insert(
            0,
            "Bilgi Notu: Bu ürün/parti ile ilgili kamuya açık resmi duyuru kaydı bulunmaktadır. "
            "Güncel ve kesin detaylar için T.C. Tarım ve Orman Bakanlığı resmi kayıtlarını kontrol ediniz.",
        )

    grade = _determine_grade(final_score)

    return {
        "insufficient_data": False,
        "score": final_score,
        "grade": grade,
        "warnings": warnings,
        "score_breakdown": score_breakdown,
        "cap_applied": cap_applied,
        "cap_reason": cap_reason,
        "missing_fields_count": missing_fields_count,
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
