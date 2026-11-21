"""
Gıda Dedektifi - Türkiye Popüler Ürünler Toplu İçe Aktarma Scripti (8. Gün)

Open Food Facts arama API uçları üzerinden Türkiye'de satılan (`countries_tags_en=turkey`)
ve en çok taranan (`sort_by=unique_scans_n`) ürünleri sayfa sayfa çekerek puanlama
motorundan geçirir ve yerel SQLite veritabanına (`database/irem.db`) kaydeder.

Özellikler:
- `fields=code,product_name,brands,nutriments,ingredients_text,additives_tags,image_url,image_front_url,nutriscore_grade`
  parametresiyle yalnızca gerekli alanları çeker.
- `world.openfoodfacts.org` 503/hata döndürürse artan bekleme süresiyle (3s, 6s) tekrar dener
  ve otomatik olarak `https://tr.openfoodfacts.org/api/v2/search` veya
  `https://search.openfoodfacts.org/search` yedek uçlarına geçer.
- Eksik besin verileri `None` (`null`) olarak korunur, `0.0` yapılmaz.
- Barkodu veya ürün adı boş olan bozuk kayıtlar atlanır.
- Daha önce kullanıcının etiket fotoğrafıyla (`label_photo`) eklediği kayıtların üzerine
  ASLA yazılmaz (korunur).
- Kullanım:
  python backend/import_tr_products.py --limit 500 --page-size 20
"""

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import httpx

# Doğrudan `python backend/import_tr_products.py` veya `backend/` içinden çalıştırılabilmesi için
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from database import (
    get_database_stats,
    init_db,
    is_label_photo_product,
    save_product,
)
from openfoodfacts import _safe_str, parse_off_product_dict
from scoring import calculate_product_score

SEARCH_FIELDS = (
    "code,product_name,brands,nutriments,ingredients_text,"
    "additives_tags,image_url,image_front_url,nutriscore_grade"
)

SEARCH_ENDPOINTS: List[Dict[str, str]] = [
    {
        "name": "world.openfoodfacts.org (v2)",
        "url": "https://world.openfoodfacts.org/api/v2/search",
        "mode": "v2",
    },
    {
        "name": "tr.openfoodfacts.org (v2)",
        "url": "https://tr.openfoodfacts.org/api/v2/search",
        "mode": "v2",
    },
    {
        "name": "search.openfoodfacts.org (Search-a-licious)",
        "url": "https://search.openfoodfacts.org/search",
        "mode": "search_v1",
    },
]

USER_AGENT = "IremFoodApp/0.8 (https://github.com/gorkemerzgn/irem)"
PAGE_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 25.0
MAX_RETRIES_PER_ENDPOINT = 2


def _has_valid_barcode_and_name(raw_product: Dict[str, Any]) -> bool:
    """Barkodu veya ürün adı boş olan bozuk kayıtları filtreler."""
    if not isinstance(raw_product, dict):
        return False

    barcode = str(raw_product.get("code") or raw_product.get("_id") or "").strip()
    if not barcode or len(barcode) < 4:
        return False

    name = (
        _safe_str(raw_product.get("product_name_tr"))
        or _safe_str(raw_product.get("product_name"))
        or _safe_str(raw_product.get("product_name_en"))
    )
    if not name:
        return False

    return True


def _build_product_payload(raw_product: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ham Open Food Facts ürün sözlüğünü `openfoodfacts.parse_off_product_dict` ve
    `scoring.calculate_product_score` ile işleyerek veritabanına kaydedilecek tam yapıyı üretir.
    """
    product_data = parse_off_product_dict(raw_product)
    score_result = calculate_product_score(
        nutrition=product_data["nutrition"],
        additives=product_data["additives"],
        is_adulterated=False,
        has_palm_oil=product_data["has_palm_oil"],
        has_glucose_syrup=product_data["has_glucose_syrup"],
    )

    warnings = list(score_result["warnings"])
    has_score_missing_warn = any(
        "puan sınırlı veriye göre hesaplandı" in w for w in warnings
    )
    if product_data.get("has_missing_nutrition") and not has_score_missing_warn:
        warnings.append(
            "Bilgi Notu: Bu ürünün bazı yardımcı besin değeri verileri (örn. lif/protein) kaynakta belirtilmemiş olabilir."
        )

    return {
        "barcode": product_data["barcode"],
        "product_name": product_data["product_name"],
        "brands": product_data["brands"],
        "image_url": product_data["image_url"],
        "ingredients_text": product_data["ingredients_text"],
        "nutrition": product_data["nutrition"],
        "additives": product_data["additives"],
        "has_palm_oil": product_data["has_palm_oil"],
        "has_glucose_syrup": product_data["has_glucose_syrup"],
        "data_source": "Open Food Facts",
        "source": "open_food_facts",
        "from_cache": False,
        "score": score_result["score"],
        "grade": score_result["grade"],
        "warnings": warnings,
        "score_breakdown": score_result["score_breakdown"],
        "cap_applied": score_result["cap_applied"],
        "cap_reason": score_result["cap_reason"],
        "missing_fields_count": score_result["missing_fields_count"],
        "breakdown": score_result["breakdown"],
        "disclaimer": score_result["disclaimer"],
    }


def _build_query_params(mode: str, page: int, page_size: int) -> Dict[str, Any]:
    """API ucunun türüne (v2 veya search_v1) göre sorgu parametrelerini hazırlar."""
    if mode == "search_v1":
        return {
            "q": 'countries_tags:"en:turkey"',
            "sort_by": "unique_scans_n",
            "page": page,
            "page_size": page_size,
            "fields": SEARCH_FIELDS,
        }
    return {
        "countries_tags_en": "turkey",
        "sort_by": "unique_scans_n",
        "page": page,
        "page_size": page_size,
        "fields": SEARCH_FIELDS,
    }


def _fetch_search_page(
    client: httpx.Client,
    page: int,
    page_size: int,
    preferred_idx: int = 0,
) -> Tuple[Optional[List[Dict[str, Any]]], int]:
    """
    Open Food Facts arama API'sinden tek bir sayfayı çeker.
    - 503/429/5xx veya ağ hatalarında artan bekleme süresiyle (3s, 6s) tekrar dener.
    - Bir uç 503 veya hata vermeye devam ederse otomatik olarak sıradaki yedek uca
      (`tr.openfoodfacts.org/api/v2/search` veya `search.openfoodfacts.org/search`) geçer.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    total_endpoints = len(SEARCH_ENDPOINTS)
    ordered_indices = [
        (preferred_idx + offset) % total_endpoints for offset in range(total_endpoints)
    ]

    for ep_idx in ordered_indices:
        ep = SEARCH_ENDPOINTS[ep_idx]
        url = ep["url"]
        params = _build_query_params(ep["mode"], page=page, page_size=page_size)

        for attempt in range(1, MAX_RETRIES_PER_ENDPOINT + 1):
            try:
                response = client.get(url, params=params, headers=headers)
                if response.status_code == 200:
                    payload = response.json()
                    products = (
                        payload.get("products")
                        if "products" in payload
                        else payload.get("hits")
                    )
                    return (products if isinstance(products, list) else []), ep_idx

                wait_sec = 3.0 * attempt  # 3s, 6s artan bekleme süresi
                if response.status_code in (503, 502, 504, 429):
                    print(
                        f"  [!] {ep['name']} -> HTTP {response.status_code} (Sayfa {page}, "
                        f"Deneme {attempt}/{MAX_RETRIES_PER_ENDPOINT}). {wait_sec:.0f} sn bekleniyor...",
                        flush=True,
                    )
                    time.sleep(wait_sec)
                    continue

                print(
                    f"  [!] {ep['name']} -> Beklenmeyen HTTP {response.status_code} (Sayfa {page}, "
                    f"Deneme {attempt}/{MAX_RETRIES_PER_ENDPOINT}). {wait_sec:.0f} sn bekleniyor...",
                    flush=True,
                )
                time.sleep(wait_sec)
            except (httpx.RequestError, ValueError) as exc:
                wait_sec = 3.0 * attempt
                print(
                    f"  [!] {ep['name']} bağlantı/yanıt hatası (Sayfa {page}, "
                    f"Deneme {attempt}/{MAX_RETRIES_PER_ENDPOINT}): {exc}",
                    flush=True,
                )
                if attempt < MAX_RETRIES_PER_ENDPOINT:
                    time.sleep(wait_sec)

        # Mevcut uç tüm denemelerde başarısız olduysa bir sonraki yedek uca geç
        next_candidates = [
            SEARCH_ENDPOINTS[i]["name"] for i in ordered_indices if i != ep_idx
        ]
        if next_candidates:
            print(
                f"  [->] {ep['name']} yanıt vermedi, yedek uca geçiliyor...",
                flush=True,
            )

    return None, preferred_idx


def import_turkish_products(
    limit: int = 500,
    page_size: int = 20,
    delay_seconds: float = PAGE_DELAY_SECONDS,
) -> Dict[str, int]:
    """
    Türkiye'deki en popüler ürünleri sayfa sayfa çekip yerel SQLite veritabanına kaydeder.
    """
    init_db()
    safe_limit = max(1, int(limit))
    safe_page_size = max(1, min(100, int(page_size)))
    total_pages = math.ceil(safe_limit / safe_page_size)

    imported_count = 0
    skipped_count = 0
    protected_count = 0
    active_endpoint_idx = 0

    print(
        f"=== Türkiye Popüler Ürün İçe Aktarma Başlatıldı ===\n"
        f"Hedef Limit: {safe_limit} ürün | Sayfa Boyutu: {safe_page_size} | "
        f"User-Agent: {USER_AGENT}\n"
        f"Fields: {SEARCH_FIELDS}\n",
        flush=True,
    )

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True) as client:
        for page in range(1, total_pages + 1):
            if imported_count >= safe_limit:
                break

            products, active_endpoint_idx = _fetch_search_page(
                client,
                page=page,
                page_size=safe_page_size,
                preferred_idx=active_endpoint_idx,
            )
            if products is None:
                print(
                    f"\n[!] Sayfa {page}/{total_pages} tüm arama uçlarından alınamadı. "
                    f"İçe aktarma güvenli şekilde durduruluyor.",
                    flush=True,
                )
                break

            if not products:
                print(
                    f"\n[i] Sayfa {page}/{total_pages} boş döndü (tüm mevcut ürünler tarandı).",
                    flush=True,
                )
                break

            page_added = 0
            page_skipped = 0
            page_protected = 0

            for raw_prod in products:
                if imported_count >= safe_limit:
                    break

                if not _has_valid_barcode_and_name(raw_prod):
                    skipped_count += 1
                    page_skipped += 1
                    continue

                barcode = str(raw_prod.get("code") or raw_prod.get("_id") or "").strip()

                # Kullanıcının etiket fotoğrafıyla (label_photo) eklediği kayıtları ASLA ezme
                if is_label_photo_product(barcode):
                    protected_count += 1
                    page_protected += 1
                    continue

                try:
                    payload = _build_product_payload(raw_prod)
                    save_product(payload)
                    imported_count += 1
                    page_added += 1
                except Exception as exc:
                    skipped_count += 1
                    page_skipped += 1
                    print(f"  [!] Barkod {barcode} işlenirken hata: {exc}", flush=True)

            ep_name = SEARCH_ENDPOINTS[active_endpoint_idx]["name"]
            print(
                f"[Sayfa {page}/{total_pages} | {ep_name}] "
                f"Eklenen: {page_added}, Atlanan: {page_skipped}, Korunan (label_photo): {page_protected} | "
                f"Toplam Eklenen: {imported_count}/{safe_limit}",
                flush=True,
            )

            if page < total_pages and imported_count < safe_limit and delay_seconds > 0:
                time.sleep(delay_seconds)

    db_stats = get_database_stats()
    print(
        f"\n=== İçe Aktarma Tamamlandı ===\n"
        f"• Bu çalışmada eklenen/güncellenen : {imported_count}\n"
        f"• Eksik barkod/isim nedeniyle atlanan: {skipped_count}\n"
        f"• Etiket fotoğrafı (label_photo) korunan: {protected_count}\n"
        f"• Veritabanı Genel Durumu -> Toplam: {db_stats['total_products']} "
        f"(Open Food Facts: {db_stats['open_food_facts_count']}, "
        f"Etiket Fotoğrafı: {db_stats['label_photo_count']})",
        flush=True,
    )

    return {
        "imported": imported_count,
        "skipped": skipped_count,
        "protected": protected_count,
        "total_products": db_stats["total_products"],
        "open_food_facts_count": db_stats["open_food_facts_count"],
        "label_photo_count": db_stats["label_photo_count"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open Food Facts üzerinden Türkiye'deki popüler ürünleri yerel SQLite veritabanına aktarır."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="İçe aktarılacak maksimum ürün sayısı (varsayılan: 500)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=20,
        help="Her API isteğinde çekilecek sayfa boyutu (varsayılan: 20, maksimum: 100)",
    )
    args = parser.parse_args()
    import_turkish_products(limit=args.limit, page_size=args.page_size)


if __name__ == "__main__":
    main()
