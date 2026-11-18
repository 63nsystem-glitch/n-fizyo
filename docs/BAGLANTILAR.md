# Dış bağlantılar: SMS, e-posta, HIN, MediData

Bütün erişim bilgileri **sadece** kutudaki `/etc/n-fizyo/n-fizyo.env` dosyasına yazılır (sohbete asla).
Değiştirdikten sonra: `sudo systemctl restart n-fizyo`

## SMS (ASPSMS veya eCall)

```
NFIZYO_SMS=live                  # log = hiçbir şey gönderilmez (demo, test)
NFIZYO_ASPSMS_USERKEY=…          # ASPSMS hesabından
NFIZYO_ASPSMS_PASSWORD=…
NFIZYO_ECALL_USERNAME=…          # eCall hesabından (veya API alt kullanıcısı)
NFIZYO_ECALL_PASSWORD=…
```

Sonra Yönetim › Einstellungen › SMS: sağlayıcıyı seç (sadece erişim bilgisi olan seçilebilir), gönderen adı (en fazla 11 harf/rakam),
«Test-SMS» ile dene. Her SMS önce giden kutusuna yazılır; hata olursa 1, 5, 15, 60 dakika sonra tekrar denenir.
Bir mesaj gönderilmeden önce giden kutusunda tek bir gönderime ayrılır (10 dk); tekrar deneme döngüsü aynı anda uyansa bile
hasta aynı SMS'i (veya e-postayı) iki kez almaz. Sunucu tam gönderirken kapanırsa, mesaj 10 dk sonra tekrar denenir.
Sabit hatlara (İsviçre/Liechtenstein, cep dışı) SMS gönderilmez (ulaşmaz, ücret çıkar): hatırlatma hastanın onayı varsa
e-postayla gider; hasta formunda «Festnetz» notu, uygulama davetinde «QR kodu gösterin» uyarısı çıkar.
SMS'te teşhis/bulgu olmaz – sadece randevu, yer, kod veya uygulama linki (hasta bildirimleri içeriksiz, aşağıda).
Kod veya link içeren SMS giden kutusunda maskelenir («Code ••••••»; tam metin sadece gönderilene kadar). Online randevu sadece
İsviçre/Lihtenştayn cep numaralarına SMS kodu gönderir. Gerçek kullanımdan önce sağlayıcıyla veri işleme sözleşmesi (AVV) yap.

- ASPSMS: `https://json.aspsms.com/SendSimpleTextSMS` (JSON; StatusCode 1 = tamam)
- eCall: `https://rest.ecall.ch/api/v2/message` (Basic giriş)

## E-posta (hastaya: onay, hatırlatma, fatura/makbuz PDF)

```
NFIZYO_MAIL=live                 # log = hiçbir şey gönderilmez (demo, test)
NFIZYO_SMTP_HOST=smtp.example.ch # muayenehanenin veya NSystem'in posta sunucusu
NFIZYO_SMTP_PORT=587
NFIZYO_SMTP_TLS=starttls         # starttls | ssl (465) | none
NFIZYO_SMTP_USER=…
NFIZYO_SMTP_PASSWORD=…
NFIZYO_SMTP_FROM=termine@praxis.ch   # gönderen; cevaplar muayenehanenin e-postasına gider (Reply-To)
```

- Sadece **hastanın izniyle**: hasta kaydında «Einverstanden mit E-Mails (unverschlüsselt)» işareti (tarih kaydedilir).
  Online randevuda hasta bu izni kendisi verebilir.
- Hatırlatma kanalı hasta kaydında: SMS, e-posta, ikisi, hiçbiri.
- İçerik kısa: randevu, yer, telefon; fatura/makbuz PDF eki; hasta bildirimi (içeriksiz, link). Teşhis yok. Her e-postada «Im Notfall 144».
- PDF eki gönderilince giden kutusundan silinir. Hata olursa 1, 5, 15, 60 dakika sonra tekrar.
- Yönetim › Einstellungen › SMS sayfasında e-posta durumu ve «Test-E-Mail».
- Doktorlara rapor **her zaman HIN** ile, asla normal e-postayla.
- Gönderen alan adı için SPF/DKIM kaydı (posta sağlayıcısında) ayarlanmalı, yoksa e-postalar spam'e düşebilir.

## Hasta bildirimleri (0.9.0, modül «notify»)

Terapist mesaja cevap verince veya hasta bilgisi gönderince hasta kısa bir haber alır: «Sie haben eine neue Nachricht von
<Praxis>. Öffnen: <link>» – hastanın uygulama dilinde, içerik yok, linkte giriş anahtarı yok. Yönetim › Einstellungen ›
**Benachrichtigungen**: kapalı / sadece e-posta (izinle) / sadece SMS / izin varsa e-posta, yoksa SMS (varsayılan); isteğe bağlı
yeni randevu. Kişi başına 15 dakikada en çok bir, 20:00–08:00 arası SMS yok, sadece uygulamayı bağlamış hastaya. Gönderim yukarıdaki
SMS ve e-posta ayarlarıyla (ek erişim bilgisi yok). Ayrıntı: `docs/MIMARI.md` › «yarım kalanların tamamlanması».
Linkteki adres: `NFIZYO_BASE_URL` (işletmede https şart). Ayarlı değilse (demo) diğer SMS'lerdeki gibi muayenehanenin sunucuyu en son
açtığı adres kullanılır (`core/web.py` › `background_public_url`); hiç istek gelmemişse sunucunun gerçek portu – sabit
127.0.0.1:8080 asla.

## HIN

Sunucuda **HIN Client** çalışır (şifrelemeyi o yapar). Kutu ona bağlanır:

```
NFIZYO_HIN_PRACTICE=balance      # posta kutusu hangi muayenehanenin (kısa ad)
NFIZYO_HIN_FROM=praxis@hin.ch    # muayenehanenin HIN adresi
NFIZYO_HIN_SMTP_HOST=127.0.0.1   # HIN Client: gönderme 5018, alma (IMAP) 5020
NFIZYO_HIN_SMTP_PORT=5018
NFIZYO_HIN_IMAP_HOST=127.0.0.1
NFIZYO_HIN_IMAP_PORT=5020
NFIZYO_HIN_TLS=none              # HIN Client yerel; ağ geçidi kullanılırsa starttls veya ssl
NFIZYO_HIN_USER=…                # HIN girişi (gelen posta için)
NFIZYO_HIN_PASSWORD=…
NFIZYO_HIN_POLL_MINUTES=5
```

- Gönderme: Terapi › rapor kesinleşince «Über HIN senden» (doktorun HIN adresi Einstellungen › Zuweisende'de olmalı).
  Başarısızsa rapor «gönderildi» sayılmaz.
- Alma: gelen HIN postasındaki PDF/resimler (ek yoksa metin) posta kutusuna düşer, YZ okur, eşleştirmeyi önerir.
  Hiçbir posta sessizce kaybolmaz: sadece HTML olan postaların (spital sistemleri) metni alınır, Word (.docx) metne çevrilir,
  diğer biçimler (.doc, .rtf …) ve 25 MB'tan büyük ekler bir notta adıyla listelenir («HIN posta kutusunda açın»).
  İmzadaki logolar (image001.png) ayrı belge olmaz. Okunamayan bir posta diğerlerini durdurmaz.
- Kontrol: Yönetim › Einstellungen › HIN › «Verbindung prüfen».

## MediData

Belgeler: «Leistungsabrechnung – Schnittstellendokumentation» V5.2.0, «B_ClientSchnittstellen» V1.5, «Best Practice Webservice»
V1.2/V1.3 (+ Simulator 2.5, CSR V1.0, X-Client-ID V1.1). Bunlara göre uygulandı; gerçek test sistemiyle ilk deneme yapılmalı.

```
NFIZYO_MEDIDATA=test                     # off · test · production
NFIZYO_MEDIDATA_URL=https://services-acc.medidata.ch/md/srv/id0   # test (ACC); üretim https://services.medidata.ch/md/srv/id<N>
NFIZYO_MEDIDATA_CLIENT_ID=1000xxxxxx_1200yyyyyy
NFIZYO_MEDIDATA_USER=…                   # müşteri portalı › Branchenapplikation › Credentials (Basic)
NFIZYO_MEDIDATA_PASSWORD=…
NFIZYO_MEDIDATA_CERT=/etc/n-fizyo/medidata/client.crt   # client sertifikası (karşılıklı TLS), CN = X-Client-ID
NFIZYO_MEDIDATA_KEY=/etc/n-fizyo/medidata/client.key    # chmod 600, sahibi nfizyo
# NFIZYO_MEDIDATA_CA=…                   # sadece sunucu sertifikası tanınmıyorsa
# NFIZYO_MEDIDATA_PRACTICE=…             # kutuda birden çok muayenehane: MediData kimin (kısa ad)
```

- Gönderim, katılımcı listesi (24 saat), durum, cevaplar ve bildirimler (30 dakika): `docs/FATURA.md` › MediData.
- Kurulum adımları (partner kaydı, erişim, X-Client-ID, client sertifikası, bağlantı testi, simülatörler, üretim):
  `docs/VERBINDUNGSTEST.md` › MediData.
- eBill adres eşleştirme (`ebillrecipients`) isteğe bağlı, uygulanmadı (FATURA.md).

## Video (kendi Jitsi sunucusu)

Bkz. `docs/VIDEO.md`. `.env` satırları (anahtar sadece sunucuda, asla sohbette):

```
NFIZYO_VIDEO_URL=https://video.physio.example.ch
NFIZYO_VIDEO_APP_ID=nfizyo
NFIZYO_VIDEO_SECRET=…   # install-jitsi.sh üretir; Jitsi sunucusundaki JWT_APP_SECRET ile aynı
```

## İki adımlı giriş

```
NFIZYO_REQUIRE_2FA=1             # gerçek kurulumda varsayılan; demo'da 0
NFIZYO_STAFF_IDLE_HOURS=8        # personel bu kadar saat hiç tıklamazsa çıkış (varsayılan 8); en uzun oturum yine 14 gün
NFIZYO_TRUSTED_PROXIES=127.0.0.1,::1   # sadece bu adreslerden gelen X-Forwarded-For'a güvenilir (varsayılan: kutudaki Caddy)
```

Caddy'nin önünde başka bir proxy varsa (ör. kurumun kendi ağ geçidi) onun adresi/ağı da eklenir: `127.0.0.1,::1,10.0.0.0/8`.
`none` = hiçbir proxy'ye güvenme (kutu doğrudan internete açıksa). Merkez panel için aynı ayar: `NFIZYO_CENTRAL_TRUSTED_PROXIES`.


## bexio (muhasebe)

```
NFIZYO_BEXIO_TOKEN=…             # Personal Access Token (pilot; 60 gün geçerli) – VEYA:
NFIZYO_BEXIO_PRACTICE=…          # token'ın sahibi muayenehane: kısa ad (slug) veya numara (0.9.0)
NFIZYO_BEXIO_CLIENT_ID=…         # NSystem'in bexio OAuth uygulaması
NFIZYO_BEXIO_CLIENT_SECRET=…
```

Token **tek bir muayenehaneye** aittir (0.9.0): `NFIZYO_BEXIO_PRACTICE` boşsa kutudaki tek muayenehane; kutuda birden çok
muayenehane varsa zorunlu – yoksa token kimse için çalışmaz. Diğer muayenehaneler bexio'yu «nicht verfügbar» görür (OAuth
ayarlıysa kendi bexio'larını bağlayabilirler). `check-connections` bunu kontrol eder.

Kullanım: Yönetim › Einstellungen › Export › bexio. Test ve hesap açma sırası: `docs/VERBINDUNGSTEST.md`.

## Hepsini birden test et

`python -m nfizyo --env /etc/n-fizyo/n-fizyo.env check-connections` – ayrıntı: `docs/VERBINDUNGSTEST.md`.
