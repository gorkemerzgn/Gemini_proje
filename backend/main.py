import os
from contextlib import asynccontextmanager
from typing import List, Literal, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import (
    get_cached_product,
    get_database_stats,
    get_recent_products,
    init_db,
    is_cached_product_missing_nutrition,
    save_product,
    search_products_by_text,
)
from label_analyzer import extract_label_data_from_image
from openfoodfacts import _merge_parsed_off_products, fetch_product_by_barcode
from scoring import DEFAULT_DISCLAIMER, calculate_product_score

# .env dosyasındaki ortam değişkenlerini yükle
load_dotenv()

# Modül yüklendiğinde ve uygulama başlarken SQLite tablosunun hazır olmasını sağla
init_db()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Uygulama başlangıcında yerel SQLite veritabanı tablosunu hazırlar."""
    init_db()
    yield


# FastAPI uygulama başlatma
app = FastAPI(
    title="İrem - Gıda & Etiket Analiz API",
    description="Barkod tarama, yapay zekâ ile etiket analizi ve taklit-tağşiş kontrolü API servisi",
    version="0.8.0",
    lifespan=lifespan,
)

# Mobil uygulama ve web istemcileri için CORS izinleri
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Geliştirme aşamasında tüm kaynaklara izin verilir
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------------------------------------------------------
# Pydantic Veri Modelleri (/api/score)
# -----------------------------------------------------------------------------
class NutritionValues(BaseModel):
    """100g veya 100ml ürün başına besin değerleri (okunamayan/eksik alanlar None olarak tutulur)"""

    sugar_g: Optional[float] = Field(default=None, ge=0, description="Şeker miktarı (g / 100g)")
    saturated_fat_g: Optional[float] = Field(default=None, ge=0, description="Doymuş yağ miktarı (g / 100g)")
    salt_g: Optional[float] = Field(default=None, ge=0, description="Tuz miktarı (g / 100g)")
    sodium_mg: Optional[float] = Field(default=None, ge=0, description="Sodyum miktarı (mg / 100g)")
    calories_kcal: Optional[float] = Field(default=None, ge=0, description="Enerji / Kalori (kcal / 100g)")
    fiber_g: Optional[float] = Field(default=None, ge=0, description="Lif miktarı (g / 100g)")
    protein_g: Optional[float] = Field(default=None, ge=0, description="Protein miktarı (g / 100g)")


class ScoreRequest(BaseModel):
    """Ürün puanlama isteği modeli"""

    product_name: Optional[str] = Field(default=None, description="Ürün adı (isteğe bağlı)")
    nutrition: NutritionValues = Field(
        default_factory=NutritionValues,
        description="100g/100ml başına besin değerleri",
    )
    additives: List[str] = Field(
        default_factory=list,
        description="Üründeki katkı maddesi E-kodları listesi (örn: ['E621', 'E450'])",
    )
    has_palm_oil: bool = Field(default=False, description="Ürün palm yağı içeriyor mu?")
    has_glucose_syrup: bool = Field(
        default=False, description="Ürün glikoz/fruktoz şurubu içeriyor mu?"
    )
    is_adulterated: bool = Field(
        default=False,
        description="Ürün/parti ile ilgili kamuya açık T.C. Tarım ve Orman Bakanlığı resmi duyuru kaydı bulunuyor mu?",
    )


class ScoreBreakdown(BaseModel):
    """Puan kırılım detayları"""

    nutrition_score: float = Field(description="Besin değerleri puanı (0 - 60)")
    nutrition_max: int = Field(default=60, description="Besin değerleri maksimum puanı")
    additives_score: float = Field(description="Katkı maddeleri puanı (0 - 30)")
    additives_max: int = Field(default=30, description="Katkı maddeleri maksimum puanı")
    clean_content_score: float = Field(description="Doğal/temiz içerik puanı (0 - 10)")
    clean_content_max: int = Field(default=10, description="Doğal/temiz içerik maksimum puanı")
    raw_total_score: int = Field(description="Resmi duyuru sınırı öncesi ham toplam puan")
    adulteration_penalty_applied: bool = Field(
        description="Resmi duyuru kaydı nedeniyle puan sınırı (maks 10) uygulandı mı?"
    )


class ScoreBreakdownItem(BaseModel):
    """Şeffaf puan kırılımı tekil kalemi"""

    label: str = Field(description="Değerlendirilen kriter adı (örn: 'Şeker (100g)')")
    value_text: str = Field(
        description="Okunan veya hesaplanan değer metni (örn: '24.5 g' veya '—')"
    )
    impact: float = Field(description="Puana etkisi (örn: -18, +3, 0)")
    reason: str = Field(description="Nesnel ve yargısız hesaplama gerekçesi")


GradeLiteral = Literal["Çok İyi", "İyi", "Orta", "Düşük Skor", "Yetersiz Veri"]


class ScoreResponse(BaseModel):
    """Ürün puanlama yanıt modeli"""

    product_name: Optional[str] = Field(default=None, description="Ürün adı")
    insufficient_data: bool = Field(
        default=False,
        description="Besin değerleri eksik olduğu için puan hesaplanamadı mı?",
    )
    score: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Hesaplanan nihai ürün puanı (0 - 100) veya yetersiz veride None",
    )
    grade: GradeLiteral = Field(
        description="Puan derecesi (Çok İyi / İyi / Orta / Düşük Skor / Yetersiz Veri)"
    )
    warnings: List[str] = Field(description="Türkçe içerik ve besin bilgilendirme notları")
    score_breakdown: List[ScoreBreakdownItem] = Field(
        default_factory=list,
        description="Kalem bazlı şeffaf puan kırılımı listesi",
    )
    cap_applied: bool = Field(
        default=False,
        description="Eksik veri veya resmi duyuru nedeniyle puan tavanı uygulandı mı?",
    )
    cap_reason: Optional[str] = Field(
        default=None,
        description="Uygulanan puan tavanının nesnel açıklaması",
    )
    missing_fields_count: int = Field(
        default=0,
        ge=0,
        description="Eksik (None) olan besin değeri alanı sayısı",
    )
    breakdown: ScoreBreakdown = Field(description="Kategori bazlı puan kırılımı")
    disclaimer: str = Field(
        default=DEFAULT_DISCLAIMER,
        description="Bilgilendirme ve yasal sorumluluk reddi metni",
    )


class ProductAnalysisResponse(BaseModel):
    """Barkod ile sorgulanan ürünün detaylı bilgileri ve puanlama sonucu"""

    barcode: str = Field(description="Ürün barkod numarası")
    product_name: str = Field(description="Ürün adı")
    brands: Optional[str] = Field(default=None, description="Marka bilgisi")
    image_url: Optional[str] = Field(default=None, description="Ürün görseli URL adresi")
    ingredients_text: Optional[str] = Field(default=None, description="İçindekiler metni")
    nutrition: NutritionValues = Field(description="100g/100ml başına besin değerleri")
    additives: List[str] = Field(default_factory=list, description="Tespit edilen E-kodları")
    has_palm_oil: bool = Field(default=False, description="İçerikte palm yağı tespit edildi mi?")
    has_glucose_syrup: bool = Field(
        default=False, description="İçerikte glikoz/fruktoz şurubu tespit edildi mi?"
    )
    data_source: str = Field(
        default="Open Food Facts",
        description="Veri kaynağı ('Open Food Facts' veya 'Etiket Fotoğrafı (OCR)')",
    )
    source: str = Field(
        default="open_food_facts",
        description="Veri kaynağı kodu (open_food_facts veya label_photo)",
    )
    from_cache: bool = Field(
        default=False,
        description="Ürün verisi yerel SQLite önbelleğinden mi getirildi?",
    )
    insufficient_data: bool = Field(
        default=False,
        description="Besin değerleri eksik olduğu için puan hesaplanamadı mı?",
    )
    score: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Hesaplanan nihai ürün puanı (0 - 100) veya yetersiz veride None",
    )
    grade: GradeLiteral = Field(
        description="Puan derecesi (Çok İyi / İyi / Orta / Düşük Skor / Yetersiz Veri)"
    )
    warnings: List[str] = Field(description="Türkçe içerik ve besin bilgilendirme notları")
    score_breakdown: List[ScoreBreakdownItem] = Field(
        default_factory=list,
        description="Kalem bazlı şeffaf puan kırılımı listesi",
    )
    cap_applied: bool = Field(
        default=False,
        description="Eksik veri veya resmi duyuru nedeniyle puan tavanı uygulandı mı?",
    )
    cap_reason: Optional[str] = Field(
        default=None,
        description="Uygulanan puan tavanının nesnel açıklaması",
    )
    missing_fields_count: int = Field(
        default=0,
        ge=0,
        description="Eksik (None) olan besin değeri alanı sayısı",
    )
    breakdown: ScoreBreakdown = Field(description="Kategori bazlı puan kırılımı")
    disclaimer: str = Field(
        default=DEFAULT_DISCLAIMER,
        description="Bilgilendirme ve yasal sorumluluk reddi metni",
    )


class LabelAnalysisRequest(BaseModel):
    """Etiket fotoğrafından (OCR) ürün analizi isteği modeli"""

    image_base64: str = Field(description="Base64 formatında ürün etiketi fotoğrafı")
    mime_type: Optional[str] = Field(
        default="image/jpeg",
        description="Görsel MIME türü (image/jpeg, image/png, image/webp)",
    )
    barcode: Optional[str] = Field(
        default=None,
        description="İsteğe bağlı ürün barkod numarası (verilirse analiz sonucu SQLite önbelleğine kaydedilir)",
    )


class RecentProductItem(BaseModel):
    """Son taranan ürünler listesindeki özet ürün modeli"""

    barcode: str = Field(description="Ürün barkod numarası")
    product_name: str = Field(description="Ürün adı")
    brands: Optional[str] = Field(default=None, description="Marka bilgisi")
    image_url: Optional[str] = Field(default=None, description="Ürün görseli URL adresi")
    insufficient_data: bool = Field(
        default=False,
        description="Besin değerleri eksik olduğu için puan hesaplanamadı mı?",
    )
    score: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Hesaplanan ürün puanı (0 - 100) veya yetersiz veride None",
    )
    grade: GradeLiteral = Field(description="Puan derecesi")
    created_at: str = Field(description="İlk kayıt tarihi (ISO-8601 UTC)")
    updated_at: str = Field(description="Son güncellenme tarihi (ISO-8601 UTC)")


class RecentProductsResponse(BaseModel):
    """Son taranan ürünler yanıt modeli"""

    count: int = Field(description="Dönen ürün sayısı")
    items: List[RecentProductItem] = Field(description="Son taranan ürünler listesi")
    disclaimer: str = Field(
        default=DEFAULT_DISCLAIMER,
        description="Bilgilendirme ve yasal sorumluluk reddi metni",
    )


class ProductSearchItem(BaseModel):
    """İsim veya marka araması sonucunda dönen ürün özeti modeli"""

    barcode: str = Field(description="Ürün barkod numarası")
    product_name: str = Field(description="Ürün adı")
    brands: Optional[str] = Field(default=None, description="Marka bilgisi")
    insufficient_data: bool = Field(
        default=False,
        description="Besin değerleri eksik olduğu için puan hesaplanamadı mı?",
    )
    score: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Hesaplanan ürün puanı (0 - 100) veya yetersiz veride None",
    )
    score_label: GradeLiteral = Field(
        description="Puan etiketi / derecesi"
    )
    grade: GradeLiteral = Field(
        description="Puan derecesi"
    )
    image_url: Optional[str] = Field(default=None, description="Ürün görseli URL adresi")
    data_source: str = Field(
        default="open_food_facts",
        description="Veri kaynağı (open_food_facts veya label_photo)",
    )


class ProductSearchResponse(BaseModel):
    """İsim veya marka ile ürün arama yanıt modeli"""

    query: str = Field(description="Aranan metin")
    count: int = Field(description="Eşleşen ürün sayısı")
    items: List[ProductSearchItem] = Field(description="Arama sonuçları listesi")
    disclaimer: str = Field(
        default=DEFAULT_DISCLAIMER,
        description="Bilgilendirme ve yasal sorumluluk reddi metni",
    )


class MethodologyWeightItem(BaseModel):
    """Puanlama metodolojisi kategori ağırlık kalemi"""

    category: str = Field(description="Değerlendirme kategorisi")
    max_points: int = Field(description="Maksimum puan")
    percentage: int = Field(description="Toplam skordaki ağırlık yüzdesi (%)")
    description: str = Field(description="Kategorinin değerlendirme kapsamı")


class MethodologyReferenceItem(BaseModel):
    """Referans alınan resmi veya bilimsel kurum/doküman bilgisi"""

    institution: str = Field(description="Kurum veya otorite adı")
    document: str = Field(description="Referans doküman veya rehber adı")
    scope: str = Field(description="Puanlama motorunda yararlanılan kapsam")


class MethodologyResponse(BaseModel):
    """Puanlama şeffaflığı, metodoloji ve resmi kaynaklar yanıt modeli"""

    title: str = Field(description="Metodoloji başlığı")
    summary: str = Field(description="Genel puanlama yaklaşımı özeti")
    weights: List[MethodologyWeightItem] = Field(description="Puanlama ağırlık dağılımı")
    special_rules: List[str] = Field(description="Özel bilgilendirme ve sınır kuralları")
    references: List[MethodologyReferenceItem] = Field(
        description="Eşik değerlerde yararlanılan bilimsel ve resmi referanslar"
    )
    legal_notice: str = Field(description="Bağımsızlık ve hukuki şeffaflık bildirimi")
    disclaimer: str = Field(
        default=DEFAULT_DISCLAIMER,
        description="Bilgilendirme ve yasal sorumluluk reddi metni",
    )


class DatabaseStatsResponse(BaseModel):
    """Yerel SQLite veritabanı ürün istatistikleri yanıt modeli"""

    total_products: int = Field(description="Veritabanındaki toplam ürün sayısı")
    open_food_facts_count: int = Field(
        description="Open Food Facts kaynağından eklenen ürün sayısı"
    )
    label_photo_count: int = Field(
        description="Etiket fotoğrafından (OCR) eklenen ürün sayısı"
    )


# -----------------------------------------------------------------------------
# API Uç Noktaları (Endpoints)
# -----------------------------------------------------------------------------
@app.get("/")
def read_root():
    """Kök uç noktası - Servis karşılama mesajı"""
    return {
        "message": "Gıda Dedektifi API çalışıyor",
        "version": "0.8.0",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    """Sağlık kontrolü uç noktası - Sunucunun ayakta olduğunu doğrular"""
    return {
        "status": "healthy",
        "service": "backend-api",
    }


@app.get("/api/stats", response_model=DatabaseStatsResponse)
def read_database_stats() -> DatabaseStatsResponse:
    """
    Yerel SQLite veritabanındaki toplam ürün sayısını, Open Food Facts kaynaklı
    ürün sayısını ve etiket fotoğrafından (`label_photo`) eklenen ürün sayısını döndürür.
    """
    stats = get_database_stats()
    return DatabaseStatsResponse(**stats)


@app.get("/api/methodology", response_model=MethodologyResponse)
def get_methodology() -> MethodologyResponse:
    """
    Puanlama motorunun ağırlık dağılımını, yararlanılan ulusal/uluslararası
    bilimsel referansları ve hukuki şeffaflık bildirimini döndürür.
    """
    return MethodologyResponse(
        title="Nasıl Puanlıyoruz? Metodoloji ve Referanslar",
        summary=(
            "İrem puanlama motoru, ürün etiketlerinde yer alan kamuya açık besin ve içerik "
            "verilerini ulusal ve uluslararası beslenme rehberlerindeki referans eşiklere göre "
            "0 ile 100 arasında ölçeklendirir."
        ),
        weights=[
            MethodologyWeightItem(
                category="Besin Değerleri",
                max_points=60,
                percentage=60,
                description=(
                    "100g/100ml başına enerji (kalori), şeker, doymuş yağ ve tuz/sodyum "
                    "yoğunluğu ile lif ve protein katkısını değerlendirir."
                ),
            ),
            MethodologyWeightItem(
                category="Katkı Maddeleri (E-Kodları)",
                max_points=30,
                percentage=30,
                description=(
                    "İçerikte yer alan katkı maddelerinin sayısını ve fonksiyonel gruplarını "
                    "(koruyucu, renklendirici, tatlandırıcı, kıvam artırıcı vb.) değerlendirir."
                ),
            ),
            MethodologyWeightItem(
                category="İçerik Profili",
                max_points=10,
                percentage=10,
                description=(
                    "Ürünün genel içerik sadeliğini, palm yağı ve glikoz/fruktoz şurubu gibi "
                    "endüstriyel bileşenlerin varlığını değerlendirir."
                ),
            ),
        ],
        special_rules=[
            "Kamuya açık resmi duyuru kaydı bulunan ürün veya partilerde toplam skor, "
            "genel bilgilendirme amacıyla en fazla 10 puan ile sınırlandırılır.",
            "80-100 puan: 'Çok İyi', 60-79 puan: 'İyi', 40-59 puan: 'Orta', "
            "0-39 puan: 'Düşük Skor' olarak sınıflandırılır.",
        ],
        references=[
            MethodologyReferenceItem(
                institution="T.C. Tarım ve Orman Bakanlığı",
                document="Türk Gıda Kodeksi (Gıda Katkı Maddeleri ve Etiketleme Yönetmelikleri)",
                scope="Etiket beyan kuralları, E-kodu fonksiyonel tanımları ve kamuya açık resmi gıda duyuruları.",
            ),
            MethodologyReferenceItem(
                institution="T.C. Sağlık Bakanlığı",
                document="Türkiye Beslenme Rehberi (TÜBER)",
                scope="Şeker, tuz/sodyum, doymuş yağ, lif ve protein için dengeli beslenme referans eşikleri.",
            ),
            MethodologyReferenceItem(
                institution="Dünya Sağlık Örgütü (WHO / DSÖ)",
                document="Serbest Şeker, Sodyum ve Doymuş Yağ Günlük Alım Rehberleri",
                scope="100g/100ml başına şeker, tuz ve doymuş yağ yoğunluğu değerlendirme sınırları.",
            ),
            MethodologyReferenceItem(
                institution="Avrupa Gıda Güvenliği Otoritesi (EFSA)",
                document="Gıda Katkı Maddeleri Bilimsel Görüş ve Değerlendirme Raporları",
                scope="Katkı maddelerinin (E-kodları) fonksiyonel sınıflandırması ve tüketim duyarlılığı referansları.",
            ),
        ],
        legal_notice=(
            "Bu uygulama bağımsız bir tüketici bilgilendirme aracıdır; adı geçen resmi kurum ve "
            "kuruluşlarla herhangi bir kurumsal bağlantısı veya resmi onay ilişkisi bulunmamaktadır. "
            "Sunulan skorlar yalnızca kamuya açık etiket verilerinin bilimsel referans eşiklerine göre "
            "otomatik hesaplanmasıyla oluşturulur; tıbbi, diyetetik veya hukuki tavsiye ya da kesin yargı niteliği taşımaz."
        ),
        disclaimer=DEFAULT_DISCLAIMER,
    )


@app.post("/api/score", response_model=ScoreResponse)
def score_product(payload: ScoreRequest) -> ScoreResponse:
    """
    Ürünün besin değerleri (%60), katkı maddeleri (%30), doğal içerik (%10)
    ve kamuya açık resmi duyuru durumuna göre 0-100 arası puanını hesaplar.
    """
    result = calculate_product_score(
        nutrition=payload.nutrition.model_dump(),
        additives=payload.additives,
        is_adulterated=payload.is_adulterated,
        has_palm_oil=payload.has_palm_oil,
        has_glucose_syrup=payload.has_glucose_syrup,
    )
    return ScoreResponse(
        product_name=payload.product_name,
        **result,
    )


@app.get("/api/products/recent", response_model=RecentProductsResponse)
def list_recent_products(
    limit: int = Query(default=10, ge=1, le=100, description="Listelenecek maksimum ürün sayısı"),
) -> RecentProductsResponse:
    """
    Yerel veritabanına kaydedilmiş son taranan ürünleri yeniden eskiye doğru listeler.
    """
    recent_rows = get_recent_products(limit=limit)
    items = [RecentProductItem(**row) for row in recent_rows]
    return RecentProductsResponse(
        count=len(items),
        items=items,
        disclaimer=DEFAULT_DISCLAIMER,
    )


@app.get("/api/products/search", response_model=ProductSearchResponse)
def search_products(
    q: str = Query(..., min_length=2, description="Aranacak ürün veya marka adı (en az 2 karakter)"),
    limit: int = Query(default=15, ge=1, le=50, description="Maksimum arama sonucu sayısı"),
) -> ProductSearchResponse:
    """
    Yerel SQLite veritabanındaki ürünlerde ürün adı (`product_name`) ve marka (`brands`)
    üzerinden Türkçe karakter ve büyük/küçük harf duyarsız arama yapar.
    """
    cleaned_q = (q or "").strip()
    if len(cleaned_q) < 2:
        raise HTTPException(
            status_code=400,
            detail="Arama yapmak için en az 2 karakter girmelisiniz.",
        )

    matched_rows = search_products_by_text(query=cleaned_q, limit=limit)
    items = [ProductSearchItem(**row) for row in matched_rows]
    return ProductSearchResponse(
        query=cleaned_q,
        count=len(items),
        items=items,
        disclaimer=DEFAULT_DISCLAIMER,
    )


@app.get("/api/product/{barcode}", response_model=ProductAnalysisResponse)
@app.get("/api/products/{barcode}", response_model=ProductAnalysisResponse)
async def get_product_by_barcode(barcode: str) -> ProductAnalysisResponse:
    """
    Barkod numarası ile ürünü önce yerel SQLite önbelleğinde (30 gün geçerli) arar.
    - Ürün önbellekte varsa ancak temel besin değerleri/içindekiler eksikse ve kaynağı
      `label_photo` değilse, otomatik olarak Open Food Facts ürün detay API'lerinden
      (`world.openfoodfacts.org` ve `tr.openfoodfacts.org`) eksik verileri tamamlamayı dener.
    - Önbellekte yoksa doğrudan Open Food Facts'ten çeker, puanlar ve kaydeder.
    """
    cached = get_cached_product(barcode)
    if cached is not None:
        raw_src = str(cached.get("source") or cached.get("data_source") or "open_food_facts")
        is_label = raw_src in ("label_photo", "Etiket Fotoğrafı (OCR)")
        cached["source"] = "label_photo" if is_label else "open_food_facts"
        cached["data_source"] = "Etiket Fotoğrafı (OCR)" if is_label else "Open Food Facts"

        needs_recalc = (
            not cached.get("score_breakdown")
            or "insufficient_data" not in cached
        )

        # Kayıt yerel DB'de varsa ama besin değerleri eksikse ve label_photo değilse dış kaynaktan tamamla
        if not is_label and is_cached_product_missing_nutrition(cached):
            needs_recalc = True
            try:
                fresh_off = await fetch_product_by_barcode(barcode)
                merged = _merge_parsed_off_products(cached, fresh_off)
                cached["product_name"] = merged["product_name"]
                cached["brands"] = merged["brands"]
                cached["image_url"] = merged["image_url"]
                cached["ingredients_text"] = merged["ingredients_text"]
                cached["nutrition"] = merged["nutrition"]
                cached["additives"] = merged["additives"]
                cached["has_palm_oil"] = merged["has_palm_oil"]
                cached["has_glucose_syrup"] = merged["has_glucose_syrup"]
            except Exception:
                # Dış kaynak erişilemezse veya 404 dönerse mevcut verilerle puanlamayı güncelle
                pass

        if needs_recalc:
            recalc = calculate_product_score(
                nutrition=cached.get("nutrition") or {},
                additives=cached.get("additives") or [],
                is_adulterated=bool(
                    (cached.get("breakdown") or {}).get("adulteration_penalty_applied", False)
                ),
                has_palm_oil=bool(cached.get("has_palm_oil", False)),
                has_glucose_syrup=bool(cached.get("has_glucose_syrup", False)),
            )
            cached["insufficient_data"] = recalc["insufficient_data"]
            cached["score"] = recalc["score"]
            cached["grade"] = recalc["grade"]
            cached["warnings"] = recalc["warnings"]
            cached["score_breakdown"] = recalc["score_breakdown"]
            cached["cap_applied"] = recalc["cap_applied"]
            cached["cap_reason"] = recalc["cap_reason"]
            cached["missing_fields_count"] = recalc["missing_fields_count"]
            cached["breakdown"] = recalc["breakdown"]
            cached["disclaimer"] = recalc["disclaimer"]
            save_product(cached)
            cached["from_cache"] = True

        return ProductAnalysisResponse(**cached)

    product_data = await fetch_product_by_barcode(barcode)

    score_result = calculate_product_score(
        nutrition=product_data["nutrition"],
        additives=product_data["additives"],
        is_adulterated=False,
        has_palm_oil=product_data["has_palm_oil"],
        has_glucose_syrup=product_data["has_glucose_syrup"],
    )

    warnings = list(score_result["warnings"])
    has_score_missing_warn = any(
        "puan sınırlı veriye göre hesaplandı" in w
        or "besin değerleri veritabanında bulunmadığından" in w
        for w in warnings
    )
    if product_data.get("has_missing_nutrition") and not has_score_missing_warn:
        warnings.append(
            "Bilgi Notu: Bu ürünün bazı yardımcı besin değeri verileri (örn. lif/protein) kaynakta belirtilmemiş olabilir."
        )

    response_obj = ProductAnalysisResponse(
        barcode=product_data["barcode"],
        product_name=product_data["product_name"],
        brands=product_data["brands"],
        image_url=product_data["image_url"],
        ingredients_text=product_data["ingredients_text"],
        nutrition=NutritionValues(**product_data["nutrition"]),
        additives=product_data["additives"],
        has_palm_oil=product_data["has_palm_oil"],
        has_glucose_syrup=product_data["has_glucose_syrup"],
        data_source="Open Food Facts",
        source="open_food_facts",
        from_cache=False,
        insufficient_data=score_result["insufficient_data"],
        score=score_result["score"],
        grade=score_result["grade"],
        warnings=warnings,
        score_breakdown=[ScoreBreakdownItem(**item) for item in score_result["score_breakdown"]],
        cap_applied=score_result["cap_applied"],
        cap_reason=score_result["cap_reason"],
        missing_fields_count=score_result["missing_fields_count"],
        breakdown=ScoreBreakdown(**score_result["breakdown"]),
        disclaimer=score_result["disclaimer"],
    )

    save_product(response_obj.model_dump())
    return response_obj


@app.post("/api/analyze-label", response_model=ProductAnalysisResponse)
async def analyze_product_label(payload: LabelAnalysisRequest) -> ProductAnalysisResponse:
    """
    Ürün etiketi fotoğrafından ('İçindekiler' ve 'Besin Değerleri') Hibrit OCR
    (Ücretsiz Yerel RapidOCR / Gemini Vision) ile yapılandırılmış verileri çıkarır,
    ardından deterministik puanlama motorumuzdan (`scoring.py`) geçirerek skor, derece,
    uyarılar ve sorumluluk reddi metniyle döndürür.
    Eğer `barcode` gönderildiyse sonucu SQLite önbelleğine kaydeder.
    """
    cleaned_barcode = (payload.barcode or "").strip() or None

    label_data = await extract_label_data_from_image(
        image_base64=payload.image_base64,
        mime_type=payload.mime_type or "image/jpeg",
        barcode=cleaned_barcode,
    )

    score_result = calculate_product_score(
        nutrition=label_data["nutrition"],
        additives=label_data["additives"],
        is_adulterated=False,
        has_palm_oil=label_data["has_palm_oil"],
        has_glucose_syrup=label_data["has_glucose_syrup"],
    )

    warnings = list(score_result["warnings"])
    has_score_missing_warn = any(
        "puan sınırlı veriye göre hesaplandı" in w
        or "besin değerleri veritabanında bulunmadığından" in w
        for w in warnings
    )
    if label_data.get("has_missing_nutrition") and not has_score_missing_warn:
        warnings.append(
            "Bilgi Notu: Fotoğraftaki besin değerleri tablosunun bir kısmı görünmüyor veya eksik olabilir."
        )

    warnings.append(
        "Bilgi Notu: Bu veriler ürün etiket fotoğrafından optik karakter tanıma (OCR) ile okunmuştur; "
        "fotoğraf netliği ve ışık koşullarına bağlı olarak değerlerde küçük sapmalar olabilir."
    )

    response_obj = ProductAnalysisResponse(
        barcode=label_data["barcode"],
        product_name=label_data["product_name"],
        brands=label_data["brands"],
        image_url=label_data["image_url"],
        ingredients_text=label_data["ingredients_text"],
        nutrition=NutritionValues(**label_data["nutrition"]),
        additives=label_data["additives"],
        has_palm_oil=label_data["has_palm_oil"],
        has_glucose_syrup=label_data["has_glucose_syrup"],
        data_source="Etiket Fotoğrafı (OCR)",
        source="label_photo",
        from_cache=False,
        insufficient_data=score_result["insufficient_data"],
        score=score_result["score"],
        grade=score_result["grade"],
        warnings=warnings,
        score_breakdown=[ScoreBreakdownItem(**item) for item in score_result["score_breakdown"]],
        cap_applied=score_result["cap_applied"],
        cap_reason=score_result["cap_reason"],
        missing_fields_count=score_result["missing_fields_count"],
        breakdown=ScoreBreakdown(**score_result["breakdown"]),
        disclaimer=score_result["disclaimer"],
    )

    # Gerçek bir barkod numarası gönderildiyse SQLite önbelleğine kaydet
    if cleaned_barcode and cleaned_barcode != "ETIKET-FOTO":
        save_product(response_obj.model_dump())

    return response_obj
