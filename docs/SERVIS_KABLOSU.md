# Servis kablosu – dizüstüyle yerinde güncelleme

NSystem'in dizüstü bilgisayarı ağ kablosuyla kutunun **servis girişine** takılır; «N-Fizyo Servis» programı kutunun
durumunu gösterir, imzalı yeni sürümü yükler, güncellemeyi başlatır ve izler. İnternet ve merkez panel gerekmez.
Panelden uzaktan güncelleme ayrıca aynen kalır (`docs/MERKEZ.md` › «Sürümler»).

## Parçalar

| Nerede | Dosya | Ne yapar |
|---|---|---|
| Kutu | `nfizyo/agent/service.py` | Servis dinleyicisi (root, `n-fizyo-service`) – sadece `169.254.77.1:8780` |
| Kutu + dizüstü | `nfizyo/agent/servicelink.py` | Ortak kurallar: imza, tek kullanımlık kod, sınırlar |
| Kutu | `deploy/service-port-setup.sh` | Servis girişini, güvenlik duvarını ve dinleyiciyi kurar (bir kez) |
| Dizüstü | `tools/servis/n_fizyo_servis.py` | Windows programı (pencere, Türkçe) |
| Dizüstü | `tools/servis/servis_client.py` | Bağlantı (pencere olmadan; testler de bunu kullanır) |
| Dizüstü | `tools/servis/build_windows.bat` | Tek dosya `N-Fizyo-Servis.exe` üretir (PyInstaller) |

## Donanım

- Kutuda **ikinci bir ağ girişi**: anakartta ikinci giriş yoksa bir USB 3 – Gigabit Ethernet adaptörü. Muayenehane ağına bağlı giriş
  asla servis girişi olarak kullanılmaz (kurulum betiği reddeder).
- Normal bir ağ kablosu (Cat 5e/6). Dizüstü ile kutu arasında başka cihaz yok.

## Bir kez: dizüstünde

1. Python 3.12+ kur (python.org, «Add to PATH»).
2. Projede `tools\servis\build_windows.bat` → `dist\N-Fizyo-Servis.exe`.
3. Programı aç › **Anahtar › Anahtar oluştur**. Özel anahtar `%APPDATA%\NSystem\n-fizyo-service-key.pem` altında kalır
   (proje klasörüne konamaz). Açık anahtar panoya kopyalanır – gizli değildir.
4. Özel anahtarın çevrimdışı bir yedeğini al (USB bellek, kasada). Kaybolursa yeni anahtar + her kutuda kurulumu tekrar.

## Bir kez: her kutuda

```bash
sudo bash deploy/service-port-setup.sh --list                       # kartlar; «← Praxisnetz» olanı KULLANMA
sudo bash deploy/service-port-setup.sh --interface enx00e04c680001 --pubkey <açık anahtar>
```

Kurulan: kartta sabit `169.254.77.1/16` (DHCP, ağ geçidi, DNS yok) · güvenlik duvarında bu kartta sadece 8780 açık, SSH/web/YZ dahil
gerisi kapalı, yönlendirme yok · `NFIZYO_SERVICE_PUBKEY` ve `NFIZYO_SERVICE_IFACE` `.env`'e · `n-fizyo-service` servisi.
Kapatmak: `sudo bash deploy/service-port-setup.sh --off`.

## Her güncellemede

1. Senin bilgisayarında paketi yap: `python3 tools/release.py build` → `dist/` altında `.tar.gz` + `.manifest.json`.
   Bu iki dosyayı dizüstünde aynı klasöre koy.
2. Kabloyu tak, programda **Bağlan**. Windows adresini (169.254.x.y) kendisi 20–60 saniyede alır; ayar yok.
3. Durumu kontrol et: sürüm, program çalışıyor mu, son yedek, disk.
4. **1. Paket seç** (`…manifest.json`) › **2. Kutuya yükle ve kontrol ettir**. Kutu kontrol eder: manifest NSystem release
   anahtarıyla imzalı mı, sağlama toplamı tutuyor mu, kurulu sürümden yeni mi. Program değişen modülleri, veritabanı
   değişikliğini ve yenilikleri gösterir. Bu adım kutuda henüz hiçbir şeyi değiştirmez.
5. **3. Güncellemeyi başlat** (onay sorar). Kutu `update.sh` ile panel güncellemesiyle aynı yolu izler: yedek, geçiş,
   migration, kontrol; hata olursa kendiliğinden eski sürüme döner. Program akışı her 3 saniyede gösterir.
6. Bitince 4 ekrana kısa giriş yap. Gerekirse **Eski sürüme dön…** – güncellemeden sonra girilen veri kaybolur, program bunu sorar.

## Güvenlik

- Dinleyici sadece link-local adreste (`169.254.0.0/16`); başka adres verilirse başlamaz. Güvenlik duvarı da sadece bu portu açar.
- `/hello` dışında her istek dizüstünün servis anahtarıyla imzalı (Ed25519). İmza şunları kapsar: işlem, kutunun verdiği
  **tek kullanımlık kod** (5 dk geçerli, bir kez), içeriğin sağlama toplamı. Kopyalanan istek ikinci kez çalışmaz; dizüstünün saati önemli değil.
- Kutuda `NFIZYO_SERVICE_PUBKEY` yoksa her şey kapalı.
- Sabit liste: `status · upload · update · progress · rollback · backup`. Dosya okuma, veritabanı, kabuk yok. Hasta verisi hiç gitmez.
- Yeni program iki anahtarla korunur: istek **servis anahtarıyla**, paket **release anahtarıyla** imzalı olmalı. Biri çalınsa bile
  tek başına sahte program kurulamaz. Eski sürüm paketi kabul edilmez (geri dönüş sadece «Eski sürüme dön»).
- Her işlem ve her reddedilen istek muayenehanenin **Protokoll NSystem** listesine düşer (Einstellungen › NSystem): «Servicekabel vor Ort».
- Kablo üzerindeki trafik şifresiz HTTP'dir: içerik imzalı ve gizli bilgi taşımaz (sürüm, servis durumu, yedek yaşı).

## Sınırlar

- Aynı anda tek görev (güncelleme, geri dönüş veya yedek).
- Paket en fazla 400 MB.
- Güncelleme sırasında program yaklaşık 1 dakika kapalı – mesai dışında.
- Yapay zekâ modelleri (Whisper, Qwen, TranslateGemma) pakette değil; kablo onları güncellemez.
- Windows programı bu ortamda pencere olarak çalıştırılamadı (tkinter yok); bağlantı kısmı testli, pencere ilk gerçek denemede kontrol edilmeli.
