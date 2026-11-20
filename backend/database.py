"""
Gıda Dedektifi - SQLite Yerel Veritabanı (Önbellek ve Tarama Geçmişi)

Taranan ürünleri `database/irem.db` içerisinde önbelleğe (cache) alır.
30 günden yeni kayıtlar doğrudan yerel veritabanından hızlıca sunulur;
30 günü aşan kayıtlar dış kaynaktan yenilenerek güncellenir.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Proje kök dizinindeki database/irem.db yolu
BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "database"
DB_PATH = DB_DIR / "irem.db"

CACHE_TTL_DAYS = 30


def _get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """SQLite veritabanı bağlantısı oluşturur ve Row fabrikasını ayarlar."""
    target_path = Path(db_path) if db_path else DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """
    Veritabanı tablosunu (`products`) ve gerekli indeksleri oluşturur.
    Uygulama başlangıcında otomatik çağrılır.
    """
    with _get_connection(db_path) as conn:
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
    with _get_connection(db_path) as conn:
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
    score = int(product_data.get("score", 0))
    grade = str(product_data.get("grade") or "Orta")

    now_iso = datetime.now(timezone.utc).isoformat()

    # Önbelleğe kaydedilen JSON'un daima tutarlı olması için kopyasını al
    stored_payload = dict(product_data)
    stored_payload["from_cache"] = False
    response_json_str = json.dumps(stored_payload, ensure_ascii=False)

    with _get_connection(db_path) as conn:
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
                score,
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
    with _get_connection(db_path) as conn:
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
        try:
            parsed = json.loads(row["response_json"])
            image_url = parsed.get("image_url")
        except ValueError:
            pass

        recent_list.append(
            {
                "barcode": row["barcode"],
                "product_name": row["product_name"],
                "brands": row["brands"],
                "image_url": image_url,
                "score": row["score"],
                "grade": row["grade"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    return recent_list
