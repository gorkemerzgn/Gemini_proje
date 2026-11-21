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
OPENFOODFACTS_PRODUCT_URLS = (
    "https://world.openfoodfacts.org/api/v2/product/{barcode}.json",
    "https://tr.openfoodfacts.org/api/v2/product/{barcode}.json",
)
USER_AGENT = "IremFoodApp/0.8 (https://github.com/gorkemerzgn/irem)"
REQUEST_TIMEOUT_SECONDS = 12.0

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
    is_serving_only = str(product.get("nutrition_data_per") or "100g").lower() == "serving"

    def _get_nutr(base_key: str) -> Optional[float]:
        val = _safe_optional_float(nutriments.get(f"{base_key}_100g"))
        if val is None and not is_serving_only:
            val = _safe_optional_float(nutriments.get(base_key))
        if val is None and not is_serving_only:
            val = _safe_optional_float(nutriments.get(f"{base_key}_value"))
        return val

    # Kalori değeri energy-kcal_100g yoksa energy-kj_100g / 4.184 üzerinden hesaplanabilir
    raw_kcal = _get_nutr("energy-kcal")
    if raw_kcal is None:
        raw_kj = _get_nutr("energy-kj")
        if raw_kj is None:
            raw_kj = _get_nutr("energy")
        if raw_kj is not None:
            raw_kcal = round(raw_kj / 4.184, 1)

    # Open Food Facts'te sodium_100g genellikle gram cinsinden tutulur -> mg'a çevir
    raw_sodium_g = _get_nutr("sodium")
    sodium_mg: Optional[float] = None
    if raw_sodium_g is not None:
        sodium_mg = (
            round(raw_sodium_g * 1000.0, 2)
            if raw_sodium_g < 50
            else round(raw_sodium_g, 2)
        )

    raw_sugar = _get_nutr("sugars")
    raw_sat_fat = _get_nutr("saturated-fat")
    raw_salt = _get_nutr("salt")
    if raw_salt is None and sodium_mg is not None:
        raw_salt = round((sodium_mg / 1000.0) * 2.5, 3)

    raw_fiber = _get_nutr("fiber")
    raw_protein = _get_nutr("proteins")

    has_salt_or_sodium = raw_salt is not None or sodium_mg is not None
    has_missing_nutrition = (
        not nutriments
        or any(v is None for v in (raw_sugar, raw_sat_fat, raw_kcal))
        or not has_salt_or_sodium
        or raw_fiber is None
        or raw_protein is None
    )

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
    if combined_ingredients_for_analysis:
        for m in re.finditer(
            r"\bE[-\s]?(\d{3,4})[a-zA-Z]?\b",
            combined_ingredients_for_analysis,
            flags=re.IGNORECASE,
        ):
            num = int(m.group(1))
            if 100 <= num <= 1599:
                code = f"E{num}"
                if code not in additives:
                    additives.append(code)

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


def _merge_parsed_off_products(
    primary: Dict[str, Any],
    secondary: Dict[str, Any],
) -> Dict[str, Any]:
    """İki OFF kaynağından (world ve tr) gelen ürün verilerini birleştirerek eksik alanları tamamlar."""
    merged = dict(primary)
    if (
        not merged.get("product_name")
        or merged.get("product_name") == "İsimsiz Ürün"
    ) and secondary.get("product_name"):
        merged["product_name"] = secondary["product_name"]

    for field in ("brands", "image_url", "ingredients_text"):
        if not merged.get(field) and secondary.get(field):
            merged[field] = secondary[field]

    merged_nutr = dict(merged.get("nutrition") or {})
    sec_nutr = secondary.get("nutrition") or {}
    for k, v in sec_nutr.items():
        if merged_nutr.get(k) is None and v is not None:
            merged_nutr[k] = v
    merged["nutrition"] = merged_nutr

    merged_add = list(merged.get("additives") or [])
    for code in secondary.get("additives") or []:
        if code not in merged_add:
            merged_add.append(code)
    merged["additives"] = merged_add

    merged["has_palm_oil"] = bool(
        merged.get("has_palm_oil") or secondary.get("has_palm_oil")
    )
    merged["has_glucose_syrup"] = bool(
        merged.get("has_glucose_syrup") or secondary.get("has_glucose_syrup")
    )

    core_vals = (
        merged_nutr.get("sugar_g"),
        merged_nutr.get("saturated_fat_g"),
        merged_nutr.get("calories_kcal"),
    )
    has_salt = (
        merged_nutr.get("salt_g") is not None
        or merged_nutr.get("sodium_mg") is not None
    )
    merged["has_missing_nutrition"] = (
        any(v is None for v in core_vals)
        or not has_salt
        or merged_nutr.get("fiber_g") is None
        or merged_nutr.get("protein_g") is None
    )
    return merged


def _has_complete_core_and_ingredients(parsed: Dict[str, Any]) -> bool:
    """Ürünün 4 temel besin değerinin ve içindekiler metninin tam olup olmadığını kontrol eder."""
    n = parsed.get("nutrition") or {}
    has_all_core = all(
        n.get(k) is not None
        for k in ("sugar_g", "saturated_fat_g", "salt_g", "calories_kcal")
    )
    return has_all_core and bool(parsed.get("ingredients_text"))


def fetch_product_by_barcode_sync(
    barcode: str,
    client: Optional[httpx.Client] = None,
) -> Optional[Dict[str, Any]]:
    """
    Senkron olarak `world.openfoodfacts.org` ve gerekirse `tr.openfoodfacts.org`
    ürün detay uçlarından barkod verisini çeker ve birleştirir. Bulunamazsa None döner.
    """
    cleaned_barcode = (barcode or "").strip()
    if not cleaned_barcode:
        return None

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    best_parsed: Optional[Dict[str, Any]] = None

    def _query_all(http_client: httpx.Client) -> Optional[Dict[str, Any]]:
        for url_tpl in OPENFOODFACTS_PRODUCT_URLS:
            url = url_tpl.format(barcode=cleaned_barcode)
            try:
                resp = http_client.get(url, headers=headers)
            except httpx.RequestError:
                continue

            if resp.status_code == 404:
                return None

            if resp.status_code != 200:
                continue

            try:
                payload = resp.json()
            except ValueError:
                continue

            if payload.get("status") == 0 or not isinstance(payload.get("product"), dict):
                return None

            return parse_off_product_dict(
                payload["product"], default_barcode=cleaned_barcode
            )

        return None

    if client is not None:
        return _query_all(client)

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True) as temp_client:
        return _query_all(temp_client)


async def fetch_product_by_barcode(barcode: str) -> Dict[str, Any]:
    """
    Verilen barkod numarası ile Open Food Facts v2 API'sinden (`world` ve gerekirse `tr`)
    ürün verisini asenkron olarak çeker ve puanlama motoruna uygun standart yapıda döndürür.
    """
    cleaned_barcode = barcode.strip()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    best_parsed: Optional[Dict[str, Any]] = None
    saw_not_found = False
    last_network_or_server_error = False

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        for url_tpl in OPENFOODFACTS_PRODUCT_URLS:
            url = url_tpl.format(barcode=cleaned_barcode)
            try:
                response = await client.get(url, headers=headers)
            except httpx.RequestError:
                last_network_or_server_error = True
                continue

            if response.status_code == 404:
                saw_not_found = True
                continue

            if response.status_code != 200:
                last_network_or_server_error = True
                continue

            try:
                payload = response.json()
            except ValueError:
                last_network_or_server_error = True
                continue

            if payload.get("status") == 0 or not isinstance(payload.get("product"), dict):
                saw_not_found = True
                continue

            parsed = parse_off_product_dict(
                payload["product"], default_barcode=cleaned_barcode
            )
            best_parsed = (
                parsed
                if best_parsed is None
                else _merge_parsed_off_products(best_parsed, parsed)
            )
            if _has_complete_core_and_ingredients(best_parsed):
                break

    if best_parsed is not None:
        return best_parsed

    if saw_not_found:
        raise HTTPException(
            status_code=404,
            detail=f"'{cleaned_barcode}' barkod numarasına ait ürün kaydı açık veri kaynağında bulunamadı.",
        )

    raise HTTPException(
        status_code=503,
        detail="Dış ürün veri kaynağına (Open Food Facts) şu anda ulaşılamıyor. Lütfen daha sonra tekrar deneyiniz.",
    )
