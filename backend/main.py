import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# .env dosyasındaki ortam değişkenlerini yükle
load_dotenv()

# FastAPI uygulama başlatma
app = FastAPI(
    title="İrem - Gıda & Etiket Analiz API",
    description="Barkod tarama, yapay zekâ ile etiket analizi ve taklit-tağşiş kontrolü API servisi",
    version="0.1.0",
)

# Mobil uygulama ve web istemcileri için CORS izinleri
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Geliştirme aşamasında tüm kaynaklara izin verilir
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    """Kök uç noktası - Servis karşılama mesajı"""
    return {
        "message": "Gıda Dedektifi API çalışıyor",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    """Sağlık kontrolü uç noktası - Sunucunun ayakta olduğunu doğrular"""
    return {
        "status": "healthy",
        "service": "backend-api",
    }
