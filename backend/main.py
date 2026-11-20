import os
from typing import List, Literal, Optional
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from openfoodfacts import fetch_product_by_barcode
from scoring import DEFAULT_DISCLAIMER, calculate_product_score

# .env dosyasındaki ortam değişkenlerini yükle
load_dotenv()

# FastAPI uygulama başlatma
app = FastAPI(
    title="İrem - Gıda & Etiket Analiz API",
    description="Barkod tarama, yapay zekâ ile etiket analizi ve taklit-tağşiş kontrolü API servisi",
    version="0.3.0",
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


# -----------------------------------------------------------------------------
# API Uç Noktaları (Endpoints)
# -----------------------------------------------------------------------------
@app.get("/")
def read_root():
    """Kök uç noktası - Servis karşılama mesajı"""
    return {
        "message": "Gıda Dedektifi API çalışıyor",
        "version": "0.3.0",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    """Sağlık kontrolü uç noktası - Sunucunun ayakta olduğunu doğrular"""
    return {
        "status": "healthy",
        "service": "backend-api",
    }


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


@app.get("/api/product/{barcode}", response_model=ProductAnalysisResponse)
async def get_product_by_barcode(barcode: str) -> ProductAnalysisResponse:
    """
    Barkod numarası ile ürünü Open Food Facts veri tabanından çeker,
    puanlama motorundan geçirir ve ürün bilgileriyle birlikte analiz sonucunu döndürür.
    """
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

    return ProductAnalysisResponse(
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
        score=score_result["score"],
        grade=score_result["grade"],
        warnings=warnings,
        breakdown=ScoreBreakdown(**score_result["breakdown"]),
        disclaimer=score_result["disclaimer"],
    )
