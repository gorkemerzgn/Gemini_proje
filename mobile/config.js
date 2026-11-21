/**
 * İrem - Mobil Uygulama Sunucu Yapılandırması
 *
 * Expo Go / Metro geliştirme sunucusunun çalıştığı güncel Wi-Fi IP adresini
 * `expo-constants` üzerinden otomatik algılar ve backend (:8001) portuna bağlar.
 * Otomatik algılanamazsa yedek (fallback) olarak `http://192.168.1.102:8001` kullanılır.
 */

import Constants from "expo-constants";

const FALLBACK_API_BASE_URL = "http://192.168.1.102:8001";

function resolveApiBaseUrl() {
  try {
    const rawHostUri =
      Constants.expoConfig?.hostUri ||
      Constants.manifest2?.extra?.expoGo?.debuggerHost ||
      Constants.expoGoConfig?.debuggerHost ||
      Constants.manifest?.debuggerHost;

    if (typeof rawHostUri === "string" && rawHostUri.trim().length > 0) {
      const cleanedHost = rawHostUri
        .trim()
        .replace(/^[a-zA-Z]+:\/\//, "")
        .split("/")[0]
        .split(":")[0];

      if (cleanedHost) {
        return `http://${cleanedHost}:8001`;
      }
    }
  } catch (_err) {
    // Otomatik IP algılama başarısız olursa yedek adrese düş
  }

  return FALLBACK_API_BASE_URL;
}

export const API_BASE_URL = resolveApiBaseUrl();
