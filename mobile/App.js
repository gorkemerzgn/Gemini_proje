import React, { useEffect, useState, useCallback, useRef } from "react";
import {
  ActivityIndicator,
  Image,
  Keyboard,
  Modal,
  Platform,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  useWindowDimensions,
  View,
} from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";
import { API_BASE_URL } from "./config";

const SAMPLE_BARCODES = [
  { label: "Nutella (3017620422003)", barcode: "3017620422003" },
  { label: "Barilla Makarna (8076800195057)", barcode: "8076800195057" },
];

const SUPPORTED_BARCODE_TYPES = ["ean13", "ean8", "upc_a", "upc_e"];

const FALLBACK_METHODOLOGY = {
  title: "Nasıl Puanlıyoruz? Metodoloji ve Referanslar",
  summary:
    "İrem puanlama motoru, ürün etiketlerinde yer alan kamuya açık besin ve içerik verilerini ulusal ve uluslararası beslenme rehberlerindeki referans eşiklere göre 0 ile 100 arasında ölçeklendirir.",
  weights: [
    {
      category: "Besin Değerleri",
      max_points: 60,
      percentage: 60,
      description:
        "100g/100ml başına enerji (kalori), şeker, doymuş yağ ve tuz/sodyum yoğunluğu ile lif ve protein katkısını değerlendirir.",
    },
    {
      category: "Katkı Maddeleri (E-Kodları)",
      max_points: 30,
      percentage: 30,
      description:
        "İçerikte yer alan katkı maddelerinin sayısını ve fonksiyonel gruplarını (koruyucu, renklendirici, tatlandırıcı, kıvam artırıcı vb.) değerlendirir.",
    },
    {
      category: "İçerik Profili",
      max_points: 10,
      percentage: 10,
      description:
        "Ürünün genel içerik sadeliğini, palm yağı ve glikoz/fruktoz şurubu gibi endüstriyel bileşenlerin varlığını değerlendirir.",
    },
  ],
  special_rules: [
    "Kamuya açık resmi duyuru kaydı bulunan ürün veya partilerde toplam skor, genel bilgilendirme amacıyla en fazla 10 puan ile sınırlandırılır.",
    "80-100 puan: 'Çok İyi', 60-79 puan: 'İyi', 40-59 puan: 'Orta', 0-39 puan: 'Düşük Skor' olarak sınıflandırılır.",
  ],
  references: [
    {
      institution: "T.C. Tarım ve Orman Bakanlığı",
      document:
        "Türk Gıda Kodeksi (Gıda Katkı Maddeleri ve Etiketleme Yönetmelikleri)",
      scope:
        "Etiket beyan kuralları, E-kodu fonksiyonel tanımları ve kamuya açık resmi gıda duyuruları.",
    },
    {
      institution: "T.C. Sağlık Bakanlığı",
      document: "Türkiye Beslenme Rehberi (TÜBER)",
      scope:
        "Şeker, tuz/sodyum, doymuş yağ, lif ve protein için dengeli beslenme referans eşikleri.",
    },
    {
      institution: "Dünya Sağlık Örgütü (WHO / DSÖ)",
      document: "Serbest Şeker, Sodyum ve Doymuş Yağ Günlük Alım Rehberleri",
      scope:
        "100g/100ml başına şeker, tuz ve doymuş yağ yoğunluğu değerlendirme sınırları.",
    },
    {
      institution: "Avrupa Gıda Güvenliği Otoritesi (EFSA)",
      document:
        "Gıda Katkı Maddeleri Bilimsel Görüş ve Değerlendirme Raporları",
      scope:
        "Katkı maddelerinin (E-kodları) fonksiyonel sınıflandırması ve tüketim duyarlılığı referansları.",
    },
  ],
  legal_notice:
    "Bu uygulama bağımsız bir tüketici bilgilendirme aracıdır; adı geçen resmi kurum ve kuruluşlarla herhangi bir kurumsal bağlantısı veya resmi onay ilişkisi bulunmamaktadır. Sunulan skorlar yalnızca kamuya açık etiket verilerinin bilimsel referans eşiklerine göre otomatik hesaplanmasıyla oluşturulur; tıbbi, diyetetik veya hukuki tavsiye ya da kesin yargı niteliği taşımaz.",
  disclaimer:
    "Bu skor yalnızca genel bilgilendirme amaçlıdır; tıbbi, diyetetik veya hukuki tavsiye niteliği taşımaz.",
};

/**
 * Puana (0-100) göre Yuka tarzı renk paleti döndürür
 */
function getScoreTheme(score) {
  if (score >= 80) {
    return {
      primary: "#16A34A",
      softBg: "#DCFCE7",
      border: "#86EFAC",
      dot: "#22C55E",
    };
  }
  if (score >= 60) {
    return {
      primary: "#CA8A04",
      softBg: "#FEF9C3",
      border: "#FDE047",
      dot: "#EAB308",
    };
  }
  if (score >= 40) {
    return {
      primary: "#EA580C",
      softBg: "#FFEDD5",
      border: "#FDBA74",
      dot: "#F97316",
    };
  }
  return {
    primary: "#DC2626",
    softBg: "#FEE2E2",
    border: "#FCA5A5",
    dot: "#EF4444",
  };
}

function ProgressBar({ label, value, max, color }) {
  const ratio = Math.max(0, Math.min(1, (value || 0) / max));
  return (
    <View style={styles.progressRow}>
      <View style={styles.progressHeader}>
        <Text style={styles.progressLabel}>{label}</Text>
        <Text style={styles.progressValue}>
          {value} / {max} puan
        </Text>
      </View>
      <View style={styles.progressTrack}>
        <View
          style={[
            styles.progressFill,
            { width: `${Math.round(ratio * 100)}%`, backgroundColor: color },
          ]}
        />
      </View>
    </View>
  );
}

export default function App() {
  const { width: windowWidth, height: windowHeight } = useWindowDimensions();

  const [barcode, setBarcode] = useState("");
  const [product, setProduct] = useState(null);
  const [recentProducts, setRecentProducts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [recentLoading, setRecentLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState(null);
  const [notFoundBarcode, setNotFoundBarcode] = useState(null);

  // Metodoloji Modal state'leri
  const [methodologyVisible, setMethodologyVisible] = useState(false);
  const [methodology, setMethodology] = useState(FALLBACK_METHODOLOGY);
  const [methodologyLoading, setMethodologyLoading] = useState(false);

  // Kamera ve Barkod Tarayıcı state'leri
  const [permission, requestPermission] = useCameraPermissions();
  const [scannerOpen, setScannerOpen] = useState(false);
  const [torchEnabled, setTorchEnabled] = useState(false);
  const [scannedLock, setScannedLock] = useState(false);
  const [cameraReady, setCameraReady] = useState(false);
  const [cameraError, setCameraError] = useState(null);
  const scanLockRef = useRef(false);
  const cameraRef = useRef(null);

  const fetchRecentProducts = useCallback(async () => {
    setRecentLoading(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/products/recent?limit=10`);
      if (response.ok) {
        const data = await response.json();
        setRecentProducts(Array.isArray(data.items) ? data.items : []);
      }
    } catch (_err) {
      // İlk açılışta sunucu henüz erişilebilir değilse sessizce geç
    } finally {
      setRecentLoading(false);
    }
  }, []);

  const openMethodologyModal = useCallback(async () => {
    setMethodologyVisible(true);
    setMethodologyLoading(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/methodology`);
      if (response.ok) {
        const data = await response.json();
        setMethodology(data);
      }
    } catch (_err) {
      // Çevrimdışı durumda yerel metodoloji verisini göster
    } finally {
      setMethodologyLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRecentProducts();
  }, [fetchRecentProducts]);

  const analyzeBarcode = useCallback(
    async (targetBarcode) => {
      const cleaned = (targetBarcode ?? barcode).trim();
      if (!cleaned) {
        setNotFoundBarcode(null);
        setErrorMsg("Lütfen analiz etmek için geçerli bir barkod numarası girin.");
        return;
      }

      Keyboard.dismiss();
      setBarcode(cleaned);
      setLoading(true);
      setErrorMsg(null);
      setNotFoundBarcode(null);

      try {
        const response = await fetch(
          `${API_BASE_URL}/api/product/${encodeURIComponent(cleaned)}`
        );

        let data = null;
        try {
          data = await response.json();
        } catch (_parseErr) {
          data = null;
        }

        if (!response.ok) {
          setProduct(null);
          if (response.status === 404) {
            setNotFoundBarcode(cleaned);
          } else if (response.status === 503) {
            setErrorMsg(
              data?.detail ||
                "Dış ürün veri kaynağına şu anda ulaşılamıyor. Lütfen biraz sonra tekrar deneyin."
            );
          } else {
            setErrorMsg(
              data?.detail || `Sunucu hatası oluştu (HTTP ${response.status}).`
            );
          }
          return;
        }

        setProduct(data);
        fetchRecentProducts();
      } catch (_networkErr) {
        setProduct(null);
        setNotFoundBarcode(null);
        setErrorMsg(
          `Sunucuya bağlanılamadı (${API_BASE_URL}). Telefonunuzun ve bilgisayarınızın aynı Wi-Fi ağında olduğundan ve backend sunucusunun 8001 portunda çalıştığından emin olun.`
        );
      } finally {
        setLoading(false);
      }
    },
    [barcode, fetchRecentProducts]
  );

  // Kamera ekranını açma
  const handleOpenScanner = async () => {
    Keyboard.dismiss();
    scanLockRef.current = false;
    setScannedLock(false);
    setTorchEnabled(false);
    setCameraReady(false);
    setCameraError(null);
    setScannerOpen(true);

    if (!permission?.granted) {
      await requestPermission();
    }
  };

  // Kamera ekranını kapatma
  const handleCloseScanner = () => {
    setTorchEnabled(false);
    setScannerOpen(false);
    setCameraReady(false);
    setCameraError(null);
    scanLockRef.current = false;
    setScannedLock(false);
  };

  // Barkod okunduğunda çift okumayı engelleyip otomatik analiz başlatma
  const handleBarcodeScanned = (scanResult) => {
    if (scanLockRef.current || scannedLock) {
      return;
    }

    const rawData = String(scanResult?.data || "").trim();
    if (!rawData) {
      return;
    }

    // Ardışık/çift taramayı anında kilitle
    scanLockRef.current = true;
    setScannedLock(true);
    setTorchEnabled(false);
    setScannerOpen(false);

    // Barkodu kutuya yaz ve analizi otomatik başlat
    setBarcode(rawData);
    analyzeBarcode(rawData);
  };

  // Android'de CameraView mount olduktan sonra önizlemeyi garantiye almak için resumePreview çağrısı
  useEffect(() => {
    if (!scannerOpen || permission?.granted !== true) {
      return;
    }
    const timer = setTimeout(() => {
      if (cameraRef.current?.resumePreview) {
        cameraRef.current.resumePreview().catch(() => {});
      }
    }, 350);
    return () => clearTimeout(timer);
  }, [scannerOpen, permission?.granted]);

  const scoreTheme = product ? getScoreTheme(product.score) : null;

  // ---------------------------------------------------------------------------
  // TAM EKRAN KAMERA GÖRÜNÜMÜ (3 Bölgeli Dengeli Yerleşim)
  // ---------------------------------------------------------------------------
  if (scannerOpen) {
    const camWidth = Math.max(1, Math.round(windowWidth || 360));
    const camHeight = Math.max(
      1,
      cameraReady
        ? Math.round(windowHeight || 640)
        : Math.round(windowHeight || 640) - 1
    );

    return (
      <View style={styles.scannerFullScreen}>
        <StatusBar barStyle="light-content" backgroundColor="#000000" />

        {!permission ? (
          <View style={styles.permissionCenterBox}>
            <ActivityIndicator size="large" color="#16A34A" />
            <Text style={styles.permissionSubtext}>
              Kamera izni kontrol ediliyor...
            </Text>
          </View>
        ) : permission.granted !== true ? (
          <View style={styles.permissionCenterBox}>
            <Text style={styles.permissionIcon}>📷</Text>
            <Text style={styles.permissionTitle}>Kamera İzni Gerekiyor</Text>
            <Text style={styles.permissionSubtext}>
              Ürün paketleri üzerindeki barkodları kamera ile okutabilmek için
              kamera erişim izni vermeniz gerekmektedir.
            </Text>

            <TouchableOpacity
              style={styles.permissionGrantBtn}
              onPress={requestPermission}
              activeOpacity={0.85}
            >
              <Text style={styles.permissionGrantBtnText}>
                Kamera İznini Ver
              </Text>
            </TouchableOpacity>

            <TouchableOpacity
              style={styles.permissionCancelBtn}
              onPress={handleCloseScanner}
            >
              <Text style={styles.permissionCancelBtnText}>
                Vazgeç ve Ana Ekrana Dön
              </Text>
            </TouchableOpacity>
          </View>
        ) : (
          <View style={styles.cameraRootContainer}>
            {/* 1. KENDİ KENDİNE KAPANAN CAMERAVIEW */}
            <CameraView
              ref={cameraRef}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: camWidth,
                height: camHeight,
              }}
              facing="back"
              mode="picture"
              autofocus="on"
              enableTorch={torchEnabled}
              barcodeScannerSettings={{
                barcodeTypes: SUPPORTED_BARCODE_TYPES,
              }}
              onCameraReady={() => {
                setCameraReady(true);
              }}
              onBarcodeScanned={scannedLock ? undefined : handleBarcodeScanned}
              onMountError={(event) => {
                const msg =
                  event?.nativeEvent?.message ||
                  event?.message ||
                  "Kamera başlatılırken bir hata oluştu.";
                setCameraError(msg);
              }}
            />

            {/* 2. KARDEŞ (SIBLING) ŞEFFAF OVERLAY KATMANI (3 Dengeli Bölge) */}
            <View
              style={[StyleSheet.absoluteFillObject, styles.cameraOverlayLayer]}
              pointerEvents="box-none"
            >
              {/* BÖLGE 1 (ÜST): KAPAT VE FENER BUTONLARI */}
              <View style={styles.cameraTopBar} pointerEvents="box-none">
                <TouchableOpacity
                  style={styles.cameraControlBtn}
                  onPress={handleCloseScanner}
                  activeOpacity={0.8}
                >
                  <Text style={styles.cameraControlBtnText}>✕ Kapat</Text>
                </TouchableOpacity>

                <TouchableOpacity
                  style={[
                    styles.cameraControlBtn,
                    torchEnabled && styles.cameraTorchActiveBtn,
                  ]}
                  onPress={() => setTorchEnabled((prev) => !prev)}
                  activeOpacity={0.8}
                >
                  <Text
                    style={[
                      styles.cameraControlBtnText,
                      torchEnabled && styles.cameraTorchActiveText,
                    ]}
                  >
                    {torchEnabled ? "🔦 Fener Açık" : "🔦 Fener"}
                  </Text>
                </TouchableOpacity>
              </View>

              {/* BÖLGE 2 (TAM ORTA): YEŞİL TARAMA ÇERÇEVESİ VE LAZER ÇİZGİSİ */}
              <View style={styles.viewfinderWrapper} pointerEvents="none">
                {cameraError && (
                  <View style={styles.cameraErrorBanner}>
                    <Text style={styles.cameraErrorBannerText}>
                      Kamera Hatası: {cameraError}
                    </Text>
                  </View>
                )}

                <View style={styles.viewfinderBox}>
                  <View style={[styles.corner, styles.cornerTopLeft]} />
                  <View style={[styles.corner, styles.cornerTopRight]} />
                  <View style={[styles.corner, styles.cornerBottomLeft]} />
                  <View style={[styles.corner, styles.cornerBottomRight]} />
                  <View style={styles.scanLaserLine} />
                </View>
              </View>

              {/* BÖLGE 3 (ALT): YÖNLENDİRME BİLGİ KARTI */}
              <View style={styles.cameraBottomBar} pointerEvents="none">
                <View style={styles.cameraHintCard}>
                  <Text style={styles.cameraHintTitle}>
                    Barkodu Çerçeveye Hizalayın
                  </Text>
                  <Text style={styles.cameraHintSubtitle}>
                    EAN-13, EAN-8 ve UPC ürün barkodları algılandığında otomatik
                    olarak analiz başlatılır.
                  </Text>
                </View>
              </View>
            </View>
          </View>
        )}
      </View>
    );
  }

  // ---------------------------------------------------------------------------
  // ANA UYGULAMA EKRANI
  // ---------------------------------------------------------------------------
  return (
    <View style={styles.safeArea}>
      <StatusBar barStyle="dark-content" backgroundColor="#F8FAFC" />
      <ScrollView
        style={styles.container}
        contentContainerStyle={styles.contentContainer}
        keyboardShouldPersistTaps="handled"
      >
        {/* ÜST BAŞLIK VE NASIL PUANLIYORUZ BUTONU */}
        <View style={styles.header}>
          <View style={styles.headerTopRow}>
            <View style={styles.logoRow}>
              <View style={styles.logoBadge}>
                <Text style={styles.logoBadgeText}>İ</Text>
              </View>
              <View>
                <Text style={styles.appTitle}>İrem</Text>
                <Text style={styles.appSubtitle}>
                  Gıda & Etiket Analiz Rehberi
                </Text>
              </View>
            </View>

            <TouchableOpacity
              style={styles.methodologyHeaderBtn}
              onPress={openMethodologyModal}
              activeOpacity={0.8}
            >
              <Text style={styles.methodologyHeaderIcon}>ⓘ</Text>
              <Text style={styles.methodologyHeaderBtnText}>
                Nasıl Puanlıyoruz?
              </Text>
            </TouchableOpacity>
          </View>
        </View>

        {/* BARKOD TARAMA VE ARAMA KARTI */}
        <View style={styles.searchCard}>
          {/* KAMERA İLE BARKOD TARA BUTONU */}
          <TouchableOpacity
            style={styles.cameraScanButton}
            onPress={handleOpenScanner}
            activeOpacity={0.85}
          >
            <Text style={styles.cameraScanIcon}>📷</Text>
            <View style={styles.cameraScanTextCol}>
              <Text style={styles.cameraScanTitle}>Kamera ile Barkod Tara</Text>
              <Text style={styles.cameraScanSubtitle}>
                Ürün barkodunu kameraya okutarak anında analiz edin
              </Text>
            </View>
            <Text style={styles.cameraScanArrow}>›</Text>
          </TouchableOpacity>

          <View style={styles.dividerRow}>
            <View style={styles.dividerLine} />
            <Text style={styles.dividerText}>veya elle barkod girin</Text>
            <View style={styles.dividerLine} />
          </View>

          <View style={styles.inputRow}>
            <TextInput
              style={styles.input}
              placeholder="Barkod girin (örn: 3017620422003)"
              placeholderTextColor="#94A3B8"
              keyboardType="numeric"
              value={barcode}
              onChangeText={setBarcode}
              onSubmitEditing={() => analyzeBarcode(barcode)}
              returnKeyType="search"
            />
            {barcode.length > 0 && (
              <TouchableOpacity
                style={styles.clearBtn}
                onPress={() => setBarcode("")}
              >
                <Text style={styles.clearBtnText}>✕</Text>
              </TouchableOpacity>
            )}
          </View>

          <TouchableOpacity
            style={[styles.analyzeButton, loading && styles.analyzeButtonDisabled]}
            onPress={() => analyzeBarcode(barcode)}
            disabled={loading}
            activeOpacity={0.85}
          >
            {loading ? (
              <View style={styles.buttonLoadingRow}>
                <ActivityIndicator color="#FFFFFF" size="small" />
                <Text style={styles.analyzeButtonText}>Analiz Ediliyor...</Text>
              </View>
            ) : (
              <Text style={styles.analyzeButtonText}>Analiz Et</Text>
            )}
          </TouchableOpacity>

          {/* HIZLI TEST ÖRNEK BUTONLARI */}
          <View style={styles.sampleSection}>
            <Text style={styles.sampleTitle}>Hızlı Test Örnekleri:</Text>
            <View style={styles.sampleChipsRow}>
              {SAMPLE_BARCODES.map((sample) => (
                <TouchableOpacity
                  key={sample.barcode}
                  style={styles.sampleChip}
                  onPress={() => analyzeBarcode(sample.barcode)}
                  disabled={loading}
                >
                  <Text style={styles.sampleChipText}>{sample.label}</Text>
                </TouchableOpacity>
              ))}
            </View>
          </View>
        </View>

        {/* 404 BULUNAMAYAN ÜRÜN BİLGİ KARTI */}
        {notFoundBarcode && (
          <View style={styles.notFoundCard}>
            <View style={styles.notFoundHeaderRow}>
              <View style={styles.notFoundIconBadge}>
                <Text style={styles.notFoundIconText}>🔍</Text>
              </View>
              <View style={styles.notFoundTitleCol}>
                <Text style={styles.notFoundTitle}>
                  Ürün Henüz Veritabanında Yok
                </Text>
                <Text style={styles.notFoundBarcodeTag}>
                  Barkod: #{notFoundBarcode}
                </Text>
              </View>
            </View>

            <Text style={styles.notFoundDescription}>
              Okuttuğunuz{" "}
              <Text style={styles.notFoundBold}>#{notFoundBarcode}</Text> barkod
              numaralı ürün açık gıda veritabanında henüz kayıtlı değil. Çok
              yakında <Text style={styles.notFoundBold}>"İçindekiler"</Text>{" "}
              etiketi fotoğrafıyla analiz özelliği eklenecektir.
            </Text>

            <TouchableOpacity
              style={styles.notFoundScanAgainBtn}
              onPress={handleOpenScanner}
              activeOpacity={0.85}
            >
              <Text style={styles.notFoundScanAgainBtnText}>
                📷 Başka Ürün Tara
              </Text>
            </TouchableOpacity>
          </View>
        )}

        {/* BAĞLANTI / SUNUCU HATASI KARTI (503, Ağ Kopması vb.) */}
        {errorMsg && (
          <View style={styles.errorCard}>
            <Text style={styles.errorTitle}>Bağlantı / Sunucu Uyarısı</Text>
            <Text style={styles.errorText}>{errorMsg}</Text>
          </View>
        )}

        {/* ÜRÜN ANALİZ SONUÇ KARTI */}
        {product && scoreTheme && (
          <View style={styles.resultCard}>
            {/* Üst Bilgi: Görsel + İsim + Marka */}
            <View style={styles.productTopRow}>
              {product.image_url ? (
                <Image
                  source={{ uri: product.image_url }}
                  style={styles.productImage}
                  resizeMode="contain"
                />
              ) : (
                <View style={styles.productImagePlaceholder}>
                  <Text style={styles.placeholderText}>Görsel Yok</Text>
                </View>
              )}

              <View style={styles.productMeta}>
                <View style={styles.badgeRow}>
                  <Text style={styles.barcodeTag}>#{product.barcode}</Text>
                  <View
                    style={[
                      styles.cacheTag,
                      {
                        backgroundColor: product.from_cache
                          ? "#E0F2FE"
                          : "#F1F5F9",
                      },
                    ]}
                  >
                    <Text
                      style={[
                        styles.cacheTagText,
                        {
                          color: product.from_cache ? "#0369A1" : "#475569",
                        },
                      ]}
                    >
                      {product.from_cache ? "Önbellek" : "Canlı Veri"}
                    </Text>
                  </View>
                </View>

                <Text style={styles.productName}>{product.product_name}</Text>
                <Text style={styles.productBrand}>
                  {product.brands || "Marka bilgisi belirtilmemiş"}
                </Text>
              </View>
            </View>

            {/* YUKA TARZI SKOR ROZETİ VE DERECE */}
            <View
              style={[
                styles.scoreBanner,
                {
                  backgroundColor: scoreTheme.softBg,
                  borderColor: scoreTheme.border,
                },
              ]}
            >
              <View
                style={[
                  styles.scoreCircle,
                  { borderColor: scoreTheme.primary },
                ]}
              >
                <Text
                  style={[styles.scoreNumber, { color: scoreTheme.primary }]}
                >
                  {product.score}
                </Text>
                <Text style={styles.scoreMaxText}>/ 100</Text>
              </View>

              <View style={styles.scoreGradeContainer}>
                <Text style={styles.scoreGradeLabel}>Değerlendirme</Text>
                <View style={styles.gradePillRow}>
                  <View
                    style={[
                      styles.gradeDot,
                      { backgroundColor: scoreTheme.dot },
                    ]}
                  />
                  <Text
                    style={[
                      styles.scoreGradeValue,
                      { color: scoreTheme.primary },
                    ]}
                  >
                    {product.grade}
                  </Text>
                </View>
                <Text style={styles.scoreSubtext}>
                  Besin değerleri, katkı maddeleri ve içerik analizi
                </Text>
              </View>
            </View>

            {/* SONUÇ KARTI İÇİ METODOLOJİ / KAYNAKLAR BUTONU */}
            <TouchableOpacity
              style={styles.cardMethodologyBtn}
              onPress={openMethodologyModal}
              activeOpacity={0.8}
            >
              <Text style={styles.cardMethodologyBtnText}>
                ⓘ Nasıl Puanlıyoruz? Metodoloji ve Resmi Kaynaklar
              </Text>
              <Text style={styles.cardMethodologyArrow}>›</Text>
            </TouchableOpacity>

            {/* PUAN KIRILIMI (BREAKDOWN) */}
            {product.breakdown && (
              <View style={styles.sectionBlock}>
                <Text style={styles.blockTitle}>Puan Kırılımı</Text>
                <ProgressBar
                  label="Besin Değerleri (%60)"
                  value={product.breakdown.nutrition_score}
                  max={product.breakdown.nutrition_max || 60}
                  color="#0EA5E9"
                />
                <ProgressBar
                  label="Katkı Maddeleri (%30)"
                  value={product.breakdown.additives_score}
                  max={product.breakdown.additives_max || 30}
                  color="#8B5CF6"
                />
                <ProgressBar
                  label="İçerik Profili (%10)"
                  value={product.breakdown.clean_content_score}
                  max={product.breakdown.clean_content_max || 10}
                  color="#10B981"
                />
              </View>
            )}

            {/* 100g BESİN DEĞERLERİ ÖZETİ */}
            {product.nutrition && (
              <View style={styles.sectionBlock}>
                <Text style={styles.blockTitle}>
                  Besin Değerleri (100g / 100ml)
                </Text>
                <View style={styles.nutritionGrid}>
                  <View style={styles.nutritionBox}>
                    <Text style={styles.nutritionValue}>
                      {product.nutrition.calories_kcal} kcal
                    </Text>
                    <Text style={styles.nutritionLabel}>Enerji</Text>
                  </View>
                  <View style={styles.nutritionBox}>
                    <Text style={styles.nutritionValue}>
                      {product.nutrition.sugar_g} g
                    </Text>
                    <Text style={styles.nutritionLabel}>Şeker</Text>
                  </View>
                  <View style={styles.nutritionBox}>
                    <Text style={styles.nutritionValue}>
                      {product.nutrition.saturated_fat_g} g
                    </Text>
                    <Text style={styles.nutritionLabel}>Doymuş Yağ</Text>
                  </View>
                  <View style={styles.nutritionBox}>
                    <Text style={styles.nutritionValue}>
                      {product.nutrition.salt_g} g
                    </Text>
                    <Text style={styles.nutritionLabel}>Tuz</Text>
                  </View>
                  <View style={styles.nutritionBox}>
                    <Text style={styles.nutritionValue}>
                      {product.nutrition.fiber_g} g
                    </Text>
                    <Text style={styles.nutritionLabel}>Lif</Text>
                  </View>
                  <View style={styles.nutritionBox}>
                    <Text style={styles.nutritionValue}>
                      {product.nutrition.protein_g} g
                    </Text>
                    <Text style={styles.nutritionLabel}>Protein</Text>
                  </View>
                </View>
              </View>
            )}

            {/* UYARILAR VE BİLGİLENDİRME NOTLARI */}
            <View style={styles.sectionBlock}>
              <Text style={styles.blockTitle}>
                İçerik ve Besin Bilgilendirme Notları
              </Text>
              {product.warnings && product.warnings.length > 0 ? (
                product.warnings.map((warn, idx) => (
                  <View key={idx} style={styles.warningItem}>
                    <View style={styles.warningBullet} />
                    <Text style={styles.warningText}>{warn}</Text>
                  </View>
                ))
              ) : (
                <View style={styles.cleanInfoBox}>
                  <Text style={styles.cleanInfoText}>
                    Bu ürün için dikkat çeken olumsuz bir bileşen veya sınır
                    aşımı tespit edilmedi.
                  </Text>
                </View>
              )}
            </View>

            {/* TESPİT EDİLEN E-KODLARI */}
            {product.additives && product.additives.length > 0 && (
              <View style={styles.sectionBlock}>
                <Text style={styles.blockTitle}>
                  Tespit Edilen Katkı Maddeleri (E-Kodları)
                </Text>
                <View style={styles.eCodeContainer}>
                  {product.additives.map((code) => (
                    <View key={code} style={styles.eCodeChip}>
                      <Text style={styles.eCodeText}>{code}</Text>
                    </View>
                  ))}
                </View>
              </View>
            )}

            {/* YASAL SORUMLULUK REDDİ (DISCLAIMER) */}
            {product.disclaimer && (
              <View style={styles.disclaimerBox}>
                <Text style={styles.disclaimerText}>{product.disclaimer}</Text>
              </View>
            )}
          </View>
        )}

        {/* SON TARANAN ÜRÜNLER LİSTESİ */}
        <View style={styles.recentSection}>
          <View style={styles.recentHeaderRow}>
            <Text style={styles.recentTitle}>Son Tarananlar</Text>
            <TouchableOpacity
              onPress={fetchRecentProducts}
              disabled={recentLoading}
            >
              <Text style={styles.refreshBtnText}>
                {recentLoading ? "Yenileniyor..." : "Yenile"}
              </Text>
            </TouchableOpacity>
          </View>

          {recentProducts.length === 0 ? (
            <View style={styles.emptyRecentBox}>
              <Text style={styles.emptyRecentText}>
                Henüz taranmış bir ürün bulunmuyor. Yukarıdan barkod okutarak
                veya numara girerek başlayabilirsiniz.
              </Text>
            </View>
          ) : (
            recentProducts.map((item) => {
              const itemTheme = getScoreTheme(item.score);
              return (
                <TouchableOpacity
                  key={item.barcode}
                  style={styles.recentCard}
                  onPress={() => analyzeBarcode(item.barcode)}
                  activeOpacity={0.8}
                >
                  {item.image_url ? (
                    <Image
                      source={{ uri: item.image_url }}
                      style={styles.recentImage}
                      resizeMode="contain"
                    />
                  ) : (
                    <View style={styles.recentImagePlaceholder}>
                      <Text style={styles.recentPlaceholderText}>Ürün</Text>
                    </View>
                  )}

                  <View style={styles.recentInfo}>
                    <Text style={styles.recentName} numberOfLines={1}>
                      {item.product_name}
                    </Text>
                    <Text style={styles.recentBrand} numberOfLines={1}>
                      {item.brands || item.barcode}
                    </Text>
                  </View>

                  <View
                    style={[
                      styles.recentScorePill,
                      {
                        backgroundColor: itemTheme.softBg,
                        borderColor: itemTheme.border,
                      },
                    ]}
                  >
                    <View
                      style={[
                        styles.recentScoreDot,
                        { backgroundColor: itemTheme.dot },
                      ]}
                    />
                    <Text
                      style={[
                        styles.recentScoreText,
                        { color: itemTheme.primary },
                      ]}
                    >
                      {item.score} • {item.grade}
                    </Text>
                  </View>
                </TouchableOpacity>
              );
            })
          )}
        </View>
      </ScrollView>

      {/* METODOLOJİ VE RESMİ KAYNAKLAR MODAL PENCERESİ */}
      <Modal
        visible={methodologyVisible}
        animationType="slide"
        transparent={true}
        onRequestClose={() => setMethodologyVisible(false)}
      >
        <View style={styles.modalOverlay}>
          <View style={styles.modalContainer}>
            <View style={styles.modalHeader}>
              <Text style={styles.modalTitle}>
                {methodology?.title || "Nasıl Puanlıyoruz?"}
              </Text>
              <TouchableOpacity
                style={styles.modalCloseBtn}
                onPress={() => setMethodologyVisible(false)}
              >
                <Text style={styles.modalCloseBtnText}>✕</Text>
              </TouchableOpacity>
            </View>

            <ScrollView
              style={styles.modalScroll}
              contentContainerStyle={styles.modalScrollContent}
              showsVerticalScrollIndicator={false}
            >
              {methodologyLoading && (
                <View style={styles.modalLoadingRow}>
                  <ActivityIndicator size="small" color="#16A34A" />
                  <Text style={styles.modalLoadingText}>
                    Güncel metodoloji bilgileri kontrol ediliyor...
                  </Text>
                </View>
              )}

              <Text style={styles.modalSummary}>{methodology?.summary}</Text>

              {/* 1. Puanlama Ağırlıkları */}
              <Text style={styles.modalSectionTitle}>
                1. Puanlama Dağılımı (100 Puan)
              </Text>
              {methodology?.weights?.map((w, idx) => (
                <View key={idx} style={styles.weightCard}>
                  <View style={styles.weightHeaderRow}>
                    <Text style={styles.weightCategory}>{w.category}</Text>
                    <View style={styles.weightBadge}>
                      <Text style={styles.weightBadgeText}>
                        %{w.percentage} ({w.max_points} Puan)
                      </Text>
                    </View>
                  </View>
                  <Text style={styles.weightDesc}>{w.description}</Text>
                </View>
              ))}

              {/* Özel Kurallar */}
              {methodology?.special_rules &&
                methodology.special_rules.length > 0 && (
                  <View style={styles.rulesBox}>
                    <Text style={styles.rulesTitle}>
                      Sınıflandırma ve Özel Kurallar
                    </Text>
                    {methodology.special_rules.map((rule, idx) => (
                      <Text key={idx} style={styles.ruleItemText}>
                        • {rule}
                      </Text>
                    ))}
                  </View>
                )}

              {/* 2. Bilimsel ve Resmi Referanslar */}
              <Text style={styles.modalSectionTitle}>
                2. Bilimsel ve Resmi Referans Kaynakları
              </Text>
              {methodology?.references?.map((ref, idx) => (
                <View key={idx} style={styles.referenceCard}>
                  <Text style={styles.refInstitution}>{ref.institution}</Text>
                  <Text style={styles.refDocument}>{ref.document}</Text>
                  <Text style={styles.refScope}>{ref.scope}</Text>
                </View>
              ))}

              {/* 3. Hukuki Şeffaflık ve Sorumluluk Reddi */}
              <Text style={styles.modalSectionTitle}>
                3. Hukuki Şeffaflık ve Bağımsızlık Bildirimi
              </Text>
              <View style={styles.legalNoticeBox}>
                <Text style={styles.legalNoticeText}>
                  {methodology?.legal_notice}
                </Text>
              </View>
            </ScrollView>

            <TouchableOpacity
              style={styles.modalDoneButton}
              onPress={() => setMethodologyVisible(false)}
              activeOpacity={0.85}
            >
              <Text style={styles.modalDoneButtonText}>Anladım, Kapat</Text>
            </TouchableOpacity>
          </View>
        </View>
      </Modal>
    </View>
  );
}

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: "#F8FAFC",
    paddingTop: Platform.OS === "android" ? (StatusBar.currentHeight || 24) : 48,
  },
  container: {
    flex: 1,
  },
  contentContainer: {
    padding: 16,
    paddingBottom: 40,
  },
  header: {
    marginBottom: 16,
  },
  headerTopRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  logoRow: {
    flexDirection: "row",
    alignItems: "center",
  },
  logoBadge: {
    width: 44,
    height: 44,
    borderRadius: 14,
    backgroundColor: "#16A34A",
    alignItems: "center",
    justifyContent: "center",
    marginRight: 12,
    shadowColor: "#16A34A",
    shadowOpacity: 0.25,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 3 },
    elevation: 4,
  },
  logoBadgeText: {
    color: "#FFFFFF",
    fontSize: 22,
    fontWeight: "800",
  },
  appTitle: {
    fontSize: 26,
    fontWeight: "800",
    color: "#0F172A",
    letterSpacing: -0.5,
  },
  appSubtitle: {
    fontSize: 12.5,
    color: "#64748B",
    marginTop: 1,
  },
  methodologyHeaderBtn: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "#FFFFFF",
    borderWidth: 1,
    borderColor: "#CBD5E1",
    paddingHorizontal: 11,
    paddingVertical: 8,
    borderRadius: 12,
  },
  methodologyHeaderIcon: {
    fontSize: 14,
    color: "#16A34A",
    fontWeight: "800",
    marginRight: 5,
  },
  methodologyHeaderBtnText: {
    fontSize: 12,
    fontWeight: "700",
    color: "#1E293B",
  },
  searchCard: {
    backgroundColor: "#FFFFFF",
    borderRadius: 20,
    padding: 18,
    marginBottom: 16,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    shadowColor: "#0F172A",
    shadowOpacity: 0.04,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 4 },
    elevation: 2,
  },
  cameraScanButton: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "#0F172A",
    borderRadius: 16,
    paddingVertical: 14,
    paddingHorizontal: 16,
    shadowColor: "#0F172A",
    shadowOpacity: 0.15,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 3 },
    elevation: 3,
  },
  cameraScanIcon: {
    fontSize: 24,
    marginRight: 12,
  },
  cameraScanTextCol: {
    flex: 1,
  },
  cameraScanTitle: {
    color: "#FFFFFF",
    fontSize: 16,
    fontWeight: "800",
  },
  cameraScanSubtitle: {
    color: "#CBD5E1",
    fontSize: 11.5,
    marginTop: 2,
  },
  cameraScanArrow: {
    color: "#22C55E",
    fontSize: 24,
    fontWeight: "800",
    marginLeft: 8,
  },
  dividerRow: {
    flexDirection: "row",
    alignItems: "center",
    marginVertical: 14,
  },
  dividerLine: {
    flex: 1,
    height: 1,
    backgroundColor: "#E2E8F0",
  },
  dividerText: {
    fontSize: 12,
    color: "#64748B",
    fontWeight: "600",
    marginHorizontal: 10,
  },
  inputRow: {
    position: "relative",
    justifyContent: "center",
  },
  input: {
    backgroundColor: "#F8FAFC",
    borderWidth: 1.5,
    borderColor: "#CBD5E1",
    borderRadius: 14,
    paddingHorizontal: 14,
    paddingRight: 40,
    paddingVertical: 13,
    fontSize: 16,
    color: "#0F172A",
    fontWeight: "500",
  },
  clearBtn: {
    position: "absolute",
    right: 12,
    padding: 6,
  },
  clearBtnText: {
    fontSize: 16,
    color: "#94A3B8",
    fontWeight: "700",
  },
  analyzeButton: {
    backgroundColor: "#16A34A",
    borderRadius: 14,
    paddingVertical: 14,
    alignItems: "center",
    justifyContent: "center",
    marginTop: 12,
  },
  analyzeButtonDisabled: {
    opacity: 0.7,
  },
  buttonLoadingRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  analyzeButtonText: {
    color: "#FFFFFF",
    fontSize: 16,
    fontWeight: "700",
    marginLeft: 6,
  },
  sampleSection: {
    marginTop: 14,
    paddingTop: 12,
    borderTopWidth: 1,
    borderTopColor: "#F1F5F9",
  },
  sampleTitle: {
    fontSize: 12,
    color: "#64748B",
    fontWeight: "600",
    marginBottom: 8,
  },
  sampleChipsRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  sampleChip: {
    backgroundColor: "#F1F5F9",
    paddingHorizontal: 11,
    paddingVertical: 7,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  sampleChipText: {
    fontSize: 12,
    color: "#334155",
    fontWeight: "600",
  },

  /* 404 BULUNAMAYAN ÜRÜN KARTI STİLLERİ */
  notFoundCard: {
    backgroundColor: "#FFFFFF",
    borderWidth: 1.5,
    borderColor: "#CBD5E1",
    borderRadius: 20,
    padding: 18,
    marginBottom: 18,
    shadowColor: "#0F172A",
    shadowOpacity: 0.05,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 4 },
    elevation: 2,
  },
  notFoundHeaderRow: {
    flexDirection: "row",
    alignItems: "center",
    marginBottom: 12,
  },
  notFoundIconBadge: {
    width: 46,
    height: 46,
    borderRadius: 14,
    backgroundColor: "#F1F5F9",
    borderWidth: 1,
    borderColor: "#E2E8F0",
    alignItems: "center",
    justifyContent: "center",
    marginRight: 12,
  },
  notFoundIconText: {
    fontSize: 22,
  },
  notFoundTitleCol: {
    flex: 1,
  },
  notFoundTitle: {
    fontSize: 16.5,
    fontWeight: "800",
    color: "#0F172A",
  },
  notFoundBarcodeTag: {
    fontSize: 12.5,
    fontWeight: "600",
    color: "#64748B",
    marginTop: 2,
  },
  notFoundDescription: {
    fontSize: 13.5,
    color: "#475569",
    lineHeight: 20,
    marginBottom: 16,
  },
  notFoundBold: {
    fontWeight: "700",
    color: "#0F172A",
  },
  notFoundScanAgainBtn: {
    backgroundColor: "#0F172A",
    borderRadius: 14,
    paddingVertical: 13,
    alignItems: "center",
    justifyContent: "center",
  },
  notFoundScanAgainBtnText: {
    color: "#FFFFFF",
    fontSize: 15,
    fontWeight: "700",
  },

  errorCard: {
    backgroundColor: "#FEF2F2",
    borderWidth: 1,
    borderColor: "#FECACA",
    borderRadius: 16,
    padding: 16,
    marginBottom: 16,
  },
  errorTitle: {
    fontSize: 14,
    fontWeight: "700",
    color: "#B91C1C",
    marginBottom: 4,
  },
  errorText: {
    fontSize: 13.5,
    color: "#7F1D1D",
    lineHeight: 19,
  },
  resultCard: {
    backgroundColor: "#FFFFFF",
    borderRadius: 22,
    padding: 18,
    marginBottom: 20,
    borderWidth: 1,
    borderColor: "#E2E8F0",
    shadowColor: "#0F172A",
    shadowOpacity: 0.06,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 5 },
    elevation: 3,
  },
  productTopRow: {
    flexDirection: "row",
    alignItems: "center",
    marginBottom: 16,
  },
  productImage: {
    width: 84,
    height: 84,
    borderRadius: 14,
    backgroundColor: "#F8FAFC",
    borderWidth: 1,
    borderColor: "#E2E8F0",
    marginRight: 14,
  },
  productImagePlaceholder: {
    width: 84,
    height: 84,
    borderRadius: 14,
    backgroundColor: "#F1F5F9",
    alignItems: "center",
    justifyContent: "center",
    marginRight: 14,
  },
  placeholderText: {
    fontSize: 11,
    color: "#94A3B8",
    fontWeight: "600",
  },
  productMeta: {
    flex: 1,
  },
  badgeRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginBottom: 6,
  },
  barcodeTag: {
    fontSize: 11.5,
    color: "#64748B",
    fontWeight: "600",
  },
  cacheTag: {
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 6,
  },
  cacheTagText: {
    fontSize: 10.5,
    fontWeight: "700",
  },
  productName: {
    fontSize: 18,
    fontWeight: "800",
    color: "#0F172A",
    lineHeight: 23,
  },
  productBrand: {
    fontSize: 13.5,
    color: "#64748B",
    fontWeight: "500",
    marginTop: 2,
  },
  scoreBanner: {
    flexDirection: "row",
    alignItems: "center",
    padding: 16,
    borderRadius: 18,
    borderWidth: 1.5,
    marginBottom: 12,
  },
  scoreCircle: {
    width: 78,
    height: 78,
    borderRadius: 39,
    borderWidth: 5,
    backgroundColor: "#FFFFFF",
    alignItems: "center",
    justifyContent: "center",
    marginRight: 16,
  },
  scoreNumber: {
    fontSize: 26,
    fontWeight: "900",
    lineHeight: 30,
  },
  scoreMaxText: {
    fontSize: 11,
    color: "#64748B",
    fontWeight: "700",
  },
  scoreGradeContainer: {
    flex: 1,
  },
  scoreGradeLabel: {
    fontSize: 11.5,
    color: "#475569",
    fontWeight: "600",
    textTransform: "uppercase",
    letterSpacing: 0.4,
  },
  gradePillRow: {
    flexDirection: "row",
    alignItems: "center",
    marginVertical: 3,
  },
  gradeDot: {
    width: 10,
    height: 10,
    borderRadius: 5,
    marginRight: 7,
  },
  scoreGradeValue: {
    fontSize: 22,
    fontWeight: "800",
  },
  scoreSubtext: {
    fontSize: 12,
    color: "#475569",
  },
  cardMethodologyBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    backgroundColor: "#F8FAFC",
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 12,
    paddingHorizontal: 13,
    paddingVertical: 10,
    marginBottom: 10,
  },
  cardMethodologyBtnText: {
    fontSize: 12.5,
    fontWeight: "700",
    color: "#0F172A",
  },
  cardMethodologyArrow: {
    fontSize: 18,
    fontWeight: "700",
    color: "#16A34A",
  },
  sectionBlock: {
    marginTop: 6,
    marginBottom: 14,
    paddingTop: 14,
    borderTopWidth: 1,
    borderTopColor: "#F1F5F9",
  },
  blockTitle: {
    fontSize: 14.5,
    fontWeight: "700",
    color: "#1E293B",
    marginBottom: 10,
  },
  progressRow: {
    marginBottom: 10,
  },
  progressHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
    marginBottom: 5,
  },
  progressLabel: {
    fontSize: 13,
    color: "#334155",
    fontWeight: "600",
  },
  progressValue: {
    fontSize: 13,
    color: "#0F172A",
    fontWeight: "700",
  },
  progressTrack: {
    height: 8,
    backgroundColor: "#F1F5F9",
    borderRadius: 4,
    overflow: "hidden",
  },
  progressFill: {
    height: "100%",
    borderRadius: 4,
  },
  nutritionGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    justifyContent: "space-between",
    rowGap: 8,
  },
  nutritionBox: {
    width: "31.5%",
    backgroundColor: "#F8FAFC",
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 12,
    paddingVertical: 10,
    paddingHorizontal: 8,
    alignItems: "center",
  },
  nutritionValue: {
    fontSize: 14,
    fontWeight: "800",
    color: "#0F172A",
  },
  nutritionLabel: {
    fontSize: 11.5,
    color: "#64748B",
    marginTop: 2,
    fontWeight: "500",
  },
  warningItem: {
    flexDirection: "row",
    alignItems: "flex-start",
    backgroundColor: "#FFFBEB",
    borderWidth: 1,
    borderColor: "#FDE68A",
    borderRadius: 12,
    padding: 11,
    marginBottom: 8,
  },
  warningBullet: {
    width: 7,
    height: 7,
    borderRadius: 4,
    backgroundColor: "#D97706",
    marginTop: 6,
    marginRight: 10,
  },
  warningText: {
    flex: 1,
    fontSize: 13,
    color: "#78350F",
    lineHeight: 18,
    fontWeight: "500",
  },
  cleanInfoBox: {
    backgroundColor: "#F0FDF4",
    borderWidth: 1,
    borderColor: "#BBF7D0",
    borderRadius: 12,
    padding: 12,
  },
  cleanInfoText: {
    fontSize: 13,
    color: "#166534",
    lineHeight: 18,
  },
  eCodeContainer: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  eCodeChip: {
    backgroundColor: "#F1F5F9",
    borderWidth: 1,
    borderColor: "#CBD5E1",
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 5,
  },
  eCodeText: {
    fontSize: 12.5,
    fontWeight: "700",
    color: "#334155",
  },
  disclaimerBox: {
    marginTop: 8,
    backgroundColor: "#F8FAFC",
    borderRadius: 12,
    padding: 12,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  disclaimerText: {
    fontSize: 11.5,
    color: "#64748B",
    lineHeight: 16,
    fontStyle: "italic",
    textAlign: "center",
  },
  recentSection: {
    marginTop: 4,
  },
  recentHeaderRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 10,
  },
  recentTitle: {
    fontSize: 17,
    fontWeight: "800",
    color: "#0F172A",
  },
  refreshBtnText: {
    fontSize: 13,
    fontWeight: "700",
    color: "#16A34A",
  },
  emptyRecentBox: {
    backgroundColor: "#FFFFFF",
    borderRadius: 16,
    padding: 18,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  emptyRecentText: {
    fontSize: 13,
    color: "#64748B",
    textAlign: "center",
    lineHeight: 19,
  },
  recentCard: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: "#FFFFFF",
    borderRadius: 16,
    padding: 12,
    marginBottom: 10,
    borderWidth: 1,
    borderColor: "#E2E8F0",
  },
  recentImage: {
    width: 46,
    height: 46,
    borderRadius: 10,
    backgroundColor: "#F8FAFC",
    marginRight: 12,
  },
  recentImagePlaceholder: {
    width: 46,
    height: 46,
    borderRadius: 10,
    backgroundColor: "#F1F5F9",
    alignItems: "center",
    justifyContent: "center",
    marginRight: 12,
  },
  recentPlaceholderText: {
    fontSize: 10,
    color: "#94A3B8",
    fontWeight: "600",
  },
  recentInfo: {
    flex: 1,
    marginRight: 10,
  },
  recentName: {
    fontSize: 14.5,
    fontWeight: "700",
    color: "#0F172A",
  },
  recentBrand: {
    fontSize: 12,
    color: "#64748B",
    marginTop: 2,
  },
  recentScorePill: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 12,
    borderWidth: 1,
  },
  recentScoreDot: {
    width: 7,
    height: 7,
    borderRadius: 4,
    marginRight: 6,
  },
  recentScoreText: {
    fontSize: 12.5,
    fontWeight: "800",
  },

  /* KAMERA TARAYICI TAM EKRAN STİLLERİ (3 DENGELİ BÖLGE) */
  scannerFullScreen: {
    flex: 1,
    backgroundColor: "#000000",
  },
  cameraRootContainer: {
    flex: 1,
    backgroundColor: "transparent",
  },
  cameraOverlayLayer: {
    flex: 1,
    justifyContent: "space-between",
    backgroundColor: "transparent",
  },
  permissionCenterBox: {
    flex: 1,
    backgroundColor: "#0F172A",
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 28,
  },
  permissionIcon: {
    fontSize: 48,
    marginBottom: 14,
  },
  permissionTitle: {
    fontSize: 20,
    fontWeight: "800",
    color: "#FFFFFF",
    marginBottom: 8,
    textAlign: "center",
  },
  permissionSubtext: {
    fontSize: 14,
    color: "#CBD5E1",
    textAlign: "center",
    lineHeight: 20,
    marginBottom: 24,
  },
  permissionGrantBtn: {
    backgroundColor: "#16A34A",
    paddingHorizontal: 24,
    paddingVertical: 14,
    borderRadius: 14,
    width: "100%",
    alignItems: "center",
    marginBottom: 12,
  },
  permissionGrantBtnText: {
    color: "#FFFFFF",
    fontSize: 16,
    fontWeight: "700",
  },
  permissionCancelBtn: {
    paddingVertical: 12,
  },
  permissionCancelBtnText: {
    color: "#94A3B8",
    fontSize: 14,
    fontWeight: "600",
  },
  cameraTopBar: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingTop: Platform.OS === "android" ? 48 : 58,
    paddingHorizontal: 20,
    paddingBottom: 12,
    backgroundColor: "transparent",
  },
  cameraControlBtn: {
    backgroundColor: "rgba(15, 23, 42, 0.78)",
    paddingHorizontal: 18,
    paddingVertical: 11,
    borderRadius: 22,
    borderWidth: 1,
    borderColor: "rgba(255, 255, 255, 0.25)",
  },
  cameraTorchActiveBtn: {
    backgroundColor: "#FACC15",
    borderColor: "#EAB308",
  },
  cameraControlBtnText: {
    color: "#FFFFFF",
    fontSize: 14,
    fontWeight: "700",
  },
  cameraTorchActiveText: {
    color: "#0F172A",
  },
  cameraErrorBanner: {
    marginHorizontal: 20,
    marginBottom: 16,
    backgroundColor: "rgba(220, 38, 38, 0.9)",
    paddingVertical: 10,
    paddingHorizontal: 14,
    borderRadius: 12,
  },
  cameraErrorBannerText: {
    color: "#FFFFFF",
    fontSize: 13,
    fontWeight: "600",
    textAlign: "center",
  },
  viewfinderWrapper: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "transparent",
  },
  viewfinderBox: {
    width: 270,
    height: 180,
    borderRadius: 20,
    backgroundColor: "transparent",
    justifyContent: "center",
    alignItems: "center",
    position: "relative",
  },
  corner: {
    position: "absolute",
    width: 36,
    height: 36,
    borderColor: "#22C55E",
  },
  cornerTopLeft: {
    top: 0,
    left: 0,
    borderTopWidth: 4,
    borderLeftWidth: 4,
    borderTopLeftRadius: 16,
  },
  cornerTopRight: {
    top: 0,
    right: 0,
    borderTopWidth: 4,
    borderRightWidth: 4,
    borderTopRightRadius: 16,
  },
  cornerBottomLeft: {
    bottom: 0,
    left: 0,
    borderBottomWidth: 4,
    borderLeftWidth: 4,
    borderBottomLeftRadius: 16,
  },
  cornerBottomRight: {
    bottom: 0,
    right: 0,
    borderBottomWidth: 4,
    borderRightWidth: 4,
    borderBottomRightRadius: 16,
  },
  scanLaserLine: {
    width: "84%",
    height: 2,
    backgroundColor: "#22C55E",
    opacity: 0.85,
  },
  cameraBottomBar: {
    paddingHorizontal: 24,
    paddingTop: 12,
    paddingBottom: Platform.OS === "android" ? 44 : 52,
    backgroundColor: "transparent",
  },
  cameraHintCard: {
    backgroundColor: "rgba(15, 23, 42, 0.82)",
    borderRadius: 18,
    paddingVertical: 15,
    paddingHorizontal: 16,
    borderWidth: 1,
    borderColor: "rgba(255, 255, 255, 0.15)",
    alignItems: "center",
  },
  cameraHintTitle: {
    color: "#FFFFFF",
    fontSize: 15.5,
    fontWeight: "800",
    marginBottom: 4,
  },
  cameraHintSubtitle: {
    color: "#CBD5E1",
    fontSize: 12.5,
    textAlign: "center",
    lineHeight: 18,
  },

  /* METODOLOJİ MODAL STİLLERİ */
  modalOverlay: {
    flex: 1,
    backgroundColor: "rgba(15, 23, 42, 0.55)",
    justifyContent: "flex-end",
  },
  modalContainer: {
    backgroundColor: "#FFFFFF",
    borderTopLeftRadius: 24,
    borderTopRightRadius: 24,
    maxHeight: "88%",
    paddingTop: 18,
    paddingHorizontal: 18,
    paddingBottom: 24,
  },
  modalHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingBottom: 12,
    borderBottomWidth: 1,
    borderBottomColor: "#E2E8F0",
  },
  modalTitle: {
    flex: 1,
    fontSize: 17,
    fontWeight: "800",
    color: "#0F172A",
    paddingRight: 10,
  },
  modalCloseBtn: {
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: "#F1F5F9",
    alignItems: "center",
    justifyContent: "center",
  },
  modalCloseBtnText: {
    fontSize: 15,
    fontWeight: "700",
    color: "#475569",
  },
  modalScroll: {
    marginTop: 12,
  },
  modalScrollContent: {
    paddingBottom: 16,
  },
  modalLoadingRow: {
    flexDirection: "row",
    alignItems: "center",
    marginBottom: 10,
    gap: 8,
  },
  modalLoadingText: {
    fontSize: 12,
    color: "#64748B",
    marginLeft: 6,
  },
  modalSummary: {
    fontSize: 13.5,
    color: "#334155",
    lineHeight: 20,
    marginBottom: 14,
  },
  modalSectionTitle: {
    fontSize: 15,
    fontWeight: "800",
    color: "#0F172A",
    marginTop: 6,
    marginBottom: 10,
  },
  weightCard: {
    backgroundColor: "#F8FAFC",
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 14,
    padding: 12,
    marginBottom: 8,
  },
  weightHeaderRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 5,
  },
  weightCategory: {
    fontSize: 14,
    fontWeight: "700",
    color: "#0F172A",
  },
  weightBadge: {
    backgroundColor: "#DCFCE7",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 8,
  },
  weightBadgeText: {
    fontSize: 11.5,
    fontWeight: "800",
    color: "#15803D",
  },
  weightDesc: {
    fontSize: 12.5,
    color: "#475569",
    lineHeight: 18,
  },
  rulesBox: {
    backgroundColor: "#F0F9FF",
    borderWidth: 1,
    borderColor: "#BAE6FD",
    borderRadius: 14,
    padding: 12,
    marginTop: 4,
    marginBottom: 14,
  },
  rulesTitle: {
    fontSize: 13,
    fontWeight: "700",
    color: "#0369A1",
    marginBottom: 6,
  },
  ruleItemText: {
    fontSize: 12.5,
    color: "#0C4A6E",
    lineHeight: 18,
    marginBottom: 4,
  },
  referenceCard: {
    backgroundColor: "#FFFFFF",
    borderWidth: 1,
    borderColor: "#E2E8F0",
    borderRadius: 14,
    padding: 12,
    marginBottom: 8,
  },
  refInstitution: {
    fontSize: 13.5,
    fontWeight: "800",
    color: "#16A34A",
    marginBottom: 2,
  },
  refDocument: {
    fontSize: 13,
    fontWeight: "700",
    color: "#0F172A",
    marginBottom: 4,
  },
  refScope: {
    fontSize: 12,
    color: "#475569",
    lineHeight: 17,
  },
  legalNoticeBox: {
    backgroundColor: "#F8FAFC",
    borderWidth: 1,
    borderColor: "#CBD5E1",
    borderRadius: 14,
    padding: 13,
    marginBottom: 8,
  },
  legalNoticeText: {
    fontSize: 12,
    color: "#475569",
    lineHeight: 18,
  },
  modalDoneButton: {
    backgroundColor: "#16A34A",
    borderRadius: 14,
    paddingVertical: 13,
    alignItems: "center",
    marginTop: 8,
  },
  modalDoneButtonText: {
    color: "#FFFFFF",
    fontSize: 15,
    fontWeight: "700",
  },
});
