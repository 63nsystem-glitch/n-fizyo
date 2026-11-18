# Kasa ve online randevu

## Kasa (Resepsiyon › Kasse)

Fiyatlar Yönetim › Einstellungen › **Preisliste**'den gelir.

**Verkauf (satış)**
1. Hasta seç (isteğe bağlı: ürün için gerekmez; **abonman** ve **«Auf Rechnung»** için gerekir). Hasta kaydındaki «Verkauf» düğmesi
   hastayı hazır seçer.
2. Kalemlerin adedini yaz, ödeme türünü seç: **Bar**, **Twint**, **Karte**, **Auf Rechnung**.
3. «Verkaufen»den önce kutuda **Total / Erhalten / Rückgeld** canlı görünür (statik `web/static/js/cashdesk.js`, CSP uyumlu;
   Enter tuşu satış yapmaz). Sadece gösterim: satışta sunucu her şeyi yeniden hesaplar. JavaScript yoksa «Total berechnen»
   düğmesi aynı hesabı sunucuda yapar (satış olmaz, girişler kalır).
4. Makbuz (Quittung) numarası `Q2026-0001` biçiminde. PDF; hastanın e-posta izni varsa «Per E-Mail».
5. «Auf Rechnung»: Abrechnung'da özel tarifeli **taslak fatura** oluşur → Yönetim kesinleştirir → QR-fatura.

**Abos (abonmanlar)**
- MTT abonmanı satılınca abonman açılır: ziyaret sayısı (veya sınırsız) ve bitiş tarihi.
- Hasta gelince «Besuch» → günde bir kez işaretlenir; yanlışlıkla işaretlendiyse aynı gün «Zurücknehmen».
- Durum: Gültig / Aufgebraucht / Abgelaufen → «Verlängern» ile yeni satış.
- Hasta kaydında «Kasse und Abos» kartı.

**Offene Rechnung bezahlen**: fatura numarasını gir → nakit/Twint/kart ile ödeme kaydedilir (gün sonuna girer). Nakit 5 Rappen'e
yuvarlanır (ekranda «Bar (auf 5 Rappen gerundet)»), fatura ödenmiş olur; fark kasada sayılır. Satışta da nakit toplam 5 Rappen
(makbuz: «Summe der Positionen», «Rundung auf 5 Rappen», «Total»). Twint ve kart yuvarlanmaz. `docs/FATURA.md` › «Ödeme türü».

**Quittungen**: günün makbuzları ve kasada ödenen faturalar; önceki günlere geçilebilir.

**Tagesabschluss (gün sonu)**
- Sistemin beklediği nakit + Twint + kart toplamları gösterilir. Kasadaki nakit (bozuk para fonu hariç) sayılır ve girilir → fark kaydedilir.
- Gün kapandıktan sonra o güne **satış ve iptal yapılamaz** (veritabanı kilidi). Düzeltme Yönetim üzerinden (aşağıda).
- Kapanmamış günler Yönetim genel bakışında «Tage ohne Kassenabschluss» olarak görünür.

**İptal (Storno)**: makbuz sayfasında, gerekçeyle, gün kapanmadan önce. Abonman da iptal olur. «Auf Rechnung» satışta taslak faturayı
ayrıca silmek gerekir. Makbuzlar silinmez, değiştirilemez.

**Gün sonundan sonra düzeltme (0.9.0, sadece Yönetim)** – Leitung › Kasse › makbuz › «Korrektur nach dem Tagesabschluss»:
- **Nachträglich stornieren** (gerekçe zorunlu): makbuz ve gün sonu değişmez; `cash_corrections` tablosuna **eklenir**
  (değiştirilemez, silinemez, sadece kapalı gün için – tetikleyici). «Geld heute zurückgegeben» işaretlenirse tutar **bugünün**
  kasasından düşer (bugün henüz kapanmamış olmalı); işaretsiz = kayıt hatası, para hareketi yok. Abonman iptal olur.
- **Zahlungsart korrigieren**: ör. nakit yerine kart; tutar bir ödeme türünden diğerine geçer.
- Kapalı günün raporunda (Tagesabschluss / Quittungen) «Korrekturen nach dem Abschluss» listesi ve «… nach Korrektur» toplamları.
  Makbuzda kırmızı/sarı not. Denetim kaydı (`correct`).
- Muhasebe: iptal = satışın karşı kaydı (gelir ve KDV geri, para hesabı alacak), ödeme türü = kasa ↔ geçiş hesabı aktarması;
  ikisi de düzeltmenin yapıldığı gün (para geri verildiyse o gün). Excel'de «Kasse Korrekturen» sayfası.
- Faturalarla kasada ödenen ödemeleri düzeltmek: Abrechnung'da ödeme iptali (bkz. `docs/FATURA.md`).
- 0.9.0: ödeme türü düzeltildikten sonra (ör. nakit → kart) yapılan iptal **yeni** türden düşer. Makbuz PDF'i de düzeltmeyi
  gösterir («STORNIERT nach dem Tagesabschluss …», «Zahlungsart korrigiert …»). Fatura sayfasından kapanmış güne nakit/Twint/kart
  kaydı yapılamaz (`docs/FATURA.md`).

## Online randevu

**Yönetim › Einstellungen › Online-Termine**
- Aç/kapat, kaç gün ileri (varsayılan 21), en az kaç saat önceden (12), saat ızgarası (15 dk).
- Online alınabilen **tedavi türleri** ve **terapistler**. Boş zamanlar terapistlerin çalışma saatlerinden (Einstellungen › Arbeitszeiten)
  randevular, bekleyen istekler, **resmî tatiller, muayenehane kapanışları ve terapistin izinleri** çıkarılarak hesaplanır.
- Yakında kapanış varsa (ör. yılbaşı tatili) hasta sayfasında «Die Praxis ist geschlossen: …» yazar (11 dil).
- İsteğe bağlı: terapist adlarını göster, kısa giriş metni (DE/FR).
- Web sitesi için link: `https://<kutunun-adresi>/p/termin/<kısa-ad>`. Kutunun **dışarıdan erişilebilir HTTPS adresi** olmalı
  (`NFIZYO_BASE_URL`), yoksa sayfa sadece muayenehane ağında açılır – bunu NSystem kurar.
  Aynı kural uygulama davetinde (QR/link) geçerli: adres https ve dışarıdan erişilebilir değilse (127.0.0.1, localhost,
  iç ağ IP'si, `.local`) davet sayfası bunu açıkça yazar; demoda «Demo: link sadece bu bilgisayarda çalışır» notu çıkar
  (`core.web.link_is_public`).
- Giriş metni ve tedavi türü adları diğer 9 dilde KI çevirisiyle gösterilir (orijinal açılır kutuda). Demo verisinde bu
  çeviriler hazır kayıtlıdır (motor `demo`).

**Hasta (11 dil)**
1. Tedavi türü → (birden çok şube varsa şube seçimi) → boş saat.
2. Ad, soyad, doğum tarihi, cep telefonu, isteğe bağlı e-posta (+ e-posta izni), sigorta, reçete var mı, kısa not, veri izni.
3. Telefona **SMS kodu** → kod girilir → istek gönderilir. Saat bekleyen istek için ayrılır (kodsuz en fazla 15 dakika).
4. Randevu ancak resepsiyon onaylayınca kesinleşir.

Korumalar: robot tuzağı (gizli alan), adres başına 15 dakikada en fazla 8 deneme, SMS kodu saatte en fazla 3 (aynı numara) / 6 (aynı adres),
kod 15 dakika geçerli ve 5 yanlış denemede kilit,
saat bilgisi imzalı (değiştirilemez), sağlık ayrıntısı istenmez.

**Resepsiyon › Anfragen**
- Bekleyen istekler (menüde sayı). Kişi tam tanınıyorsa (ad + soyad + doğum tarihi) «Bekannt: Nr. …» ve o kişi seçili gelir.
- **Kimin için?** Benzer kişiler önerilir (yazım hatası, ad/soyad yer değiştirmiş, aynı doğum tarihi, aynı cep numarası) –
  «Ähnliche Person gefunden»: resepsiyon birini seçmeden onaylayamaz. Ayrıca arama kutusundan **başka bir hasta** seçilebilir
  ya da bilerek **yeni kayıt** açılır. Böylece yazım hatası çift kayıt yaratmaz. Sadece kendi muayenehanesinin hastaları.
- **Bestätigen**: gerekirse hasta kaydı + randevu oluşur, terapist tedavi ekibine eklenir, hastaya SMS (izin varsa e-posta).
  Saat bu arada dolduysa ya da izin/tatil/kapanışa denk geliyorsa onaylanmaz, uyarı çıkar.
- **Ablehnen …**: ayrı bir pencere açılır (gerekçe, sadece dahili) → hastaya «lütfen arayın» SMS'i.
- Reddedilen/süresi dolan istekler 90 gün sonra silinir – arka planda **6 saatte bir** kontrol edilir (sadece açılışta değil).

## İzinler, kapanışlar ve resmî tatiller (Ajanda)

**Resepsiyon / Yönetim › Agenda › takvim-çarpı simgesi (Abwesenheiten)**
- Terapist izni: **Ferien, Krankheit, Weiterbildung, Andere**; tarih aralığı, bütün gün ya da her gün için saat aralığı
  (ör. 13:00–17:00), kısa not (**tıbbi bilgi yok**). Resepsiyon ve yönetim herkesinkini girer/düzenler/siler; terapist sadece
  kendisininkini görür (Therapie › Agenda › aynı simge). Her işlem denetim kaydına yazılır (not metni hariç).
- **Ganze Praxis** = muayenehane kapanışı (Betriebsferien, tek kapalı gün; saat aralığı da olur, ör. yerel yarım gün tatil).
- Kaydettikten sonra o zamana düşen **planlı randevular listelenir** → taşımak veya iptal etmek için.

**Yönetim › Einstellungen › Feiertage**
- Kanton: varsayılan muayenehane adresindeki kanton (Einstellungen › Praxis), yoksa BE; burada değiştirilebilir.
- Yıl yıl tatil listesi (sabit ve Paskalya'ya bağlı günler). Kaynak: Federal Adalet Dairesi (BJ) «Gesetzliche Feiertage in der Schweiz»
  (1.1.2011 durumu, https://www.bj.admin.ch/dam/bj/de/data/publiservice/service/zivilprozessrecht/kant-feiertage.pdf) – hem
  «gesetzlich» hem «wie Feiertag behandelt» günler; 1 Ağustos her yerde. Dipnotlar uygulanır: UR/AR/AI'da Noel pazartesi veya
  cuma ise Stephanstag yok; NE'de 2.1./26.12. sadece 1.1./25.12. pazar ise; SO'da 1 Mayıs 12:00'den sonra. Glarus Näfelser Fahrt
  (nisanın ilk perşembesi, Kutsal Hafta'ya denk gelirse bir hafta sonra), Cenevre Jeûne genevois (eylülün ilk pazarından sonraki
  perşembe), Vaud Bettagsmontag. Sadece bazı belediyelerde geçerli günler not ile gösterilir (SO Josefstag, NE Le Landeron
  Fête-Dieu varsayılan kapalı). Her gün tek tek **açılıp kapatılabilir**; yerel tatiller kapanış olarak girilir.
- Hesap: `nfizyo/modules/agenda/holidays.py` (testler: `tests/test_absences.py`, ge.ch/zh.ch/lausanne.ch/gl.ch tarihleriyle).

**Etkisi**
- Online randevu ve «boşalan randevuyu doldurma» bu zamanlarda **hiç teklif etmez** (gönderilmiş SMS teklifi sonradan girilen izne
  denk gelirse kabul edilmez).
- Ajanda gün/hafta görünümünde etiketli bant: «Ferien – A. Beispiel», «Karfreitag», «Praxis geschlossen – …»; gün görünümünün
  üstünde ve «Heute» sayfalarında uyarı, resepsiyonun «Heute»sünde «Heute abwesend: …».
- Elle randevu açarken veya taşırken uyarı + «Trotzdem speichern» (bilinçli geçme).
- Seri randevu bu günleri **atlar**, seriyi o kadar uzatır ve hangi günlerin atlandığını gösterir (önizleme + kayıttan sonra).

## Ajanda – küçük düzeltmeler (0.9.0)

- **Neuer Termin**: «Titel des Blockers» sadece «Kein Patient» işaretliyse görünür. Hastadan açılınca hastanın **ana terapisti**
  seçili gelir; tarih/saat = o kişinin bir sonraki boş zamanı (çalışma saati, randevu, izin ve tatil dikkate alınır).
- **Erledigt** randevuda sadece «Zurück auf geplant»; iptal/gelmedi/taşı/geldi yok (sunucu da izin vermez).
- **Serie**: hasta alanı tam etiketle dolu; «Anzahl Termine» = kalan reçeteli seans; fazlası uyarı ve «ohne Verordnung» işareti.
- Reçeteden fazla seans («10/9») her yerde kırmızı **«ohne Verordnung»** etiketiyle: ajanda bloğu, hastanın randevu listesi,
  günlük plan, randevu sayfası, hasta kartı.
- Yönetim › Agenda › Woche: varsayılan, o hafta randevusu olan ilk terapist; boşsa açıklama. Boş gün: «Heute niemand eingeplant.»
  sadece bugün için, diğer günlerde «An diesem Tag war/ist niemand eingeplant.»
- Einstellungen › Standorte: şube (ad, adres, telefon), oda ve cihaz **yeniden adlandırılır** (Bearbeiten); pasifleştirme vardı.

## Takvim aboneliği (terapist)

Terapi › Agenda › takvim simgesi › **Kalender-Abo**: link oluştur → telefonda/Outlook'ta abone ol. Son 30 ve gelecek 120 gün.
Varsayılan sadece baş harfler («Termin L. M.»), istenirse isimsiz. Teşhis/not yok. Link sadece bir kez gösterilir, istenince iptal edilir;
kullanıcı pasifleştirilince link çalışmaz.

## Tam veri dışa aktarma (Yönetim)

Einstellungen › Export › «Vollständiger Datenexport»: **şifre** tekrar girilir → ZIP (her tablo CSV + posta kutusundaki dokümanlar
+ hastaların kendi egzersiz videoları, şifre/anahtar yok, ses kayıtları yok). Kutuda **24 saat** kalır, sonra otomatik silinir; elle de silinebilir. Hasta verisi içerir –
güvenli saklanmalı. Her oluşturma ve indirme denetim kaydına yazılır.

## Hasta kaydı: reçete düzeltme, çift kayıtlar (0.9.0)

**Reçeteler** (Resepsiyon › hasta › Verordnungen): her reçeteye tıklanınca
- **düzelt** (seans, tarih, tür, doktor) – sadece yazım hatası için; eski değerler denetim kaydına yazılır;
- **durum**: aktif / aufgebraucht (kullanıldı) / abgelaufen (süresi doldu), isteğe bağlı gerekçe. Durum kendiliğinden değişmez
  (reçete takibi bitmiş reçete için hatırlatmaya devam etsin diye) – resepsiyon kapatır.
- Yeni reçetede **tür** seçilir (fizyoterapi, kompleks, ev ziyareti, lenf drenajı, grup).
- Eski yazılımda yapılmış seanslar (`sessions_before`, veri taşıma) artık reçete takibinde (kalan seans, 36 seans/12 ay) ve
  ajandadaki «Sitzung n von m» numarasında sayılır.

**Çift kayıtlar** (Leitung › Einstellungen › **Doppelte Einträge**, sadece Yönetim, tıbbi içerik yok – sadece ana bilgi ve sayılar):
- «Mögliche Doppel-Einträge»: aynı doğum tarihi + aynı ad/soyad (ya da yer değiştirmiş), veya aynı ad ve soyad (biri doğum tarihsiz).
  Arama ile iki kayıt seçilir: «Behalten» ve «Doppelt»; yan yana, her modülün bağlı veri sayılarıyla.
- **Silme**: sadece hiçbir bağlı verisi olmayan kayıt (yanlışlıkla açılmış). Kontrol her modülün kendi sayımıyla (`patient.linked`).
- **Birleştirme**: onay kutusu + onay penceresi, «geri alınamaz» uyarısı. Çift kaydın her şeyi (randevular, faturalar, makbuzlar,
  notlar, raporlar, uygulama …) kalan kayda geçer; kalan kayıttaki boş alanlar çiftten doldurulur; çift kayıt silinir. Tek
  işlem (hepsi ya da hiçbiri). Her modül **kendi tablolarını** `patient.merge` haberiyle taşır (`ModuleContext.patient_tables`);
  kilitli kayıtlar (kesin faturalar, raporlar, makbuzlar) sadece açık `patient_merges` satırı sürdükçe hasta numarası değiştirebilir.
  Denetim kaydı (`merge`) + `patient_merges` (özet). Aynı gün iki MTT ziyareti / egzersiz atlama / ölçüm paylaşımı / video onayı
  çakışırsa kalan kaydınki tutulur.
- Saklama notu artık muayenehanenin kantonunu gösterir (Einstellungen › Praxis), «Kanton Bern» sabit değil.

## Ajanda: yeni muayenehane ve izin gizliliği (0.9.0)

- Yeni randevu formu: şube, tedavi türü veya çalışma saati yoksa boş liste yerine bilgi kutusu; Yönetim rolündeki kişi için
  ilgili ayara link (`/leitung/einstellungen/standorte|behandlungen|zeiten`), diğerlerine «Yönetim ayarlar».
- Kurulum listesindeki «Standorte und Räume» artık oda da ister (`agenda.api.rooms`).
- İzin türü **«Krankheit»** (personelin sağlık bilgisi): ajanda bantlarında, uyarılarda, Heute'de ve resepsiyonun izin listesinde
  herkes **«Abwesend»** görür; sadece Yönetim ekranı «Krankheit» gösterir (bandın CSS sınıfı da nötr). Terapist kendi izinlerini görür.

## Ajanda, online randevu, veri taşıma: QA düzeltmeleri (0.9.0, bölüm C)

- **Tek sayım kuralı:** «kullanılmış seans» = eski yazılımda yapılmış (`sessions_before`) + erledigt + angemeldet + geçmişte kalmış,
  iptal/gelmedi olmayan randevular (henüz «erledigt» işaretlenmemiş olsa da). Ajanda numarası («10/9»), reçete takibi, seri planlayıcı,
  boşluk doldurma ve rapor ipucu aynı sayıyı gösterir.
- **Reçeteye bağlama:** online istekten ve veri taşımadan gelen randevular da hastanın aktif reçetesine bağlanır (elle randevu gibi).
- **Çift fatura koruması:** veri taşımada geçmiş, yapılmış randevular varsayılan olarak «eski programda zaten faturalandı»
  işaretiyle gelir (`appointments.billed_externally`, ajanda 006). Abrechnung › «Bereit» listesinde görünmezler; reçete
  sayımında ve «ilk tedavi» kuralında seans olarak sayılırlar. İşaret kaldırılırsa faturalanmak üzere listeye düşerler.
  Reçeteler randevulardan **sonra** taşınırsa bağlanmamış randevular sonradan bağlanır: gelecektekiler her zaman, geçmiştekiler sadece
  «bisher» (yapılmış seans) boşsa – yoksa aynı seanslar iki kez sayılırdı. Sonuçta «Mit Verordnung verbunden: n Termine».
- **Online istek onayı** ya hep ya hiç: saat bu arada doluysa hiçbir şey kaydedilmez (yeni hasta da), istek açık kalır.
  Seçilen/önerilen kişinin aynı gün başka randevusu varsa kartta «am selben Tag schon ein Termin: 08:15», onaydan sonra da uyarı.
- **Sınırlar:** IPv6 adresleri /64 ağına göre sayılır (adres değiştirerek sınır aşılamaz). Muayenehane başına **girişsiz SMS** (randevu
  kodu + uygulama giriş kodu) saatte en fazla 30, günde en fazla 100 (`core/sms.py` `ANON_SMS_PER_HOUR/DAY`); dolunca hasta
  «später wieder versuchen oder anrufen» görür, sunucu kaydına bir uyarı düşer.
- **Çeviri işleri:** herkese açık sayfa her metin+dil için en fazla bir çeviri işi açar; boş sonuçta bekleme 15 dk, 1 sa, 4 sa,
  16 sa, en çok 1 gün; adres başına saatte en fazla 3 yeni iş (dikte işleri artık bunların arkasında beklemez).
- **Arşivleme:** hastanın gelecekte randevusu varsa onay sayfası randevuları listeler; «absagen» (varsayılan) ve isteğe bağlı
  «per SMS informieren» (hastanın dilinde). Arşivlenmiş hastaya hatırlatma SMS'i gitmez.
- **Hasta numarası ve kimliği asla tekrar verilmez** (birleştirme/silme sonrası da): çekirdek göç 012, `practices.patient_id_last`
  ve `patient_number_last`; eski kimliği tekrar kullanmaya çalışan kayıt tetikleyici tarafından reddedilir.
- **Telefon:** başında 0 olmayan İsviçre numarası («79 123 45 67», Excel'den «791234567») +41 olarak okunur ve «079 …» diye
  kaydedilir; yurt dışı sadece + veya 00 ile. İmkânsız numara formda hata; veri taşımada «Telefonnummer ungültig» (telefon olarak
  kaydedilmez, nota yazılır), tahmin edilen numara ve yabancı numara için uyarı.
- **Excel taşıma:** dosya açılmadan önce kontrol edilir (açılmış hali en çok 100 MB, hücre en çok 32 767 karakter, en çok
  50 000 × 80 hücre, şüpheli sıkıştırma reddedilir); okuma ayrı iş parçacığında, sunucu donmaz.
- Saçma sayılar (çok uzun, negatif, var olmayan seçim) formda hata ya da boş değer olur, asla 500 değil (bekleme listesi,
  Online-Termine ayarları, yeni randevu süresi 5–600 dk, hasta formu).

