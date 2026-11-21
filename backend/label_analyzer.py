"""
Gıda Dedektifi - Hibrit Etiket Fotoğrafı Okuma Servisi
(Ücretsiz Yerel RapidOCR + Satır/Y-Koordinat Hizalamalı Türkçe Tablo Ayrıştırıcı + Opsiyonel Gemini Flash Vision)

MİMARİ KURALLAR:
1. Yapay zekâ veya yerel OCR motoru ASLA ürün puanı veya derecesi hesaplamaz.
   Yalnızca fotoğraftaki "İçindekiler" metnini, katkı maddelerini (E-kodları) ve
   100g/100ml başına besin değerlerini yapılandırılmış veri olarak çıkarır.
2. Okunamayan veya etikette bulunmayan besin değerleri ASLA 0.0 yapılmaz; `None` (`null`) olarak tutulur.
3. Nihai puanlama daima `backend/scoring.py` içindeki deterministik motor tarafından yapılır.
4. `GEMINI_API_KEY` tanımlıysa Gemini Flash kullanılır; anahtar boşsa, tanımlı değilse
   veya kota/ağ hatası oluşursa 503 fırlatılmaz, otomatik olarak ücretsiz yerel OCR
   (`rapidocr-onnxruntime`) + Y-koordinat hizalamalı Türkçe Regex ayrıştırıcı devreye girer.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx
from dotenv import load_dotenv
from fastapi import HTTPException

from openfoodfacts import GLUCOSE_SYRUP_KEYWORDS, PALM_OIL_KEYWORDS, _safe_optional_float

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEFAULT_GEMINI_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
)
REQUEST_TIMEOUT_SECONDS = 20.0

BLURRY_IMAGE_ERROR_MSG = (
    "Etiket metni net okunamadı, lütfen İçindekiler ve Besin Değerleri kısmını daha net ve aydınlık çekin."
)

# Tekil (singleton) yerel RapidOCR motoru
_RAPID_OCR_ENGINE = None

# Türk Gıda Kodeksi Referans Alım (RA / GKM - 8400 kJ / 2000 kcal) değerleri
# OCR virgülü kaçırdığında (örn. "4,5 g %5" -> "45 %5") %RA ile çapraz doğrulama için kullanılır
REFERENCE_INTAKE_100G: Dict[str, float] = {
    "fat_g": 70.0,
    "saturated_fat_g": 20.0,
    "carbohydrate_g": 260.0,
    "sugar_g": 90.0,
    "fiber_g": 25.0,
    "protein_g": 50.0,
    "salt_g": 6.0,
}

# Türkçe etiketlerde E-kodu yazılmadan yalnızca adıyla geçen yaygın katkı maddeleri eşlemesi
TURKISH_ADDITIVE_NAME_MAP: Dict[str, str] = {
    "monosodyum glutamat": "E621",
    "monosodyumglutamat": "E621",
    "çin tuzu": "E621",
    "cin tuzu": "E621",
    "sodyum benzoat": "E211",
    "sodyumbenzoat": "E211",
    "potasyum sorbat": "E202",
    "potasyumsorbat": "E202",
    "sodyum nitrit": "E250",
    "sodyumnitrit": "E250",
    "potasyum nitrit": "E249",
    "sodyum nitrat": "E251",
    "potasyum nitrat": "E252",
    "kükürt dioksit": "E220",
    "kukurt dioksit": "E220",
    "sodyum metabisülfit": "E223",
    "sodyum metabisulfit": "E223",
    "titanyum dioksit": "E171",
    "tartrazin": "E102",
    "allura red": "E129",
    "ponceau 4r": "E124",
    "sunset yellow": "E110",
    "azorubin": "E122",
    "aspartam": "E951",
    "asesülfam": "E950",
    "asesulfam": "E950",
    "sukraloz": "E955",
    "sakarin": "E954",
    "karragenan": "E407",
    "karagenan": "E407",
    "guar gam": "E412",
    "guargam": "E412",
    "guar gum": "E412",
    "ksantan gam": "E415",
    "ksantangam": "E415",
    "ksantan gum": "E415",
    "karboksimetil selüloz": "E466",
    "karboksimetil seluloz": "E466",
    "mono ve digliserit": "E471",
    "monovedigliserit": "E471",
    "mono- ve digliserit": "E471",
    "difosfat": "E450",
    "pirofosfat": "E450",
    "trifosfat": "E451",
    "polifosfat": "E452",
    "fosforik asit": "E338",
    "fosforikasit": "E338",
    "sitrik asit": "E330",
    "sitrikasit": "E330",
    "askorbik asit": "E300",
    "askorbikasit": "E300",
    "lesitin": "E322",
    "soya lesitini": "E322",
    "soyalesitini": "E322",
    "ayçiçek lesitini": "E322",
    "aycicek lesitini": "E322",
    "bütillenmiş hidroksianisol": "E320",
    "bütillenmiş hidroksitoluen": "E321",
}

EXTRACTION_SYSTEM_PROMPT = """Sen uzman bir gıda etiketi optik karakter tanıma (OCR) ve veri ayrıştırma asistanısın.
Görevin, gönderilen ürün etiketi fotoğrafındaki Türkçe veya uluslararası "İçindekiler" listesini ve "Besin Değerleri (100 g / 100 ml)" tablosunu okuyup yalnızca yapılandırılmış JSON olarak çıkarmaktır.

KESİN KURALLAR:
1. ASLA ürün puanı, sağlık skoru, derece veya yorum üretme.
2. Yalnızca aşağıda belirtilen JSON şemasını döndür (markdown kod bloğu olmadan saf JSON):
{
  "product_name": "Etikette ürün adı görünüyorsa yaz, görünmüyorsa null",
  "brand": "Etikette marka adı görünüyorsa yaz, görünmüyorsa null",
  "ingredients_text": "Etiketteki İçindekiler metninin tamamı (görünmüyorsa null)",
  "additives": ["E322", "E450"],
  "has_palm_oil": false,
  "has_glucose_syrup": false,
  "has_missing_nutrition": false,
  "nutrition_per_100g": {
    "energy_kcal": null,
    "fat_g": null,
    "saturated_fat_g": null,
    "carbohydrate_g": null,
    "sugar_g": null,
    "salt_g": null,
    "sodium_mg": null,
    "fiber_g": null,
    "protein_g": null
  }
}
3. İçindekiler metninde geçen E-kodlarını (örn: E322, E450, E211) veya isimle yazılmış katkı maddelerini (örn: Soya lesitini -> E322, Mono ve digliseritler -> E471, Difosfat -> E450, Sodyum benzoat -> E211, Potasyum sorbat -> E202, Monosodyum glutamat -> E621, Sitrik asit -> E330) "additives" listesine standart "E123" formatında ekle.
4. İçindekiler metninde palm yağı (hurma/palmiye yağı, palmolein) varsa "has_palm_oil": true yap.
5. İçindekiler metninde glikoz şurubu, fruktoz şurubu, glikoz-fruktoz şurubu, mısır şurubu veya invert şeker şurubu varsa "has_glucose_syrup": true yap.
6. Besin değerleri tablosunda 100 g veya 100 ml sütunundaki sayısal değerleri oku (2 sütun varsa 100g sütununu al). Okunamayan veya tabloda yer almayan alanları ASLA 0 yazma, mutlaka null bırak ve temel değerlerden eksik varsa "has_missing_nutrition": true yap.
"""


def _normalize_tr_ascii(text: str) -> str:
    """Türkçe karakterleri ASCII karşılıklarına dönüştürerek küçük harfli arama metni üretir."""
    return (
        (text or "")
        .replace("İ", "i")
        .replace("I", "i")
        .replace("ı", "i")
        .replace("Ş", "s")
        .replace("ş", "s")
        .replace("Ğ", "g")
        .replace("ğ", "g")
        .replace("Ü", "u")
        .replace("ü", "u")
        .replace("Ö", "o")
        .replace("ö", "o")
        .replace("Ç", "c")
        .replace("ç", "c")
        .lower()
    )


def _clean_base64_and_bytes(
    image_base64: str, default_mime: str = "image/jpeg"
) -> tuple[str, bytes, str]:
    """Base64 dizgesini temizler, ham byte dizisini ve MIME türünü döndürür."""
    raw = (image_base64 or "").strip()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail="Etiket analizi için geçerli bir fotoğraf verisi (image_base64) gönderilmedi.",
        )

    mime_type = default_mime or "image/jpeg"
    if raw.startswith("data:"):
        header, _, encoded = raw.partition(",")
        if not encoded:
            raise HTTPException(
                status_code=400,
                detail="Gönderilen fotoğraf verisi (data URL) formatı geçersiz.",
            )
        mime_match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64$", header.strip())
        if mime_match:
            mime_type = mime_match.group(1)
        raw = encoded.strip()

    cleaned = re.sub(r"\s+", "", raw)
    try:
        decoded = base64.b64decode(cleaned, validate=False)
        if len(decoded) < 32:
            raise ValueError("Görsel verisi çok kısa")
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Gönderilen fotoğraf verisi geçerli bir base64 görseli değil.",
        ) from exc

    return cleaned, decoded, mime_type


def _extract_e_codes_from_text_and_list(
    raw_additives: Optional[List[Any]],
    ingredients_text: Optional[str],
) -> List[str]:
    """
    Hem 'additives' listesinden hem de 'ingredients_text' metninden
    E100-E1599 aralığındaki E-kodlarını ve Türkçe katkı maddesi isimlerini ayıklar.
    """
    codes: List[str] = []

    def _add_code(candidate: str) -> None:
        cleaned = candidate.strip().upper().replace("-", "").replace(" ", "")
        match = re.match(r"^E(\d{3,4})", cleaned)
        if match:
            num = int(match.group(1))
            if 100 <= num <= 1599:
                base_code = f"E{num}"
                if base_code not in codes:
                    codes.append(base_code)

    if isinstance(raw_additives, list):
        for item in raw_additives:
            if isinstance(item, str):
                _add_code(item)

    if ingredients_text:
        for match in re.finditer(
            r"\bE[-\s]?(\d{3,4})[a-zA-Z]?\b", ingredients_text, re.IGNORECASE
        ):
            _add_code(f"E{match.group(1)}")

        lowered = ingredients_text.lower()
        normalized = _normalize_tr_ascii(ingredients_text)
        for name_kw, mapped_e_code in TURKISH_ADDITIVE_NAME_MAP.items():
            if name_kw in lowered or _normalize_tr_ascii(name_kw) in normalized:
                _add_code(mapped_e_code)

    return codes


def _parse_tr_number(raw_num: Optional[str]) -> Optional[float]:
    """Virgüllü veya noktalı sayı dizgesini (örn: '12,5' veya '12.5') float'a çevirir."""
    if not raw_num:
        return None
    cleaned = raw_num.strip().replace(",", ".")
    try:
        val = float(cleaned)
        return max(0.0, val)
    except ValueError:
        return None


def _maybe_recover_decimal_with_ra(
    nutrient_key: str,
    raw_str: str,
    val: Optional[float],
    ra_pct: Optional[float],
) -> Optional[float]:
    """
    OCR virgülü kaçırıp '4,5' yerine '45' okuduğunda, aynı satırdaki %RA (%5 gibi)
    değeriyle çapraz kontrol yaparak gerçek ondalık değeri (4.5) kurtarır.
    """
    if val is None:
        return None
    has_decimal_sep = ("," in (raw_str or "")) or ("." in (raw_str or ""))
    if has_decimal_sep or val < 10.0:
        return val

    ra_ref = REFERENCE_INTAKE_100G.get(nutrient_key)
    if ra_ref and ra_pct is not None and 0 < ra_pct <= 100:
        pct_as_is = (val / ra_ref) * 100.0
        pct_div_10 = ((val / 10.0) / ra_ref) * 100.0
        if abs(round(pct_div_10) - ra_pct) <= 1 and abs(round(pct_as_is) - ra_pct) >= 7:
            return round(val / 10.0, 2)

    return val


def _extract_energy_kcal_from_segment(seg: str) -> Optional[float]:
    """Metin parçasından enerji (kcal) değerini (örn. '782/189', '2050 kJ / 490 kcal', '480 kcal') ayıklar."""
    norm = _normalize_tr_ascii(seg)

    # 1. "782/189" veya "2050 kJ / 490 kcal" gibi kJ / kcal çifti
    slash_match = re.search(
        r"\b(\d{2,4}(?:[.,]\d{1,2})?)\s*(?:kj)?\s*[/|\\]\s*(\d{2,3}(?:[.,]\d{1,2})?)\b",
        norm,
    )
    if slash_match:
        v1 = _parse_tr_number(slash_match.group(1))
        v2 = _parse_tr_number(slash_match.group(2))
        if v1 is not None and v2 is not None:
            # kJ her zaman kcal'den büyüktür (~4.184 katı)
            kcal_candidate = min(v1, v2) if v1 > v2 else v2
            if 0 < kcal_candidate <= 950:
                return kcal_candidate

    # 2. Açıkça "kcal" veya "kalori" birimi yazılmış sayı (8400k/2000kcal referans notunu hariç tut)
    for m in re.finditer(r"\b(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:kcal|kalori)\b", norm):
        val = _parse_tr_number(m.group(1))
        if val is not None and 0 <= val <= 950 and val != 2000:
            return val

    # 3. Yan yana iki sayı varsa ve biri diğerinin ~4.18 katıysa (kJ ve kcal)
    nums = [
        _parse_tr_number(x)
        for x in re.findall(r"(?<![%-])\b(\d{2,4}(?:[.,]\d{1,2})?)\b", norm)
    ]
    valid_nums = [n for n in nums if n is not None and n not in (100.0, 2000.0, 8400.0)]
    for i in range(len(valid_nums) - 1):
        n1, n2 = valid_nums[i], valid_nums[i + 1]
        if n2 > 0 and 3.5 <= (n1 / n2) <= 4.8 and n2 <= 950:
            return n2

    return None


def _extract_first_100g_value_from_tail(
    nutrient_key: str,
    tail_norm: str,
    max_valid: float = 100.0,
) -> Optional[float]:
    """
    Besin adının sağındaki/devamındaki metin parçasından (tail_norm)
    ilk (100g) sütunundaki sayısal değeri ve varsa %RA yüzdesini ayıklar.
    Başka bir besin başlığı başlarsa aramayı orada keser.
    """
    # Başka bir besin satırı başlarsa orada kes (boş satırın veya hatalı eşleşmenin başka satırdaki sayıyı çalmasını önler)
    stop_patterns: Dict[str, str] = {
        "fat_g": r"\b(?:enerji|energy|doymus|saturated|trans|karbonhidrat|carbohydrate|seker|sekerler|sugar|sugars|lif|fiber|fibre|protein|tuz|salt|sodyum|sodium)\b",
        "saturated_fat_g": r"\b(?:enerji|energy|trans|karbonhidrat|carbohydrate|seker|sekerler|sugar|sugars|lif|fiber|fibre|protein|tuz|salt|sodyum|sodium)\b",
        "carbohydrate_g": r"\b(?:enerji|energy|yag|fat|doymus|saturated|seker|sekerler|ilave\s*seker|sugar|sugars|lif|fiber|fibre|protein|tuz|salt|sodyum|sodium)\b",
        "sugar_g": r"\b(?:enerji|energy|yag|fat|doymus|saturated|karbonhidrat|carbohydrate|lif|diyet\s*lifi|fiber|fibre|protein|tuz|salt|sodyum|sodium)\b",
        "fiber_g": r"\b(?:enerji|energy|yag|fat|doymus|saturated|karbonhidrat|carbohydrate|seker|sekerler|sugar|sugars|protein|tuz|salt|sodyum|sodium)\b",
        "protein_g": r"\b(?:enerji|energy|yag|fat|doymus|saturated|karbonhidrat|carbohydrate|seker|sekerler|sugar|sugars|lif|fiber|fibre|tuz|salt|sodyum|sodium|vitamin|kalsiyum)\b",
        "salt_g": r"\b(?:enerji|energy|yag|fat|doymus|saturated|karbonhidrat|carbohydrate|seker|sekerler|sugar|sugars|lif|fiber|fibre|protein|sodyum|sodium|vitamin|kalsiyum|referans|bu\s*urun)\b",
    }
    stop_pat = stop_patterns.get(nutrient_key)
    segment = tail_norm
    if stop_pat:
        stop_m = re.search(stop_pat, segment)
        if stop_m:
            segment = segment[: stop_m.start()]

    # Karşılaştırma sırasında "100g", "100 ml", telefon/kod (örn. "10-1338", "4446967") gibi gürültüleri temizle
    segment_cleaned = re.sub(r"\b100\s*(?:g|gr|ml)\b", " ", segment)
    segment_cleaned = re.sub(r"\b\d{2,4}-\d{2,5}\b", " ", segment_cleaned)

    # Sayı adaylarını bul: önünde '%' olmayan ilk geçerli sayı 100g sütunudur
    # Örn: "17 %24" -> num="17", ra="24"; "4,5 g (1 porsiyon: 1,2 g)" -> num="4,5"
    val_match = re.search(
        r"(?<![%\d-])<?\s*(\d{1,3}(?:[.,]\d{1,3})?)\s*(?:g|gr)?\b",
        segment_cleaned,
    )
    if not val_match:
        return None

    raw_num_str = val_match.group(1)
    val = _parse_tr_number(raw_num_str)
    if val is None or val > max_valid:
        return None

    # Aynı satırda hemen sonrasında %RA değeri var mı kontrol et (örn. "%5", "%55")
    after_num = segment_cleaned[val_match.end() : val_match.end() + 25]
    ra_match = re.search(r"%\s*(\d{1,3})\b", after_num)
    ra_pct = _parse_tr_number(ra_match.group(1)) if ra_match else None

    return _maybe_recover_decimal_with_ra(nutrient_key, raw_num_str, val, ra_pct)


def _group_ocr_boxes_into_rows(
    ocr_items: List[Any],
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    RapidOCR'dan dönen `[[box, text, score], ...]` kutularını Y koordinatlarına (satır hizasına)
    göre gruplar, her satırın içindeki kutuları soldan sağa (X koordinatına göre) sıralar.
    Hem hizalanmış metni hem de zenginleştirilmiş kutu listesini döndürür.
    """
    boxes: List[Dict[str, Any]] = []
    for item in ocr_items or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        raw_box, raw_txt = item[0], str(item[1] or "").strip()
        if not raw_txt or not isinstance(raw_box, (list, tuple)) or len(raw_box) < 4:
            continue
        try:
            xs = [float(pt[0]) for pt in raw_box]
            ys = [float(pt[1]) for pt in raw_box]
        except (ValueError, TypeError, IndexError):
            continue

        height = max(1.0, max(ys) - min(ys))
        boxes.append(
            {
                "text": raw_txt,
                "norm": _normalize_tr_ascii(raw_txt),
                "x_min": min(xs),
                "x_max": max(xs),
                "x_center": sum(xs) / 4.0,
                "y_min": min(ys),
                "y_max": max(ys),
                "y_center": sum(ys) / 4.0,
                "height": height,
            }
        )

    if not boxes:
        return "", []

    # Y merkezine göre yukarıdan aşağıya sırala
    boxes.sort(key=lambda b: (b["y_center"], b["x_min"]))

    rows: List[List[Dict[str, Any]]] = []
    for b in boxes:
        placed = False
        for row in rows:
            row_y = sum(x["y_center"] for x in row) / len(row)
            row_h = sum(x["height"] for x in row) / len(row)
            if abs(b["y_center"] - row_y) <= 0.62 * max(row_h, b["height"]):
                row.append(b)
                placed = True
                break
        if not placed:
            rows.append([b])

    aligned_lines: List[str] = []
    for row in rows:
        row.sort(key=lambda b: b["x_min"])
        aligned_lines.append(" ".join(b["text"] for b in row))

    return "\n".join(aligned_lines), boxes


def _extract_nutrients_from_spatial_boxes(
    boxes: List[Dict[str, Any]],
) -> Dict[str, Optional[float]]:
    """
    RapidOCR metin kutularının Y koordinatlarını kullanarak besin adı kutusu ile
    aynı satırda ve onun SAĞINDA (`x_center > label.x_min`) yer alan `100g` sütun
    değerini eşleştirir (2 sütun varsa soldaki ilk/100g değerini alır).
    """
    extracted: Dict[str, Optional[float]] = {
        "energy_kcal": None,
        "fat_g": None,
        "saturated_fat_g": None,
        "carbohydrate_g": None,
        "sugar_g": None,
        "fiber_g": None,
        "protein_g": None,
        "salt_g": None,
        "sodium_mg": None,
    }
    if not boxes:
        return extracted

    label_specs: List[Tuple[str, str]] = [
        ("energy_kcal", r"\b(?:enerji|energy|kalori)\b"),
        ("saturated_fat_g", r"\b(?:doymus\s*yag|doymus|saturated\s*fat|saturates)\b"),
        ("fat_g", r"\b(?:toplam\s*yag|yag\s*/\s*fat|yag|fat)\b"),
        ("carbohydrate_g", r"\b(?:karbonhidratlar|karbonhidrat|carbohydrate|carbohydrates)\b"),
        ("sugar_g", r"\b(?:ilave\s*seker|sekerler|seker|sugars|sugar)\b"),
        ("fiber_g", r"\b(?:diyet\s*lifi|lif|fiber|fibre)\b"),
        ("protein_g", r"\b(?:proteinler|protein)\b"),
        ("salt_g", r"\b(?:tuz|salt)\b"),
        ("sodium_mg", r"\b(?:sodyum|sodium)\b"),
    ]

    for b in boxes:
        b_norm = b["norm"]
        for key, pat in label_specs:
            if extracted.get(key) is not None:
                continue
            # Toplam yağ ararken "doymuş yağ" veya "trans yağ" kutusunu atla
            if key == "fat_g" and ("doymus" in b_norm or "saturated" in b_norm or "trans" in b_norm):
                continue

            m = re.search(pat, b_norm)
            if not m:
                continue

            # Aynı Y hizasında ve etiketin sağında kalan tüm kutuları soldan sağa (X sırasıyla) topla
            same_row_right = [
                cand
                for cand in boxes
                if cand is not b
                and cand["x_center"] > (b["x_min"] + 6.0)
                and abs(cand["y_center"] - b["y_center"])
                <= 1.18 * max(b["height"], cand["height"])
            ]
            same_row_right.sort(key=lambda c: c["x_min"])

            # Etiket kutusunun içindeki anahtar kelime sonrası kısım + sağındaki kutular
            in_box_tail = b_norm[m.end() :].strip()
            right_texts = " ".join(c["norm"] for c in same_row_right)
            combined_row_tail = f"{in_box_tail} {right_texts}".strip()

            if not combined_row_tail:
                continue

            if key == "energy_kcal":
                val = _extract_energy_kcal_from_segment(combined_row_tail)
                if val is not None:
                    extracted["energy_kcal"] = val
            elif key == "sodium_mg":
                sod_m = re.search(
                    r"(?<![%\d-])<?\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*(mg|g|gr)?\b",
                    combined_row_tail,
                )
                if sod_m:
                    s_val = _parse_tr_number(sod_m.group(1))
                    s_unit = (sod_m.group(2) or ("mg" if "mg" in b_norm else "g")).lower()
                    if s_val is not None:
                        extracted["sodium_mg"] = (
                            round(s_val * 1000.0, 2)
                            if s_unit in ("g", "gr") and s_val < 40
                            else s_val
                        )
            else:
                val = _extract_first_100g_value_from_tail(key, combined_row_tail, max_valid=100.0)
                if val is not None:
                    extracted[key] = val

    return extracted


def _apply_macro_and_calorie_sanity_checks(
    nutr: Dict[str, Optional[float]],
) -> None:
    """
    1. Şeker > Karbonhidrat veya Doymuş Yağ > Toplam Yağ gibi OCR virgül kaymalarını (45 -> 4.5) düzeltir.
    2. Kalori (energy_kcal) biliniyorsa, tek başına toplam kaloriyi aşan 2 haneli tam sayı okumalarını (45 -> 4.5) düzeltir.
    3. Tuz okunamamış ancak Sodyum (mg) okunmuşsa Tuz (g) = (Sodyum_mg / 1000) * 2.5 dönüşümünü uygular.
    """
    fat_g = nutr.get("fat_g")
    sat_fat_g = nutr.get("saturated_fat_g")
    carb_g = nutr.get("carbohydrate_g")
    sugar_g = nutr.get("sugar_g")
    protein_g = nutr.get("protein_g")
    kcal = nutr.get("energy_kcal")

    # Doymuş yağ toplam yağdan büyük olamaz
    if fat_g is not None and sat_fat_g is not None and sat_fat_g > fat_g:
        if (sat_fat_g / 10.0) <= fat_g:
            nutr["saturated_fat_g"] = round(sat_fat_g / 10.0, 2)
            sat_fat_g = nutr["saturated_fat_g"]

    # Şeker karbonhidrattan büyük olamaz
    if carb_g is not None and sugar_g is not None and sugar_g > carb_g:
        if (sugar_g / 10.0) <= carb_g:
            nutr["sugar_g"] = round(sugar_g / 10.0, 2)
            sugar_g = nutr["sugar_g"]

    # Kalori bütçesi kontrolü (1g yağ = 9 kcal, 1g karb/şeker/protein = 4 kcal)
    if kcal is not None and kcal > 0:
        known_fat = fat_g if fat_g is not None else (sat_fat_g or 0.0)
        fat_kcal = known_fat * 9.0 if known_fat * 9.0 <= kcal * 1.15 else 0.0

        for key in ("carbohydrate_g", "sugar_g", "protein_g"):
            val = nutr.get(key)
            if val is not None and val >= 10.0 and float(val).is_integer():
                if (fat_kcal + val * 4.0) > (kcal * 1.18) and (
                    fat_kcal + (val / 10.0) * 4.0
                ) <= (kcal * 1.08):
                    nutr[key] = round(val / 10.0, 2)

    # Sodyum (mg) varsa ve Tuz (g) yoksa: Tuz (g) = (Sodyum_mg / 1000) * 2.5
    if nutr.get("salt_g") is None and nutr.get("sodium_mg") is not None:
        nutr["salt_g"] = round((float(nutr["sodium_mg"]) / 1000.0) * 2.5, 3)


def parse_turkish_label_text(
    raw_text: str,
    ocr_boxes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Yerel OCR'dan çıkan Türkçe etiket metninden ve (varsa) Y-koordinat hizalı kutulardan:
    - İçindekiler metni (`ingredients_text`)
    - Katkı maddeleri (`additives` / E-kodları)
    - Palm yağı ve glikoz/fruktoz şurubu varlığı
    - 100g başına besin değerleri (`energy_kcal`, `fat_g`, `saturated_fat_g`, `carbohydrate_g`,
      `sugar_g`, `salt_g`, `sodium_mg`, `fiber_g`, `protein_g`)
    bilgilerini ayıklar. Okunamayan besin değerlerini ASLA 0 yapmaz, `None` olarak bırakır.
    """
    cleaned_text = re.sub(r"[ \t]+", " ", (raw_text or "")).strip()
    if len(cleaned_text) < 8:
        raise HTTPException(status_code=422, detail=BLURRY_IMAGE_ERROR_MSG)

    normalized_ascii = _normalize_tr_ascii(cleaned_text)

    # 1. İÇİNDEKİLER BÖLÜMÜNÜ AYIKLA
    ingredients_text: Optional[str] = None
    ing_match = re.search(
        r"(?:i\s*ç\s*i\s*n\s*d\s*e\s*k\s*i\s*l\s*e\s*r|icindekiler|ingredients)\s*[:\-]?\s*(.+?)(?=(?:besin\s+de[ğg]er|enerji\s+ve\s+besin|besin\s+ogeleri|nutrition\s+facts|nutritional\s+facts|alerjen\s+uyar|saklama\s+ko[şs]ul|net\s+miktar|üretici|uretici|$))",
        cleaned_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if ing_match:
        extracted_ing = re.sub(r"\s+", " ", ing_match.group(1)).strip(" .:-")
        if len(extracted_ing) >= 4:
            ingredients_text = extracted_ing

    analysis_text = ingredients_text or cleaned_text

    # 2. Y-KOORDİNAT (SATIR HİZALAMASI) İLE TABLO OKUMA (Varsa önce kutulardan eşleştir)
    nutr = _extract_nutrients_from_spatial_boxes(ocr_boxes or [])

    # 3. NORMALİZE TÜRKÇE REGEX İLE METİN ÜZERİNDEN EŞLEŞTİRME (Kutularda eksik kalan alanları tamamlar)
    if nutr["energy_kcal"] is None:
        # Önce "enerji" kelimesinin geçtiği yerden itibaren dene, yoksa tüm metinde "kcal" ara
        en_m = re.search(r"\b(?:enerji|energy)\b(.{0,80})", normalized_ascii, flags=re.DOTALL)
        if en_m:
            nutr["energy_kcal"] = _extract_energy_kcal_from_segment(en_m.group(1))
        if nutr["energy_kcal"] is None:
            nutr["energy_kcal"] = _extract_energy_kcal_from_segment(normalized_ascii)

    regex_specs: List[Tuple[str, str]] = [
        (
            "saturated_fat_g",
            r"\b(?:doymus\s*yag\s*/\s*saturated\s*fat|doymus\s*yag|saturated\s*fat|saturates|doymus)\b\s*(?:\(\s*(?:g|gr)\s*\))?(.{1,95})",
        ),
        (
            "fat_g",
            r"(?<![a-z])(?<!doymus\s)(?<!trans\s)(?:toplam\s*yag|yag\s*/\s*fat|yag|fat)\b\s*(?:\(\s*(?:g|gr)\s*\))?(.{1,80})",
        ),
        (
            "carbohydrate_g",
            r"\b(?:karbonhidrat\s*/\s*carbohydrate|karbonhidratlar|karbonhidrat|carbohydrates|carbohydrate)\b\s*(?:\(\s*(?:g|gr)\s*\))?(.{1,95})",
        ),
        (
            "sugar_g",
            r"\b(?:ilave\s*seker|sekerler\s*/\s*sugars?|seker\s*/\s*sugars?|sekerler|seker|sugars|sugar)\b\s*(?:\(\s*(?:g|gr)\s*\))?(.{1,80})",
        ),
        (
            "fiber_g",
            r"\b(?:diyet\s*lifi|lif\s*/\s*fib(?:er|re)|lif|fiber|fibre)\b\s*(?:\(\s*(?:g|gr)\s*\))?(.{1,70})",
        ),
        (
            "protein_g",
            r"\b(?:proteinler|protein)\b\s*(?:\(\s*(?:g|gr)\s*\))?(.{1,70})",
        ),
        (
            "salt_g",
            r"\b(?:tuz\s*/\s*salt|tuz|salt)\b\s*(?:\(\s*(?:g|gr)\s*\))?(.{1,70})",
        ),
    ]

    for key, pattern in regex_specs:
        if nutr.get(key) is not None:
            continue
        for m in re.finditer(pattern, normalized_ascii, flags=re.IGNORECASE | re.DOTALL):
            val = _extract_first_100g_value_from_tail(key, m.group(1), max_valid=100.0)
            if val is not None:
                nutr[key] = val
                break

    # Sodyum (mg veya g -> mg ve tuz dönüşümü)
    if nutr["sodium_mg"] is None:
        sodium_match = re.search(
            r"\b(?:sodyum|sodium)\s*(?:\(\s*(mg|g|gr)\s*\))?\s*[:\-]?\s*<?\s*(\d{1,4}(?:[.,]\d{1,3})?)\s*(mg|g|gr)?\b",
            normalized_ascii,
            flags=re.IGNORECASE,
        )
        if sodium_match:
            unit_in_label = sodium_match.group(1)
            s_val = _parse_tr_number(sodium_match.group(2))
            unit_after_num = sodium_match.group(3)
            s_unit = (unit_after_num or unit_in_label or "").lower()
            if s_val is not None and s_unit in ("mg", "g", "gr"):
                nutr["sodium_mg"] = (
                    round(s_val * 1000.0, 2)
                    if s_unit in ("g", "gr") and s_val < 40
                    else s_val
                )

    # 4. MAKRO / KALORİ TUTARLILIK KONTROLÜ VE SODYUM -> TUZ (x2.5) DÖNÜŞÜMÜ
    _apply_macro_and_calorie_sanity_checks(nutr)

    energy_kcal = nutr["energy_kcal"]
    fat_g = nutr["fat_g"]
    saturated_fat_g = nutr["saturated_fat_g"]
    carbohydrate_g = nutr["carbohydrate_g"]
    sugar_g = nutr["sugar_g"]
    salt_g = nutr["salt_g"]
    sodium_mg = nutr["sodium_mg"]
    fiber_g = nutr["fiber_g"]
    protein_g = nutr["protein_g"]

    # 5. KATKI MADDELERİ VE ÖZEL BİLEŞENLER
    additives = _extract_e_codes_from_text_and_list([], cleaned_text)

    lowered_full = cleaned_text.lower()
    collapsed_ascii = re.sub(r"\s+", "", normalized_ascii)
    has_palm_oil = any(
        kw in lowered_full
        or _normalize_tr_ascii(kw) in normalized_ascii
        or _normalize_tr_ascii(kw).replace(" ", "") in collapsed_ascii
        for kw in PALM_OIL_KEYWORDS + ("hurma yağı", "hurma yagi")
    )
    has_glucose_syrup = any(
        kw in lowered_full
        or _normalize_tr_ascii(kw) in normalized_ascii
        or _normalize_tr_ascii(kw).replace(" ", "") in collapsed_ascii
        for kw in GLUCOSE_SYRUP_KEYWORDS
    )

    found_any_nutrient = any(
        v is not None
        for v in (
            energy_kcal,
            fat_g,
            saturated_fat_g,
            carbohydrate_g,
            sugar_g,
            salt_g,
            sodium_mg,
            fiber_g,
            protein_g,
        )
    )
    found_any_ingredient_signal = bool(
        ingredients_text
        or additives
        or has_palm_oil
        or has_glucose_syrup
        or any(
            kw in normalized_ascii
            for kw in (
                "icindekiler",
                "besin",
                "enerji",
                "seker",
                "yag",
                "karbonhidrat",
                "protein",
                "tuz",
                "unu",
                "su",
                "sut",
                "kakao",
            )
        )
    )

    # Debug / Kontrol amaçlı backend log özeti
    preview_text = re.sub(r"\s+", " ", cleaned_text)[:320]
    logger.info(
        "[OCR DEBUG] Ham Metin (%d kr): %s%s",
        len(cleaned_text),
        preview_text,
        "..." if len(cleaned_text) > 320 else "",
    )
    logger.info(
        "[OCR DEBUG] 100g Besinler -> kcal=%s, yag=%s, doymus_yag=%s, karb=%s, "
        "seker=%s, lif=%s, protein=%s, tuz=%s, sodyum_mg=%s | E-kodlari=%s",
        energy_kcal,
        fat_g,
        saturated_fat_g,
        carbohydrate_g,
        sugar_g,
        fiber_g,
        protein_g,
        salt_g,
        sodium_mg,
        additives,
    )

    if not found_any_nutrient and not found_any_ingredient_signal:
        raise HTTPException(status_code=422, detail=BLURRY_IMAGE_ERROR_MSG)

    has_missing_nutrition = any(
        v is None for v in (energy_kcal, sugar_g, saturated_fat_g)
    ) or (salt_g is None and sodium_mg is None)

    return {
        "product_name": "Etiket Fotoğrafından Analiz Edilen Ürün",
        "brand": None,
        "ingredients_text": re.sub(r"\s+", " ", analysis_text).strip(),
        "additives": additives,
        "has_palm_oil": has_palm_oil,
        "has_glucose_syrup": has_glucose_syrup,
        "has_missing_nutrition": has_missing_nutrition,
        "nutrition_per_100g": {
            "energy_kcal": energy_kcal,
            "fat_g": fat_g,
            "saturated_fat_g": saturated_fat_g,
            "carbohydrate_g": carbohydrate_g,
            "sugar_g": sugar_g,
            "salt_g": salt_g,
            "sodium_mg": sodium_mg,
            "fiber_g": fiber_g,
            "protein_g": protein_g,
        },
        "ocr_engine": "local_rapidocr",
    }


def _get_rapid_ocr():
    """RapidOCR örneğini ilk kullanımda yükler ve bellekte tutar."""
    global _RAPID_OCR_ENGINE
    if _RAPID_OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            _RAPID_OCR_ENGINE = RapidOCR()
        except ImportError as exc:
            raise HTTPException(
                status_code=500,
                detail="Yerel OCR paketi (rapidocr-onnxruntime) kurulu değil.",
            ) from exc
    return _RAPID_OCR_ENGINE


def _run_local_ocr_sync(image_bytes: bytes) -> Tuple[str, List[Dict[str, Any]]]:
    """
    PIL ve RapidOCR kullanarak görseldeki metin kutularını senkron olarak okur,
    Y koordinatlarına göre satırları hizalar ve (aligned_text, boxes) döndürür.
    """
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            max_dim = max(img.size)
            if max_dim > 1800:
                scale = 1800.0 / max_dim
                new_size = (
                    max(1, int(img.size[0] * scale)),
                    max(1, int(img.size[1] * scale)),
                )
                img = img.resize(new_size)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92)
            processed_bytes = buf.getvalue()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Gönderilen fotoğraf dosyası açılamadı. Lütfen geçerli bir fotoğraf çekin.",
        ) from exc

    ocr_engine = _get_rapid_ocr()
    result, _elapse = ocr_engine(processed_bytes)
    if not result:
        return "", []

    return _group_ocr_boxes_into_rows(result)


def _extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    """Gemini yanıtındaki JSON nesnesini güvenli şekilde ayrıştırır."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass

    return None


async def _try_gemini_extraction(
    api_key: str,
    cleaned_b64: str,
    resolved_mime: str,
) -> Optional[Dict[str, Any]]:
    """
    Geçerli bir GEMINI_API_KEY mevcutsa Gemini Flash ile etiket verisini çıkarmayı dener.
    Herhangi bir hata (kota, anahtar, ağ vb.) durumunda None dönerek yerel OCR'a düşmesini sağlar.
    """
    configured_model = (os.getenv("GEMINI_MODEL") or "").strip()
    candidate_models = (
        [configured_model] + [m for m in DEFAULT_GEMINI_MODELS if m != configured_model]
        if configured_model
        else list(DEFAULT_GEMINI_MODELS)
    )

    request_body = {
        "contents": [
            {
                "parts": [
                    {"text": EXTRACTION_SYSTEM_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": resolved_mime,
                            "data": cleaned_b64,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            for model_name in candidate_models:
                url = f"{GEMINI_API_URL.format(model=model_name)}?key={api_key}"
                try:
                    response = await client.post(url, json=request_body)
                except httpx.RequestError:
                    return None

                if response.status_code == 200:
                    payload = response.json()
                    candidates = payload.get("candidates") or []
                    if candidates:
                        parts = (candidates[0].get("content") or {}).get("parts") or []
                        text_chunks = [
                            p.get("text", "") for p in parts if isinstance(p, dict)
                        ]
                        joined_text = "\n".join(filter(None, text_chunks)).strip()
                        if joined_text:
                            parsed = _extract_json_object(joined_text)
                            if parsed:
                                parsed["ocr_engine"] = f"gemini:{model_name}"
                                return parsed
                    return None

                if response.status_code == 404:
                    continue

                return None
    except Exception:
        return None

    return None


async def extract_label_data_from_image(
    image_base64: str,
    mime_type: str = "image/jpeg",
    barcode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Hibrit etiket okuma fonksiyonu:
    1. `GEMINI_API_KEY` geçerliyse öncelikle Gemini Flash Vision ile yapılandırılmış veriyi okur.
    2. `GEMINI_API_KEY` yoksa veya Gemini hata/kota döndürürse otomatik olarak ücretsiz yerel
       `RapidOCR` + Y-koordinat hizalamalı Türkçe Regex ayrıştırıcı ile etiketi okur.
    3. Okunamayan besin alanlarını ASLA 0.0 yapmaz; `None` (`null`) olarak korur.
    """
    load_dotenv()
    cleaned_b64, image_bytes, resolved_mime = _clean_base64_and_bytes(
        image_base64, mime_type
    )

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    has_valid_key = bool(
        api_key
        and api_key != "your-gemini-api-key-here"
        and not api_key.startswith("your-")
    )

    extracted: Optional[Dict[str, Any]] = None
    if has_valid_key:
        extracted = await _try_gemini_extraction(api_key, cleaned_b64, resolved_mime)

    if extracted is None:
        # Ücretsiz ve API anahtarsız yerel OCR + Y-koordinat hizalamalı Türkçe Regex ayrıştırıcı
        raw_ocr_text, ocr_boxes = await asyncio.to_thread(
            _run_local_ocr_sync, image_bytes
        )
        if not raw_ocr_text or len(raw_ocr_text.strip()) < 8:
            raise HTTPException(status_code=422, detail=BLURRY_IMAGE_ERROR_MSG)
        extracted = parse_turkish_label_text(raw_ocr_text, ocr_boxes=ocr_boxes)

    # Ürün adı ve marka
    product_name = (str(extracted.get("product_name") or "").strip()) or (
        "Etiket Fotoğrafından Analiz Edilen Ürün"
    )
    brands = (str(extracted.get("brand") or "").strip()) or None
    ingredients_text = (str(extracted.get("ingredients_text") or "").strip()) or None

    raw_nutr = extracted.get("nutrition_per_100g")
    if not isinstance(raw_nutr, dict):
        raw_nutr = {}

    raw_sugar = _safe_optional_float(raw_nutr.get("sugar_g"))
    raw_sat_fat = _safe_optional_float(raw_nutr.get("saturated_fat_g"))
    raw_salt = _safe_optional_float(raw_nutr.get("salt_g"))
    raw_sodium = _safe_optional_float(raw_nutr.get("sodium_mg"))
    raw_kcal = _safe_optional_float(raw_nutr.get("energy_kcal"))
    raw_fiber = _safe_optional_float(raw_nutr.get("fiber_g"))
    raw_protein = _safe_optional_float(raw_nutr.get("protein_g"))

    # Sodyum (mg) okunmuş ancak Tuz (g) okunmamışsa: Tuz (g) = (Sodyum_mg / 1000) * 2.5
    if raw_salt is None and raw_sodium is not None:
        raw_salt = round((raw_sodium / 1000.0) * 2.5, 3)

    has_salt_or_sodium = raw_salt is not None or raw_sodium is not None
    has_missing_nutrition = bool(
        extracted.get("has_missing_nutrition")
        or any(v is None for v in (raw_kcal, raw_sugar, raw_sat_fat))
        or not has_salt_or_sodium
    )

    nutrition = {
        "sugar_g": round(raw_sugar, 2) if raw_sugar is not None else None,
        "saturated_fat_g": round(raw_sat_fat, 2) if raw_sat_fat is not None else None,
        "salt_g": round(raw_salt, 3) if raw_salt is not None else None,
        "sodium_mg": round(raw_sodium, 2) if raw_sodium is not None else None,
        "calories_kcal": round(raw_kcal, 1) if raw_kcal is not None else None,
        "fiber_g": round(raw_fiber, 2) if raw_fiber is not None else None,
        "protein_g": round(raw_protein, 2) if raw_protein is not None else None,
    }

    additives = _extract_e_codes_from_text_and_list(
        extracted.get("additives"),
        ingredients_text,
    )

    lowered_ing = (ingredients_text or "").lower()
    normalized_ing = _normalize_tr_ascii(ingredients_text or "")

    has_palm_oil = bool(extracted.get("has_palm_oil")) or any(
        kw in lowered_ing or _normalize_tr_ascii(kw) in normalized_ing
        for kw in PALM_OIL_KEYWORDS + ("hurma yağı", "hurma yagi")
    )
    has_glucose_syrup = bool(extracted.get("has_glucose_syrup")) or any(
        kw in lowered_ing or _normalize_tr_ascii(kw) in normalized_ing
        for kw in GLUCOSE_SYRUP_KEYWORDS
    )

    resolved_barcode = (barcode or "").strip() or "ETIKET-FOTO"

    return {
        "barcode": resolved_barcode,
        "product_name": product_name,
        "brands": brands,
        "image_url": None,
        "ingredients_text": ingredients_text,
        "nutrition": nutrition,
        "additives": additives,
        "has_palm_oil": has_palm_oil,
        "has_glucose_syrup": has_glucose_syrup,
        "has_missing_nutrition": has_missing_nutrition,
        "data_source": "label_photo",
        "ocr_engine": extracted.get("ocr_engine", "local_rapidocr"),
    }
