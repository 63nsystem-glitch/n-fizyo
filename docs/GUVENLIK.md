# Güvenlik

Bu belgedeki kontroller **otomatik** ve kod incelemesiyle yapıldı; dışarıdan bir uzmanın yapacağı sızma testinin (pentest) yerini tutmaz.

## 0.9.0 – düzeltilenler (Kasım 2026)

Denetimde bulunan güvenlik sorunları ve çözümleri. Testler: `tests/test_security_090.py`.

| Sorun | Ne yapıldı |
|---|---|
| **Deneme sınırları sadece proxy'yi görüyordu.** Caddy önde olduğu için `request.remote` hep 127.0.0.1'di: birinin 8 yanlış hasta girişi, bütün muayenehanelerin hasta girişini 15 dakika kilitliyordu. | Tek yardımcı: `core/web.py` › `client_ip()`. `X-Forwarded-For` **sadece** bağlantı güvenilir bir proxy'den geliyorsa okunur (sağdan, proxy'nin kendi eklediği adres). Ayar: `NFIZYO_TRUSTED_PROXIES` (varsayılan `127.0.0.1,::1`). Başka biri bu başlığı gönderirse yok sayılır. Kullanıldığı yerler: personel girişi, hasta uygulaması girişi, online randevu, video linki, boşalan randevu linki, merkez panel girişi. |
| Hesap başına sınır yoktu (veya IP'ye bağlıydı). | Personel: e-posta başına, adresten bağımsız **artan bekleme** (1 saatte 5 yanlış denemeden sonra 30 sn, 1, 2, 4, 8 dk … en çok 15 dk). Kişinin daha önce başarıyla girdiği tarayıcıda (imzalı `nf_dev` çerezi) bu bekleme uygulanmaz → kimse bir hesabı dışarıdan kilitleyemez. Ayrıca adres+e-posta (15 dk'da 8) ve adres (15 dk'da 30). 2. adım kodu: hesap başına 15 dk'da 8. Hasta: kişi başına saatte 10 yanlış SMS kodu, kod başına 5 deneme. |
| Deneme kaydı bellekte büyüyordu (hiç temizlenmiyordu). | `LoginThrottle` dakikada bir eski kayıtları siler (en geç 1 gün), anahtar başına en çok 200 kayıt. |
| **SMS seli:** telefon + doğum tarihi doğruysa her istek SMS gönderiyordu (40 istek = 40 SMS, muayenehanenin parası). | Hasta girişi: numara başına ve hasta başına **saatte 3 kod**, adres başına saatte 10; sonra nazik bir mesaj (11 dil). Numara sayacı hasta olsun olmasın her isteği sayar → mesaj kimin hasta olduğunu ele vermez. Hasta sayacı veritabanından (yeniden başlatmada da geçerli). **0.9.0 QA SEC-04:** IPv6 adresleri /64 ağına göre sayılır; muayenehane başına girişsiz SMS (online randevu kodu + hasta giriş kodu) saatte 30 / günde 100 üst sınırı (`core/sms.py`). |
| Online randevu formu her ülkeye kod SMS'i gönderiyordu. | Sadece **İsviçre ve Lihtenştayn cep numaraları** (+41 74–79, +423 6/7). Numara başına saatte 3, adres başına saatte 6 kod. |
| **Formül enjeksiyonu:** camt.054'teki ödeyen adı veya randevu formundaki ad `=…` ile başlarsa `buchhaltung.xlsx`'te çalışan formül oluyordu. | `core/spreadsheet.py` › `safe_cell()`: `= + - @`, TAB, CR, LF ile başlayan metnin önüne `'`. Gerçek sayılar ve `-12.50` gibi sayı metinleri değişmez. Kullanıldığı yerler: yevmiye CSV ve Excel (`exports/journal.py`), tam veri dışa aktarma (`core/export.py`), içe aktarma uyarı listesi (`dataimport`). |
| **Bağımlılıklar:** sadece `>=`; kurulu pypdf 6.17.0 bilinen DoS açıkları içeriyordu; `update.sh` hiç yükseltmiyordu. | `pypdf>=6.21.4`. Yeni **`requirements.lock`** (pip-compile, Python 3.12 = Ubuntu 24.04, her paket için sha256). `install.sh`, `update.sh`, `install-central.sh`, `install-ai-server.sh` sadece `pip install --require-hashes -r requirements.lock` kullanır → güncellemede yeni sürümler gerçekten kurulur. Kilit dosyası olmayan eski sürüme geri dönüşte eski yol. |
| Dışarıdan gelen PDF (HIN, tarayıcı, yükleme) uygulama sürecinde okunuyordu. | pypdf artık **ayrı kısa bir süreçte**: 60 sn süre, 45 sn işlemci, 1 GiB bellek sınırı (`ai/ocr.py`). Bozuk/zararlı PDF işi durdurmaz; metin boş kalır. |
| **SMS kodları düz metin:** giden kutusu (`outbox`) giriş kodlarını, davet linki + kodunu (14 gün geçerli) saklıyordu ve tam dışa aktarmadaydı. | `send(…, secrets=[…])`: `outbox.body` her zaman maskeli (`••••••`), tam metin sadece gönderilene kadar `outbox.body_secret`'te; gönderilince veya kesin hata olunca silinir. Maskelenenler: hasta giriş kodu, randevu kodu, davet linki + kodu, video linki, boşalan randevu linki. `body_secret` dışa aktarmada yok. Eski kayıtlardaki linkler göç 008'de silindi (eski giriş kodları zaten 15 dk'da geçersiz). Test modunda (`NFIZYO_SMS=log`) demo dışında hiç düz metin saklanmaz. |
| Şifre özeti zayıftı (scrypt N=2^14), bilinmeyen e-posta daha hızlı cevap veriyordu. | scrypt **N=2^17** (128 MiB, ~0,4 sn), `maxmem` buna göre; aynı anda en çok 2 hesaplama (bellek). Biçim `scrypt$ln=17,r=8,p=1$…`. Eski özetler geçerli, **sonraki başarılı girişte sessizce yenilenir**. Bilinmeyen/pasif e-postada sahte bir kontrol yapılır → süre hangi hesabın var olduğunu ele vermez. Kontrol arka plan iş parçacığında (sunucu donmaz). |
| Personel oturumu 14 gün, hareketsizlik sınırı yok. | **Kayan hareketsizlik sınırı:** varsayılan 8 saat (`NFIZYO_STAFF_IDLE_HOURS`); 14 günlük üst sınır kalır. `sessions.last_seen_at` dakikada en çok bir kez yazılır. Süresi dolan oturum silinir. |

Testlerle korunanlar (yeni): uygulama kodu (TOTP) ile giriş ve aynı kodun ikinci kez kabul edilmemesi, `/login/code` sınırı, yanlış hasta SMS kodları ve SMS sınırları, kullanıcı pasif yapılınca / şifre değişince / hasta uygulama erişimini bitirince oturumların kapanması, hareketsizlik ve üst sınır, `client_ip` (güvenilir proxy ↔ sahte başlık), CSV ve Excel'de formül etkisizleştirme, PDF süreç sınırları, kilit dosyası.

Not: testler hız için scrypt'i N=2^14 ile çalıştırır (`tests/conftest.py`); gerçek değer (2^17) ayrı testte kontrol edilir.

**Yedekler (0.9.0):** kutudaki yedekler sadece root (`/var/backups/n-fizyo`, 0700). İsteğe bağlı dış kopya kutuda şifrelenir
(GnuPG, AES-256, bütünlük kontrollü); parola dosyası sadece root (`chmod 600` değilse kopya yapılmaz), sağlayıcıya ve NSystem'e
gitmez. Panele sadece durum/saat/boyut gider. Ayrıntı ve sağlayıcı seçimi: `docs/YEDEK.md`.

## 0.9.0 – NSystem işletimi (sürüm, destek erişimi, talepler, duyurular, panel yedeği)

Ayrıntı: `docs/MERKEZ.md`. Testler: `tests/test_nsystem_ops.py`, `tests/test_panel_backup.py`, `tests/test_central.py`,
`tests/test_tenant_isolation.py`.

| Konu | Koruma |
|---|---|
| Yeni sürüm | İki ayrı anahtar: panel komutu imzalar (Ed25519 + 2FA), **release anahtarı** (sadece NSystem'in bilgisayarında, projenin dışında) manifesti imzalar. Kutu: manifest imzası, dosya adı, SHA-256 (komut = manifest = dosya), paketteki sürüm; sadece https. Panel ele geçirilse bile imzasız paket kurulamaz. **Eski imzalı sürüme dönüş yok:** sadece kurulu ve şimdiye kadarki en yüksek sürümden yenisi; geri dönüş sadece `rollback`. |
| Runner (root) ↔ uygulama klasörleri | Runner, uygulamanın yazabildiği `runner/` klasörlerinde sembolik bağlara uymaz, yol parçası olarak sadece doğrulanmış kimlikler, chown sadece kendi açtığı dosyaya (`agent/safefs.py`). Uygulama ele geçirilse bile runner üzerinden root olunamaz (0.9.0 QA SEC-01). |
| Merkez panel başlıkları | CSP (`script-src 'self'`, `frame-ancestors 'none'`, `form-action 'self'`), HSTS (https'te), nosniff, Referrer-Policy, Permissions-Policy – her yanıtta (hatalar ve statik dosyalar dahil). |
| Bulut YZ (sadece demo) | `cloud_demo` her **istekten önce** veritabanında sadece demo muayenehane olduğunu kontrol eder; işletme sırasında gerçek muayenehane eklenirse anında durur. `create-practice` `cloud_demo` ile reddedilir. |
| Destek erişimi | **Sadece Leitung açar** (1–8 h); NSystem açamaz (komut yok), sadece kapatabilir. Uygulama → runner işi HMAC'li, 10 dk, tek sefer. Tünel sadece dışarıya, tünelden sadece hub ve sadece port 22; SSH anahtarı sadece açıkken, `from=hub`, `restrict` (pty yok), zorunlu araç (+ sshd `ForceCommand`); sshd hub dışında reddeder; şifre kilitli; giriş kabuğu = araç (bash yok); araç Readline kullanmaz (Ctrl-X Ctrl-E ile editör → kabuk kaçışı kapalı, 0.9.0 QA); cron/at yasak, kapanınca silinir. Reddedilen her giriş de kayıtta. Süre dolunca kapanır (runner + systemd zamanlayıcısı), açık oturumlar sonlandırılır. Her açma/kapama/bağlantı/komut kayıtta; panele isimsiz. |
| Açıkken | NSystem kutuya **teknik olarak erişebilir** (teknik günlükler, servis durumu, yeniden başlatma). Bu yüzden açık onay ve AVV'de kural. Veritabanı, dokümanlar, `.env` okunamaz (araçta komut yok). |
| Destek talebi | Hasta verisi kalıpları ve muayenehanenin hasta adları/doğum tarihleri/numaraları → gönderim engellenir, metin hiç kaydedilmez. Panel ikinci kez kontrol eder ve tutarsa metni saklamaz; rapor kaydında talep metni yok; bildirimde metin yok. NSystem cevabında da kalıp kontrolü. |
| Duyurular ve cevaplar | Panel anahtarıyla imzalı (kutuya özel, tür «notice»/«support_reply» – komut olarak geçmez). İmzasız/yabancı imzalı → atılır. Sadece personel görür. |
| Panel yedeği | Veritabanı + anahtarlar her gece, dışarıya GnuPG AES-256 şifreli (kutu yedeğiyle aynı yöntem); parola sadece root'ta ve kâğıtta. |

## Sürüm öncesi kontrol (her sürümde)

```bash
cd n-fizyo                                # proje klasörü
bash tools/deps.sh audit                 # pip-audit: requirements.lock'taki sürümlerde bilinen açık var mı? (varsa durur)
python3 -m pytest -q                     # tüm testler
ruff check --select F,E9 nfizyo tests tools   # kod kontrolü
```

Kod kendi (özel) GitHub deposuna geçince aynı üçü her push'ta ve haftada bir otomatik çalışır: `.github/workflows/tests.yml`
(Python 3.12 ve 3.13, paketler `requirements.lock`'tan sha256 kontrolüyle; secret gerekmez).

Paket güncellemek için: `requirements.txt` değiştir → `bash tools/deps.sh lock` (Python 3.12 ile, pip-tools) → `audit` → testler.
GPU kutularındaki yapay zekâ paketleri (`requirements-ai.txt`) kilitli değil; ayrıca `pip-audit -r requirements-ai.txt`.

## Açık kalanlar

- Bağımsız **pentest** (özellikle: hasta uygulaması girişi, herkese açık linkler `/p/angebot`, `/p/video`, `/p/termin`, dosya yükleme,
  yeni sınırların gerçek Caddy arkasında davranışı).
- Canlı sunucuda **TLS ayarı** ve güvenlik duvarı kontrolü (ör. SSL Labs, `nmap`); Caddy'nin `X-Forwarded-For` başlığını
  gerçekten kendisi yazdığının kontrolü (önünde başka proxy varsa `NFIZYO_TRUSTED_PROXIES`).
- Hukuki taslakların kontrolü: `docs/HUKUK.md` (hukukçu).
- Dış yedek için sağlayıcı (İsviçre, sözleşme, silme kilidi) seçilmedi – sahibin kararı; o zamana kadar yedek sadece kutuda.
- Destek erişimi: AVV'ye madde (ne zaman, ne için, kim, kayıtlar) – hukukçu; hub sunucusunun sıkılaştırılması (sadece anahtarla SSH,
  2FA/donanım anahtarı) ve destek yolunun pentesti; `DenyUsers …@*,!hub` kuralının gerçek kutuda denenmesi
  (`ssh nfizyo-support@<kutu-LAN-adresi>` reddedilmeli).
- Hasta verisi kontrolü «belirgin» verileri yakalar (ad soyad, tarih, numara, e-posta); serbest anlatım («68 yaşındaki diz hastası»)
  yakalanmaz – formdaki uyarı bu yüzden var.
- Sınırlar bellekte tutulur: servis yeniden başlayınca sıfırlanır (hasta başına saatte 3 kod sınırı hariç – o veritabanında).
  Birden fazla uygulama süreci çalıştırılırsa sınırlar süreç başına olur (şu an tek süreç).
- Hasta SMS sınırı (numara başına saatte 3) kasıtlı olarak var olup olmadığına bakmadan sayar; biri bir hastanın numarasıyla
  bu sınırı doldurursa hasta en çok 1 saat kod alamaz (muayenehane yeni QR davet gönderebilir).
- Eski (0.8.0) şifre özetine sahip hesaplar ilk girişe kadar biraz daha hızlı cevap verir (özet o girişte yenilenir).

## Önceki kontrol – sürüm 0.7.0

| Kontrol | Araç | Sonuç |
|---|---|---|
| Python kodu | Bandit 1.9.4 (`bandit -r nfizyo -ll`) | 1 yüksek: statik dosya sürümü için SHA1 → güvenlik amaçlı değil, `usedforsecurity=False` ile işaretlendi. 36 orta «SQL metinle kuruluyor»: hepsi elle kontrol edildi – sütun/tablo adları sabit listelerden geliyor, kullanıcı girdisi her zaman `?` parametresi. 2 orta «Markup»: girdiler önce `escape` ediliyor. |
| Bağımlılıklar | pip-audit (`requirements.txt`) | o zaman bilinen açık yok (0.9.0'dan beri: `tools/deps.sh audit`, kilit dosyası) |
| HTTP başlıkları | test + tarayıcı taraması | **Content-Security-Policy** (sadece kendi script dosyalarımız, inline JavaScript yok, dış kaynak yok, iframe içine alınamaz), **Permissions-Policy**, https'te **HSTS**, nosniff, DENY, same-origin. |
| Inline JavaScript | tüm şablonlar | statik dosyalara taşındı; `tests/test_security_headers.py` yenisinin eklenmesini engeller. |
| Tarayıcı taraması | Playwright, ~430 sayfa | CSP ihlali ve JS hatası yok. |
| Pratikler arası ayrım | `tests/test_tenant_isolation.py` | her sürümde genişletiliyor. |

## Hasta uygulaması: telefondaki çevrimdışı kopyalar (0.9.0)

Uygulama ana sayfayı ve egzersizleri çevrimdışı için telefonda saklar. Çıkış ve «Zugang beenden» bunları siler
(`Clear-Site-Data: "cache", "storage"`). Muayenehane «App abmelden» yaptığında veya oturum bittiğinde, telefon bir sonraki
açılışta sunucudan girişe yönlendirilir ve servis çalışanı (`patientapp/sw.js`) saklı sayfaları siler.

## Personel cihazları: paylaşılan tablet ve yarım yüklemeler (0.9.0, 4. tur)

- Diktat kayıtları cihazda (IndexedDB) onu kaydeden kişiye bağlıdır. Tablet başkasına geçerse kayıt asla o kişinin hesabına
  yüklenmez («andere Person»); sunucu da farklı kullanıcıyı reddeder (409). Sahibi tekrar girince, herhangi bir terapi
  sayfasını açması yeter – yükleme kendiliğinden başlar.
- Çıkıştan sonra giriş sayfası diktat sayfasının çevrimdışı kopyasını (günün hasta adları) cihazdan siler.
- Bağlantı yüklemenin ortasında koparsa yarım ses dosyası hemen silinir; sunucu yeniden başlarken kalan parçaları
  düzenli temizlik 6 saat sonra siler. Hasta sesli anamnezinde de (intake) aynı kural.
