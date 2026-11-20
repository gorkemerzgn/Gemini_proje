"""
Gıda Dedektifi - Open Food Facts API Entegrasyon Servisi

Barkod numarası ile Open Food Facts (v2 API) üzerinden ürün bilgilerini,
100g başına besin değerlerini, katkı maddelerini (E-kodları) ve içerik analizini
asenkron olarak çeker.
"""

import re
from typing import Any, Dict, List, Optional
import httpx
from fastapi import HTTPException

OPENFOODFACTS_API_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
USER_AGENT = "IremApp/0.3 (contact@iremapp.local)"
REQUEST_TIMEOUT_SECONDS = 10.0

PALM_OIL_KEYWORDS = (
    "palm yağı",
    "palm yagi",
    "palmiye yağı",
    "palmiye yagi",
    "palm oil",
    "palm fat",
    "palmolein",
    "huile de palme",
)

GLUCOSE_SYRUP_KEYWORDS = (
    "glikoz şurubu",
    "glikoz surubu",
    "glukoz şurubu",
    "glukoz surubu",
    "fruktoz şurubu",
    "fruktoz surubu",
    "glikoz-fruktoz",
    "fruktoz-glikoz",
    "mısır şurubu",
    "misir surubu",
    "nişasta bazlı şeker",
    "glucose syrup",
    "fructose syrup",
    "glucose-fructose syrup",
    "high fructose corn syrup",
    "corn syrup",
    "invert sugar syrup",
    "invert şeker şurubu",
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Değeri güvenli şekilde pozitif float'a çevirir; geçersizse varsayılanı döndürür."""
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
        return max(0.0, parsed)
    except (ValueError, TypeError):
        return default


def _extract_e_codes(additives_tags: Optional[List[Any]]) -> List[str]:
    """
    Open Food Facts 'additives_tags' listesini (örn: ['en:e322', 'en:e322i', 'en:e471'])
    standart E-kodu listesine (örn: ['E322', 'E471']) dönüştürür.
    """
    if not additives_tags or not isinstance(additives_tags, list):
        return []

    cleaned_codes: List[str] = []
    for tag in additives_tags:
        if not isinstance(tag, str):
            continue
        # 'en:e322' veya 'tr:e450a' gibi etiketlerden E + rakam kısmını ayıkla
        raw = tag.split(":")[-1].strip().upper()
        match = re.match(r"^(E\d{3,4}[A-Z]?)$", raw)
        if match:
            # Standart sözlük eşleşmesi için ana E-kodunu al (örn: E322I -> E322)
            base_match = re.match(r"^(E\d{3,4})", raw)
            code = base_match.group(1) if base_match else raw
            if code not in cleaned_codes:
                cleaned_codes.append(code)

    return cleaned_codes


def _detect_ingredient_flags(product: Dict[str, Any], ingredients_text: str) -> Dict[str, bool]:
    """
    İçindekiler metni ve Open Food Facts analiz etiketlerinden
    palm yağı ve glikoz/fruktoz şurubu varlığını tespit eder.
    """
    lowered_text = (ingredients_text or "").lower()

    # Türkçe büyük/küçük İ-ı dönüşüm farklarını yakalamak için ek normalizasyon
    normalized_text = (
        lowered_text.replace("ı", "i")
        .replace("ş", "s")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ö", "o")
        .replace("ç", "c")
    )

    has_palm_oil = any(
        kw in lowered_text or kw in normalized_text for kw in PALM_OIL_KEYWORDS
    )

    # Open Food Facts'in kendi palm yağı analiz etiketlerini de kontrol et
    analysis_tags = product.get("ingredients_analysis_tags") or []
    if isinstance(analysis_tags, list) and "en:palm-oil" in analysis_tags:
        has_palm_oil = True
    if _safe_float(product.get("ingredients_from_palm_oil_n"), 0.0) > 0:
        has_palm_oil = True

    has_glucose_syrup = any(
        kw in lowered_text or kw in normalized_text for kw in GLUCOSE_SYRUP_KEYWORDS
    )

    return {
        "has_palm_oil": has_palm_oil,
        "has_glucose_syrup": has_glucose_syrup,
    }


async def fetch_product_by_barcode(barcode: str) -> Dict[str, Any]:
    """
    Verilen barkod numarası ile Open Food Facts v2 API'sinden ürün verisini çeker
    ve puanlama motoruna uygun standart yapıda döndürür.
    """
    cleaned_barcode = barcode.strip()
    url = OPENFOODFACTS_API_URL.format(barcode=cleaned_barcode)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail="Dış ürün veri kaynağına (Open Food Facts) şu anda ulaşılamıyor. Lütfen daha sonra tekrar deneyiniz.",
        ) from exc

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail=f"'{cleaned_barcode}' barkod numarasına ait ürün kaydı açık veri kaynağında bulunamadı.",
        )

    if response.status_code >= 500:
        raise HTTPException(
            status_code=503,
            detail="Dış ürün veri kaynağı (Open Food Facts) geçici olarak yanıt vermiyor.",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail="Dış ürün veri kaynağından geçersiz yanıt alındı.",
        ) from exc

    # Open Food Facts ürün bulunamadığında HTTP 200 veya 404 ile status=0 dönebilir
    if payload.get("status") == 0 or not payload.get("product"):
        raise HTTPException(
            status_code=404,
            detail=f"'{cleaned_barcode}' barkod numarasına ait ürün kaydı açık veri kaynağında bulunamadı.",
        )

    product: Dict[str, Any] = payload["product"]

    # Ürün adı (Türkçe öncelikli, yoksa genel ürün adı)
    product_name = (
        (product.get("product_name_tr") or "").strip()
        or (product.get("product_name") or "").strip()
        or (product.get("product_name_en") or "").strip()
        or "İsimsiz Ürün"
    )

    brands = (product.get("brands") or "").strip() or None
    image_url = (
        (product.get("image_url") or "").strip()
        or (product.get("image_front_url") or "").strip()
        or None
    )

    # İçindekiler metni (Türkçe öncelikli, yoksa genel/İngilizce metin)
    ingredients_text_tr = (product.get("ingredients_text_tr") or "").strip()
    ingredients_text_general = (product.get("ingredients_text") or "").strip()
    ingredients_text_en = (product.get("ingredients_text_en") or "").strip()

    display_ingredients = (
        ingredients_text_tr or ingredients_text_general or ingredients_text_en or None
    )
    combined_ingredients_for_analysis = " ".join(
        filter(None, [ingredients_text_tr, ingredients_text_general, ingredients_text_en])
    )

    # 100g başına besin değerleri
    nutriments: Dict[str, Any] = product.get("nutriments") or {}

    # Temel besin alanlarından eksik olan var mı kontrolü
    core_keys = ("sugars_100g", "saturated-fat_100g", "energy-kcal_100g")
    has_salt_or_sodium = (
        nutriments.get("salt_100g") is not None or nutriments.get("sodium_100g") is not None
    )
    has_missing_nutrition = (
        not nutriments
        or any(nutriments.get(k) is None for k in core_keys)
        or not has_salt_or_sodium
        or nutriments.get("fiber_100g") is None
        or nutriments.get("proteins_100g") is None
    )

    # Kalori değeri energy-kcal_100g yoksa energy-kj_100g / 4.184 üzerinden hesaplanabilir
    raw_kcal = nutriments.get("energy-kcal_100g")
    if raw_kcal is None and nutriments.get("energy_100g") is not None:
        raw_kcal = round(_safe_float(nutriments.get("energy_100g")) / 4.184, 1)

    # Open Food Facts'te sodium_100g genellikle gram cinsinden tutulur -> mg'a çevir
    raw_sodium_g = _safe_float(nutriments.get("sodium_100g"), 0.0)
    sodium_mg = round(raw_sodium_g * 1000.0, 2) if raw_sodium_g < 50 else raw_sodium_g

    nutrition = {
        "sugar_g": round(_safe_float(nutriments.get("sugars_100g"), 0.0), 2),
        "saturated_fat_g": round(_safe_float(nutriments.get("saturated-fat_100g"), 0.0), 2),
        "salt_g": round(_safe_float(nutriments.get("salt_100g"), 0.0), 3),
        "sodium_mg": sodium_mg,
        "calories_kcal": round(_safe_float(raw_kcal, 0.0), 1),
        "fiber_g": round(_safe_float(nutriments.get("fiber_100g"), 0.0), 2),
        "protein_g": round(_safe_float(nutriments.get("proteins_100g"), 0.0), 2),
    }

    additives = _extract_e_codes(product.get("additives_tags"))
    flags = _detect_ingredient_flags(product, combined_ingredients_for_analysis)

    return {
        "barcode": cleaned_barcode,
        "product_name": product_name,
        "brands": brands,
        "image_url": image_url,
        "ingredients_text": display_ingredients,
        "nutrition": nutrition,
        "additives": additives,
        "has_palm_oil": flags["has_palm_oil"],
        "has_glucose_syrup": flags["has_glucose_syrup"],
        "has_missing_nutrition": has_missing_nutrition,
        "data_source": "open_food_facts",
    }
