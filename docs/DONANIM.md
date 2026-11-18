# Donanım ve kurulum yolları

Yapay zekâ zorunlu. Müşteri üç yoldan birini seçer. Üçü de aynı programla çalışır.

## Üç yol

| Yol | Veri nerede | Yapay zekâ nerede | Muayenehanede ne var | Kurulum |
|---|---|---|---|---|
| 1. Kutu (GPU'lu) | Muayenehanede | Muayenehanede | Ekran kartlı kutu | `sudo bash deploy/install.sh` |
| 2. Küçük kutu + YZ sunucusu | Muayenehanede | NSystem YZ sunucusu (İsviçre) | Ekran kartısız küçük kutu | Kutu: `sudo bash deploy/install.sh --ai-server https://ai.…` · Sunucu: `sudo bash deploy/install-ai-server.sh --domain ai.…` |
| 3. Her şey sunucuda | NSystem sunucusu (İsviçre) | Aynı sunucu | Sadece tarayıcı ve tabletler | Sunucuda: `sudo bash deploy/install.sh --domain physio.…` |

**Yol 2 nasıl çalışır?** Hasta dosyası kutuda kalır. Sadece o anki iş (diktat sesi, rapor metni, çevrilecek cümle) HTTPS ile şifreli olarak sunucuya gider; sonuç geri gelir. Sunucu içerik saklamaz, sadece «hangi muayenehane, hangi iş, kaç saniye» kaydeder (kapasite ve fatura için). Her muayenehanenin kendi erişim anahtarı (token) var; anahtar sadece kutudaki `/etc/n-fizyo/n-fizyo.env` dosyasına girer.

İnternet kesilirse (yol 2): program çalışmaya devam eder. Diktatlar kaydedilir ve YZ geri gelince **otomatik** işlenir (her 2 dakikada kontrol).

Yol 3 için not: personel internetten giriş yapacağı için ilk müşteriden önce **iki adımlı giriş (2FA)** eklenmeli.

## Kutu boyu: terapist sayısına ve muayenehane büyüklüğüne göre

Tahmin. Pilotta gerçek değerler «Kennzahlen → KI-Leistung (7 Tage)» bölümünden ölçülür: ortalama bekleme süresi düzenli olarak 1 dakikayı geçerse bir üst boya geçilir.

| Muayenehane | Terapist | Kutu | Ekran kartı | Kabaca fiyat |
|---|---|---|---|---|
| Küçük | 1–4 | S | 16 GB (örn. RTX 5060 Ti 16 GB, ~CHF 450–700) | CHF 1'800–2'300 |
| Orta | 5–12 | M | 24 GB (RTX PRO 4000 Blackwell SFF, 70 W, sessiz, ~CHF 2'800) | CHF 4'500–5'200 |
| Büyük / birkaç şube | 13–30 | L | 2× RTX PRO 4000 SFF (48 GB, 140 W) | CHF 8'000–9'500 |
| Zincir | 30+ | – | Yol 3 veya özel sunucu | teklif |

Fiyatlar UPS dahil, Kasım 2026 (ekran kartları Digitec). Bütün parça listesi (işlemci, RAM, anakart, disk, güç kaynağı, kasa,
soğutucu, UPS): «N-Fizyo Kurulum Kılavuzu» belgesi › Donanım › Parça listesi.

Physio Balance (7 terapist) → **M**. Pilota S ile başlanabilir; istatistik sayfası yeterli olup olmadığını gösterir.

Yol 2'de muayenehane kutusu: ekran kartısız küçük PC (4 çekirdek, 16 GB RAM, 2× 512 GB SSD aynalı). Yapay zekâ maliyeti aylık ücrete girer.

## Her kutuda olması gerekenler

- 2 SSD aynalı (RAID 1): biri bozulursa veri kaybolmaz.
- Kesintisiz güç kaynağı (UPS).
- Disk şifreleme: Ubuntu kurulumunda seçilir (kutu çalınırsa veri okunmaz).
- Günlük yedek kutuda (`/var/backups/n-fizyo`) + her gece şifreli dış kopya (İsviçre'de depolama, başka binada sunucu/NAS veya harici disk) – kurulum `docs/YEDEK.md`.
- Kablolu ağ, kutu internete açık değil; uzaktan destek sadece Leitung açınca, süreli WireGuard tüneliyle (`docs/MERKEZ.md` › «Destek erişimi»).
- Mac ve AMD kutular uygun değil (ses → yazı NVIDIA/CUDA ister). Yol 2'de kutu için bu şart yok.

## Veri koruma (yol 2 ve 3)

- Sunucu İsviçre'de bir veri merkezinde.
- Her muayenehane ile NSystem arasında veri işleme sözleşmesi (Auftragsverarbeitungsvertrag).
- Yol 2: sunucu içerik saklamaz; ses dosyası sadece işlenirken geçici durur ve silinir; erişim kayıtları kapalı.
- Yol 2'de ekranlar ve hasta uygulaması dürüst konuşur: «yerel YZ» yerine «YZ sunucusu İsviçre» yazar (diktat, bugün, posta
  kutusu, hasta profili, çeviri onayı – 11 dil).

## YZ sunucusu yokken veya yavaşken (yol 2)

- Kutunun ekranları YZ sunucusunu **hiç beklemez**: durum arka planda sorulur (30 sn'de bir). Sunucu kilitlense bile
  ajanda, kasa, hasta uygulaması akıcı kalır.
- Bağlantı kurma süresi en fazla 10 sn; cevap vermeyen sunucu diğer arka plan işlerini dakikalarca tutmaz.
- Sunucu ulaşılamazsa diktat kaydedilir, ekranda «YZ şu an ulaşılamıyor, sonra otomatik işlenecek» yazar; sunucu dönünce
  2 dk içinde kendiliğinden işlenir (14 gün boyunca).
- Sunucunun kendisi reddettiği kayıt (bozuk ses, 40 MB'tan uzun kayıt) tekrar tekrar gönderilmez: terapiste «metni kendiniz
  girin» / «kayıt çok uzun» denir. 40 MB sınırı kutuya `/v1/status` ile bildirilir, büyük kayıt hiç yüklenmez.
- Sunucu meşgulken (bir muayenehanenin 3 işi aynı anda) `/v1/status` sırada beklemez – «ulaşılamıyor» diye yanlış görünmez.
- Beklenmeyen bir hata diktatı «işleniyor…» ekranında asılı bırakmaz: «başarısız» olur, tanınan metin düzenlenebilir kalır.
