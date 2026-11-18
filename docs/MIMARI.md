# Mimari

Tek Python süreci, tek SQLite dosyası, tek kutu. Proje **parçalara ayrılmış**: yeni özellik eklemek ve güncellemek kolay olsun diye.

## Üç katman

```
nfizyo/
├── core/          ÇEKİRDEK – herkesin kullandığı temel (modüllere hiç bağlı değil)
│   ├── screens.py   4 ekran: /therapie, /empfang, /leitung (+ hasta uygulaması /p)
│   ├── modules.py   Modül kimlik kartı (Module) ve modülün yapabildikleri (ModuleContext)
│   ├── slots.py     Takma yerleri: ekranlarda modüllerin kart/sekme/buton ekleyebildiği yerler
│   ├── events.py    Haber sistemi: «not onaylandı», «randevu alındı» … modüller birbirini tanımadan haberleşir
│   ├── access.py    Kim hangi hasta dosyasını görebilir (tedavi ekibi + vekâlet)
│   ├── patients.py  Hasta ana bilgileri, sigorta, sevk eden doktor, reçete (ortak veri)
│   ├── practice.py  Muayenehane bilgileri
│   ├── pages.py     Ortak sayfa çerçeveleri (Yönetim › Ayarlar sekmeleri)
│   ├── mfa.py, totp.py, crypto.py   İki adımlı giriş (uygulama kodu, kurtarma kodları, şifreli anahtar)
│   ├── sms.py       SMS: giden kutusu + ASPSMS / eCall, tekrar deneme
│   ├── hin.py       HIN: SMTP ile gönder, IMAP ile al (HIN Client)
│   ├── pdf.py       PDF (Liberation Sans) ve İsviçre QR ödeme kısmı
│   ├── box.py       NSystem bağlantısı: izinli komut listesi, bağlantı durumu
│   ├── formats.py   İsviçre biçimleri: CHF 1’290.00, 03.11.2026, 079 000 00 04, «Ad Soyad» – arayüz, PDF ve metinler hep bunu kullanır
│   ├── timeutil.py  Zürih saatiyle bugün/şimdi (`today()` – asla `date.today()`)
│   ├── money.py     Para: ticari yuvarlama (ROUND_HALF_UP, `rp`, `times`, `total`, `vat_in`), nakit 5 Rappen (`cash`, `cash_settle`), form tutarı (`parse`) – asla `round(x, 2)`
│   ├── auth.py, security.py, web.py, db.py, jobs.py, i18n.py, audit.py, tenant.py, spreadsheet.py …
├── agent/         Kutu ajanı (merkez panele durum bildirir, imzalı komut alır) + runner (root, komutları çalıştırır)
├── central/       NSystem merkez paneli (ayrı servis, ayrı sunucu)
├── ai/            Yerel yapay zekâ (ses→yazı, not taslağı, hasta bilgisi, rapor okuma, çeviri, OCR)
├── knowledge/     Bilgi (anatomi, vücut bölgeleri, ağrı yüzleri, tarife kataloğu)
├── modules/<ad>/  MODÜLLER – her özellik kendi klasöründe
└── web/           Ortak şablonlar, CSS, JS (ekran düzenleri, iskelet/kas haritası, sesli okuma)
```

## Dört ekran

| Ekran | Adres | Kim | Ne var | Tıbbi içerik |
|---|---|---|---|---|
| Terapi | `/therapie` | her terapist (tablet + telefon) | kendi günü, **kendi hastaları** + vekâlet, dosya, diktat, raporlar, hasta bilgisi | evet |
| Resepsiyon | `/empfang` | resepsiyon (PC) | herkesin ajandası, hasta kaydı, QR davet, gelen raporu hastaya atama (içeriği görmeden), fatura hazırlama | **hayır** |
| Yönetim | `/leitung` | muayenehane sahibi (PC) | genel bakış, ekip ve roller, vekâlet, fatura kontrol/kesinleştirme, ayarlar | **hayır** |
| Hasta | `/p` | hasta (kendi telefonu, QR ile bağlanır) | egzersizler, terapistten bilgi (sesli), randevular, mesaj, şikâyet | sadece kendisi |

- Bir kişinin birden fazla rolü olabilir (ör. sahip + terapist). Girişte ekran seçilir, son seçim hatırlanır.
- Terapist sadece **tedavi ekibinde** olduğu hastaları görür. Ekibe: ana terapist olarak, randevu alınınca otomatik, ya da resepsiyon/yönetim elle ekler.
- **Vekâlet:** Yönetim «A, B'yi şu tarihler arasında vekâleten görür» der; süre bitince erişim kendiliğinden kapanır.
- Hasta dosyasına her erişim denetim kaydına yazılır.

## Bir modülün içi

```
nfizyo/modules/<ad>/
├── __init__.py     KİMLİK KARTI: ad, sürüm, requires (zorunlu), uses (varsa kullanır), tables (kendi tabloları)
├── api.py          Başka modüllerin kullanabileceği TEK kapı
├── views.py        Sayfalar; setup(ctx) içinde ekranlara bağlanır
├── repo.py         Kendi tablolarına erişim (her sorgu practice_id ile)
├── migrations/     001_xxx.sql (sadece kendi tabloları)
├── locales/        de.json, fr.json (+ hasta metinleri 11 dilde)
└── templates/<ad>/ Şablonlar
```

`setup(ctx)` örneği:

```python
ctx.page("therapie", "/patienten/{pid}/info", sayfa)            # ekrana sayfa
ctx.nav("therapie", NavItem("nav.x", "info", "/x", order=50))   # menü öğesi
ctx.slot("therapie.patient.right", 25, kart, "x/kart.html")      # hasta dosyasına kart
ctx.on("dictation.approved", diktat_onaylandi)                   # habere tepki
ctx.patient_page("/info", hasta_sayfasi)                         # hasta uygulamasına sayfa
```

## Kurallar (testlerle korunur: `tests/test_architecture.py`)

1. Modül sadece çekirdeği, `ai/`, `knowledge/`, kendini ve kimlik kartında yazan modüllerin **api.py**'sini import eder.
2. Modül sadece **kendi tablolarına** yazar; okurken kendi + çekirdek tablolarını kullanır. Gerisi api.py veya haberle.
3. Her tablonun tek sahibi var (kimlik kartı = migration dosyaları).
4. Kimlik kartında sürüm var; bağımlılıklar mevcut ve döngüsüz.
5. Şablonlar `templates/<modül>/` altında; takma yerleri ve haberler belgelenmiş.

Bir modül kapatılırsa (Yönetim › Ayarlar › Module): sayfaları 404, menüsü ve kartları kaybolur; ona bağlı modüller de kapanır.

## Haberler (events)

| Haber | Kim yayınlar | Kim dinler |
|---|---|---|
| `appointment.booked` | ajanda | bildirim (`notify`): Yönetim açarsa «yeni randevu» – randevu 26 saatten yakınsa gönderilmez (hatırlatma zaten gider) |
| `note.approved` | notlar | ajanda → randevu «erledigt» |
| `note.discarding` | notlar | diktat → kayıtla bağlantıyı çözer |
| `dictation.transcribed` | diktat | hasta bilgisi → aynı metinden öneri hazırlar |
| `dictation.approved` | diktat | hasta bilgisi → notla birlikte onaylanır, çevrilir |
| `document.assigned` | posta kutusu | (hastaya bildirim yok – tıbbi belge; terapist posta kutusu rozetinde görür) |
| `message.sent` | mesajlar (terapist cevabı) | bildirim (`notify`) → hastaya «yeni mesaj» |
| `patientinfo.released` | hasta bilgisi | bildirim (`notify`) → hastaya «yeni bilgi» |
| `patient.linked` | hastalar (Yönetim › Doppelte Einträge) | hasta bağlı tablosu olan her modül: kendi kayıtlarını sayar |
| `patient.merge` | hastalar (birleştirme, tek işlem) | hasta bağlı tablosu olan her modül: kendi tablolarını taşır |

`appointment.status` ve `patient.created` için bilinçli olarak dinleyici yok: durum değişikliği muayenehanede olur,
yeni hasta henüz uygulamaya bağlı değildir (davet QR/SMS ile).

## Veri ayrımı (muayenehaneler arası)

- Her tabloda `practice_id`; her sorgu `current_practice_id()` ile.
- Başka muayenehanenin her adresi 404. Testler: `tests/test_tenant_isolation.py` (**asla silinmez**) – 4 ekranın hepsi ve hasta uygulaması.

## Kilitler

SQLite tetikleyicileri: onaylı notlar, kesinleşmiş raporlar ve faturalar değişmez; `audit_log` sadece eklenir.

## Yeni çekirdek tablolar (0.3.0)

`users.totp_*`, `sessions.stage` (giriş adımı), `practices.street/zip/city/sms_provider`, `outbox` (SMS ve HIN kaydı, durum, deneme),
`patients.ahv_number`, `insurers.street/zip/city`, `box_commands` ve `box_state` (NSystem işlemleri).

## Yeni (0.9.0) – güvenlik (ayrıntı: `docs/GUVENLIK.md`)

- `core/web.py` › `client_ip(request)`: deneme sınırları için gerçek adres (sadece güvenilir proxy'den `X-Forwarded-For`,
  `Settings.trusted_proxies`). Herkese açık yeni bir sayfa sınır koyacaksa **her zaman bunu** kullanır, `request.remote` değil.
- `core/auth.py` › `LoginThrottle`: `blocked(key, limit, window)`, `fail/hit`, `reset`, `delay` (hesap için artan bekleme), kendini temizler.
- `core/sms.py`: `send(…, secrets=[kod, token])` – kod/link içeren her SMS'te zorunlu; `is_ch_li_mobile()`.
- `core/spreadsheet.py` › `safe_cell/safe_row`: CSV ve Excel'e yazan **her** yer bunu kullanır.
- Çekirdek göç 008: `sessions.last_seen_at` (hareketsizlik, `Settings.staff_idle_hours`), `outbox.body_secret` (sadece gönderilene kadar).
- `ai/ocr.py`: pypdf ayrı süreçte (süre/bellek sınırı). Paketler: `requirements.lock` (sha256), `tools/deps.sh audit|lock`.
- `ai/cloud.py`: **sadece sunum** için OpenAI uyumlu bulut YZ (`NFIZYO_AI_MODE=cloud_demo`). `NFIZYO_DEMO=1` değilse veya
  demo olmayan muayenehane varsa `CloudDemoRefused` → program başlamaz. Modüller simülasyonu `app[AI].simulate_demo` ile
  sorar (cloud_demo'da False); şablonlarda `ai_cloud` = servis adresi. Ayrıntı: `docs/AI.md`.

## Yeni (0.9.0) – ajanda ve online randevu

- `agenda` 2.3.0, göç 004: `absences` (terapist izni: vacation | sick | training | other; `user_id` boş = muayenehane kapanışı
  `closure`; tarih aralığı, isteğe bağlı saat aralığı), `holiday_settings` (kanton, boş = Einstellungen › Praxis), `holiday_overrides`
  (tek tek açılan/kapanan tatiller). Hesap `agenda/holidays.py` (veritabanısız), «müsait değil» zamanları `agenda/absences.py`
  (`periods`, `overlapping`). Sayfalar `agenda/absence_views.py`.
- Yeni API (`agenda/api.py`): `off_times`, `unavailable`, `closures_ahead`; `book()` izin/tatilde `None` döner.
  `booking` ve `gapfill` bunları kullanır (bu zamanlar asla teklif edilmez).
- `booking` 1.1.0 (göç yok): `likely_patients` (yazım hatası, ters ad, doğum tarihi, telefon), `patient_for_choice`,
  `confirm(..., patient_id, create_new)`; eski istekleri silme 6 saatte bir (`purge_loop`, hatırlatma döngüsü gibi; `jobs`
  tablosu kullanılmadı, çünkü YZ istatistiğini bozardı). Statik `web/static/js/booking.js`, `agenda.js` blocker alanını açar/kapar.

## Yeni (0.9.0) – yarım kalanların tamamlanması

- **Yeni modül `notify`** (tablolar `notify_settings`, `notify_queue`; requires patients + patientapp, uses agenda/messages/patientinfo).
  Yönetim › Einstellungen › Benachrichtigungen: kapalı / sadece e-posta (izinle, `patients.email_ok_at`) / sadece SMS /
  **izin varsa e-posta, yoksa SMS (varsayılan)**. Metin içeriksiz, sağlık verisi yok, hastanın uygulama dilinde (11 dil), uygulama
  linkiyle («Sie haben eine neue Nachricht von <Praxis>. Öffnen: …»). Sadece uygulamayı bağlamış hastaya. Kişi başına 15 dakikada
  en fazla bir bildirim (toplanır), 2 dakika bekler (hasta o an okuyorsa SMS gitmez), okunmuş içerik için bildirim yok,
  20:00–08:00 arası SMS yok (sabah 08:00'de). Gönderim çekirdeğin giden kutusu ile (`core/sms.py`, `core/mail.py`); link token içermez.
  Terapist mesaj ve bilgi ekranında «benachrichtigt (SMS, 14:32)» / «geplant» / «nicht benachrichtigt: …» görür (`notify.api.status_for`).
  Web push yok (servis çalışanında push altyapısı yok).
- Yeni API'ler: `patientapp.api.has_app`, `messages.api.unread_out`, `patientinfo.api.unread`,
  `billing.api.kvg_tp_value / imported_exists / import_open_item`, `ai.translate.cached` (sadece önbellekten okur).
- Çekirdek göç 009: `prescriptions.sessions_before` (eski yazılımda yapılmış seanslar), `practices.reply_time`
  (uygulamadaki cevap süresi: same_day | next_day | two_days | three_days | week – 11 dilde metin `p.msg.reply.*`; boşsa sabit metin).
- `documentation` 002: `note_templates.owner_id` (boş = muayenehane şablonu, Yönetim › Einstellungen › Notizvorlagen;
  dolu = terapistin kendi şablonu, Therapie › Notizen › Vorlagen). Yeni not formunda seçici her zaman görünür.
- `dataimport` 002: tablo yeniden kuruldu (tür listesi): **Verordnungen** (hasta, tarih, doktor, seans, yapılmış seans, tanı)
  ve **offene Posten** (eski fatura no, tarih, açık tutar, vade, ihtar kademesi). Açık kalemler `billing` 005 `invoices.imported_from`
  ile «V-<eski no>» kesinleşmiş fatura olur (QR, ihtar devam eder); muhasebe dışa aktarımına girmez (gelir eski sistemde),
  sadece ödemeleri girer.
- `settings` 001: `practice_setup` – Yönetim › Übersicht'te kurulum listesi (kart + kutucuk, `/leitung/einrichtung`):
  muayenehane bilgileri, IBAN, KVG puan değeri, şube, tedavi türleri, çalışma saatleri, sigortalar, ekip (GLN), SMS/e-posta, herkes için 2FA.
  Hepsi gerçek veriden kontrol edilir. Şube yeter (oda isteğe bağlı, «noch keine Räume» notu); SMS sağlayıcısı **isteğe bağlı** (sunucu
  .env'inde NSystem kurar) – ilerlemeye sayılmaz, listeyi açık tutmaz. Yönetim listeyi **her zaman** gizleyebilir (açık maddeler
  `/leitung/einrichtung`'da görünmeye devam eder, «wieder zeigen» ile geri gelir).
- Yönetim › Übersicht «Offene Arbeit»: her satır bir listeye gider (tıbbi içerik yok): `/leitung/offene-notizen?art=entwuerfe|ohne-notiz`,
  `/leitung/offene-berichte`, `/leitung/offene-berichtsentwuerfe`.
- `briefing`: hastanın hedefi terapistin dilinde (iş `translate_goal`, önbellek), orijinal açılabilir.
- `inbox`: «Original» uygulama içinde (`/posteingang/<id>/original`, geri tuşu), metin dosyaları UTF-8; resepsiyonda sunucu yolu yok;
  başlığı olmayan dokümanda tür adı («OP-Bericht»).

## Yeni (0.9.0) – resepsiyon ve fatura

- Haberler `patient.linked` (sayım, `found[tablo]`) ve `patient.merge` (`drop_id`, `keep_id`): `core/events.py` › `publish_strict`
  (hata = her şey geri alınır). Modül kendi tablolarını `ctx.patient_tables(tablolar, merge_sql)` ile bildirir; SQL modülün içinde
  yazılı (`:keep`, `:drop`, `:pid`). Çekirdek: `core/patients.py` › `linked_counts`, `delete_empty`, `merge`, `likely_duplicates`,
  `update_prescription`, `set_prescription_status`. Çekirdek göç 010: `patient_merges`, `insurers.tp_group`.
- Kilit tetikleyicileri (kesin fatura, rapor, makbuz) sadece açık `patient_merges` satırı varken hasta numarası değişimine izin
  verir: `billing` 006, `reports` 002, `cashdesk` 003.
- `billing` 006: `invoice_payments.kind/reversal_of/dismissed_*`, `payment_moves`, silme/değiştirme tetikleyicileri;
  `invoice_transmissions.status='unknown'` + `checked_*`; `tax_point_values.insurer_group`. `billing.api.accounting_payment_moves`.
- `cashdesk` 003: `cash_corrections` (sadece ekleme, sadece kapalı gün). `cashdesk.api.accounting_corrections`.
- `agenda.api.rooms`; `agenda.absences.public_kind` (Krankheit → Abwesend, Yönetim hariç). Jinja global `chf()`.

## Yeni (0.9.0) – ev programı

- `homeprogram` 003: `exercises.drawing` (çizim kodu), `exercises.duration_min` ve `program_items.duration_min` (dakika; örn. yürüyüş 20 dk).
  Standart kütüphanenin her egzersizi kendi çizimine bağlanır; «Spazieren gehen» değişmemişse «1 ×» yerine 20 dk olur.
  Onaylanmış programlara dokunulmaz.
- Çizimler: `web/static/img/exercises/<kod>.svg` (19 egzersiz) ve `region-<bölge>.svg` (çizimi olmayan egzersiz için nötr vücut
  bölgesi kartı – asla yanlış bir poz değil). Kendi çizimimiz, `tools/gen_exercise_drawings.py` ile üretilir. Stil: yuvarlak çizgi,
  `currentColor` (açık/koyu), hareket eden kısım ve ok `--ex-accent` rengi, başlangıç pozisyonu soluk. Sayfa dosyayı
  `<svg><use href="…svg#fig"/></svg>` ile kullanır (inline script yok, CSP değişmedi). Hastanın kendi videosu her zaman önce gelir.
- Dozaj metni: `homeprogram/dose.py` – set, tekrar, tutma, süre, günde kaç kez; 11 dilde doğru çoğul (uk/bks one/few/many,
  ar one/two/few/many). Hasta uygulamasında uzun («3 Serien à 10 Wiederholungen · 5 s halten»), terapide kısa («3 × 10 · 5 s halten»).
  Eksik alan gösterilmez (eskiden «2 ×»).
- Kütüphane: `/therapie/uebungen` (liste), `/uebungen/neu` (ayrı sayfa), `/uebungen/<id>` (düzenle), `/uebungen/<id>/aktiv`
  (pasif/aktif). Pasif egzersiz seçimden çıkar, mevcut programlarda ve geçmişte kalır. Hepsi muayenehaneye bağlı
  (`tests/test_tenant_isolation.py`). Stil dosyası `web/static/css/homeprogram.css` sadece bu sayfalarda yüklenir.

## Yeni (0.9.0) – işletme

- Dışarıda şifreli yedek (isteğe bağlı): `deploy/n-fizyo-backup` her gece seti GnuPG (AES-256) ile şifreleyip rclone veya rsync ile
  dışarı yükler, orada son N seti tutar; `deploy/restore.sh --offsite …` geri getirir. Ayarlar `NFIZYO_OFFSITE_*` (`.env`), parola
  dosyası sadece root. Sonuç `/var/backups/n-fizyo/status.json` → runner (`agent/runner.py` › `backup_status`, sadece sabit alanlar)
  → merkez panel (`central/health.py`: uyarı `offsite`, kontrol listesi). Ayrıntı `docs/YEDEK.md`, test `tests/test_backup_offsite.py`.
- Temizlik (`core/housekeeping.py`, 6 saatte bir): `jobs` bitenler 30 gün / başarısızlar 90 gün, `outbox` gönderilmiş/kaydedilmiş/
  kesin başarısız 90 gün, süresi dolmuş ve hareketsiz (`NFIZYO_STAFF_IDLE_HOURS`) oturumlar. Bekleyenlere (queued/running/pending)
  asla dokunulmaz. Modüller sadece kendi tablolarını temizler: `ctx.housekeeping(fn)` (örn. `notify/housekeeping.py` › `notify_queue`
  90 gün).
- CI: `.github/workflows/tests.yml` (Python 3.12 ve 3.13, `requirements.lock` + `requirements-dev.txt`, pytest, ruff F/E9,
  `tools/deps.sh audit`; haftada bir de çalışır). `.gitignore`: kökteki `/data/` (dikkat: `nfizyo/knowledge/data/` Git'e girer).

## Yeni (0.9.0) – NSystem işletimi

- `core/nsystem.py` + çekirdek göç 011: `box_support_sessions` (destek erişimi: kim, kaç saat, durum), `box_events` (NSystem kaydı,
  sadece ekleme; `reported`), `box_support_requests` (talep + NSystem cevabı, `rev`), `box_notices`, `box_notice_dismissals`.
  Hepsi kutuya ait (muayenehaneye değil); sorgular yine `practice_id` ile (talepler, kapatmalar; kayıtta başka muayenehanenin adı yok).
- `core/patientdata.py` › `scan(metin, db, practice_id)`: hasta verisi kalıpları (ad soyad, hitap/baş harf + soyad, hasta numarası,
  doğum tarihi – ay adıyla da –, AHV, sigortalı no, telefon, e-posta; yazım varyantları için bkz. MERKEZ.md). Panel sadece kalıpları
  kullanır (hasta numarası: P + 5 rakam).
- App → runner: `runner/local/*.json` (HMAC, `core/nsystem.py` › `local_mac`), runner → app: `runner/events/*.json`
  (`ingest_runner_events`). Runner bu klasörlere sadece `agent/safefs.py` ile dokunur (O_NOFOLLOW, klasör tanıtıcısı, yeni dosya +
  rename, fchown; kimlik `[A-Za-z0-9_-]{1,64}`). Runner: `agent/support.py` (WireGuard, authorized_keys, süre, journal), `agent/release.py` (manifest/index
  imzası, paket), `tools/release.py`. İmzalı içerik: `agent/signing.py` › `sign_payload/verify_payload` (tür + veri).
- Yeni modül `support` 1.0.0 (tablosuz, zorunlu): Hilfe sayfası 3 ekranda, rozet `support_replies`. `settings` 2.5.0: NSystem sayfası
  POST (destek erişimi). Çekirdek: `<ekran>/hinweise/<id>/ausblenden`, bant `layouts/staff.html` (`nsystem_notices`, `nsystem_support`),
  `web/static/js/countdown.js`.
- Panel göç 002: `commands.deliver_after/notice_id`, `boxes.wg_pubkey/wg_ip`, `box_events`, `support_requests`, `notices`,
  `notice_boxes`, `central_state`. `central/ops.py`, `central/releases.py`; CLI `support-peer`, `support-peers`, `releases`.

## Yeni (0.9.0) – MediData (resmî belgelere göre)

- `billing/medidata_cert.py`: client sertifikası için SSL ayarı (her istekte yeniden; sistem güven listesi + isteğe bağlı
  `NFIZYO_MEDIDATA_CA`, doğrulama asla kapanmaz), anahtar dosyası sadece sahibine açık (yoksa `CertError`), durum
  (`status`: off | ok | warn ≤ 60 gün | expired | error), CSR (RSA 2048; C, L, O, CN = X-Client-ID), yenileme
  (`POST …/md/srv/<applianceid>/client/certificates`, 400 kodları), atomik kurulum + `.bak-…` yedek.
- `billing/medidata.py` (Schnittstellendokumentation V5.2.0, `CONFIRMED = True`): `MediDataClient` – `upload` (tek deneme,
  `classify`: sent | failed | unknown), `participants` (limit/next/offset, lawtype), `upload_status` (+ detail), `downloads`,
  `download`, `confirm_download`, `notifications` (from/limit/offset), `confirm_notification`; her istekte client sertifikası +
  Basic + X-CLIENT-ID; `Link` sadece aynı sunucuda izlenir. `new_client_id()` (`GET {kutu}/md/cloudmigration`).
- `billing/medidata_sync.py`: arka plan döngüsü (`ctx.on_startup`, diğer modüllerdeki gibi) – katılımcı listesi 24 saat;
  durum, cevaplar, bildirimler 30 dakika (Best Practice). Sadece `NFIZYO_MEDIDATA_PRACTICE` (veya tek) muayenehanede çalışır,
  her şey o `practice_id` ile. Önce kaydet, sonra onayla; 401/403/429/5xx'te tur biter; asla fatura göndermez.
  Göç `billing/migrations/007_medidata_webservice.sql`: `medidata_participants`, `medidata_downloads`, `medidata_notifications`,
  `medidata_sync`, `invoice_transmissions`'a referans/durum sütunları. Testler `tests/test_medidata_webservice.py`
  (sahte sunucu `tests/medidata_fake.py`), ayrılık `tests/test_tenant_isolation.py`.
- `billing/simulator.py`: simülatör hedefleri (GLN 2099988876514 / 2099988880030), flag'ler, yorum XML başlığının hemen arkasında,
  uydurma test kişisi, sadece `NFIZYO_MEDIDATA=test` (+ XML `modus="test"` kontrolü). Geçmiş `audit_log` (`medidata_simulator`).
  Sayfa `/leitung/einstellungen/medidata` (Zahlung sekmesi altında): simülatör, sorgu durumu, bildirimler, belgeler.
- `nfizyo/medidata_cli.py` + `__main__.py`: `medidata-csr`, `medidata-renew`, `medidata-client-id`.
- Ayarlar `NFIZYO_MEDIDATA_CERT`, `_KEY`, `_CA`, `_PRACTICE`, `_SERVICE_URL`, `_APPLIANCE_ID` (son ikisi sadece adres
  `…/md/srv/id<N>` değilse). `connections.py`: salt okunur `GET …/ela/participants?limit=1`, sertifika, X-CLIENT-ID biçimi.
  `agent/status.py` › `medidata_cert` = `{state, days_left}` (yol, ID yok) → `central/health.py`: `medidata_cert`, `medidata_cert_error`.

## Yeni (0.9.0) – ekran düzeni
- Telefon alt çubuğu en fazla 5 sekme: 4 alan + «Mehr» (alttan açılan liste: kalan menü öğeleri, ekran değiştir, hesap).
  `NavItem(short_key=…)` alt çubukta kısa ad (örn. «Patienten»). Menüde sadece en uygun öğe aktif (Warteliste ≠ Agenda).
- İki sütun: `grid-main-side`; yan sütun genişliği `style="--side:minmax(300px,420px)"` ile (asla satır içi
  `grid-template-columns` – yoksa 900 px altında alt alta gelmez). Yapışkan yan panel: `side-sticky`. Formun «Speichern» düğmesi
  yardım kartında değil: `grid-side` + `form-actions form-foot`. Kenarsız liste kartı: `card flush`. Uzun yardım: `<details
  class="card help-card" data-help>` (1400 px altında kapalı).
- Demo etiketi sayfanın içinde (başlık üstü / giriş kartı), hiçbir düğmenin üstüne binmez. Ajanda kendi kaydırma alanında:
  başlık satırı ve saat ekseni sabit, renk açıklaması altta; telefonda seçim yoksa gün görünümü (`agenda.js`).
- Hasta uygulaması: yazı en az 14 px, dokunma alanı en az 44 px; testler `tests/test_layout_090.py`.

## Yeni (0.7.0)

- `knowledge/tariffs.py`: KVG 312 tam (13 pozisyon), `Position.group` (session | supplement), `fixed_chf` (CHF pozisyonları),
  `KVG_ALLOWED` (kombinasyonlar), `Tariff.valid_until/default_code/remote_code`, `tariff_for_law(law, on=gün)`.
- `billing`: satır tutarı `amounts()`; fatura tarif başına bölünür; otomatik 7350; `kvg_checks()` (engelleyen) ve `warnings()` (bilgi);
  `uses=("video",)` → `video.api.took_place`.
- `mtt` 002: `goal_lang`, `note_lang` (çeviri kaynağı). `video`: Yönetim ayar sayfası ve sunucu kontrolü.
- `patientapp`: herkese açık `/p/datenschutz`.
- `core/web.py`: `content_security_policy()` – form-action video sunucusunu içerir; inline script yasak (test).
- Hukuki taslaklar `docs/rechtliches/`, güvenlik raporu `docs/GUVENLIK.md`.

## Yeni (0.6.0)

- Yeni modül `mtt` (tablolar `mtt_machines`, `mtt_plans`, `mtt_items`, `mtt_visits`, `mtt_sets`, `mtt_suggestions`; `uses` cashdesk, agenda).
  Canlı ekran statik `web/static/js/mtt_live.js` ile yenilenir. Öneri kuralları `mtt/rules.py` – terapist onaylamadan plan değişmez.
- `homeprogram` 002: `program_items.video_*` (kendi video, onay zamanı), `essential`, tablo `exercise_skips` (günde bir sebep).
  Videolar `data_dir/videos/<praxis>/<rastgele>.<uzantı>`, sadece ilk baytlara göre MP4/MOV/WebM, en çok 60 MB, `Cache-Control: no-store`.
- `patients` 002: `measurement_shares` (hangi ölçüm serisi uygulamada görünür; boş yerine '' ile UNIQUE).
- `agenda` 003: `slot_offers`, `slot_offer_recipients` (sadece token hash'i). «İlk onaylayan alır»: `UPDATE … WHERE status='open' RETURNING id`.
  Herkese açık sayfa `/p/angebot/<token>` (giriş yok, IP başına deneme sınırı).
- `cashdesk` 002: `subscription_visits` yeniden kuruldu (`created_by` boş olabilir, `source` desk | checkin). Yeni API: `checkin_visit`,
  `active_subscription`. Eski veriler aynen taşınır.
- Yeni modül `video` (tablolar `video_rooms`, `video_consents`; `requires` patients, agenda). JWT (HS256) `video/jitsi.py` içinde,
  kütüphanesiz. Herkese açık sayfa `/p/video/<link>` (sadece hash, IP başına deneme sınırı). Yeni yer: `empfang.appointment.action`.
  Sunucu paketi: `deploy/jitsi/` (docker-jitsi-meet sabit sürüm, JWT, misafir yok, kayıt yok, STUN/P2P kapalı).
- Yeni API'ler: `mtt.trainings`, `agenda.locations`. `briefing` artık `mtt` de kullanır. Yeni ikonlar (star, videocam, qr_code_scanner, …).
- Not: `Database.execute` UPDATE için etkilenen satır sayısını güvenilir döndürmez (lastrowid); sayı gerekiyorsa `RETURNING` kullanın.

## Yeni (0.5.0)

- Yeni modüller: `rxwatch` (tablo `rx_requests`: doktora yapılan istekler – devam reçetesi, Kostengutsprache, düzeltme, yeni reçete),
  `briefing` (tablosuz; sadece diğer modüllerin `api.py`'sini okur).
- Yeni `api.py`: `reports` (devam reçetesi mektubu), `homeprogram` (egzersiz günleri, program), `messages` (gelen mesajlar, açık işler),
  `intake` (yeni şikâyetler), `billing.cost_approval` (Kostengutsprache durumu).
- Yeni yer: `therapie.today.next` («Als Nächstes» içine ek). `reports` modülünün «rapor zamanı» kartı, `rxwatch` açıksa gizlenir
  (aynı bilgi reçete kartında).

## Yeni (0.4.0)

- Çekirdek: `core/mail.py` (e-posta giden kutusu, SMTP, tekrar deneme, ek gönderilince silinir), `core/export.py` (muayenehanenin tüm verisi
  ZIP, gizli sütunlar hariç, 24 saat). Tablolar: `outbox.subject/attachment*`, `patients.email_ok_at` (e-posta izni) ve
  `patients.reminder_channel` (sms | email | both | none).
- Yeni modüller: `cashdesk` (kasa: `cash_sales`, `cash_sale_lines`, `subscriptions`, `subscription_visits`, `cash_closings`),
  `booking` (online randevu: `booking_settings`, `booking_types`, `booking_therapists`, `booking_requests`),
  `exports` (`export_settings`).
- `billing`: `price_items` (fiyat listesi), `invoice_responses` (sigorta cevapları), `cost_approvals` (Kostengutsprache),
  faturada KDV (`vat_chf`, `vat_number`), satırda `unit_price/vat_rate/price_item_id`, ödemede `method`. Yeni `api.py`
  (kasa ve dışa aktarma bunu kullanır).
- `agenda`: `ical_tokens` (takvim aboneliği, sadece hash). Yeni API: çalışma saatleri, dolu zamanlar, online randevudan kayıt.
- Kilitler: makbuzlar ve satırları değişmez/silinmez, gün sonu değişmez, kapanmış güne satış/iptal yok, kesinleşmiş faturanın KDV'si kilitli.

## Arka plan işleri

`jobs` tablosu + iki işçi yan yana: «biri bekliyor» işleri (`transcribe`, `draft_note`, `patientinfo_draft`,
`patientinfo_translate`, `translate_intake`, `translate_message`, `translate_goal`) ve uzun arka plan işleri (`ctx.job(..., background=True)`:
`read_document`, `translate_booking`, `video_convert`). Böylece 2 dakikalık bir video çevirme ya da sabah taranan 20 rapor hiçbir diktatı
bekletmez. Ağır yapay zekâ işleri ayrı iş parçacığında, arayüz donmaz.
SMS ve e-posta kendi döngülerinde (giriş kodu YZ işinin arkasında beklemez), HIN postası, tarama klasörü, merkez bildirimi,
randevu hatırlatma, hasta bildirimleri (`notify`), diktat tekrar denemesi, eski online randevu isteklerini silme (6 saatte bir)
ve dışa aktarma temizliği (saatte bir) kendi döngülerinde.

## Güncelleme

- Her sürüm ayrı klasörde: `/opt/n-fizyo/releases/<sürüm>_<zaman>/`, `/opt/n-fizyo/app` → güncel sürüm.
- `sudo bash deploy/update.sh` (açılmış yeni paketten): değişen modülleri gösterir → aynı sürümse hiçbir şey yapmaz, eskiyse
  reddeder → **servis çalışırken** yeni klasör + paketler + yedek → servisi durdurur → veritabanı yedeği tekrar → geçiş → göç →
  başlat → `/health` yeni sürümü söylüyor mu? Durduktan sonra her hata (veya iptal, `trap EXIT`) → **otomatik geri döner**
  (göç olduysa veritabanı yedeği geri, eski sürüm ve paketleri, servis açık). Durmadan önceki hata → servis hiç durmaz.
- Elle geri dönüş: `sudo bash /opt/n-fizyo/app/deploy/update.sh --rollback` (güncelleme öncesi yedek yoksa durur; `--force` = sadece
  program). Son 3 sürüm saklanır (klasör adındaki zamana göre; güncel ve önceki hiç silinmez); başarısız denemenin klasörü silinir.
- `install.sh` başka sürüm kurulu kutuda durur (→ `update.sh`); aynı sürümde tekrar çalışabilir. Gerçek kutuyu `--demo` yapmaz;
  `reset-demo` veritabanında gerçek muayenehane (`is_demo = 0`) varsa hiçbir şey silmez.
- Her modülün sürümü veritabanında (`module_versions`) ve Yönetim › Ayarlar › Module'de görünür.

## Yeni modül eklemek (özet)

1. `nfizyo/modules/<ad>/` klasörünü aç (yukarıdaki yapı), `__init__.py`'de kimlik kartını doldur.
2. `nfizyo/modules/__init__.py` içinde `MODULE_ORDER`'a ekle (bağımlı olduğu modüllerden sonra).
3. `setup(ctx)` içinde sayfa/menü/kart/haber bağla. Başka modülün verisine sadece onun `api.py`'si ile eriş.
4. `python3 -m pytest tests` (proje klasöründe) – mimari testleri kuralları kontrol eder.

## 0.9.0 QA düzeltmeleri – bölüm C (ajanda, booking, rxwatch, dataimport, hastalar)

- `agenda` 2.5.0, göç 005: indeks `appointments(practice_id, prescription_id, status)`. Bir yıllık veriyle Empfang sayfaları
  17–20 sn → ~0,2 sn (rozet `rx_act` artık birkaç sorgu: `rxwatch.repo.evaluate_many`).
- `agenda.repo.rx_usage / used_sessions` (API: `agenda_api.used_sessions`, `rx_usage`, `future_booked`, `same_day`,
  `future_for_patient`, `cancel_future`, `link_prescription`): tek sayım kuralı (`USED_SQL`). `session_numbers` tek sorgu.
  `book()` ve `import_appointment()` aktif reçeteyi bağlar (`rx_for_new_appointment`).
- `booking.repo.confirm`: tek işlem, saat doluysa istisna → geri alma, `(None, None)`.
- Çekirdek göç 012: `practices.patient_id_last`, `patient_number_last` + tetikleyici `patients_id_never_reused`;
  `core.patients.create` kimliği açıkça verir (`next_id`). 0.8.0 veritabanında mevcut en büyük kimlik/numara
  (`patient_merges`, denetim kaydı dahil) başlangıç değeri olur.
- `core/sms.py`: `classify_phone` (ok | guessed | foreign | invalid), `normalize_phone` artık tahmin etmez (sadece İsviçre/FL),
  `ip_bucket` (IPv6 → /64), `anon_sms_blocked/hit` (muayenehane başına üst sınır). Kullananlar: `booking`, `patientapp` girişi.
- `patients` artık `uses=("agenda",)` (arşivlemede gelecek randevular). `dataimport.reader.check_xlsx` (ZIP bombası kontrolü).

