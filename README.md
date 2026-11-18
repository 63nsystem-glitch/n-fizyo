# N-Fizyo

İsviçre'deki fizyoterapi muayenehaneleri için yazılım. Geliştiren: NSystem GmbH, Biel/Bienne.
Arayüz Almanca ve Fransızca. Hasta uygulaması 11 dilde.

Sunucu, veritabanı ve yapay zekâ aynı kutuda çalışır. Hasta verisi buluta gitmez.

## Ne var? Dört ayrı ekran

| Ekran | Adres | Kim kullanır | Ne yapar |
|---|---|---|---|
| Terapi | `/therapie` | her terapist (tablet + telefon) | kendi günü, **sadece kendi hastaları** (+ vekâlet), hasta dosyası, diktat, raporlar, hastaya bilgi |
| Resepsiyon | `/empfang` | resepsiyon (PC) | herkesin ajandası, hasta kaydı, QR davet, gelen raporu hastaya atama (içeriği görmeden), fatura hazırlama |
| Yönetim | `/leitung` | muayenehane sahibi (PC) | genel bakış, ekip ve roller, vekâlet, fatura kontrol, ayarlar – tıbbi içerik yok |
| Hasta | `/p` | hasta, kendi telefonu (QR ile) | egzersizler, terapistten bilgi (okur + sesli dinler), randevular, mesaj, şikâyet |

Ayrıca (sürüm 0.3.0):

- **İki adımlı giriş (2FA)** herkes için zorunlu: telefondaki uygulamadan 6 haneli kod + kurtarma kodları. Telefon kaybolursa Yönetim › Team › sıfırla.
- **Fatura:** QR-faturalı PDF, XML 5.0 (resmî şemaya göre kontrol), MediData'ya gönderim (önce test), banka dosyası (camt.054) ile ödeme eşleme, 3 kademeli hatırlatma.
- **HIN:** kesinleşmiş rapor tek tıkla PDF olarak doktora; gelen HIN postası otomatik posta kutusuna.
- **Gerçek SMS:** ASPSMS veya eCall, Yönetim › Einstellungen › SMS'ten seçilir.
- **Veri taşıma:** eski yazılımdan Excel/CSV (hastalar, sigortalar, doktorlar, randevular) – önce deneme, sonra aktarım.
- **NSystem merkez paneli:** kutular dışarıya durum bildirir (hasta verisi yok), uyarılar telefona gelir, imzalı komutlar.

Yeni (sürüm 0.9.0 – eksik ve yarım kalanların tamamlanması):

- **Hastaya bildirim:** terapist mesaja cevap verince veya bilgi gönderince hasta kısa bir e-posta (izinle) ya da SMS alır –
  içeriksiz, kendi dilinde, uygulama linkiyle; 15 dakikada en fazla bir, gece SMS yok. Yönetim › Einstellungen › Benachrichtigungen.
- **Not şablonları** (terapistin kendi + muayenehanenin), **cevap süresi** uygulamada 11 dilde, **kurulum listesi** (Yönetim › Übersicht),
  «Offene Arbeit»ın her satırı bir liste, **veri taşımada reçeteler ve açık kalemler**, mesaj listesinde son mesaj ve saat,
  kısa bakışta hedef terapistin dilinde, posta kutusunda orijinal uygulama içinde.
- **İzinler, kapanışlar, resmî tatiller** (kantona göre): ajandada etiketli bant, online randevu ve boşalan randevu bu zamanları
  teklif etmez, seri randevu bu günleri atlar. «Krankheit» sadece Yönetim'de görünür. `docs/KASA_RANDEVU.md`
- **Ev programı:** her egzersizin kendi çizimi, süreli egzersiz (ör. yürüyüş 20 dk), dozaj 11 dilde düz cümle, kütüphanede düzenleme
  ve pasif yapma.
- **Resepsiyon ve fatura düzeltmeleri:** reçete düzeltme/kapatma, çift hasta kaydını birleştirme, ödeme eşlemesini kaldırma, ödeme
  iptali ve iade, banka satırını gizleme, MediData «unklar», sigorta grubuna göre KVG puan değeri, gün sonundan sonra kasa düzeltmesi.
  Nakit 5 Rappen'e yuvarlanır (fatura birkaç Rappen için açık kalmaz), kapanmış kasa gününe fatura sayfasından nakit kaydı yok,
  bexio satırları isim değişince tekrar gitmez, bexio token'ı tek muayenehaneye ait (`NFIZYO_BEXIO_PRACTICE`).
  `docs/FATURA.md`, `docs/KASA_RANDEVU.md`
- **İsviçre biçimleri** her ekranda ve PDF'te aynı: CHF 1’290.00, 03.11.2026, 079 000 00 04.
- **Güvenlik:** deneme sınırları gerçek adrese göre, hesap başına artan bekleme, SMS seli sınırı, SMS kodları giden kutusunda gizli,
  daha güçlü şifre özeti, 8 saat hareketsizlikte çıkış, paketler sabit sürüm + sha256. `docs/GUVENLIK.md`
- **Dışarıda şifreli yedek** (isteğe bağlı): her gece yedek kutuda şifrelenir (AES-256) ve başka bir yere gider (İsviçre'de depolama,
  başka binada sunucu veya disk); orada son 14 set kalır, `deploy/restore.sh --offsite` ile geri gelir, sonuç NSystem panelinde
  (hasta verisi yok). Sağlayıcı sahibin kararı. `docs/YEDEK.md`
- **Otomatik test (GitHub):** `.github/workflows/tests.yml` – Python 3.12 ve 3.13, tüm testler, ruff, pip-audit.
- **MediData, resmî belgelere göre:** web servis için client sertifikası (karşılıklı TLS, anahtar sadece sahibine açık),
  `medidata-csr` ve `medidata-renew` (bitişten 60 gün önce, en çok 2 aktif sertifika), bitiş uyarısı (check-connections, NSystem
  paneli, Yönetim), X-Client-ID geçişi (`medidata-client-id`), Yönetim'den MediData simülatörüne test faturası (sadece test modu,
  uydurma test kişisi). Leistungsabrechnung web servisi Schnittstellendokumentation V5.2.0'a göre: gönderim, katılımcı listesi
  (gönderimden önce kontrol), durum takibi, cevapları ve bildirimleri otomatik alma (24 saat / 30 dakika). `docs/VERBINDUNGSTEST.md`,
  `docs/FATURA.md`
- **KVG 2027:** yeni tarif yapısı 7.11.2026 itibarıyla hâlâ onaysız; N-Fizyo 312 ile devam eder. Onay gelince ne girileceği:
  `docs/FATURA.md` › «Yeni KVG tarif yapısı».
- **NSystem işletimi (yerinde olmadan pilot):** imzalı sürümler (`tools/release.py`; panel listeden sürüm seçer, SHA-256 kendisi
  dolar, istenirse saatli + otomatik bakım duyurusu; kutu imzayı ve SHA-256'yı kontrol etmeden kurmaz), **destek erişimi** (sadece
  Leitung açar, 1–8 saat, geri sayım, kendiliğinden kapanır; WireGuard tüneli + sadece tünelden, sadece anahtarla, sadece sabit destek
  aracı; her bağlantı ve komut muayenehanenin kaydında), **Hilfe › Support anfragen** (her rol; hasta verisi içeren metin
  engellenir; cevap uygulamada), **bakım duyuruları** (de/fr bant, kişi başına kapatılır), **panel yedeği** (gece, şifreli dış kopya).
  `docs/MERKEZ.md`

Yeni (sürüm 0.8.0):

- **Firmalara fatura** (modül «Firmenkunden», Resepsiyon ve Yönetim › Firmen): firma kaydı, fiyat listesinden veya serbest kalemler,
  KDV sadece muayenehanenin KDV numarası varsa, kendi numara serisi (F2026-0001), QR-faturalı PDF, e-postayla gönderme, banka dosyasından
  otomatik ödeme eşleme (Abrechnung › Zahlungen), 3 kademeli hatırlatma, iptal. Muhasebe dışa aktarımında da var. Hasta verisi yok.
- **bexio:** Yönetim › Einstellungen › Export › bexio – dönemin muhasebe kayıtları bexio'ya «manuelle Buchungen» olarak gider;
  hesaplar numarayla eşlenir, iki kez gönderilmez, belirsiz cevap otomatik tekrarlanmaz. Erişim PAT veya OAuth (sadece `.env`).
- **Bağlantı testi:** `python -m nfizyo check-connections` – SMS, e-posta, HIN, MediData, Video, bexio; ek parametre olmadan hiçbir şey
  göndermez. Hesap açma ve test sırası: `docs/VERBINDUNGSTEST.md`.

Sürüm 0.7.1 – yedek: gece yedeği ve güncelleme yedeği artık egzersiz videolarını ve onaylanmamış diktatları da içeriyor,
yedek bütünlüğü kontrol ediliyor, yeni `deploy/restore.sh` (kontrol modu, geri alınabilir, test edildi), tam dışa aktarımda videolar.
Demo'da KVG puan değeri (sadece demo, resmi değil). Ayrıntı: `docs/YEDEK.md`.

Sürüm 0.7.0 – eksiklerin tamamlanması:

- **KVG faturası tam:** tarif yapısı 312'nin tüm pozisyonları (MTT 7340, grup, hipoterapi, ek ücretler, ev ziyareti, malzeme, sondalar),
  otomatik «ilk tedavi» ek ücreti, kombinasyon kuralları faturada kontrol. Ayrıntı: `docs/FATURA.md`.
- **2027 hazırlığı:** tarif tedavi gününe göre seçilir, sınırda fatura otomatik bölünür; yeni liste gelince veri girişi + birkaç kural (`docs/FATURA.md`).
- **Video seansı faturada:** UVG'de otomatik 25.310; KVG'de uyarı.
- **MTT metinleri** (hedef, not) hastanın diline çevrilir, orijinali görülebilir.
- **Yönetim › Einstellungen › Video:** video sunucusunu kontrol et (ulaşılabilir mi, gizlilik ayarı açık mı, misafir kapalı mı).
- **Hasta gizlilik metni** uygulamada (`/p/datenschutz`, 11 dil) ve hukuki taslaklar: `docs/HUKUK.md`.
- **Güvenlik:** Content-Security-Policy (inline JavaScript yok), Permissions-Policy, HSTS; otomatik tarama raporu
  `docs/GUVENLIK.md`.

Önceki sürüm 0.6.0 – rakiplerde olanlar, kendi yolumuzla:

- **Ev programı paketi:** terapist hastanın egzersizini praksis cihazıyla çeker (sadece onay kutusuyla; hasta uygulamada görür ve
  istediği an siler). «Vaktin azsa önce bunlar» yıldızı. «Bugün yapamadın mı?» – hasta sebebi seçer (unuttum, anlamadım, vakit yok,
  ağrı, uygulama, fayda görmüyorum); her sebebe tıbbi olmayan kısa cevap, ağrıda 144. Terapist sebebi ev programında ve kısa bakışta görür.
  Egzersiz metni telefonun kendi sesiyle okunur. Terapist izin verirse hasta kendi ölçüm eğrisini görür (sadece sayı ve hedef).
- **Boşalan randevuyu doldurma:** iptal edilen randevuda bekleme listesinden uyan kişiler (terapist, yer, gün/saat isteği, reçete, aynı gün,
  tercüman). Tek tıkla herkese kendi dilinde SMS; ilk onaylayan alır (randevu otomatik, diğerlerine «artık boş değil»). Çift randevu olmaz.
- **MTT salonu:** aletler ve yazdırılabilir QR kodları; hasta QR'ı okutup seti telefonda kaydeder (tekrar, kg, zorluk, ağrı; alet ayarı görünür);
  abonmanı varsa giriş otomatik düşer. Terapist için canlı salon ekranı (20 sn) ve son iki antrenmana göre artış/azaltma önerisi –
  terapist «Übernehmen» demeden hiçbir şey değişmez. Kısa bakışta «MTT seit letzter Sitzung».
- **Görüntülü görüşme:** muayenehanenin kendi Jitsi sunucusu (İsviçre). Randevu başına rastgele oda, sadece randevu saatinde geçerli
  anahtar, hastaya kendi dilinde SMS linki, ilk seferde onay, kayıt yok, dış servis yok. Kurulum: `docs/VIDEO.md`.

Önceki sürüm 0.5.0 – terapistin günlük işi için (NSystem'in iç araştırma notu «Fizyoterapist şikayetleri ve istekleri»):

- **Reçete takibi** (Verordnungen im Blick): her hastanın reçetesi için kalan seans, 5 haftalık başlama süresi, 36 seans sınırı
  (Kostengutsprache), eksik reçete. «Bu tarihten sonra reçetesiz randevu var» uyarısı; doktordan istendi olarak işaretle,
  7 gün cevap yoksa hatırlat; devam reçetesi mektubunu tek tıkla raporlardan oluştur. Terapi (kendi hastaları) ve resepsiyon (herkes, tanısız).
- **Kısa bakış** (Kurzüberblick): «Heute» ekranında sıradaki hasta için ve her randevuda: son not (plan), son seanstan beri ev egzersizi
  günleri ve ağrı, uygulamadan gelen mesaj/sorun, yeni şikâyet, reçete durumu, ölçüm değişimi, hastanın hedefi. YZ yok, her bilgi kaynaklı.
- **Devir özeti** (Übergabe): başka terapistin hastasını devralınca dosyada otomatik özet (hedef, reçete, kısıtlamalar, son notlar,
  ölçümler, ev programı, açık mesajlar, sıradaki randevular); asıl terapist dönünce «Siz yokken» özeti.

Önceki sürüm 0.4.0 – eski yazılımın yerini tam tutmak için:

- **Özel ödeme + MTT abonman:** muayenehanenin fiyat listesi (hizmet, ürün, abonman, KDV oranı). VVG/özel hastalara fiyat listesiyle fatura,
  sigortalı hastayı da «Privat abrechnen» ile. Yeni **Kasa** (Kasse): Theraband vb. satış, MTT abonmanı, nakit/Twint/kart veya faturaya,
  makbuz PDF, abonman ziyaret işaretleme, açık faturayı kasada ödeme, gün sonu kasa sayımı.
- **Sigorta cevapları:** MediData'dan otomatik gelir (veya XML/ZIP yüklenir), reddedilen fatura ekranda satır satır görünür;
  düzelt → yeniden gönder veya «iptal + yeni taslak». **Kostengutsprache** takibi (36 KVG seansı uyarısı).
- **Online randevu:** muayenehanenin herkese açık sayfası (`/p/termin/<kısa-ad>`, 11 dil), SMS koduyla doğrulama, resepsiyon onaylar.
  **E-posta:** onay, hatırlatma, fatura ve makbuz PDF'i (sadece hastanın izniyle).
- **Dışa aktarma:** muhasebe için yevmiye (CSV) ve Excel, hesap planı; muayenehanenin tüm verisi ZIP (şifreyle, 24 saat);
  terapist için takvim aboneliği (iCal).

Kasa ve online randevu: `docs/KASA_RANDEVU.md`

Mimari: `docs/MIMARI.md` · Modüller: `docs/MODULLER.md` · Fatura: `docs/FATURA.md` ·
Dış bağlantılar (SMS, e-posta, HIN, MediData): `docs/BAGLANTILAR.md` · Merkez panel: `docs/MERKEZ.md`

## Bilgisayarında denemek (demo)

Sadece uydurma verilerle çalışır. Gerçek hasta verisi girme.

```bash
cd n-fizyo                       # açılmış proje klasörü
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
NFIZYO_DEMO=1 .venv/bin/python -m nfizyo run
```

Sonra tarayıcıda: `http://127.0.0.1:8080`

Demo hesapları (şifre hepsinde `demo-praxis`):

| E-posta | Rol | Açılan ekran |
|---|---|---|
| `therapie@demo.n-fizyo.ch` | Terapist (T. Demo) | Terapi |
| `a.beispiel@demo.n-fizyo.ch` | Terapist, şu an L. Esempio'ya **vekâlet** ediyor | Terapi |
| `s.exemple@demo.n-fizyo.ch` | Terapist, Fransızca arayüz | Terapi |
| `empfang@demo.n-fizyo.ch` | Resepsiyon | Resepsiyon |
| `leitung@demo.n-fizyo.ch` | Sahip **ve** terapist | ekran seçimi (Yönetim / Terapi) |

Hasta uygulaması: `http://127.0.0.1:8080/p/anmelden` → telefon `079 000 00 08`, doğum tarihi `14.07.1980` (Ayşe, Türkçe). Demo'da SMS kodu ekranda görünür.

Demo'da denenecek akışlar:

1. Terapi › Heute › bir hastada mikrofon → «Text eingeben» → yaz → not taslağı + **hasta bilgisi önerisi** → «Freigeben» → randevu «erledigt», bilgi hastanın diline çevrilir.
2. Hasta uygulaması (Ayşe) › «Bilgi» → Türkçe okur, «Sesli dinle» (telefonda Türkçe ses varsa).
3. Resepsiyon › Posteingang › «Zuzuordnen» → dokümanı hastaya ata (içerik görünmez) → terapistin posta kutusuna düşer.
4. Terapi (T. Demo) Sandra'yı göremez; A. Beispiel vekâleten Luca'yı görür.
5. Resepsiyon › Abrechnung › taslak → Yönetim › Abrechnung › kesinleştir (KVG için önce Einstellungen › Tarife).
   Sonra «PDF» → son sayfada QR ödeme kısmı. Lea (UVG) için «Elektronische Rechnung» → XML.
6. Abrechnung › «Offene Posten» → hatırlatma; «Zahlungen» → banka dosyası (camt.054) yükle.
7. Yönetim › Import → eski yazılımdan CSV/Excel → sütunları eşle → deneme → aktar.
8. Yönetim › Vertretungen → vekâlet ver; Ayarlar › Module → modül kapat, menüden kaybolur.

Demo'da 2FA kapalıdır (denemesi kolay olsun). Açmak için: `NFIZYO_REQUIRE_2FA=1`. Gerçek kurulumda her zaman açık.

## Muayenehaneye kurulum (Ubuntu 24.04)

Yerel yapay zekâ ürünün parçası, isteğe bağlı değil. Kutuda **en az 16 GB hafızalı NVIDIA ekran kartı** olmalı (24 GB önerilir).

```bash
cd n-fizyo-<sürüm>              # açılmış sürüm paketi (n-fizyo-<sürüm>.tar.gz)
sudo bash deploy/install.sh                            # muayenehane kutusu (yapay zekâ dahil)
sudo bash deploy/install.sh --domain physio.ornek.ch   # İsviçre'de sunucu, gerçek alan adı
sudo bash deploy/install.sh --demo --no-ai             # sadece test: ekran kartı olmadan, yapay zekâ taklit edilir
```

Ekran kartı sürücüsü yoksa betik kurar ve yeniden başlatmanı ister; sonra betiği tekrar çalıştır.
Kart yoksa veya hafızası 16 GB'tan azsa kurulum durur.

Kurulum şunları yapar:

- Program: `/opt/n-fizyo/releases/<sürüm>_<zaman>/`, `/opt/n-fizyo/app` güncel sürümü gösterir; Python ortamı: `/opt/n-fizyo/venv`
- Veriler: `/var/lib/n-fizyo` (veritabanı `/var/lib/n-fizyo/n-fizyo.sqlite3`)
- Ayar dosyası: `/etc/n-fizyo/n-fizyo.env` (gizli anahtar otomatik üretilir, ekrana yazılmaz)
- Tarayıcı klasörü: `/var/lib/n-fizyo/scan-eingang/<kısa-ad>/`
- Günlük yedek: `/var/backups/n-fizyo` (30 gün); isteğe bağlı şifreli dış kopya: `docs/YEDEK.md`
- Runner servisi (`n-fizyo-runner`, root): sadece NSystem'in imzaladığı, listedeki komutları çalıştırır; yedek ve cihaz durumunu yazar
- Yapay zekâ: Whisper (ses → yazı), Ollama + Qwen (not, rapor okuma), TranslateGemma (çeviri) – modeller kurulumda bir kez indirilir, sonra internetsiz çalışır
- HTTPS: Caddy (`/etc/caddy/Caddyfile`), sistem servisi: `n-fizyo.service`

Kurulumdan sonra muayenehane ve yönetici hesabı (şifre sorulur, ekrana yazılmaz):

```bash
cd /opt/n-fizyo/app                    # komutlar program klasöründe çalışır
sudo -u nfizyo /opt/n-fizyo/venv/bin/python -m nfizyo --env /etc/n-fizyo/n-fizyo.env create-practice --name "Physio Balance" --slug balance
sudo -u nfizyo /opt/n-fizyo/venv/bin/python -m nfizyo --env /etc/n-fizyo/n-fizyo.env create-user --email leitung@ornek.ch --name "Ad Soyad" --roles owner,therapist --practice balance
```

## Güncelleme (yedek + otomatik geri dönüş)

Yeni sürümü aç, sonra:

```bash
cd n-fizyo-<yeni-sürüm>                               # açılmış YENİ paket
sudo bash deploy/update.sh --check                     # neler değişiyor (program + her modülün sürümü)
sudo bash deploy/update.sh                             # güncelle
sudo bash /opt/n-fizyo/app/deploy/update.sh --rollback # elle eski sürüme dön
```

Servis mümkün olduğunca uzun çalışır: yeni sürüm klasörü, paketler (`requirements.lock`) ve yedek
(`/var/backups/n-fizyo/pre-update-…`, veritabanı + dokümanlar) servis durmadan hazırlanır; burada bir şey bozulursa
(internet yok, disk dolu) servis hiç durmaz, yarım klasör silinir. Sonra servis durur, veritabanı bir kez daha yedeklenir,
geçiş + göç + başlatma. Durduktan sonra herhangi bir hata (göç, `/health`, iptal) → **kendiliğinden** eski sürüm ve eski
veritabanı, servis yeniden açılır; servis hiçbir durumda kapalı kalmaz. Aynı sürüm tekrar → hiçbir şey yapılmaz; daha eski
sürüm → reddedilir (geri dönüş sadece `--rollback`; elle zorunluysa `--force`, runner'dan asla). Son 3 sürüm saklanır
(klasör adındaki zamana göre; güncel ve önceki asla silinmez). `--rollback` için gereken `pre-update-…` yedeği günlük
yedek temizliğinde 30 günden sonra da silinmez; yine de yoksa `--rollback` durur ve uyarır (sadece program: `--force`).
`install.sh` başka bir sürüm kurulu olan kutuda çalışmaz, `update.sh`'a yönlendirir; gerçek muayenehaneyi `--demo`'ya çevirmez.

## Merkez panele bağlama

NSystem sunucusunda kutu kaydedilir, çıkan dosya kutuya kopyalanır:

```bash
sudo bash /opt/n-fizyo/app/deploy/connect-central.sh /yol/box-bx_XXXX.env
```

Destek erişimi için (isteğe bağlı) aynı şekilde `support-bx_XXXX.env` (`support-peer`); erişim yine kapalı kalır, sadece Leitung açar.
Yeni sürümler sadece imzalı: `NFIZYO_RELEASE_BASE` + `NFIZYO_RELEASE_PUBKEY`.

Ayrıntı: `docs/MERKEZ.md`

## Güvenlik kuralları

- Şifre, API anahtarı, token sohbete **asla** yapıştırılmaz. Sadece sunucudaki `/etc/n-fizyo/n-fizyo.env` ve GitHub Secrets'a girer.
- N-Gastro'dan tamamen ayrı proje: ayrı GitHub deposu, ayrı sunucu, ayrı anahtarlar.
- Demo'da gerçek hasta verisi asla olmaz.
- Muayenehaneler arası veri ayrımı testleri (`tests/test_tenant_isolation.py`) asla silinmez.
- Yapay zekâ karar vermez: öneri yapar, terapist onaylar. Hastaya giden metinleri terapist onaylar.
- Hasta uygulaması tıbbi tavsiye, teşhis veya aciliyet değerlendirmesi yapmaz. Her ekranda: «Im Notfall rufen Sie 144 an.»
- Onaylanan notlar, raporlar ve faturalar değiştirilemez (veritabanı tetikleyicileri). Düzeltme sadece ek not / iptal ile.
- Kim neyi ne zaman gördü/değiştirdi: denetim kaydı (`audit_log`), sadece eklenebilir.
- Herkes için iki adımlı giriş. Gizli anahtarlar veritabanında şifreli, kurtarma kodları sadece özet (hash) olarak.
- SMS, e-posta (SMTP), HIN, MediData ve merkez panel erişim bilgileri sadece `/etc/n-fizyo/n-fizyo.env` içinde.
- Hastaya e-posta sadece izinle (e-posta şifrelenmez); doktora rapor her zaman HIN ile.
- Merkez panele hasta verisi gitmez (destek talepleri gönderilmeden önce kontrol edilir). NSystem şifre sıfırlayamaz; her NSystem
  işlemini muayenehane yönetimi görür. Destek erişimini sadece muayenehane yönetimi açar; açıkken NSystem kutuya teknik olarak
  erişebilir (AVV).

## Testler

```bash
cd n-fizyo                       # proje klasörü (testler pakette yok)
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Sürüm öncesi: `bash tools/deps.sh audit` (pip-audit, `requirements.lock`'taki paketlerde bilinen açık var mı) ve
`ruff check --select F,E9 nfizyo tests tools` – ayrıntı `docs/GUVENLIK.md`. GitHub'da bunları `.github/workflows/tests.yml`
her push'ta kendisi yapar (Python 3.12 ve 3.13). Kurulum ve güncelleme paketleri sadece `requirements.lock`'tan (sha256 kontrolüyle) kurar.

719 test: muayenehaneler arası veri ayrımı (4 ekran + hasta uygulaması + fatura/HIN/import + kasa, sigorta cevapları,
Kostengutsprache, online randevu, dışa aktarma, takvim, MTT, video, boşalan randevu, firma faturaları, izinler, ev programı kütüphanesi),
firma faturaları, bexio, bağlantı testi, ekran ayrımı ve roller, tedavi ekibi ve vekâlet, mimari kuralları,
kilitli kayıtlar (fatura, makbuz, gün sonu), fatura akışı, XML 5.0 (resmî şema), QR-fatura, camt.054, hatırlatma, özel fatura ve KDV,
ödeme düzeltmeleri, çift kayıt birleştirme, kasa, sigorta cevabı okuma, online randevu (SMS kodu, sınırlar), e-posta, hasta bildirimleri,
izinler ve resmî tatiller, not şablonları, muhasebe yevmiyesi, tam veri dışa aktarma, iCal, 2FA, güvenlik (deneme sınırları, oturum,
SMS maskeleme, şifre özeti), SMS (ASPSMS/eCall), HIN, veri taşıma, merkez panel (imzalı rapor ve komutlar, imzalı sürümler, destek
erişimi – onay, süre, kayıt –, destek talepleri ve hasta verisi engeli, bakım duyuruları), yedek ve geri yükleme (dışarıda şifreli kopya
ve panel yedeği dahil), hasta bilgisi, hasta uygulaması, yapay zekâ, diller.

## Daha fazla

- Donanım ve kurulum yolları (kutu / küçük kutu + YZ sunucusu / her şey sunucuda): `docs/DONANIM.md`
- Yedekleme, geri yükleme, dışarıda şifreli kopya: `docs/YEDEK.md`
- Güvenlik kontrolleri: `docs/GUVENLIK.md`
- Mimari: `docs/MIMARI.md`
- Modüller: `docs/MODULLER.md`
- Yapay zekâ: `docs/AI.md`
- Lisanslar (Roboto, Material Symbols, react-body-highlighter verisi): `third_party/`
- PDF yazı tipi Liberation Sans (SIL OFL): `nfizyo/knowledge/fonts/`
- XML 5.0 şeması (Forum Datenaustausch, değiştirilmeden): `nfizyo/modules/billing/schemas/`
