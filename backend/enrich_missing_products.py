"""
Gıda Dedektifi - Eksik Ürün Verilerini Zenginleştirme ve Yetersiz Veri Güncelleme Scripti

Yerel SQLite veritabanındaki (`database/irem.db`) kayıtları tarar:
1. Etiket fotoğrafıyla (`label_photo`) eklenmiş kayıtlara ASLA dokunmaz (korur).
2. Temel besin değerleri boş/eksik olan Open Food Facts kayıtları için
   `world.openfoodfacts.org/api/v2/product/{barcode}.json` ve gerekirse
   `tr.openfoodfacts.org/api/v2/product/{barcode}.json` ürün detay uçlarından
   güncel `nutriments` ve `ingredients_text` verilerini çekip birleştirir.
3. Tüm kayıtları yeni `scoring.calculate_product_score` motorundan geçirerek:
   - Verisi tamamlananların gerçek puanını hesaplar,
   - Dış kaynakta da besin değeri bulunmayanların sahte 70 puanını kaldırıp
     `insufficient_data: True`, `score: None`, `grade: "Yetersiz Veri"` olarak günceller.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import httpx

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from database import (
    _get_connection,
    init_db,
    is_cached_product_missing_nutrition,
    save_product,
)
from openfoodfacts import (
    REQUEST_TIMEOUT_SECONDS,
    _merge_parsed_off_products,
    fetch_product_by_barcode_sync,
)
from scoring import calculate_product_score


def _count_known_core(nutr: Dict[str, Any]) -> Tuple[int, int]:
    """(known_core_count, known_risk_count) döndürür."""
    n = nutr or {}
    core = (
        n.get("sugar_g"),
        n.get("saturated_fat_g"),
        n.get("salt_g"),
        n.get("calories_kcal"),
    )
    risk = (
        n.get("sugar_g"),
        n.get("saturated_fat_g"),
        n.get("salt_g"),
    )
    return (
        sum(1 for v in core if v is not None),
        sum(1 for v in risk if v is not None),
    )


def _recalculate_and_format_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Ürün sözlüğünü `calculate_product_score` ile yeniden puanlar ve alanları günceller."""
    updated = dict(data)
    raw_src = str(updated.get("source") or updated.get("data_source") or "open_food_facts")
    is_label = raw_src in ("label_photo", "Etiket Fotoğrafı (OCR)")
    updated["source"] = "label_photo" if is_label else "open_food_facts"
    updated["data_source"] = (
        "Etiket Fotoğrafı (OCR)" if is_label else "Open Food Facts"
    )

    score_result = calculate_product_score(
        nutrition=updated.get("nutrition") or {},
        additives=updated.get("additives") or [],
        is_adulterated=bool(
            (updated.get("breakdown") or {}).get("adulteration_penalty_applied", False)
        ),
        has_palm_oil=bool(updated.get("has_palm_oil", False)),
        has_glucose_syrup=bool(updated.get("has_glucose_syrup", False)),
    )

    warnings = list(score_result["warnings"])
    has_score_missing_warn = any(
        "puan sınırlı veriye göre hesaplandı" in w
        or "besin değerleri veritabanında bulunmadığından" in w
        for w in warnings
    )
    if updated.get("has_missing_nutrition") and not has_score_missing_warn:
        warnings.append(
            "Bilgi Notu: Bu ürünün bazı yardımcı besin değeri verileri (örn. lif/protein) kaynakta belirtilmemiş olabilir."
        )

    updated["insufficient_data"] = score_result["insufficient_data"]
    updated["score"] = score_result["score"]
    updated["grade"] = score_result["grade"]
    updated["warnings"] = warnings
    updated["score_breakdown"] = score_result["score_breakdown"]
    updated["cap_applied"] = score_result["cap_applied"]
    updated["cap_reason"] = score_result["cap_reason"]
    updated["missing_fields_count"] = score_result["missing_fields_count"]
    updated["breakdown"] = score_result["breakdown"]
    updated["disclaimer"] = score_result["disclaimer"]
    return updated


def enrich_missing_products(max_workers: int = 6) -> Dict[str, int]:
    """
    Veritabanındaki eksik besin değerli ürünleri Open Food Facts detay API'lerinden
    zenginleştirir ve tüm kayıtların puan/yetersiz veri durumunu günceller.
    """
    init_db()
    with closing(_get_connection()) as conn:
        rows = conn.execute(
            "SELECT barcode, product_name, response_json FROM products ORDER BY updated_at DESC"
        ).fetchall()

    total_count = len(rows)
    protected_label_count = 0
    candidates_for_remote: List[Dict[str, Any]] = []
    already_sufficient_records: List[Dict[str, Any]] = []

    for row in rows:
        try:
            data = json.loads(row["response_json"])
        except ValueError:
            continue

        raw_src = str(data.get("source") or data.get("data_source") or "open_food_facts")
        if raw_src in ("label_photo", "Etiket Fotoğrafı (OCR)"):
            protected_label_count += 1
            continue

        known_core, known_risk = _count_known_core(data.get("nutrition") or {})
        # Temel besin değerleri boş/yetersiz veya 4 ana besinden biri eksik olan kayıtları dış kaynaktan sorgula
        if known_core < 4:
            candidates_for_remote.append(data)
        else:
            already_sufficient_records.append(data)

    print(
        f"=== Eksik Ürün Verisi Zenginleştirme Başlatıldı ===\n"
        f"• Toplam Kayıt                : {total_count}\n"
        f"• Korunan (label_photo)       : {protected_label_count}\n"
        f"• Tam Verili Kayıt            : {len(already_sufficient_records)}\n"
        f"• Dış Kaynaktan Sorgulanacak  : {len(candidates_for_remote)}\n",
        flush=True,
    )

    # 1. Tam verili mevcut kayıtların da yeni `score_breakdown` ve `insufficient_data` şemasıyla güncellenmesi
    for item in already_sufficient_records:
        recalculated = _recalculate_and_format_payload(item)
        save_product(recalculated)

    enriched_nutrition_count = 0
    enriched_ingredients_count = 0
    newly_scorable_count = 0
    remote_reachable = True

    def _fetch_one(
        client: httpx.Client, record: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        bc = str(record.get("barcode") or "").strip()
        fresh = fetch_product_by_barcode_sync(bc, client=client)
        return record, fresh

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True) as client:
        # İlk aday ile dış ağ erişimini hızlıca test et
        if candidates_for_remote:
            test_bc = str(candidates_for_remote[0].get("barcode") or "").strip()
            try:
                client.get(
                    f"https://world.openfoodfacts.org/api/v2/product/{test_bc}.json",
                    timeout=5.0,
                )
            except httpx.RequestError as exc:
                remote_reachable = False
                print(
                    f"[!] Dış ağ erişimi kısıtlı ({exc}). Yerel kayıtlar yeni puanlama/yetersiz veri korumasıyla güncelleniyor...",
                    flush=True,
                )

        if remote_reachable and candidates_for_remote:
            with ThreadPoolExecutor(max_workers=max(1, min(10, max_workers))) as pool:
                futures = [
                    pool.submit(_fetch_one, client, rec)
                    for rec in candidates_for_remote
                ]
                done_idx = 0
                for fut in as_completed(futures):
                    done_idx += 1
                    orig_rec, fresh_off = fut.result()
                    before_core, before_risk = _count_known_core(
                        orig_rec.get("nutrition") or {}
                    )
                    was_insufficient = before_core == 0 or (
                        before_risk == 0 and before_core <= 1
                    )
                    had_ing = bool(orig_rec.get("ingredients_text"))

                    if fresh_off is not None:
                        merged = _merge_parsed_off_products(orig_rec, fresh_off)
                        after_core, after_risk = _count_known_core(
                            merged.get("nutrition") or {}
                        )
                        is_now_sufficient = not (
                            after_core == 0 or (after_risk == 0 and after_core <= 1)
                        )
                        if after_core > before_core:
                            enriched_nutrition_count += 1
                        if not had_ing and bool(merged.get("ingredients_text")):
                            enriched_ingredients_count += 1
                        if was_insufficient and is_now_sufficient:
                            newly_scorable_count += 1
                        target_rec = merged
                    else:
                        target_rec = orig_rec

                    updated_payload = _recalculate_and_format_payload(target_rec)
                    save_product(updated_payload)

                    if done_idx % 25 == 0 or done_idx == len(candidates_for_remote):
                        print(
                            f"  [{done_idx}/{len(candidates_for_remote)}] İşlendi | "
                            f"Besin verisi zenginleşen: {enriched_nutrition_count} | "
                            f"İçindekiler eklenen: {enriched_ingredients_count} | "
                            f"Puanlanabilir hale gelen: {newly_scorable_count}",
                            flush=True,
                        )
        else:
            for rec in candidates_for_remote:
                updated_payload = _recalculate_and_format_payload(rec)
                save_product(updated_payload)

    # Nihai veritabanı durumunu say
    with closing(_get_connection()) as conn:
        final_rows = conn.execute("SELECT response_json FROM products").fetchall()

    scored_total = 0
    insufficient_total = 0
    for r in final_rows:
        try:
            d = json.loads(r["response_json"])
            if d.get("insufficient_data") or d.get("score") is None:
                insufficient_total += 1
            else:
                scored_total += 1
        except ValueError:
            pass

    print(
        f"\n=== Zenginleştirme ve Güncelleme Özeti ===\n"
        f"• Toplam Ürün                        : {len(final_rows)}\n"
        f"• Korunan Etiket Fotoğrafı Kaydı     : {protected_label_count}\n"
        f"• Besin Değeri Zenginleştirilen      : {enriched_nutrition_count}\n"
        f"• İçindekiler Metni Tamamlanan       : {enriched_ingredients_count}\n"
        f"• Yetersiz Veriden Puanlıya Geçen    : {newly_scorable_count}\n"
        f"• Geçerli Puanı Olan Ürün Sayısı     : {scored_total}\n"
        f"• 'Yetersiz Veri' Korumasındaki Ürün : {insufficient_total}",
        flush=True,
    )

    return {
        "total": len(final_rows),
        "protected_label_photo": protected_label_count,
        "enriched_nutrition": enriched_nutrition_count,
        "enriched_ingredients": enriched_ingredients_count,
        "newly_scorable": newly_scorable_count,
        "scored_total": scored_total,
        "insufficient_total": insufficient_total,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Yerel SQLite veritabanındaki eksik besin değerli ürünleri Open Food Facts'ten tamamlar."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Eşzamanlı HTTP istek iş parçacığı sayısı (varsayılan: 6)",
    )
    args = parser.parse_args()
    enrich_missing_products(max_workers=args.workers)


if __name__ == "__main__":
    main()
