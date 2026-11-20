-- =============================================================================
-- GIDA DEDEKTİFİ - VERİTABANI ŞEMASI (SUPABASE / POSTGRESQL)
-- 1. Gün: Temel Tablolar ve İndeksler
-- =============================================================================

-- UUID fonksiyonları için eklenti (Supabase'de varsayılan etkindir)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- -----------------------------------------------------------------------------
-- 1. TABLO: urunler
-- Taranan veya sisteme kaydedilen gıda ürünlerinin temel ve analiz bilgileri
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS urunler (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    barkod VARCHAR(64) NOT NULL UNIQUE,
    urun_adi VARCHAR(255) NOT NULL,
    marka VARCHAR(255),
    kategori VARCHAR(100),
    icindekiler_metni TEXT,
    besin_degerleri JSONB DEFAULT '{}'::jsonb,
    e_kodlari TEXT[] DEFAULT ARRAY[]::TEXT[],
    palm_yagi_var_mi BOOLEAN DEFAULT FALSE,
    glikoz_surubu_var_mi BOOLEAN DEFAULT FALSE,
    puan INTEGER CHECK (puan >= 0 AND puan <= 100),
    veri_kaynagi VARCHAR(100), -- örn: 'open_food_facts', 'manuel', 'gemini_ocr'
    guncellenme_tarihi TIMESTAMPTZ DEFAULT NOW(),
    olusturulma_tarihi TIMESTAMPTZ DEFAULT NOW()
);

-- urunler tablosu için indeksler
CREATE INDEX IF NOT EXISTS idx_urunler_barkod ON urunler(barkod);
CREATE INDEX IF NOT EXISTS idx_urunler_marka ON urunler(marka);
CREATE INDEX IF NOT EXISTS idx_urunler_kategori ON urunler(kategori);
CREATE INDEX IF NOT EXISTS idx_urunler_e_kodlari ON urunler USING GIN (e_kodlari);
CREATE INDEX IF NOT EXISTS idx_urunler_besin_degerleri ON urunler USING GIN (besin_degerleri);

-- -----------------------------------------------------------------------------
-- 2. TABLO: e_kodlari
-- Gıda katkı maddeleri, sağlık riskleri ve hassasiyet etiketleri
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS e_kodlari (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kod VARCHAR(20) NOT NULL UNIQUE, -- örn: 'E621', 'E171'
    ad VARCHAR(255) NOT NULL,        -- örn: 'Monosodyum Glutamat'
    aciklama TEXT,
    risk_seviyesi VARCHAR(50) CHECK (risk_seviyesi IN ('dusuk', 'orta', 'yuksek')),
    hassasiyet_etiketleri TEXT[] DEFAULT ARRAY[]::TEXT[], -- örn: ['helal_supheli', 'cocuklar_icin_uyari', 'alerjen']
    bilimsel_kaynak TEXT,
    olusturulma_tarihi TIMESTAMPTZ DEFAULT NOW()
);

-- e_kodlari tablosu için indeksler
CREATE INDEX IF NOT EXISTS idx_e_kodlari_kod ON e_kodlari(kod);
CREATE INDEX IF NOT EXISTS idx_e_kodlari_risk_seviyesi ON e_kodlari(risk_seviyesi);
CREATE INDEX IF NOT EXISTS idx_e_kodlari_hassasiyet ON e_kodlari USING GIN (hassasiyet_etiketleri);

-- -----------------------------------------------------------------------------
-- 3. TABLO: tagsis_listesi
-- T.C. Tarım ve Orman Bakanlığı taklit veya tağşiş yapılan gıdalar listesi
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tagsis_listesi (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    firma_adi VARCHAR(255) NOT NULL,
    marka VARCHAR(255),
    urun_adi VARCHAR(255) NOT NULL,
    uygunsuzluk_nedeni TEXT,       -- örn: 'Tek tırnaklı eti tespiti', 'Bitkisel yağ tespiti'
    parti_seri_no VARCHAR(100),
    duyuru_tarihi DATE,
    resmi_kaynak_url TEXT,
    olusturulma_tarihi TIMESTAMPTZ DEFAULT NOW()
);

-- tagsis_listesi tablosu için indeksler
CREATE INDEX IF NOT EXISTS idx_tagsis_firma_adi ON tagsis_listesi(firma_adi);
CREATE INDEX IF NOT EXISTS idx_tagsis_marka ON tagsis_listesi(marka);
CREATE INDEX IF NOT EXISTS idx_tagsis_urun_adi ON tagsis_listesi(urun_adi);
CREATE INDEX IF NOT EXISTS idx_tagsis_duyuru_tarihi ON tagsis_listesi(duyuru_tarihi DESC);

-- -----------------------------------------------------------------------------
-- Güncelleme Zamanı için Otomatik Tetikleyici (urunler tablosu için)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION guncellenme_tarihi_tetikleyici()
RETURNS TRIGGER AS $$
BEGIN
    NEW.guncellenme_tarihi = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_urunler_guncellenme_tarihi ON urunler;
CREATE TRIGGER trg_urunler_guncellenme_tarihi
BEFORE UPDATE ON urunler
FOR EACH ROW
EXECUTE FUNCTION guncellenme_tarihi_tetikleyici();
