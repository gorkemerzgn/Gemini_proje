"""
Gıda Dedektifi - SQLite Yerel Veritabanı (Önbellek ve Tarama Geçmişi)

Taranan ürünleri `database/irem.db` içerisinde önbelleğe (cache) alır.
30 günden yeni kayıtlar doğrudan yerel veritabanından hızlıca sunulur;
30 günü aşan kayıtlar dış kaynaktan yenilenerek güncellenir.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Proje kök dizinindeki database/irem.db yolu
BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "database"
DB_PATH = DB_DIR / "irem.db"

CACHE_TTL_DAYS = 30


def _normalize_tr_search(text: Optional[str]) -> str:
    """
    Türkçe harfleri (İ/i, I/ı, Ş/ş, Ğ/ğ, Ü/ü, Ö/ö, Ç/ç) ASCII küçük harf
    karşılıklarına dönüştürür; böylece SQLite sorgularında 'cikolata' -> 'Çikolata',
    'sut' -> 'Süt', 'ulker' -> 'Ülker' eşleşmesi sağlanır.
    """
    if not text:
        return ""
    return (
        str(text)
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
        .strip()
    )


def _get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """SQLite veritabanı bağlantısı oluşturur, Row fabrikasını ve Türkçe arama fonksiyonunu ayarlar."""
    target_path = Path(db_path) if db_path else DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path))
    conn.row_factory = sqlite3.Row
    conn.create_function("tr_norm", 1, _normalize_tr_search)
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """
    Veritabanı tablosunu (`products`) ve gerekli indeksleri oluşturur.
    Uygulama başlangıcında otomatik çağrılır.
    """
    with closing(_get_connection(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                barcode TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                brands TEXT,
                score INTEGER NOT NULL,
                grade TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_products_updated_at
            ON products (updated_at DESC)
            """
        )
        conn.commit()


def get_cached_product(
    barcode: str,
    max_age_days: int = CACHE_TTL_DAYS,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """
    Barkod numarasına göre yerel SQLite veritabanından ürünü getirir.
    - Kayıt yoksa veya `max_age_days` (varsayılan 30 gün) süresinden eskiyse `None` döner.
    - Geçerli kayıt varsa tam ürün verisini `"from_cache": True` olarak döner.
    """
    cleaned_barcode = barcode.strip()
    with closing(_get_connection(db_path)) as conn:
        row = conn.execute(
            """
            SELECT barcode, product_name, brands, score, grade,
                   response_json, created_at, updated_at
            FROM products
            WHERE barcode = ?
            """,
            (cleaned_barcode,),
        ).fetchone()

    if row is None:
        return None

    # 30 günlük geçerlilik (TTL) kontrolü
    try:
        updated_at_dt = datetime.fromisoformat(row["updated_at"])
        if updated_at_dt.tzinfo is None:
            updated_at_dt = updated_at_dt.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        if now_utc - updated_at_dt > timedelta(days=max_age_days):
            return None
    except ValueError:
        # Geçersiz tarih formatı varsa yenilenmesi için None dön
        return None

    try:
        data: Dict[str, Any] = json.loads(row["response_json"])
    except ValueError:
        return None

    data["from_cache"] = True
    return data


def save_product(
    product_data: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> None:
    """
    Analiz edilen ürün ve skor sonucunu SQLite `products` tablosuna kaydeder
    veya mevcut kaydı günceller (upsert).
    """
    barcode = str(product_data.get("barcode") or "").strip()
    if not barcode:
        raise ValueError("Kaydedilecek ürün verisinde 'barcode' alanı zorunludur.")

    product_name = str(product_data.get("product_name") or "İsimsiz Ürün")
    brands = product_data.get("brands")
    raw_score = product_data.get("score")
    insufficient = bool(product_data.get("insufficient_data") or raw_score is None)
    db_score = -1 if insufficient else int(raw_score)
    grade = (
        "Yetersiz Veri"
        if insufficient
        else str(product_data.get("grade") or "Orta")
    )

    now_iso = datetime.now(timezone.utc).isoformat()

    # Önbelleğe kaydedilen JSON'un daima tutarlı olması için kopyasını al
    stored_payload = dict(product_data)
    stored_payload["from_cache"] = False
    stored_payload["insufficient_data"] = insufficient
    stored_payload["score"] = None if insufficient else int(raw_score)
    stored_payload["grade"] = grade
    response_json_str = json.dumps(stored_payload, ensure_ascii=False)

    with closing(_get_connection(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO products (
                barcode, product_name, brands, score, grade,
                response_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET
                product_name = excluded.product_name,
                brands = excluded.brands,
                score = excluded.score,
                grade = excluded.grade,
                response_json = excluded.response_json,
                updated_at = excluded.updated_at
            """,
            (
                barcode,
                product_name,
                brands,
                db_score,
                grade,
                response_json_str,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()


def get_recent_products(
    limit: int = 10,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Son taranan/güncellenen ürünleri yeniden eskiye doğru sıralı olarak döndürür.
    """
    safe_limit = max(1, min(100, int(limit)))
    with closing(_get_connection(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT barcode, product_name, brands, score, grade,
                   response_json, created_at, updated_at
            FROM products
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    recent_list: List[Dict[str, Any]] = []
    for row in rows:
        image_url = None
        parsed_score = row["score"]
        parsed_grade = row["grade"]
        insufficient = parsed_score is None or int(parsed_score) < 0 or parsed_grade == "Yetersiz Veri"
        try:
            parsed = json.loads(row["response_json"])
            image_url = parsed.get("image_url")
            if parsed.get("insufficient_data") or parsed.get("score") is None:
                insufficient = True
        except ValueError:
            pass

        recent_list.append(
            {
                "barcode": row["barcode"],
                "product_name": row["product_name"],
                "brands": row["brands"],
                "image_url": image_url,
                "score": None if insufficient else int(parsed_score),
                "grade": "Yetersiz Veri" if insufficient else parsed_grade,
                "insufficient_data": insufficient,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    return recent_list


def is_label_photo_product(
    barcode: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Verilen barkodun veritabanında mevcut olup olmadığını ve kullanıcının
    etiket fotoğrafıyla (`label_photo`) kaydedilip kaydedilmediğini kontrol eder.
    """
    cleaned_barcode = (barcode or "").strip()
    if not cleaned_barcode:
        return False

    with closing(_get_connection(db_path)) as conn:
        row = conn.execute(
            "SELECT response_json FROM products WHERE barcode = ?",
            (cleaned_barcode,),
        ).fetchone()

    if row is None:
        return False

    try:
        parsed = json.loads(row["response_json"])
        source = parsed.get("source") or parsed.get("data_source")
        return source in ("label_photo", "Etiket Fotoğrafı (OCR)")
    except ValueError:
        return False


def get_database_stats(
    db_path: Optional[Path] = None,
) -> Dict[str, int]:
    """
    Yerel SQLite veritabanındaki toplam ürün sayısını, Open Food Facts kaynaklı
    ürün sayısını ve etiket fotoğrafından (`label_photo`) eklenen ürün sayısını döndürür.
    """
    with closing(_get_connection(db_path)) as conn:
        rows = conn.execute("SELECT response_json FROM products").fetchall()

    total_products = len(rows)
    label_photo_count = 0
    open_food_facts_count = 0

    for row in rows:
        source = "open_food_facts"
        try:
            parsed = json.loads(row["response_json"])
            source = str(
                parsed.get("source") or parsed.get("data_source") or "open_food_facts"
            )
        except ValueError:
            pass

        if source in ("label_photo", "Etiket Fotoğrafı (OCR)"):
            label_photo_count += 1
        else:
            open_food_facts_count += 1

    return {
        "total_products": total_products,
        "open_food_facts_count": open_food_facts_count,
        "label_photo_count": label_photo_count,
    }


def search_products_by_text(
    query: str,
    limit: int = 15,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Ürün adı (`product_name`) ve marka (`brands`) alanlarında SQLite `tr_norm`
    fonksiyonu üzerinden Türkçe karakter ve büyük/küçük harf duyarsız arama yapar.
    """
    norm_q = _normalize_tr_search(query)
    if len(norm_q) < 2:
        return []

    safe_limit = max(1, min(50, int(limit)))
    like_pattern = f"%{norm_q}%"
    prefix_pattern = f"{norm_q}%"

    with closing(_get_connection(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT barcode, product_name, brands, score, grade,
                   response_json, created_at, updated_at
            FROM products
            WHERE tr_norm(product_name) LIKE ?
               OR tr_norm(COALESCE(brands, '')) LIKE ?
               OR tr_norm(product_name || ' ' || COALESCE(brands, '')) LIKE ?
            ORDER BY
                CASE
                    WHEN tr_norm(product_name) LIKE ? THEN 0
                    WHEN tr_norm(COALESCE(brands, '')) LIKE ? THEN 1
                    ELSE 2
                END ASC,
                updated_at DESC
            LIMIT ?
            """,
            (
                like_pattern,
                like_pattern,
                like_pattern,
                prefix_pattern,
                prefix_pattern,
                safe_limit,
            ),
        ).fetchall()

    results: List[Dict[str, Any]] = []
    for row in rows:
        image_url = None
        data_source = "open_food_facts"
        parsed_score = row["score"]
        parsed_grade = row["grade"]
        insufficient = parsed_score is None or int(parsed_score) < 0 or parsed_grade == "Yetersiz Veri"
        try:
            parsed = json.loads(row["response_json"])
            image_url = parsed.get("image_url")
            data_source = str(
                parsed.get("data_source") or parsed.get("source") or "open_food_facts"
            )
            if parsed.get("insufficient_data") or parsed.get("score") is None:
                insufficient = True
        except ValueError:
            pass

        final_grade = "Yetersiz Veri" if insufficient else parsed_grade
        results.append(
            {
                "barcode": row["barcode"],
                "product_name": row["product_name"],
                "brands": row["brands"],
                "score": None if insufficient else int(parsed_score),
                "score_label": final_grade,
                "grade": final_grade,
                "insufficient_data": insufficient,
                "image_url": image_url,
                "data_source": data_source,
            }
        )

    return results


def is_cached_product_missing_nutrition(cached: Dict[str, Any]) -> bool:
    """
    Yerel SQLite önbelleğindeki ürünün temel besin değerlerinin veya içindekiler
    bilgisinin eksik/boş olup olmadığını ve dış kaynaktan zenginleştirilmeye
    uygun olup olmadığını (`label_photo` değilse) kontrol eder.
    """
    if not isinstance(cached, dict):
        return False

    raw_src = str(cached.get("source") or cached.get("data_source") or "open_food_facts")
    if raw_src in ("label_photo", "Etiket Fotoğrafı (OCR)"):
        return False

    nutr = cached.get("nutrition") or {}
    known_core = sum(
        1
        for k in ("sugar_g", "saturated_fat_g", "salt_g", "calories_kcal")
        if nutr.get(k) is not None
    )
    known_risk = sum(
        1
        for k in ("sugar_g", "saturated_fat_g", "salt_g")
        if nutr.get(k) is not None
    )

    if known_core == 0 or (known_risk == 0 and known_core <= 1):
        return True

    if known_core < 4 or not cached.get("ingredients_text"):
        return True

    return False
