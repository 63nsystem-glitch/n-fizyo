# N-Fizyo – çalışma notları (yeni oturum buradan başlar)

İsviçre fizyoterapi muayenehane yazılımı, NSystem GmbH (Biel). Sürüm **0.9.0 kalır**.
Serhat ile Türkçe, kısa, az teknik terim. Önce kod; belgeler (MediData e-postası, DE/FR kullanım kılavuzu) sonra.

## Değişmez kurallar
- Şifre, API anahtarı, token sohbete asla yazılmaz – sadece sunucudaki `.env` ve GitHub Secrets.
- N-Gastro'dan tamamen ayrı: ayrı depo, ayrı sunucu, ayrı anahtarlar.
- Demoda gerçek hasta verisi asla olmaz.
- `tests/test_tenant_isolation.py` (pratikler arası veri ayrımı) asla silinmez, sadece genişletilir.
- YZ karar vermez, terapist onaylar. Hasta uygulamasında tıbbi tavsiye yok (acil: 144).
- NSystem paneline hasta verisi gitmez; NSystem şifre sıfırlayamaz.
- E-posta sadece onayla; raporlar HIN ile.
- Rakip kopyalanmaz; «ilk/tek» iddiası yok. PROM, kamera ölçümü, canlı tercüman yok. libfaketime yok.
- Tarih: `nfizyo.core.timeutil.today` (asla `date.today`). Para: `core/money.py`.
- 0.9.0 migration'ları değiştirilebilir, 0.8.0 migration'ları asla.

## Sahip kararları (şimdilik varsayılan)
5 Rappen altı nakit kalanı faturayı kapatır · anonim SMS sınırı 30/saat, 100/gün · destek filtresi tek başına ön adı
engellemez · çok pratikli kutuda `NFIZYO_BEXIO_PRACTICE` gerekli.

## Tur rutini
1. Bir alan seç, sorun bul, düzelt, test ekle (`tests/test_qa4.py`).
2. Tam test: `python -m pytest -q -p no:randomly` · kontrol: `ruff check --select F,E9 nfizyo tests tools`
3. README satır 266: «N test:» sayısını güncelle.
4. Zip (ana klasörden): `zip -qr <scratchpad>/n-fizyo-0.9.0.zip n-fizyo -x '*/__pycache__/*' '*.pyc' 'n-fizyo/.pytest_cache/*' 'n-fizyo/.ruff_cache/*' 'n-fizyo/data/*' 'n-fizyo/.env'`
5. Kısa Türkçe özet.

## Modül modül inceleme (her tur = bir modül)
Yapı zaten modüllü: `nfizyo/modules/<ad>/` (kendi kimlik kartı, api.py, repo, sayfalar, tablolar, metinler), ortak olan
`nfizyo/core/`. Kurallar `tests/test_architecture.py` ile korunuyor. Turda sadece o modül + kullandığı api.py'ler okunur.
Kontrol listesi: kenar durumlar · yetki (hangi ekran ne görür) · de/fr metinler · eksik test · başka modüle bağlantılar.
Sıra (pilota etkisine göre): 1 billing · 2 dataimport · 3 agenda + booking · 4 cashdesk · 5 patients + rxwatch ·
6 dictation + documentation + reports · 7 inbox · 8 patientapp + messages + notify + homeprogram ·
9 firms · exports · settings · mtt · video · intake · briefing · patientinfo · stats · anatomy · support.
Küçük temizlik: `_float` ve `_id` 5 modülde ayrı ayrı yazılmış → core'da tek yere alınabilir.
Bitenler: billing 1. kısım (13.11.2026) – yeni KVG tarifine hazırlık (veri girişi kaldı), UV pozisyonları Behandlungsart'a göre,
grup bölen yöntemi. billing 2. kısım (13.11.2026): tiers payant Mahnung sigortaya, red açıkken Mahnung yok, iptal çift tık. dataimport (14.11.2026): gesetz sigortadan, doğum tarihsiz tekrar aktarım, durum metinleri, ZSR, çift SMS uyarısı. agenda (14.11.2026): kaydırılan randevuya yeni hatırlatma, gece 21–07 hatırlatma yok. cashdesk (14.11.2026): «auf Rechnung» storno taslak faturayı siler, kesinleşmişse reddeder; storno çift tık. patients (14.11.2026): yeni hastada «bu kişi zaten var mı» uyarısı; ajandada iptal SMS seçeneği. documentation (14.11.2026): iki pencerede not – sessiz üzerine yazma yok, onaylanmışsa metin kaybolmaz. inbox (14.11.2026): boş/okunamayan iki HIN postası artık ayrı kayıt, tekrar gelen ek ilk göndereni ezmez. patientapp/messages (14.11.2026): mesaj uzunluğu 4000, saatte 20 mesaj sınırı, ağrı 0–10. 9. grup (14.11.2026): exports temiz; MTT sayı girişleri (inf, 30 hane, negatif) makul sınırlı; form_int yardımcı. Küçükler (14.11.2026): intake ses dosyası 20 MB, günde 5 başvuru; video, briefing, patientinfo, stats, anatomy, support temiz. TÜM MODÜLLER BİR KEZ TURLANDI. Servis kablosu (14.11.2026): dizüstünü ağ kablosuyla kutunun servis girişine bağlayıp güncelleme – `nfizyo/agent/service.py` (kutu, sadece 169.254.77.1:8780, imzalı istek + tek kullanımlık kod), `tools/servis/` (Windows programı, tkinter, `build_windows.bat`), `deploy/service-port-setup.sh`, `docs/SERVIS_KABLOSU.md`; Windows'ta gerçek kabloyla henüz denenmedi. Mobil tur (14.11.2026): 142 sayfa 320/375 px ve tablet – yana taşma yok; alt menüde kısa yazılar (`short_key`, hasta uygulaması 11 dil), dokunmatikte büyük tıklama alanları, telefonda vücut haritası tam genişlik + «Vergrössern». Ölçüm araçları scratchpad'te (`mobcheck.js`, `navtrunc.py`). GitHub deploy (18.11.2026): yeni ayrı depo + yeni Netcup sunucusu (demo, fizyo.nsystem.ch) – `.github/workflows/deploy.yml` (Tests yeşil → git archive → scp → `sudo n-fizyo-deploy`), `deploy/server-bootstrap.sh` (deploy kullanıcısı, sadece tek programa sudo), `docs/GITHUB_DEPLOY.md`; Secrets `FIZYO_SSH_HOST/KEY/KNOWN_HOSTS`. Kuruldu ve çalışıyor (18.11.2026): GitHub `63nsystem-glitch/n-fizyo`, sunucu Netcup VPS 500 ARM (152.53.51.76, Viyana, arm64, 4 GB) – sadece demo, gerçek veri asla; anahtar sunucuda `--genkey` ile üretildi. Her push: Tests → Deploy otomatik. Eski kurulum N-Gastro sunucusunda (8090) yenisi çalışınca kaldırılacak; n-gastro deposundaki eski n-fizyo klasörü (40 test, Infomaniak demo YZ) emekli.

## Kontrol edildi, temiz (tekrar bakmaya gerek yok)
Yıl sonu geçişi (233 sayfa) · hasta birleştirme (38 tablo) · belge indirme güvenliği · PDF fuzz · online randevu tatil/izin ·
hasta JS ES2017 · iş kuyruğu iki şerit · Fransızca ekranlarda unutulmuş Almanca · 1 yıllık veri hızı (2.500 hasta, en yavaş 0,3 sn) ·
tüm formlar bozuk girişle (personel + hasta + online randevu) · sadece adı olan hasta · KLV 35 gün / 36 seans sınırları ·
çift tıklamayla kesinleştirme · eski formla kayıt (hasta ana verileri) · ağ zaman aşımları (SMS, e-posta, HIN, YZ, MediData, bexio).

## Son durum
717 test geçiyor. Rapor: «N-Fizyo – Durum Raporu 2» (80/100). Pilot hedefi 11 Ocak 2027 (kararlar 27 Kasım 2026'ya kadar),
satış en erken 15 Mart 2027.
