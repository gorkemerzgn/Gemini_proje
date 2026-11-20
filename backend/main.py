import os
from contextlib import asynccontextmanager
from typing import List, Literal, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import get_cached_product, get_recent_products, init_db, save_product
from openfoodfacts import fetch_product_by_barcode
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
    version="0.5.0",
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
    """100g veya 100ml ürün başına besin değerleri"""

    sugar_g: float = Field(default=0.0, ge=0, description="Şeker miktarı (g / 100g)")
    saturated_fat_g: float = Field(default=0.0, ge=0, description="Doymuş yağ miktarı (g / 100g)")
    salt_g: float = Field(default=0.0, ge=0, description="Tuz miktarı (g / 100g)")
    sodium_mg: float = Field(default=0.0, ge=0, description="Sodyum miktarı (mg / 100g)")
    calories_kcal: float = Field(default=0.0, ge=0, description="Enerji / Kalori (kcal / 100g)")
    fiber_g: float = Field(default=0.0, ge=0, description="Lif miktarı (g / 100g)")
    protein_g: float = Field(default=0.0, ge=0, description="Protein miktarı (g / 100g)")


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


class ScoreResponse(BaseModel):
    """Ürün puanlama yanıt modeli"""

    product_name: Optional[str] = Field(default=None, description="Ürün adı")
    score: int = Field(ge=0, le=100, description="Hesaplanan nihai ürün puanı (0 - 100)")
    grade: Literal["Çok İyi", "İyi", "Orta", "Düşük Skor"] = Field(
        description="Puan derecesi (Çok İyi / İyi / Orta / Düşük Skor)"
    )
    warnings: List[str] = Field(description="Türkçe içerik ve besin bilgilendirme notları")
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
    data_source: str = Field(default="open_food_facts", description="Veri kaynağı")
    from_cache: bool = Field(
        default=False,
        description="Ürün verisi yerel SQLite önbelleğinden mi getirildi?",
    )
    score: int = Field(ge=0, le=100, description="Hesaplanan nihai ürün puanı (0 - 100)")
    grade: Literal["Çok İyi", "İyi", "Orta", "Düşük Skor"] = Field(
        description="Puan derecesi (Çok İyi / İyi / Orta / Düşük Skor)"
    )
    warnings: List[str] = Field(description="Türkçe içerik ve besin bilgilendirme notları")
    breakdown: ScoreBreakdown = Field(description="Kategori bazlı puan kırılımı")
    disclaimer: str = Field(
        default=DEFAULT_DISCLAIMER,
        description="Bilgilendirme ve yasal sorumluluk reddi metni",
    )


class RecentProductItem(BaseModel):
    """Son taranan ürünler listesindeki özet ürün modeli"""

    barcode: str = Field(description="Ürün barkod numarası")
    product_name: str = Field(description="Ürün adı")
    brands: Optional[str] = Field(default=None, description="Marka bilgisi")
    image_url: Optional[str] = Field(default=None, description="Ürün görseli URL adresi")
    score: int = Field(ge=0, le=100, description="Hesaplanan ürün puanı (0 - 100)")
    grade: Literal["Çok İyi", "İyi", "Orta", "Düşük Skor"] = Field(description="Puan derecesi")
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


# -----------------------------------------------------------------------------
# API Uç Noktaları (Endpoints)
# -----------------------------------------------------------------------------
@app.get("/")
def read_root():
    """Kök uç noktası - Servis karşılama mesajı"""
    return {
        "message": "Gıda Dedektifi API çalışıyor",
        "version": "0.5.0",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    """Sağlık kontrolü uç noktası - Sunucunun ayakta olduğunu doğrular"""
    return {
        "status": "healthy",
        "service": "backend-api",
    }


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


@app.get("/api/product/{barcode}", response_model=ProductAnalysisResponse)
async def get_product_by_barcode(barcode: str) -> ProductAnalysisResponse:
    """
    Barkod numarası ile ürünü önce yerel SQLite önbelleğinde (30 gün geçerli) arar.
    Önbellekte yoksa veya süresi dolmuşsa Open Food Facts veri tabanından çeker,
    puanlama motorundan geçirir, veritabanına kaydeder ve analiz sonucunu döndürür.
    """
    cached = get_cached_product(barcode)
    if cached is not None:
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
    if product_data.get("has_missing_nutrition"):
        warnings.append(
            "Bilgi Notu: Bu ürünün bazı besin değeri verileri kaynakta eksik olabilir."
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
        data_source=product_data["data_source"],
        from_cache=False,
        score=score_result["score"],
        grade=score_result["grade"],
        warnings=warnings,
        breakdown=ScoreBreakdown(**score_result["breakdown"]),
        disclaimer=score_result["disclaimer"],
    )

    save_product(response_obj.model_dump())
    return response_obj
