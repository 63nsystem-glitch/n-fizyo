# Bağlantı testleri (YZ sunucusu, SMS, e-posta, HIN, MediData, Video, bexio)

Amaç: pilot başlamadan önce her dış bağlantının **gerçek hesapla** çalıştığını görmek.
Tüm erişim bilgileri sadece sunucudaki `/etc/n-fizyo/n-fizyo.env` dosyasına girer (sohbete, e-postaya asla).
Ayarların ayrıntısı: `docs/BAGLANTILAR.md`, video: `docs/VIDEO.md`.

## Tek komut

```bash
cd /opt/n-fizyo/app && sudo -u nfizyo /opt/n-fizyo/venv/bin/python -m nfizyo --env /etc/n-fizyo/n-fizyo.env check-connections
```

Ek parametre vermezsen hiçbir şey gönderilmez. Komut sadece okur ve oturum açar.
Çıktı her satırda bir işaret gösterir:

- ✓ çalışıyor
- ✗ hata
- – kurulmamış
- ! dikkat

Hata varsa çıkış kodu 1 olur. Şifreler ve anahtarlar çıktıda asla görünmez.

Gerçekten göndermek için (her biri ayrı, istersen hepsi birden):

```bash
… check-connections --sms 079XXXXXXX           # kendi numarana 1 test SMS (birkaç Rappen)
… check-connections --mail sen@ornek.ch        # 1 test e-postası
… check-connections --hin muayenehane@hin.ch   # HIN üzerinden 1 test mesajı (kendi HIN adresine)
… check-connections --praxis physio-balance    # bexio OAuth ise: hangi muayenehane
```

## Sıra ve açman gereken hesaplar

| # | Bağlantı | Açılacak hesap | .env anahtarları | Test |
|---|---|---|---|---|
| 1 | Adres (https) | Alan adı + sertifika (install.sh Caddy ile alır) | `NFIZYO_BASE_URL=https://…` | çıktıda «Adresse ✓» |
| 2 | E-posta | Muayenehanenin veya NSystem'in SMTP hesabı | `NFIZYO_MAIL=live`, `NFIZYO_SMTP_*` | önce okuma testi, sonra `--mail` |
| 3 | SMS | ASPSMS (veya eCall) iş hesabı | `NFIZYO_SMS=live`, `NFIZYO_ASPSMS_USERKEY/_PASSWORD` | ASPSMS: kalan kredi görünür; sonra `--sms` |
| 4 | HIN | Muayenehanenin HIN üyeliği + HIN Client kutuda | `NFIZYO_HIN_FROM`, `_PRACTICE`, `_USER`, `_PASSWORD`, `_TLS` | SMTP/IMAP ✓; sonra `--hin` kendi adresine |
| 5 | MediData | NSystem partner kaydı + muayenehanenin sözleşmesi, erişim müşteri portalından | `NFIZYO_MEDIDATA=test`, `_URL`, `_CLIENT_ID`, `_USER`, `_PASSWORD`, `_CERT`, `_KEY` (gerekirse `_CA`, `_PRACTICE`) | «MediData ✓ Zugang gültig», «MediData Zertifikat ✓», sonra simülatöre test faturası (aşağıda) |
| 6 | Video | Kendi Jitsi sunucumuz (`deploy/jitsi/install-jitsi.sh`) | `NFIZYO_VIDEO_URL`, `_SECRET` | 6 satır ✓; sonra kendi telefonunla bir deneme seansı |
| 7 | bexio | bexio hesabı (muayenehanenin), geliştirici erişimi | `NFIZYO_BEXIO_TOKEN` veya `_CLIENT_ID/_SECRET` | «Konten gelesen»; sonra 1 ayı gönder |
| 8 | YZ sunucusu (yol 2) | NSystem'in GPU sunucusu (`install-ai-server.sh`), muayenehane başına `add-client` | `NFIZYO_AI_MODE=remote`, `NFIZYO_AI_SERVER_URL`, `NFIZYO_AI_TOKEN` | «KI-Server ✓ erreichbar, Zugang gültig» + 3 yapı taşı ✓; sonra `--ki` (1 sn sessizlik + «Guten Tag» çevirisi, hasta verisi yok) |

Her değişiklikten sonra: `sudo systemctl restart n-fizyo`, sonra komutu tekrar çalıştır.

Dışarıdaki şifreli yedek bu komutta değil (root ister): `sudo n-fizyo-backup --offsite-test` – kurulum `docs/YEDEK.md`.

## Adım adım

### 1. E-posta

1. SMTP bilgilerini al (sunucu, port, kullanıcı, şifre, gönderen adresi).
2. `.env` dosyasına yaz, servisi yeniden başlat ve komutu çalıştır.
3. «E-Mail SMTP ✓» çıkınca `--mail kendi@adresin` ile gönder. Gelen kutusunu ve spam klasörünü kontrol et.
4. Spam'e düşüyorsa alan adında SPF/DKIM eksiktir. Posta sağlayıcısına sor.

### 2. SMS

1. ASPSMS'te iş hesabı aç ve kredi yükle.
   - Fiyat (aspsms.com, 01.11.2026): 500–5.000 kredi için kredi başına CHF 0.09.
   - Swisscom'a 1 SMS 1 kredi, Salt/Sunrise'a 1.75 kredi.
2. Hesaptan UserKey ve şifreyi alıp `.env` dosyasına yaz. `NFIZYO_SMS=live` yap.
3. Komutu çalıştır. Beklenen sonuç: «SMS ASPSMS ✓ Zugang gültig, Guthaben …».
4. `--sms 079…` ile kendi telefonuna gönder.
5. Yönetim › Einstellungen › SMS ekranında sağlayıcıyı seç ve gönderen adını gir (en fazla 11 karakter).
6. Gerçek hastadan önce ASPSMS ile veri işleme sözleşmesini (AVV) imzala.

### 3. HIN

1. Muayenehanenin HIN üyeliği gerekir (HIN, physioswiss üyeleri için ortak teklif: yılda CHF 240'tan başlıyor, hin.ch).
2. HIN Client'ı kutuya kur. Ayrıntı `BAGLANTILAR.md` dosyasında.
3. `NFIZYO_HIN_*` anahtarlarını gir.
4. Komutu çalıştır. Beklenen sonuç: «HIN senden (SMTP) ✓» ve «HIN empfangen (IMAP) ✓ Posteingang: N Nachrichten».
5. `--hin muayenehane@hin.ch` ile kendine gönder. Mesaj gelince N-Fizyo'nun Posteingang ekranına da düşmeli (5 dakika içinde).
6. İlk gerçek rapordan önce HIN Client'ın açık kaldığını kontrol et: kutu yeniden başlayınca HIN Client da başlamalı.

### 4. MediData

Kaynak: MediData'nın resmî belgeleri (brand.medidata.ch › Elektronische Leistungsabrechnung › Developer-Bereich, 08.11.2026):

| Belge | Sürüm | N-Fizyo'da |
|---|---|---|
| Leistungsabrechnung – Schnittstellendokumentation (de) | **V5.2.0**, 02.09.2026 (kapakta «5.1.1», altbilgide «V 5.1») | gönderim, katılımcı listesi, durum, cevap indirme, bildirimler, hata kodları, sorgu aralıkları |
| B_ClientSchnittstellen | V1.5, 09.05.2022 | genel HTTP kodları, hata JSON'u, kutu/VA adresi |
| Best Practice Webservice MediData | V1.2, 03.06.2024 (V5.2.0 içinde V1.3) | karşılıklı TLS + Basic + X-CLIENT-ID, curl örnekleri, «Expect: 100-continue» |
| B_Prozessbeschrieb Leistungsabrechnung | V3.7, 23.02.2023 | süreç, MIME türleri (4.x) |
| Leistungsabrechnung Simulator für Integratoren | 2.5, 17.03.2021 | simülatörler |
| Requesting a client certificate with CSR | V1.0 | sertifika yenileme |
| Requesting the new X-Client-ID | V1.1 | kutu/VA → web servis |

Arayüz bu belgelere göre yazıldı ve sahte bir MediData sunucusuyla test edildi (`tests/test_medidata_webservice.py`).
Gerçek test sistemiyle ilk deneme hâlâ yapılmalı (Adım 7). Açık sorular: `docs/FATURA.md` › «MediData'ya sorulacaklar».

Komutların başı hep aynı (aşağıda `…` ile kısaltıldı; önce `cd /opt/n-fizyo/app`):
`sudo -u nfizyo /opt/n-fizyo/venv/bin/python -m nfizyo --env /etc/n-fizyo/n-fizyo.env`

**Adım 1 – Partner kaydı (NSystem, bir kez).**

1. Dev Kit › «Schritt 1: Partner-Anmeldung MediData-Netz» › «Zum Formular». Formu NSystem yazılım firması olarak doldurur.
2. «Schritt 2: Schnittstellen integrieren»: XML standartları Forum Datenaustausch'tan gelir.

**Adım 2 – Sözleşme (muayenehane).** Muayenehane MediData ile sözleşme yapar. Ücreti başka yazılım firmaları kreditor başına
ayda yaklaşık CHF 5 (yılda 60) olarak yazıyor; teklifte kontrol et.

**Adım 3 – Adres ve erişim bilgileri.** Web servis üç şeyi birlikte ister: client sertifikası (karşılıklı TLS), Basic giriş
(kullanıcı:şifre) ve `X-CLIENT-ID` başlığı. Kullanıcı/şifre: müşteri portalı › Einstellungen › Infrastruktur › Branchenapplikation ›
Credentials (MediData adresi ve erişimi yazılım firmasına FTAPI ile de gönderir). Sadece `.env` dosyasına:

```
NFIZYO_MEDIDATA=test                                        # off = sadece XML · test · production
NFIZYO_MEDIDATA_URL=https://services-acc.medidata.ch/md/srv/id0   # test (ACC); üretim: https://services.medidata.ch/md/srv/id0
NFIZYO_MEDIDATA_CLIENT_ID=1000xxxxxx_1200yyyyyy             # 10 rakam (Master) _ 10 rakam (Subclient)
NFIZYO_MEDIDATA_USER=…
NFIZYO_MEDIDATA_PASSWORD=…
NFIZYO_MEDIDATA_CERT=/etc/n-fizyo/medidata/client.crt
NFIZYO_MEDIDATA_KEY=/etc/n-fizyo/medidata/client.key
# NFIZYO_MEDIDATA_CA=…            # sadece sunucu sertifikası tanınmıyorsa (test sistemi), doğrulama asla kapanmaz
# NFIZYO_MEDIDATA_PRACTICE=demo   # kutuda birden çok muayenehane varsa: MediData erişimi hangisinin (kısa ad)
```

- Adres `/ela/`'dan **önce** biter. `id0` belgelerdeki örnek; numarayı (`id<N>`) MediData müşteri başına verir.
- Eski MediData kutusu/VA: `NFIZYO_MEDIDATA_URL=https://<kutu>:8100` (N-Fizyo `/md` ekler; V5 belgeleri sadece web servisi
  anlatıyor, kutu yolları aynı kabul edildi). Kutuda client sertifikası şart değil.
- Kutuda birden çok muayenehane varsa `NFIZYO_MEDIDATA_PRACTICE` şart. Yoksa N-Fizyo hiçbirine MediData açmaz.

**Adım 4 – X-Client-ID.** `NFIZYO_MEDIDATA_CLIENT_ID` (müşteri portalından). Web serviste biçim `1000xxxxxx_1200yyyyyy`;
uymuyorsa `check-connections` «!» gösterir.

Eski MediData kutusundan veya VA'dan web servise geçerken yeni X-Client-ID şöyle alınır:

1. Önce MediData organizasyonu web serviste bir client'a atar ve etkinleştirir. Bu client'ın numarasını (`cloudclientinstanceid`) bildirir.
2. `.env`'de hâlâ **eski** kutu adresi ve **eski** X-Client-ID olmalı.
3. `… medidata-client-id --instance-id 1000026203`
4. Komut eski kutuya `GET …/md/cloudmigration` sorar (Basic + eski X-CLIENT-ID) ve yeni ID'yi sadece ekrana yazar (kayda/log'a yazmaz).
5. Yeni ID'yi sadece `.env`'ye yaz (`NFIZYO_MEDIDATA_CLIENT_ID`), sonra servisi yeniden başlat.

Hata: 400 = numara yanlış, 403 = kullanıcı/şifre veya eski ID yanlış, 500 = MediData'da hata.

**Adım 5 – Client sertifikası.** Web servis karşılıklı TLS ister (V5.2.0, Anhang BestPractice). Sertifikanın CN'i = X-Client-ID.

1. Klasör (sadece servis kullanıcısı): `sudo install -d -m 0700 -o nfizyo -g nfizyo /etc/n-fizyo/medidata`
2. `.env`:
   ```
   NFIZYO_MEDIDATA_KEY=/etc/n-fizyo/medidata/client.key
   NFIZYO_MEDIDATA_CERT=/etc/n-fizyo/medidata/client.crt
   # Yenileme adresi NFIZYO_MEDIDATA_URL'den çıkarılır (…/md/srv/id<N> → …/md/srv/id<N>/client/certificates).
   # Sadece farklıysa: NFIZYO_MEDIDATA_SERVICE_URL=https://… ve NFIZYO_MEDIDATA_APPLIANCE_ID=id<N>
   ```
3. Anahtar ve CSR: `… medidata-csr --org "Physio Muster AG" --city Biel`
   - Sonuç: `client.key` (RSA 2048, hakları 0600) ve `client.csr` (C=CH, L, O, CN = X-Client-ID).
   - Anahtar zaten varsa tekrar kullanılır, asla üzerine yazılmaz.
4. İlk sertifika: MediData müşteri portalından (CSR belgesine göre portal da sertifika verir; adımları MediData'ya sor).
   Zinciri PEM olarak `client.crt` dosyasına koy, `chmod 600`, sahibi `nfizyo`.
5. Komutu çalıştır. Beklenen: «MediData Zertifikat ✓ gültig bis …».

Kurallar:

- **Anahtar dosyası sadece sahibine açık** olmalı (600 veya 400). Grup veya başkaları okuyabiliyorsa N-Fizyo anahtarı **kullanmaz**
  ve açık bir hata verir. Sahibi `nfizyo` olmalı, çünkü servis gönderirken anahtarı okur. Şifreli anahtar desteklenmez.
- **Yenileme** bitişten **60 gün önce** mümkün; aynı veya yeni CSR ile. Aynı anda en çok **2 aktif sertifika** olur (portaldakiler dahil).
  - `… medidata-renew` mevcut sertifikayla `POST …/md/srv/<applianceid>/client/certificates` yapar (PEM zinciri; `--json` ile JSON).
  - Yeni zincir atomik yazılır, eskisi `client.crt.bak-<tarih>` olarak kalır. Servisi yeniden başlatmaya gerek yok.
  - 60 günden erken çalıştırılırsa hiçbir şey göndermez («Noch nicht fällig»). Günlük zamanlanmış görev olarak çalıştırılabilir.
  - Yeni anahtarla: önce `… medidata-csr --org … --city … --new-key` (`client.new.key` + `client.new.csr`), sonra `medidata-renew`.
    Anahtar da değişir, eskisi `client.key.bak-<tarih>` olarak kalır.
- **Hata kodları** (400). Hiçbirinde dosya değişmez:

  | Kod | Anlamı | Ne yap |
  |---|---|---|
  | `CSR-INVALID` | CSR boş/bozuk | `medidata-csr` ile yeniden oluştur |
  | `CN-INVALID` | CN boş/yanlış | CN = X-Client-ID olmalı (`NFIZYO_MEDIDATA_CLIENT_ID`) |
  | `NEW-CSR-OUTSIDE-RENEWAL-PERIOD` | 60 günden önce yeni CSR | bekle veya eski CSR'ı kullan |
  | `TOO-MANY-ACTIVE-CERTIFICATES` | zaten 2 aktif sertifika | biri bitsin veya portalda iptal et |
  | `UNKNOWN-ERROR` | MediData'da bilinmeyen hata | sonra tekrar dene, olmazsa MediData destek |

- **İzleme:**
  - `check-connections` bitiş tarihini gösterir; 60 gün kala «!», bitmişse veya anahtar sertifikaya uymuyorsa «✗».
  - NSystem paneli uyarır («MediData-Zertifikat läuft in N Tagen ab»). Panele sadece durum ve gün sayısı gider; dosya yolu veya ID gitmez.
  - Yönetim › Einstellungen › Zahlung › «MediData»: bitiş tarihi (dosya yolu yok).

**Adım 6 – Bağlantı testi (hiçbir şey göndermez).** Komutu çalıştır:

- «MediData ✓ Zugang gültig (Webservice ACC (Test), Teilnehmerverzeichnis lesbar)»: N-Fizyo sertifika + Basic + X-CLIENT-ID ile
  sadece `GET …/ela/participants?limit=1` sorar. Fatura gönderilmez, şifre/anahtar çıktıda görünmez.
- «MediData Zertifikat ✓ gültig bis …», «MediData Abfragen ✓» (son otomatik sorguların hatası varsa «!» ve kısa sebep).
- `production` + test adresi (`services-acc`) ise «!» uyarısı.

**Adım 7 – Kabul (test) sistemi ve simülatörler.** `NFIZYO_MEDIDATA=test`, adres `https://services-acc.medidata.ch/md/srv/id<N>`.

- Test sistemi kendinden imzalı sertifika kullanıyorsa (simülatör belgesi, «Known Bugs 3.2»):
  - O sertifikayı/CA'yı PEM olarak kaydet: `NFIZYO_MEDIDATA_CA=/etc/n-fizyo/medidata/acceptance-ca.pem`.
  - Bu dosya sistemin güvendiği listeye **eklenir**. TLS kontrolü **hiçbir zaman kapatılmaz**.
  - Bağlanılan ad sertifikadaki adla uyuşmalı (belgeye göre `*.medidata.ch`); IP adresiyle bağlantı bu yüzden reddedilir.
- Yönetim › Einstellungen › Zahlung › «MediData: Zertifikat und Test mit dem Simulator»:
  - Uydurma bir test kişisiyle (hasta verisi yok) ve muayenehanenin bilgileriyle (GLN, adres, IBAN) bir test faturası gönderir.
  - **Sadece test modunda.** Üretimde (`production`) kodda kapalı; sayfa formu göstermez, istek reddedilir.

| Simülatör | GLN | Kanun | Ne yapar |
|---|---|---|---|
| Antwortensimulator Leistungsabrechnung | 2099988876514 | KVG, VVG, OR | fatura cevabı üretir (seçilen flag ile) |
| Versicherungssimulator | 2099988880030 | UVG, IVG, MVG | `<!-- md-dsdt-simulator:success -->` ile 5 dakika içinde «Erledigt» |

- Flag'ler (belge 2.1.3, XML 4.4/4.5): flag yok = Accepted, `ClinicalDatasetMissing` (Pending), `InvalidSchema` (Rejected),
  `MultipleDelivery` (Rejected), `PriorNotice` (Pending), `WrongAmountOfMoneyForNonValuedAdditionalCharges` (Rejected),
  `UnknownInvoiceId` (bilinmeyen faturaya cevap).
- N-Fizyo `<!-- invoiceresponsegenerator:FLAG -->` yorumunu XML başlığının (`<?xml …?>`) **hemen arkasına** koyar, asla önüne değil.
- **Dikkat (XML sürümü):** V5.2.0 XML 5.0'ı destekli sayıyor, ama simülatör belgesi (2021) cevabı sadece XML 4.3–4.5 için
  yazıyor. 5.0'a cevap gelip gelmediği ilk denemede görülecek. Cevap gelmezse N-Fizyo beklemez ve bunu hata saymaz.
- Cevabı elle indirmek gerekmez: N-Fizyo 30 dakikada bir (veya sayfadaki «Jetzt abrufen» ile hemen) indirir ve
  Abrechnung › Antworten'e koyar. Gerçek bir faturaya ait değildir (numara `SIM-…`); «Erledigt» ile kapat.
- Aynı sayfada: son sorgular (zaman, hata), MediData bildirimleri, faturaya dönüştürülemeyen belgeler (indirilebilir).
- Normal akış da denenmeli: demo/test hastasıyla fatura oluştur, kapat, «Senden» (XML'de `modus="test"`). Fatura sayfasında
  önce sigortanın katılımcı listesinde olup olmadığı, sonra durum («MediData: in Bearbeitung» → «zugestellt») görünür.

**Otomatik sorgular (Best Practice «Polling»).** MediData eğer kurulu ve muayenehaneye aitse kutu kendisi sorar:

| Ne | Çağrı | Ne sıklıkla |
|---|---|---|
| Katılımcı listesi | `GET …/ela/participants` (tüm sayfalar, `limit`/`next`) | 24 saatte bir |
| Gönderilen faturaların durumu | `GET …/ela/uploads/{ref}/status` (+ `/detail`), son durum DONE/ERROR'a kadar | 30 dakikada bir |
| Sigorta cevapları | `GET …/ela/downloads` → `GET …/ela/downloads/{ref}` → kaydet → `PUT …/status {"status": "CONFIRMED"}` | 30 dakikada bir |
| Bildirimler | `GET …/ela/notifications` (`from`) → kaydet → `PUT …/notifications/{id}/status {"notificationFetched": true}` | 30 dakikada bir |

- İlk sorgu açılıştan 1–6 dakika sonra (her kutuda başka dakika, hepsi tam saatte sormasın).
- Çağrılar sırayla, aralarında kısa bekleme. 401/403/429/5xx veya bağlantı yoksa tur hemen biter; sonraki aralıkta tekrar.
- Cevap ve bildirim **önce kaydedilir, sonra** MediData'ya «alındı» denir. Aynı belge iki kez gelirse ikinci kez içeri alınmaz.
- Sorgular asla fatura göndermez. «Unklar» bir gönderim otomatik tekrar gönderilmez; bildirim, durum veya sigorta cevabı
  ulaştığını kanıtlarsa kendiliğinden «gesendet» olur.
- «Jetzt abrufen» (Yönetim › Einstellungen › Zahlung › MediData): en erken 2 dakikada bir (katılımcı listesi saatte bir).

**Adım 8 – Üretim.** Şartlar: simülatör testi ve normal akış test sisteminde çalıştı, sertifika geçerli, katılımcı listesi
yükleniyor. Sonra `NFIZYO_MEDIDATA=production` ve `NFIZYO_MEDIDATA_URL=https://services.medidata.ch/md/srv/id<N>`.
Simülatör sayfası o andan itibaren kilitlidir.

### 5. Video (Jitsi)

1. `docs/VIDEO.md` dosyasına göre sunucuyu kur.
2. Komutu çalıştır. Altı satırın hepsi ✓ olmalı: eingerichtet, https, Schlüssel, erreichbar, Datenschutz, keine Gäste.
3. Aynı kontrol Yönetim › Einstellungen › Video ekranında da var.
4. Gerçek deneme:
   1. Yarın için kendine bir randevu aç.
   2. «Video-Sitzung» ile linki SMS olarak kendine gönder.
   3. Saatinde terapist ekranından «Video starten» de.
   4. Telefonda linki aç.

### 6. bexio

İki yol var:

**a) Pilot için en kolayı: Personal Access Token (PAT)**

1. developer.bexio.com/pat adresinde muayenehanenin bexio kullanıcısıyla token oluştur.
2. `NFIZYO_BEXIO_TOKEN=…` olarak gir. Kutuda birden çok muayenehane varsa `NFIZYO_BEXIO_PRACTICE=<kısa ad>` da gir
   (token sadece o muayenehanede görünür; diğerleri «nicht verfügbar»).
3. **Dikkat:** PAT 60 gün geçerli ve bexio onu kişisel/geliştirme kullanımı için öngörüyor. 60 günde bir yenilemek gerekir.

**b) Kalıcı çözüm: OAuth uygulaması (NSystem adına)**

1. developer.bexio.com'da NSystem için bir uygulama kaydet.
2. Redirect URL olarak şunu yaz: `https://<muayenehane-adresi>/leitung/einstellungen/export/bexio/zurueck`
3. Kapsam (scope): `openid offline_access accounting`.
4. `NFIZYO_BEXIO_CLIENT_ID` ve `NFIZYO_BEXIO_CLIENT_SECRET` anahtarlarını gir.
5. Yönetim › Einstellungen › Export › bexio ekranında «Mit bexio verbinden» de. Muayenehane kendi bexio girişini yapar.
6. Yenileme anahtarı veritabanında şifreli saklanır.

**Test**

1. Komutu çalıştır. Beklenen sonuç: «bexio ✓ Zugang gültig, N Konten, CHF vorhanden».
2. Yönetim › Export ekranında «Verbindung prüfen» de. Eksik hesap numarası çıkarsa:
   - ya üstteki hesap planını bexio'daki numaralara uydur,
   - ya da hesabı bexio'da aç.
3. Bir ay seç ve «An bexio senden» de. bexio'da Buchhaltung › Journal'da kayıtları gör.
4. Aynı ayı ikinci kez göndermek hiçbir şeyi çift yazmaz.

**Önemli kurallar**

- bexio'nun banka eşleştirmesi açıksa, ödemeleri iki yerden birden yazma. Ödemeler ya bexio bankasından ya N-Fizyo'dan gelsin. Bunu Treuhand ile kararlaştır.
- KDV satırları bexio vergi kodu olmadan KDV hesabına gider. bexio'da KDV beyanı yapılıyorsa bunu Treuhand ile kontrol et.
- «Unklar» satırı (zaman aşımı) otomatik tekrar gönderilmez. Önce bexio'da bak, sonra «Ist in bexio» ya da «Fehlt – neu senden» de.

## Sorun çıkarsa

| Çıktı | Anlamı | Çözüm |
|---|---|---|
| `ConnectionRefusedError` | Sunucu/port kapalı | Host/port yanlış veya servis (HIN Client, Jitsi) çalışmıyor |
| `KI-Server … Token prüfen` | token yanlış veya muayenehane YZ sunucusunda kapatılmış | YZ sunucusunda `list-clients`; gerekirse yeni `add-client`, token sadece kutunun `.env`'ine |
| `KI-Server nicht erreichbar` | YZ sunucusu kapalı, alan adı/ağ sorunu | YZ sunucusunda `systemctl status n-fizyo-ai`, `curl https://<alan>/health`; bu sürede diktatlar kaydedilir, sunucu dönünce kendiliğinden işlenir |
| `KI-Server: Spracherkennung ✗ dort nicht bereit` | sunucu çalışıyor ama Whisper/Ollama hazır değil | YZ sunucusunda `journalctl -u n-fizyo-ai -n 50`, `ollama list`, `nvidia-smi` |
| `SMTPAuthenticationError` | Kullanıcı/şifre yanlış | `.env` içindeki bilgiyi kontrol et, sonra yeniden başlat |
| `ASPSMS 200: 3 …` | Erişim reddedildi | UserKey/şifre veya IP kısıtı (ASPSMS hesabında) |
| `bexio 401` | Token geçersiz/süresi dolmuş | PAT'ı yenile veya «Mit bexio verbinden» tekrar |
| `bexio 403` | Kullanıcının muhasebe yetkisi yok | bexio'da kullanıcıya muhasebe hakkı ver |
| `Video: Datenschutz … ✗` | Jitsi ayarları N-Fizyo'nunki değil | `deploy/jitsi/custom-config.js` dosyasını tekrar kopyala |
| `MediData … Client-Zertifikat fehlt oder wird abgelehnt?` | sunucu client sertifikası istiyor; yok, yanlış CA'dan veya başka client'ın | `NFIZYO_MEDIDATA_CERT/_KEY`, CN = X-Client-ID |
| `Schlüsseldatei … (Rechte 0640)` | anahtarı başkaları da okuyabiliyor, kullanılmadı | `chmod 600 <dosya>`, sahibi `nfizyo` |
| `CERTIFICATE_VERIFY_FAILED` | sunucunun sertifikası tanınmıyor (test sistemi kendinden imzalı) | `NFIZYO_MEDIDATA_CA` ile o CA'yı ver, adı kontrol et (asla doğrulamayı kapatma) |
| `MediData Zertifikat ! läuft in N Tagen ab` | bitişe 60 günden az | `… medidata-renew` |
| `MediData 403 … X-CLIENT-ID …` | kullanıcı/şifre, X-CLIENT-ID veya sertifika MediData'daki ile uyuşmuyor | `.env` değerleri, CN = X-Client-ID |
| `MediData X-CLIENT-ID !` | web serviste biçim `1000xxxxxx_1200yyyyyy` değil | müşteri portalındaki ID'yi gir |
| `HTTP 404 auf …/ela/participants` | adres yanlış | `NFIZYO_MEDIDATA_URL` `/ela`'dan önce bitmeli: `…/md/srv/id<N>` |
| `MediData 413` (gönderimde) | dosya büyük ve kimlik doğrulama tamamlanmadan kesildi | N-Fizyo `Expect: 100-continue` gönderir; sürerse MediData'ya sor |
| `MediData Praxis ✗` | kutuda birden çok muayenehane, `NFIZYO_MEDIDATA_PRACTICE` yok | muayenehanenin kısa adını gir |

Kaynaklar (01.11.2026'da açılan sayfalar):

- [ASPSMS fiyatlar](https://www.aspsms.com/en/prices/)
- [HIN dernek teklifleri](https://www.hin.ch/de/angebot/gesundheitsfachpersonen-praxen/verbaende.cfm)
- [bexio API dokümantasyonu](https://docs.bexio.com/): kimlik doğrulama, PAT 60 gün
- MediData geliştirici sayfası (Dev Kit): brand.medidata.ch › Elektronische Leistungsabrechnung (Schritt 1, Schritt 2,
  Schnittstellenbeschreibungen, Technische Anleitungen) – 08.11.2026
- MediData ücreti için başka yazılım firmalarının sayfaları:
  - [ePhysio](https://ephysio.pharmedsolutions.ch/de/physio)
  - [PhysioApp](https://www.physioapp.ch/fr/tarifs)
