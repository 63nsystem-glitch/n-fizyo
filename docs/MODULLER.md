# Modüller

Klasör: `nfizyo/modules/`. Yönetim bunları «Leitung › Einstellungen › Module» altından açıp kapatabilir
(Hastalar, Ajanda, Notlar, Genel bakış ve Ayarlar her zaman açık).

| Modül | Klasör | Terapi | Resepsiyon | Yönetim | Hasta | Ne yapar |
|---|---|---|---|---|---|---|
| Hastalar | `patients/` | dosya: vücut haritası, bulgular, kısıtlamalar, ölçümler | kayıt, ana bilgiler, reçete (**düzelt, kapat, tür**), tedavi ekibi | **çift kayıtları birleştir / boş kaydı sil** | – | `docs/KASA_RANDEVU.md` |
| Anatomi | `anatomy/` | atlas, yapı sayfası (hastanın dilinde); kendi metni olmayan kemik/eklemde komşu yapılar (eklemin kemikleri, kemiğin eklemleri); çene eklemi iskelette ayrı nokta | – | – | – | uzman kontrolü bekliyor (sayfada açıkça yazar) |
| Ajanda | `agenda/` | kendi günü, haftası, kendi hastasına randevu, **takvim aboneliği (iCal)**, **kendi izinleri** | herkesin ajandası, gelenler listesi, seri, bekleme listesi, **boşalan randevuyu SMS ile doldurma**, **izinler ve kapanışlar** | ajanda (bakış), **izinler ve kapanışlar**, şube/oda/cihaz (**yeniden adlandırma**), tedavi türleri, çalışma saatleri, **resmî tatiller (kanton)** | randevularım, randevu isteği, **kapanış bilgisi** | hatırlatma SMS ve/veya e-posta; tatil/izin/kapanışta online randevu yok: `docs/KASA_RANDEVU.md` |
| Notlar | `documentation/` | sürümlü notlar, onay, ek not, taslaklarım, **kendi not şablonları** | – | açık iş (liste), **muayenehane not şablonları** | – | |
| Diktat | `dictation/` | telefonda kayıt (çevrimdışı), ses→yazı, not taslağı | – | kullanım sayısı | – | |
| **Hasta bilgisi** (yeni) | `patientinfo/` | diktattan öneri: «evde ne yapmalı», «ne zaman haber vermeli» → onay | – | – | kendi dilinde okur ve **sesli dinler** | çeviri yerel, ses telefonun kendi sesi |
| Posta kutusu | `inbox/` | kendi hastalarının raporları, öneriler, onay | gelen dokümanı hastaya atar (**içeriği görmeden**) | açık iş | – | tarama klasörü (`<veri>/scan-eingang/<kısa ad>/`; alınan dosya 14 gün `verarbeitet/` altında kalır, sonra silinir) + **HIN postası** |
| Raporlar | `reports/` | doktora rapor: taslak, düzenle, kesinleştir, yazdır, **PDF, HIN ile gönder** | – | açık iş | – | |
| **Reçete takibi** (yeni) | `rxwatch/` | kendi hastalarının reçeteleri: kalan seans, 5 hafta, 36 seans, eksik alan; «istendi» işareti; devam reçetesi mektubu | tüm hastalar (tanısız), «istendi» işareti, menüde rozet | – | – | kurallar: `nfizyo/modules/rxwatch/rules.py` (sadece hatırlatır) |
| **Kısa bakış ve devir** (yeni) | `briefing/` | «Heute»da sıradaki hasta, her randevuda «Kurzüberblick», dosyada devir özeti / «siz yokken» | – | – | – | tablo yok, YZ yok, her bilgi kaynaklı |
| **MTT salonu** (yeni) | `mtt/` | aletler + QR, plan (set/tekrar/kg/ayar), canlı salon, artış önerisi (onaylanırsa) | – | – | QR ile antrenman, set kaydı, abonman girişi | kurallar: `nfizyo/modules/mtt/rules.py` (sadece öneri) |
| **Görüntülü görüşme** (yeni) | `video/` | randevuyu videolu yap, SMS linki, «Video starten» (Heute'de de) | videolu yap, SMS linki | – | onay + katıl (SMS linki veya uygulama kartı) | kendi Jitsi sunucusu: `docs/VIDEO.md` |
| Ev programı | `homeprogram/` | egzersiz kütüphanesi (**düzenle, pasif yap / geri al, çizim seç, süre**), hastaya program, **kendi video (onaylı), yıldız, yapamama sebepleri** | – | – | egzersizler (kendi dilinde, **her egzersizin kendi çizimi**, dozaj düz cümle, **sesli okuma**, kendi video), «bugün yapamadım» | çizimler: `tools/gen_exercise_drawings.py` |
| Mesajlar | `messages/` | kendi hastalarıyla mesaj (listede son mesaj + saat), «sorun bildir» | – | kullanım | mesaj, sorun bildir, muayenehanenin cevap süresi (11 dil) | iki yönlü çeviri |
| Şikâyet | `intake/` | hastanın anlattığı şikâyet, bulguya aktar | – | kullanım | şikâyetini kendi dilinde anlatır (yazı/ses) | kiosk kaldırıldı |
| **Bildirimler** (yeni, 0.9.0) | `notify/` | mesaj/bilgi ekranında «hasta bildirildi» | – | Einstellungen › Benachrichtigungen | yeni mesaj, yeni bilgi (isteğe bağlı yeni randevu) için kısa e-posta/SMS | içeriksiz, 15 dk'da bir, gece SMS yok |
| Hasta uygulaması | `patientapp/` | QR davet | QR davet | kullanım | giriş, ana sayfa, profil | |
| Fatura | `billing/` | – | taslak (tarife veya **fiyat listesi**), PDF (QR), ödeme gir (banka/nakit/Twint/kart), **eşlemeyi kaldır, banka satırını gizle**, camt.054, hatırlatma, **sigorta cevapları**, **Kostengutsprache**, **e-postayla fatura** | kesinleştir, iptal, **ödeme iptali, iade**, XML 5.0 / MediData (**«unklar» işaretleme**, **simülatöre test faturası, sertifika durumu**, **katılımcı listesi, durum, otomatik cevap ve bildirim** – V5.2.0), **red → iptal + yeni taslak**, Tarife (**sigorta grubuna göre puan değeri**), Zahlung (+KDV no.), **Preisliste** | – | `docs/FATURA.md` |
| **Kasa** (yeni) | `cashdesk/` | – | satış (ürün, abonman, hizmet; nakit/Twint/kart/faturaya; **canlı toplam ve para üstü**), makbuz PDF (+e-posta), abonman ziyareti, açık faturayı ödeme, gün sonu | kasa, gün sonu listesi, «gün sonu eksik» uyarısı, **gün sonundan sonra düzeltme** | – | `docs/KASA_RANDEVU.md` |
| **Online randevu** (yeni) | `booking/` | – | «Anfragen»: **mevcut hastayı seç (benzer kişi önerisi) veya yeni kayıt**, onayla / reddet (ayrı pencere; SMS + izinle e-posta) | Einstellungen › Online-Termine (aç/kapat, türler, kişiler, link) | herkese açık sayfa, SMS kodu, 11 dil | |
| **Firma müşterileri** (yeni, 0.8.0) | `firms/` | – | firmalar, fatura taslağı (fiyat listesi veya serbest), PDF (QR), e-posta, ödeme, hatırlatma | + kesinleştir, iptal; «Firmenrechnungen überfällig» | – | numara F2026-0001; banka dosyasındaki ödemeler otomatik eşlenir; hasta verisi yok |
| **Dışa aktarma** (yeni) | `exports/` | – | – | Einstellungen › Export: yevmiye CSV, Excel, hesap planı, tüm veri ZIP, **bexio'ya gönderme** (0.8.0) | – | `docs/VERBINDUNGSTEST.md` |
| **Veri taşıma** (yeni) | `dataimport/` | – | – | Excel/CSV: hastalar, sigortalar, doktorlar, randevular, **reçeteler (yapılmış seanslarla)**, **açık kalemler** | – | deneme → aktarım, dosya sonra silinir |
| Genel bakış | `stats/` | – | – | rakamlar, grafik, doluluk, açık işler, diller, YZ performansı | – | |
| **Yardım** (yeni, 0.9.0) | `support/` | Hilfe › Support anfragen (kendi talepleri, cevap) | aynı | aynı + muayenehanenin bütün talepleri, «Supportzugang gleich öffnen» | – | hasta verisi engeli `core/patientdata.py`; veri çekirdekte (`core/nsystem.py`) |
| Ayarlar | `settings/` | – | – | ekip ve roller (+2FA sıfırlama), vekâlet, muayenehane (+ uygulamadaki cevap süresi), **sigortalar**, **SMS**, **HIN**, doktorlar, YZ, modüller, **NSystem** (**destek erişimi aç/kapat, kayıt**), **kurulum listesi** | – | |

Her modülün sürümü: `python3 -m nfizyo versions`

## Kaldırılanlar (karar Ekim 2026)

- Tablette canlı tercüman ve tabletle form doldurma (kiosk) – yerine hasta uygulaması ve hasta bilgisi.
- Tek karışık ekran – yerine 4 ayrı ekran.
- NSystem yönetici hesabı uygulamanın içinde – yerine ayrı merkez panel (`nfizyo/central/`, bkz. `docs/MERKEZ.md`).

## Çok dillilik

- Personel: Almanca, Fransızca.
- Hasta: Almanca, Fransızca, İtalyanca, İngilizce, Portekizce, İspanyolca, Arnavutça, Boşnakça/Hırvatça/Sırpça, Türkçe, Arapça, Ukraynaca.
- Makine çevirisi her zaman etiketli ve orijinali açılabilir.

## Fatura (Kasım 2026)

- KVG: tarife yapısı 312 geçerli; puan değerini muayenehane girer (Leitung › Einstellungen › Tarife). 7301 (48 TP), 7311 (77 TP).
- UV/MV/IV: 1.7.2025'ten beri 5 dakikalık birimler (25.110 = 10.03 TP), puan değeri CHF 1.00.
  **1.10.2026'dan beri dış faktör 0,94** (tarifenin %6 düzeltmesi) – N-Fizyo tedavi tarihine göre kendisi uygular.
- XML'de tarife kodu: KVG = 312, UV/MV/IV = 311.
- Yeni KVG yapısı (27xxx) en erken 1.1.2027 – **7.11.2026 itibarıyla hâlâ onaysız**, resmî katalog yok; durum, kaynaklar ve onay
  gelince girilecekler: `docs/FATURA.md` › «Yeni KVG tarif yapısı».
- Katalog ve kaynaklar: `nfizyo/knowledge/tariffs.py`

## Sigorta listesi (0.9.0 araştırması)

GLN numaralı, herkese açık resmî bir sigorta listesi bulunamadı. BAG'ın «zugelassene Krankenversicherer» listesinde
(https://www.priminfo.admin.ch/downloads/zugelassene-krankenversicherer-2026-01-01.pdf) BAG numarası ve UID var, GLN yok.
GLN dizinleri (ör. HCI Solutions INDEX) ücretli/kısıtlı. Bu yüzden katalog eklenmedi: sigortalar eski yazılımdan CSV/Excel ile
(Yönetim › Import) veya tek tek (Einstellungen › Versicherer) girilir.

### Ev programı: kendi video her telefonda oynar (4. tur)

iPhone videoları (.mov, çoğu HEVC) Android telefonlarda ve Firefox/Chrome'da oynamaz. Yüklenen her kendi video arka planda
ffmpeg ile MP4'e (H.264/AAC, uzun kenar en fazla 1280 px, `-preset veryfast -crf 26`, hızlı başlama) çevrilir; orijinal silinir.
Çevirme başarısız olursa orijinal kalır. Bu sırada başka bir video yüklenir veya silinirse çevrilen dosya atılır.
