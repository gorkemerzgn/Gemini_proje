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


def _safe_optional_float(value: Any) -> Optional[float]:
    """Değeri güvenli şekilde pozitif float'a çevirir; eksik (None/'') veya geçersizse None döndürür."""
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
        return max(0.0, parsed)
    except (ValueError, TypeError):
        return None


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


def _safe_str(value: Any) -> str:
    """Farklı OFF uçlarından (v2 veya search-a-licious) gelen str, list veya dict değerini güvenli metne çevirir."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(str(x).strip() for x in value if x is not None and str(x).strip()).strip()
    if isinstance(value, dict):
        chosen = (
            value.get("tr")
            or value.get("main")
            or value.get("en")
            or next((v for v in value.values() if v), "")
        )
        return _safe_str(chosen)
    return str(value).strip()


def parse_off_product_dict(
    product: Dict[str, Any],
    default_barcode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Open Food Facts'ten dönen tekil `product` sözlüğünü ayrıştırır;
    eksik besin verilerini `None` olarak koruyarak puanlama motoruna uygun yapıda döndürür.
    """
    cleaned_barcode = (
        str(default_barcode or product.get("code") or product.get("_id") or "").strip()
    )

    # Ürün adı (Türkçe öncelikli, yoksa genel ürün adı)
    product_name = (
        _safe_str(product.get("product_name_tr"))
        or _safe_str(product.get("product_name"))
        or _safe_str(product.get("product_name_en"))
        or "İsimsiz Ürün"
    )

    brands = _safe_str(product.get("brands")) or None
    image_url = (
        _safe_str(product.get("image_url"))
        or _safe_str(product.get("image_front_url"))
        or None
    )

    # İçindekiler metni (Türkçe öncelikli, yoksa genel/İngilizce metin)
    ingredients_text_tr = _safe_str(product.get("ingredients_text_tr"))
    ingredients_text_general = _safe_str(product.get("ingredients_text"))
    ingredients_text_en = _safe_str(product.get("ingredients_text_en"))

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
    raw_kcal = _safe_optional_float(nutriments.get("energy-kcal_100g"))
    if raw_kcal is None:
        raw_kj = _safe_optional_float(
            nutriments.get("energy-kj_100g")
            if nutriments.get("energy-kj_100g") is not None
            else nutriments.get("energy_100g")
        )
        if raw_kj is not None:
            raw_kcal = round(raw_kj / 4.184, 1)

    # Open Food Facts'te sodium_100g genellikle gram cinsinden tutulur -> mg'a çevir
    raw_sodium_g = _safe_optional_float(nutriments.get("sodium_100g"))
    sodium_mg: Optional[float] = None
    if raw_sodium_g is not None:
        sodium_mg = (
            round(raw_sodium_g * 1000.0, 2)
            if raw_sodium_g < 50
            else round(raw_sodium_g, 2)
        )

    raw_sugar = _safe_optional_float(nutriments.get("sugars_100g"))
    raw_sat_fat = _safe_optional_float(nutriments.get("saturated-fat_100g"))
    raw_salt = _safe_optional_float(nutriments.get("salt_100g"))
    if raw_salt is None and sodium_mg is not None:
        raw_salt = round((sodium_mg / 1000.0) * 2.5, 3)

    raw_fiber = _safe_optional_float(nutriments.get("fiber_100g"))
    raw_protein = _safe_optional_float(nutriments.get("proteins_100g"))

    nutrition = {
        "sugar_g": round(raw_sugar, 2) if raw_sugar is not None else None,
        "saturated_fat_g": round(raw_sat_fat, 2) if raw_sat_fat is not None else None,
        "salt_g": round(raw_salt, 3) if raw_salt is not None else None,
        "sodium_mg": sodium_mg,
        "calories_kcal": round(raw_kcal, 1) if raw_kcal is not None else None,
        "fiber_g": round(raw_fiber, 2) if raw_fiber is not None else None,
        "protein_g": round(raw_protein, 2) if raw_protein is not None else None,
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

    if response.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail="Dış ürün veri kaynağı (Open Food Facts) geçici olarak yanıt vermiyor veya istek sınırına ulaşıldı.",
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
    return parse_off_product_dict(product, default_barcode=cleaned_barcode)
