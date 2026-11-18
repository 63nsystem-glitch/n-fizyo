# Fatura: QR-fatura, XML 5.0, MediData, ödeme, hatırlatma, özel ödeme, sigorta cevapları

Kod: `nfizyo/modules/billing/`

## Akış

1. **Resepsiyon** › Abrechnung › «Bereit»: tamamlanmış randevulardan taslak. Sayfa **içeriği olan ilk sekmede** açılır
   (genelde «Bereit»; sonra Entwürfe, Offen, Zahlungen, Antworten); her sekmede sayı görünür.
2. **Yönetim** kontrol eder, «Abschliessen» → numara, IBAN, ödeme referansı ve vade sabitlenir (sonra değişmez).
3. **PDF** (her iki ekranda): fatura + son sayfada **QR ödeme kısmı** (Tiers garant). Tiers payant'ta hastaya kopya, QR yok.
   Eski tarayıcı baskı görünümü (`/rechnungen/<id>/druck`, hiçbir yerden link yoktu, QR ve KDV'siz) 0.9.0'da kaldırıldı – belge PDF'tir.
4. **Elektronik** (sadece Yönetim): XML 5.0 üret → resmî şemaya göre kontrol → indir veya MediData'ya gönder.
   - Tiers payant (UVG/IVG/MVG her zaman): fatura sigortaya.
   - Tiers garant: isteğe bağlı **kopya** sigortaya (Einstellungen › Zahlung).
   - İptal edilmiş fatura: «Storno».
5. **Ödeme:** Abrechnung › «Zahlungen» → bankadan camt.054 (veya camt.053) yükle. QR-referansı tutanlar otomatik eşlenir,
   tutmayanlar «ohne Zuordnung» listesinde fatura numarasıyla elle eşlenir. Aynı dosya iki kez → fark edilir.
   Nakit/Twint: fatura sayfasında «Zahlung von Hand erfassen». Ödemeleri düzeltmek: aşağıda «Ödeme düzeltme (0.9.0)».
6. **Hatırlatma:** «Offene Posten». Vade geçince 1. hatırlatma (ücretsiz), sonra 2. ve 3. (ücret ayarlardan). Her biri PDF + QR.
   Tiers payant'ta hatırlatma XML olarak da gönderilebilir.

## Ayarlar (Yönetim › Einstellungen)

| Sekme | Ne |
|---|---|
| Praxis | Sokak, no, PLZ, şehir, **GLN**, ZSR (QR ve XML için şart) |
| Zahlung | **IBAN veya QR-IBAN**, ödeme süresi, hatırlatma süresi ve ücretleri, XML modu (test/produktiv), Tiers-garant kopyası |
| Versicherer | sigorta adı, kanun, kimin ödediği, **GLN, adres** (XML için şart) |
| Team | her terapistin **GLN** (ve ZSR) |
| Tarife | KVG puan değeri |

Hastada XML için: doğum tarihi, cinsiyet, adres, **AHV numarası** (resepsiyon formunda; kontrol hanesiyle denetlenir), UVG'de hasar no.
Eksik bir şey varsa fatura sayfası «Für die elektronische Rechnung fehlt noch: …» diye neyin eksik olduğunu listeler.
GLN ve ZSR mektuptaki gibi yazılabilir («7601 0012 3456 7», «H 1234.56», küçük harf): kaydederken düzeltilir; yanlış
biçim kaydedilmez, açık bir mesaj çıkar. KVG faturasında muayenehanenin ZSR'si yoksa «fehlt noch» listesinde görünür (kasa ödemez).
Sadece boşluktan oluşan adres/ad alanları da «eksik» sayılır; geçersiz e-posta XML'e yazılmaz – XML gönderirken
«Schema verletzt» hatası artık ana verilerden çıkmaz (4. turda 26 durum fuzz testiyle kapatıldı).
QR ödeme kısmında uzun muayenehane/hasta adları ve sokaklar sütunda kırılır; makbuzda gerekirse küçülür (en az 6 pt), QR koduna
ve sayfa kenarına taşmaz.

## Referanslar

- QR-IBAN (banka verir, 30000–31999) → 27 haneli QR referansı (mod 10 özyinelemeli).
- Normal IBAN → «RF…» referansı (ISO 11649).
- Referanstan fatura bulunur; kontrol hanesi yanlışsa eşleşmez.

## Tutarlar

`Tutar = adet × TP × puan değeri × dış faktör`. UV/MV/IV'de 1.10.2026'dan beri dış faktör 0,94 (tedavi tarihine göre).
XML'de `tariff_type`: KVG 312, UV/MV/IV 311.

## KVG tarif yapısı 312 (0.7.0'dan itibaren tam)

Kaynak: «Tarifstruktur für physiotherapeutische Leistungen, gültig ab 1. Januar 2018» (Tarifcode 312), `nfizyo/knowledge/tariffs.py`.

| Ziffer | Anlamı | TP |
|---|---|---|
| 7301 / 7311 | genel / zor fizyoterapi (seans) | 48 / 77 |
| 7313 | hipoterapi (seans) | 77 |
| 7330 | grup tedavisi 2–5 kişi (seans) | 25 |
| 7340 | MTT (seans) – talimat için toplamda en çok 2× 7301 | 22 |
| 7350 | ilk tedavi ek ücreti | 24 |
| 7351 | 6 yaşa kadar kronik engelli çocuk (sadece 7311 ile) | 30 |
| 7352 / 7353 | havuz / hipoterapi altyapısı | 19 / 67 |
| 7354 | ev ziyareti yol/zaman (doktor ev tedavisi yazdıysa) | 34 |
| 7361 | tedavi malzemesi – alış fiyatı CHF (kategori + miktar yazılır) | CHF |
| 7362 / 7363 | vajinal / anal sonda (yılda bir, ikisi birden değil) | CHF 50 / 90 |

- **Otomatik 7350:** faturadaki ilk seans için, hasta son 6 ayda hiç seans almadıysa (7301/7311/7313 ile). Nüks durumunda elle eklenir.
- **Kurallar faturada kontrol edilir:** izin verilmeyen kombinasyon (ör. 7350 + 7340), 6 yaş üstünde 7351, yılda ikinci sonda → kesinleştirme engellenir.
  Aynı gün iki seans, 36 seans içinde ikinci 7350, KVG'de video seansı → bilgi notu (engellemez).
- **CHF pozisyonları** (7361–7363) puan değerinden bağımsızdır; XML'de `unit` = tutar, `unit_factor` = 1.
- **Tedavi türleri** (Einstellungen › Behandlungsarten) sadece seans pozisyonlarından (7301–7340) seçilir.

## Tarif değişimi (yeni KVG yapısı, en erken 1.1.2027)

Fatura satırının tarifi **tedavi gününe** göre seçilir. Yeni yapı onaylanıp resmi liste yayınlanınca `tariffs.py`'ye yeni tarif
(`valid_from`) ve pozisyonları eklenir, `kvg312`'ye `valid_until` yazılır. Sınırı aşan seanslar otomatik olarak **iki ayrı faturaya**
bölünür. 5 dakikalık birimler zaten destekleniyor (UV/MV/IV gibi `default_code`). Test: `tests/test_tariff_rules.py`.

## Yeni KVG tarif yapısı – durum 7.11.2026

**Henüz onaylanmadı, resmî katalog yok → N-Fizyo'da girilmedi. 1.1.2027'de de 312 ile faturalanır, ta ki onay gelene kadar.**

| Tarih | Ne oldu | Kaynak |
|---|---|---|
| 2.4.2026 | physioswiss, H+ ve prio.swiss yeni yapıyı Bundesrat'a onaya sundu | BAG, «Änderung der Verordnung über die Festlegung und die Anpassung von Tarifstrukturen in der Krankenversicherung: physiotherapeutische Leistungen» («Genehmigungsgesuch eingereicht»; süreç bitene kadar BAG bilgi vermiyor) |
| 1.6.2026 | sunulan yapı yayınlandı: «Tarifstruktur für physiotherapeutische Leistungen, ambulant, KVG, gültig ab 1. Januar 2027» (Anhang 1a) + Merkblatt | physioswiss.ch › Tarif › «Die eingereichte KVG-Tarifstruktur im Überblick» |
| 12.10.2026 | aynı sayfa: «Seit Anfang April 2026 läuft das Genehmigungsverfahren beim BAG», «frühestens per 1. Januar 2027», «Der definitive Zeitpunkt steht aktuell noch nicht fest» | aynı yer (son güncelleme) |
| 27.10.2026 | «Für die Physiotherapie ist ab 2027 eine neue Tarifstruktur vorgesehen» – onay haberi yok | physioswiss haber «Klarstellung zur neuen Tarifstruktur» |
| 7.11.2026 | BAG «Leistungen: Physiotherapie» sayfasında 1.1.2026'dan beri geçerli yapı = 2018 yapısı; Bundesrat/BAG onay bülteni bulunamadı | bag.admin.ch, admin.ch (kontrol 7.11.2026) |

**Sunulan sürüm (bilgi için, onaylanırken değişebilir – veri olarak girilmedi):** her şey 5 dakikalık birimlerle, pozisyonlar
27xxx (73xx kalkar). 27100 «Marker» her tedavi pozisyonuyla birlikte yazılır, seans sayılmaz. Örnekler: 27110 muayenehanede
bireysel tedavi 7,36 TP / 5 dk (en çok 50 dk), 27120 12 yaşa kadar çocuk, 27210 evde, 27310/27320 MTT, 27410 grup (bölen yöntemi),
27610 görüntülü 7,19 TP / 5 dk (en çok 30 dk), 27710 hasta yokken yapılan iş, 27810 yol (km), 27820/27830 ve 27910–27960 sabit CHF,
27840 ikinci fizyoterapist, 27001–27006 düşme önleme. Puan değeri ayrıca müzakere edilir (Merkblatt: «Der Taxpunktwert wird in den
Taxpunktwertverhandlungen KVG separat vereinbart»). Geçiş kuralı (physioswiss FAQ): «Es ist jeweils diejenige Tarifstruktur zu
verwenden, welche am Tag der Leistungserbringung gültig ist» – bir fatura iki yapıyı birlikte de içerebilir; N-Fizyo'nun sınırda
iki faturaya bölmesi de buna uygun.

**Onay gelince girilecekler (sırayla):**

1. Kaynak: sadece Bundesrat'ın onayladığı ve resmî yayınlanan metin (Fedlex / BAG, «gültig ab …»). physioswiss'in sunduğu PDF değil.
2. `nfizyo/knowledge/tariffs.py`:
   - `TARIFFS["kvg2027"] = Tariff("kvg2027", ("KVG",), <DE adı>, <FR adı>, valid_from="<karardaki tarih>", tp_value=None,
     source=<belge adı + tarih>, code="<XML tariff_type>", default_code="27110", remote_code="27610")`
   - `kvg312`'ye `valid_until="<bir önceki gün>"`.
   - Her pozisyon için `Position("kvg2027", kod, DE, FR, TP, unit, note=<en uzun süre, sınırlar>)`; unit: `5min`, `session`
     (MTT antrenmanı), `km` (yol), `chf` (sabit frank tutarları), `chf_var` (gerekirse malzeme).
3. XML `tariff_type` kodu: yeni yapı için Forum Datenaustausch / sigortaların vereceği kod (312 mi kalıyor, yeni kod mu – henüz açıklanmadı).
4. Kod tarafı hazır (13.11.2026) – onay gelince sadece veri girilir:
   - `marker_code="27100"`: her tedaviye otomatik, puansız kennzeichnung satırı.
   - `session_map`: Behandlungsart'ın eski kodu → yeni pozisyon (örn. 7340 MTT → 27320, 7330 grup → 27410). Behandlungsart'lar
     değiştirilmez; eşlemesi olmayan → `default_code` (27110). Çocuk/ev/havuz gibi ayrı pozisyonlar için Behandlungsart'a
     uygun eski kod ya da faturada elle değişiklik.
   - `Position(max_minutes=…)`: süre sınırı (örn. 27110: 50) – fazlası faturalanmaz, faturada uyarı çıkar.
   - `Position(divided=True)`: grup, bölen yöntemi (aynı başlangıç + aynı terapist + aynı Behandlungsart = katılımcı sayısı).
   - Puan değeri sayfası bütün KVG tarifleri için (tarif seçimi), Abrechnung'daki gösterim o günkü KVG tarifine göre.
   - Test: `test_new_kvg_structure_ready_as_data` (deneme tarifiyle: sınırda bölme, 27100, 50 dk sınırı, MTT eşlemesi).
   - Hâlâ elle: 27710/27720 «9 seansta en çok 60/75 dk» (hasta yokken yapılan iş) – faturaya elle eklenir, sınır uyarısı yok.
5. Muayenehane: Einstellungen › Tarife'de yeni yapının puan değerlerini (gruplara göre, geçerlilik tarihiyle) girer; 312 değerleri
   eski seanslar için kalır.
6. Testler: katalog kaynağa karşı (`test_kvg_312_catalog_matches_the_tariff_structure` gibi), sınırda bölme, XML şeması.

## UV/MV/IV: Behandlungsart → pozisyon (13.11.2026)
Önceden her UV/MV/IV tedavisi 25.110 (5 dk) olarak çıkıyordu. Şimdi Behandlungsart'a göre: 7340 MTT → 25.520 (seans başı),
7313 Hippoterapi → 25.120, 7330 grup → 25.410 (bölen yöntemi, en çok 75 dk; physioswiss «Gut zu wissen 16», 20.11.2025),
diğerleri 25.110. Grup faturasında «katılımcı sayısını kontrol et» uyarısı çıkar.

## Görüntülü seans

- UV/MV/IV: görüntülü gerçekleşen randevu (birisi odaya girdiyse) otomatik **25.310** «Patientenbehandlung auf räumliche Distanz», 5 dk başına.
- KVG: kendi pozisyonu yok → normal seans pozisyonu kalır, faturada «sigortayla netleştirin» notu çıkar.

## MediData (Leistungsabrechnung web servisi)

Temel: MediData «Leistungsabrechnung – Schnittstellendokumentation» **V5.2.0** (02.09.2026), «B_ClientSchnittstellen» V1.5,
«Best Practice Webservice» V1.2 (V5.2.0 içinde V1.3), Prozessbeschrieb V3.7, Simulator 2.5, CSR V1.0, X-Client-ID V1.1.
Bu belgelere göre uygulandı (`medidata.py` › `CONFIRMED = True`); eski «üçüncü kaynak» uyarısı kalktı. Kurulum, adresler, `.env`,
sertifika ve simülatör: `docs/VERBINDUNGSTEST.md` › MediData.

`/etc/n-fizyo/n-fizyo.env` (kısa):

```
NFIZYO_MEDIDATA=test                                              # off = sadece XML üret · test · production
NFIZYO_MEDIDATA_URL=https://services-acc.medidata.ch/md/srv/id0   # üretim: https://services.medidata.ch/md/srv/id<N>
NFIZYO_MEDIDATA_CLIENT_ID=1000xxxxxx_1200yyyyyy
NFIZYO_MEDIDATA_USER=…
NFIZYO_MEDIDATA_PASSWORD=…
NFIZYO_MEDIDATA_CERT=/etc/n-fizyo/medidata/client.crt
NFIZYO_MEDIDATA_KEY=/etc/n-fizyo/medidata/client.key            # hakları 600, sahibi nfizyo – yoksa kullanılmaz
# NFIZYO_MEDIDATA_CA=…          # sadece sunucu sertifikası tanınmıyorsa (doğrulama asla kapanmaz)
# NFIZYO_MEDIDATA_PRACTICE=…    # kutuda birden çok muayenehane: MediData erişimi hangisinin (kısa ad)
```

Her istek: client sertifikası (karşılıklı TLS) + `Authorization: Basic` + `X-CLIENT-ID`. Kutu/VA: `https://<kutu>:8100` (`/md`
eklenir, sertifika isteğe bağlı).

### Gönderim (3.7)

- `POST {URL}/ela/uploads`, `multipart/form-data`: `elauploadinfo` (info.json, JSON) + `elauploadstream` (XML, octet-stream),
  `Expect: 100-continue`. Cevap 201 `{"transmissionReference": "…"}` → faturada saklanır.
- `elauploadinfo`: `modus` (XML ile aynı), `toPatient: false` (hastaya kopyayı N-Fizyo kendisi verir; XML'de de
  `print_copy_to_guarantor` yok = false), `documentReference` = gönderimin guid'i (bildirimleri eşlemek için),
  `correlationReference` = fatura numarası (cevaplar da taşır). `toOrganization`, `toTrustCenter`, baskı alanları gönderilmez:
  XML'deki `to` ve müşteri portalı ayarı geçerli.
- XML'de `to` = katılımcı listesindeki **`glnReceiver`** (sigortanın GLN'sinden farklı olabilir).
- Sonuç:
  - **gesendet**: 201; ya da 409 (aynı gönderim son 1 dakikada zaten ulaştı – ilkinin referansı alınır, çift olmaz).
  - **Fehler** (kesin red, düzeltip tekrar gönderilebilir): 4xx (400 hata kodlarıyla, 401/403 erişim, 408, 413, 429), 3xx,
    hiç bağlanamama, kullanılamayan client sertifikası. MediData hata kodu (ör. `UPLOAD:UNKNOWN-RECEIVER-ORGANIZATION`) Almanca/
    Fransızca açıklamayla faturada görünür.
  - **unklar**: zaman aşımı, gönderim sırasında kopan bağlantı, 5xx, referanssız 2xx. Aşağıya bak.
- **Gönderimden önce** katılımcı listesi kontrol edilir. Sigorta listede yoksa, XML 5.0'ı kabul etmiyorsa (`notSupported`),
  Tiers garant'ı kabul etmiyorsa (`tgAllowed`) veya dosya `maxReceive`'den büyükse: faturada açık uyarı, «Senden» düğmesi yok,
  istek de reddedilir (hiçbir şey gitmez). Liste henüz yüklenmediyse sarı uyarı («Empfänger nicht geprüft»).
  `additionalCosts` olan sigortada «Zusatzkosten möglich» notu.

### Katılımcı listesi (3.6)

- 24 saatte bir `GET …/ela/participants` (sayfa başı 1000, `Link: rel="next"` bitene kadar; link yoksa `offset`).
  `lawtype` filtresi belgeye göre sigortalar için; muayenehane hepsini alır, kanuna göre N-Fizyo içinde ayrılır.
- Muayenehane başına `medidata_participants` tablosunda (hata olursa eski liste kalır).
- Yönetim › Einstellungen › **Versicherer**: her sigortanın yanında «MediData» / «MediData ohne XML 5.0» / «nicht bei MediData»,
  açınca alıcı GLN, XML 5.0/4.5, Tiers garant, ek maliyet. Listede ad/GLN ile arama ve «Als Versicherer übernehmen».

### Durum takibi (3.8)

- 30 dakikada bir, gönderilen her fatura için `GET …/ela/uploads/{ref}/status`; DONE veya ERROR olunca durur (en çok 90 gün).
  PROCESSING/ERROR'da `…/status/detail` (alıcı başına: sigorta, trustcenter, hasta).
- Faturada: «MediData: in Bearbeitung / zugestellt / Fehler» + sebep; Abrechnung › Abgeschlossen listesinde de.

**Sonuç «unklar»:** fatura belki ulaştı. Durum «Unklar – bitte prüfen»; **otomatik veya ikinci kez gönderilmez** (aynı tür için
«Senden» kapalı, sorgular asla göndermez). Kendiliğinden «gesendet» olur, eğer: MediData bildirimi gönderimin
`documentReference`/`transmissionReference`'ını taşırsa, durum sorgusu gönderimi tanırsa veya sigortanın cevabı (aynı fatura,
`correlationReference`) gelirse. Yoksa Yönetim MediData portalında bakar ve işaretler: «Ist angekommen» veya «Nicht angekommen –
neu senden erlauben». Zaten gönderilmiş bir tür için «Nochmals senden» bilinçli bir tıklamadır. bexio aktarımıyla aynı mantık.

### Cevaplar ve bildirimler (3.9, 3.10)

- 30 dakikada bir `GET …/ela/downloads` → her belge `GET …/ela/downloads/{ref}` (`Accept: application/octet-stream`) →
  **tek işlemde** `medidata_downloads` + mevcut cevap içe aktarımı (`invoice_responses`, kaynak «medidata») → **sonra**
  `PUT …/status {"status": "CONFIRMED"}`. Onay başarısız olursa belge tekrar gelir; zaten kayıtlı olduğu için tekrar indirilmez
  ve iki kez içeri alınmaz (aynı içerik elle yüklenmiş olsa da).
- Fatura cevabı olmayan belgeler saklanır ve onaylanır; Yönetim › Einstellungen › Zahlung › MediData › «Nicht übernommene
  Dokumente» (orijinal indirilebilir).
- 30 dakikada bir `GET …/ela/notifications` (ilk sefer `from` olmadan, sonra son başarılı sorgudan 2 gün öncesinden) → kaydet
  (`medidata_notifications`) → `PUT …/notifications/{id}/status {"notificationFetched": true}`. Referanslarla faturaya bağlanır;
  ERROR bildirimi gönderimi «MediData: Fehler» yapar. Görünür: faturada, MediData sayfasında, Yönetim genel bakışında
  («MediData-Meldungen», «Gesehen» ile kapanır).
- Sorgu aralıkları Best Practice'e göre; ayrıntı ve «Jetzt abrufen»: `docs/VERBINDUNGSTEST.md`.

### eBill adres eşleştirme (`POST …/ela/ebillrecipients`) – uygulanmadı

Belge (3.10.5) hastanın e-posta/eBill-ID/UID (+ doğum tarihi) ile eBill'e ulaşılabilir mi diye sorar. Şart: muayenehanenin eBill
ürün seçeneği aboneliği. N-Fizyo bugün hastaya faturayı kendisi verir (`toPatient: false`); eBill ile gönderim, hasta verisini
MediData'ya sorgu için vermek ve rızası yeni ürün kararlarıdır. Bu yüzden **isteğe bağlı** bırakıldı; karar verilirse eklenebilir.

### XML sürümü

- V5.2.0 «Unterstützte XML Standards»: GeneralInvoiceRequest/Response **5.0** (ve 4.5) destekli. N-Fizyo 5.0 gönderir.
- CSS generalInvoice **5.0**'ı 01.01.2026'dan beri kabul ediyor; **4.5'in ömrü 30.06.2027'de bitiyor**
  (kaynak: css.ch › Leistungserbringer › Datenaustausch › Standards).
- Sigorta başına kabul: katılımcı listesindeki `notSupported` (yukarıda, gönderimden önce kontrol edilir).
- 5.0'ın MIME türü belgede yazmıyor; diğerlerinin düzeninden `application/x-fd-geninv-req-v50+xml` kabul edildi (`-v500+xml` de
  sayılır). MediData'ya sorulacak.
- Simülatör belgesi (2021) cevabı sadece 4.3–4.5 için yazıyor – 5.0'a cevap verip vermediği ilk testte görülecek.
- «Keine Zustellung» GLN'si: `2000000000008` (V5.2.0, 3.7.2.1). N-Fizyo bunu Tiers garant'ta hastanın borçlu GLN'si olarak
  kullanıyor; test sisteminde doğrulanacak.

### MediData'ya sorulacaklar

1. generalInvoiceRequest 5.0'ın tam MIME türü (`notSupported` için).
2. `Expect: 100-continue`: V5.1.1 değişiklik listesi «artık gerekmiyor» diyor, V5.2.0 eki (Best Practice) öneriyor. N-Fizyo gönderiyor.
3. Kutu/VA'da eLA yolları (`/md/ela/…`) web servisle aynı mı (V5 sadece web servisi anlatıyor).
4. Sertifika yenileme yolundaki `<applianceid>` ile web servis adresindeki `id<N>` aynı mı (N-Fizyo adresten alıyor).
5. Simülatör XML 5.0'a cevap veriyor mu; `2000000000008` Tiers garant borçlu GLN'si olarak kabul mü.
6. Belge kapağında «5.1.1», altbilgide «V 5.1», değişiklik listesinde «5.2.0» – hangi sürüm geçerli.

## Kilitler

Kesinleşmiş faturanın tutarı, satırları, numarası, IBAN'ı ve referansı veritabanı tetikleyicisiyle kilitli. Düzeltme = iptal + yeni fatura.
Tek istisna (0.9.0): Yönetim iki çift hasta kaydını birleştirirken, açık bir `patient_merges` satırı sürdükçe sadece
**hasta numarası** değişebilir (aynı kişi). Bkz. `docs/KASA_RANDEVU.md` › «Çift kayıtlar».

## Ödeme düzeltme (0.9.0)

Kaydedilmiş ödeme **asla silinmez ve tutarı değişmez** (veritabanı tetikleyicisi); düzeltme hep karşı kayıttır.

| İşlem | Kim | Nerede | Ne olur |
|---|---|---|---|
| **Zuordnung aufheben** | Resepsiyon, Yönetim | fatura sayfası › ödeme satırı (⋮) | gerekçeyle; ödeme «Ohne Zuordnung» listesine döner, fatura yine açık. `payment_moves`'a yazılır |
| **Zahlung stornieren** | Yönetim | aynı yer | gerekçeyle; eksi tutarlı karşı kayıt (`kind=reversal`, `reversal_of`); bir ödeme bir kez |
| **Geld zurückzahlen** | Yönetim | fatura › Zahlung (fazla ödemede açık) | eksi ödeme (`kind=refund`), en çok ödenen kadar |
| **Ausblenden** | Resepsiyon, Yönetim | Abrechnung › Zahlungen › eşlenmemiş satır | gerekçeyle (ör. sigorta iadesi, kendi hesabından havale); «Ausgeblendet» listesinde, «Wieder einblenden» ile geri |

Bakiye, hatırlatma ve kasa günü bu kayıtlarla otomatik doğru: bakiye = fatura + ücretler − tüm ödemeler (eksiler dahil).
Muhasebe: iade ve iptal karşı kayıt olarak (ör. 1100 / 1020) kendi gününde; eşleme değişince (eşlendi / kaldırıldı) o gün
«ungeklärt» ↔ alacaklar arasında aktarma (ikisi aynı hesapsa satır yok). Ödemenin ilk kaydı hep ilk eşlemeyle kalır –
bexio'ya gönderilmiş satırlar değişmez. Gizlenen banka satırı yevmiyede «ungeklärt» hesabında kalır (para bankaya geldi).

## KVG puan değeri sigorta grubuna göre (0.9.0)

Fizyoterapide KVG puan değeri **kanton başına** ve sigortacıların üç alım topluluğuyla (**tarifsuisse, HSK, CSS**) ayrı ayrı
anlaşılır veya kanton tarafından belirlenir; gruba göre farklı olabilir (physioswiss basın bülteni 31.10.2025: «Der Taxpunktwert
wird pro Kanton definiert», 26 kantonda üç gruba karşı belirleme süreci; ZH RRB 2025/702: HSK ve CSS için CHF 1.11; CSS: «kantonaler
Taxpunktwert», Tarif 312).
- Yönetim › Einstellungen › **Tarife**: «Gilt für» = Praxis (diğer tüm sigortalar) veya Gruppe tarifsuisse / HSK / CSS / Andere.
- Einstellungen › **Versicherer**: her sigortanın grubu («Gruppe für den KVG-Taxpunktwert»; boş = muayenehane değeri).
- Faturada sigortanın grubunun değeri, yoksa muayenehane değeri. Değer değişince taslaklar yeniden hesaplanır.
  UV/MV/IV değişmedi (ulusal CHF 1.00).
- XML'de **kanton** artık eksik bilgi listesinde: muayenehane kantonu geçerli bir kanton kodu değilse XML üretilmez
  (eskiden sessizce «BE»).

## Özel ödeme (Selbstzahler, VVG) – fiyat listesi

- Yönetim › Einstellungen › **Preisliste**: üç tür kalem.
  - **Leistung** (hizmet): seans fiyatı. Bir tedavi türüne bağlanabilir (ör. «Komplexe Physiotherapie» → PRIV-45).
    Tedavi türü boş olan hizmet = diğer tüm randevular için varsayılan.
  - **Produkt**: Theraband, tape vb. (kasada veya faturada).
  - **Abo**: MTT abonmanı (ziyaret sayısı veya sınırsız, kaç gün geçerli).
- Kanun «VVG» veya «privat» olan hastalar → «Bereit»te fatura otomatik fiyat listesiyle. Sigortalı (KVG/UVG) hasta için:
  «Privat abrechnen» düğmesi (sigortaya değil, hastaya).
- Özel faturada XML/MediData yok. PDF: fiyat, KDV sütunu, QR ödeme kısmı.

### KDV (MWST)

- Sadece Einstellungen › Zahlung'da **KDV numarası (UID, CHE-…)** girilirse KDV hesaplanır. Numara kontrol hanesiyle denetlenir.
- Fiyatlar **KDV dahil** son fiyattır. Oranlar: 0 (muaf), 2,6 %, 8,1 % (1.1.2024'ten beri normal oran).
- Doktor reçeteli fizyoterapi KDV'den muaf. Ürünler ve reçetesiz antrenman genelde 8,1 %. Hangi kalemin vergili olduğuna
  muayenehane **mutemetle (Treuhand)** karar verir; N-Fizyo sadece varsayılan önerir.
- Yıllık vergiye tabi ciro CHF 100'000'ün altındaysa muayenehane genelde KDV mükellefi değildir → numara girilmez.

## Ödeme türü

Ödeme kaydında tür: banka/posta, nakit, Twint, kart. Nakit/Twint/kart kasadaki gün sonuna da girer.

**Kapanmış kasa günü (0.9.0):** fatura sayfasında nakit/Twint/kart ödemesi, iadesi veya ödeme iptali, kasası kapanmış bir güne
(geriye tarihli de) **kaydedilmez**; ekranda «Dieser Kassentag ist schon abgeschlossen …» çıkar. Açık bir gün seçilir (ör. bugün);
kapanmış günün düzeltmesi Yönetim'de Kasse › makbuz. Banka ödemesi kasaya girmez, her gün kaydedilir.

**Nakit 5 Rappen (0.9.0):** İsviçre kuralı – nakit 5 Rappen'e yuvarlanır (186.48 → 186.50, 186.47 → 186.45); banka, QR, Twint,
kart kuruşu kuruşuna. Faturaya tam tutar yazılır, fark `rounding_chf` olarak ayrı tutulur (kasada = tutar + yuvarlama). Nakitten
sonra 5 Rappen'den az eksik/fazla kalırsa fatura **ödenmiş** sayılır (birkaç Rappen için hatırlatma yok). Kasa satışında da
toplam 5 Rappen'e yuvarlanır (makbuzda «Rundung auf 5 Rappen»). Yevmiye: «Rundung 5 Rappen …» satırı, hesap Export ›
«Rundungsdifferenzen» (boş = hizmet geliri hesabı). Tüm tutarlar tek yardımcıyla: `nfizyo/core/money.py` (ticari yuvarlama,
ROUND_HALF_UP: 1.5 × 180.35 = 270.53).

**Banka mesajı (0.9.0):** camt dosyasındaki serbest metin (Ustrd / AddtlRmtInf) kaydedilir ve «Ohne Zuordnung» listesinde
görünür; içinde açık bir fatura numarası varsa (ör. «Rechnung 2026-0099») kutuya önerilir.

## Sigorta cevapları (General Invoice Response 5.0)

1. MediData kuruluysa cevaplar kendiliğinden gelir. Değilse: MediData portalından indir (XML veya birden çok XML içeren ZIP).
2. Abrechnung › **Antworten** › yükle. Cevap, fatura numarasıyla (request_id) faturaya bağlanır; bulunamazsa elle numara girilir.
3. Türler: **Rückweisung** (red, hata kodları ve satır numarasıyla), **In Bearbeitung**, **Mitteilung** (Tiers garant), **Angenommen**.
4. Düzeltme:
   - Ana veri yanlış (sigortalı no, GLN, adres): hasta kaydında / sigorta ayarında düzelt → faturada **yeniden gönder**
     (XML güncel veriyle yeniden üretilir, numara aynı).
   - Satırlar yanlış: Yönetim › fatura › **«Storno und neuer Entwurf»** → fatura iptal, aynı satırlarla yeni taslak → düzelt, kesinleştir, gönder.
5. «Erledigt» + not ile kapatılır. Açık redler Yönetim genel bakışında görünür.

MediData kuruluysa cevaplar **otomatik** gelir (30 dakikada bir, yukarıda «Cevaplar ve bildirimler»); elle yüklenenlerle aynı
yerde ve aynı şekilde görünür, aynı dosya iki kez içeri alınmaz. Elle yükleme (kutu yoksa, eski dosyalar) aynen kalır.
Cevaplar resmî şemaya göre değil, toleranslı okunur (XML varlıkları çözülmez, ağ erişimi yok). Şifreli cevaplar tanınır ama açılmaz.

## Kostengutsprache (maliyet garantisi)

- KVG: reçete başına en çok 9 seans; ilk 4 seri (36 seans) serbest. **37. seanstan itibaren** sigortanın açık garantisi gerekir
  (KLV Art. 5 Abs. 4; doktor güven doktoruna rapor verir). Cevap gelmemesi onay sayılmaz.
- Resepsiyon › hasta kaydı › Rechnungen kartı › **Kostengutsprache**: talep tarihi, seans sayısı, referans; sonra karar
  (Erteilt / Teilweise / Abgelehnt, seans, geçerlilik).
- KVG faturasında son 12 ayda 36'dan fazla seans varsa ve geçerli garanti yoksa uyarı çıkar (yaklaşık hesap; kesinleştirmeyi engellemez).

## Hastaya e-postayla fatura

Fatura sayfasında **«Per E-Mail»** düğmesi sadece: Tiers garant + hastanın e-posta adresi + **e-posta izni** (hasta kaydında işaret).
PDF ek olarak gider, ek gönderimden sonra kutudan silinir. Ayrıntı: `docs/BAGLANTILAR.md`.

## Muhasebe dışa aktarma

Yönetim › Einstellungen › **Export**: dönem seç → **Excel** (yevmiye, faturalar, iptaller, ödemeler, kasa, gün sonları, açık kalemler)
veya **yevmiye CSV** (Tarih;Belge;Borç;Alacak;Tutar;Metin;KDV). Hesap planı İsviçre KMU planına göre önerilir (1100 alacaklar,
1020 banka, 1000 kasa, 1091 Twint/kart geçiş, 3400 hizmet geliri, 3200 ürün geliri, 3600 hatırlatma ücreti, 2200 KDV) –
mutemetle kontrol edilmeli. Banana / Abacus'a aktarırken sütunlar kendi içe aktarma şablonlarında eşlenir.

**bexio (0.8.0):** aynı ekranda «An bexio senden». Her yevmiye satırı bexio'da bir «manuelle Buchung» olur (hesaplar numarayla
eşlenir, belge numarası = `reference_nr`). Gönderilen satır bir daha gönderilmez; zaman aşımında satır «unklar» kalır ve
otomatik tekrarlanmaz. **0.9.0:** satırın anahtarı sadece kaynağının numarasından (ör. `src:pay:17`, `src:inv:12:vat`) – isim
veya metin değil; firma/hasta adı değişince, birleştirmede veya hesap planı değişince hiçbir şey iki kez gitmez. 0.8.0'da eski
anahtarla gönderilmiş satırlar tarih, belge, hesaplar ve tutarla (metin olmadan) tanınır ve yeni anahtarı alır. Token tek
muayenehaneye ait: `NFIZYO_BEXIO_PRACTICE` (`docs/BAGLANTILAR.md`). Excel/özet: kasa toplamı gün sonundan sonraki
iptallerden sonra (net), «Kasse» sayfasında «Korrektur nach Abschluss» ve «Netto» sütunları. Faturalar ve açık kalemler N-Fizyo'da kalır (bexio'da ikinci fatura yok). KDV satırları KDV hesabına
vergi kodu olmadan gider – bexio'da KDV beyanı yapan muayenehane bunu mutemetle kontrol etmeli. Erişim: `docs/VERBINDUNGSTEST.md`.

## Firmalara fatura (0.8.0)

Resepsiyon/Yönetim › **Firmen**: firma kaydı (adres, iletişim kişisi, fatura e-postası, UID, sipariş no, ödeme süresi, dil DE/FR) →
«Rechnung erstellen» → kalemler fiyat listesinden veya serbest (açıklama, adet, fiyat, KDV oranı, hizmet/ürün) → Yönetim kesinleştirir.
Numara serisi ayrı: `F2026-0001`. QR referansı 9 ile başlar (normal faturalar 0 ile), RF referansında `NFF` – banka dosyası
(Abrechnung › Zahlungen) ödemeyi doğru firmaya yazar. Hatırlatma 3 kademe (ücretler Zahlung ayarlarından), iptal sadece Yönetim.
Muhasebe dışa aktarımında «Firmenrechnungen» sayfası ve yevmiyede «(Firma)» satırları.

## Eski yazılımdan açık kalemler (0.9.0)

Yönetim › Import › «Offene Posten»: eski fatura no, tarih, açık tutar (+ toplam, vade, ihtar kademesi). Her satır
«V-<eski no>» numaralı, kesinleşmiş, açık tutar kadar bir fatura olur (Selbstzahler türü, QR ödeme kısmı yeni referansla).
Gönderilmiş ihtar kademeleri ücretsiz olarak taşınır, sonraki kademe normal devam eder. Muhasebe dışa aktarımında bu faturalar
**gelir olarak görünmez** (eski muhasebede zaten var); ödemeleri görünür. Aynı eski numara ikinci kez alınmaz.

## Banka dosyası (camt) – çift kayıt yok (4. tur)

- Sadece kesinleşmiş (BOOK) hareketler alınır; gün içi dosyadaki «bekleyen» (PDNG/INFO) satırlar atlanır.
- Bankanın işlem referansı yoksa kimlik içerikten (tarih, tutar, QR referansı, ad) üretilir: aynı ödeme hem camt.054
  hem aylık camt.053 dosyasında gelse bile bir kez kaydedilir.
