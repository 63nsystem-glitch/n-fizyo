# Görüntülü görüşme – kendi Jitsi sunucumuz

N-Fizyo görüntülü görüşmeyi **muayenehanenin kendi video sunucusu** üzerinden yapar (Jitsi Meet, İsviçre'de).
Görüntü ve ses dışarıya gitmez: Google yok, jitsi.org yok, kayıt yok.

## Nasıl çalışır?

1. **Randevu sayfası › «Video-Sitzung»** (terapi veya resepsiyon) › «Als Video-Sitzung einrichten».
   Her randevu için tahmin edilemez bir oda adı (`nf-` + 24 rastgele karakter) oluşur; içinde isim yoktur.
2. **«Link per SMS senden»**: hasta kendi dilinde SMS alır (`/p/video/<link>`). Veritabanında linkin sadece hash'i durur.
   Yeni link gönderilirse eskisi geçersiz olur. Hasta uygulamayı kullanıyorsa ana sayfasında «Görüntülü randevu» kartı da çıkar.
3. **Hasta**: ilk seferde onay verir (İsviçre'deki sunucu, kayıt yok, sessiz bir yer), randevudan **15 dakika önce** girebilir.
   Görüşmede sadece adı ve soyadının baş harfi görünür.
4. **Terapist**: «Video starten» (randevudan 30 dk önce açılır, «Heute» ekranında da düğme var). Terapist moderatördür.
5. Randevu bittikten 30 dakika sonra hiçbir anahtar artık geçmez.

Giriş anahtarı (JWT, HS256) her seferinde N-Fizyo'da üretilir: oda + zaman aralığı + rol. Sunucu anahtarsız kimseyi almaz.

## Kurulum (bir kerelik)

1. **Sunucu**: İsviçre'de bir Ubuntu 24.04 sunucu (örn. Infomaniak Public Cloud veya Exoscale, Cenevre/Zürih).
   Birkaç eşzamanlı görüşme için 2 vCPU / 4 GB yeterli. N-Fizyo ile aynı sunucu da olabilir.
2. **DNS**: `video.<muayenehane-alanı>` → sunucunun genel IP'si.
3. **Kurulum**:
   ```bash
   sudo bash deploy/jitsi/install-jitsi.sh --domain video.physio.example.ch --email it@physio.example.ch
   # N-Fizyo aynı sunucudaysa anahtar otomatik yazılır:
   sudo bash deploy/jitsi/install-jitsi.sh --domain video.physio.example.ch --email it@physio.example.ch --nfizyo-env /etc/n-fizyo/n-fizyo.env
   ```
   Betik: docker-jitsi-meet (sabit sürüm), Let's Encrypt, `ENABLE_AUTH=1`, `AUTH_TYPE=jwt`, misafir yok, kayıt/yayın/transkript kapalı,
   STUN kapalı, P2P kapalı (`deploy/jitsi/custom-config.js`), güvenlik duvarı (80/443 tcp, 10000 udp).
4. **Anahtarı N-Fizyo'ya taşımak** (farklı sunucudaysa) – ekranda göstermeden:
   ```bash
   # Jitsi sunucusunda çalıştırın; değer doğrudan N-Fizyo sunucusundaki .env'e yazılır
   sudo grep '^JWT_APP_SECRET=' /opt/jitsi/.env | sed 's/^JWT_APP_SECRET=/NFIZYO_VIDEO_SECRET=/' \
     | ssh yonetici@nfizyo-sunucu 'sudo tee -a /etc/n-fizyo/n-fizyo.env >/dev/null'
   ```
   Sonra N-Fizyo `.env` dosyasına `NFIZYO_VIDEO_URL=https://video…` ve `NFIZYO_VIDEO_APP_ID=nfizyo` ekleyin, `sudo systemctl restart n-fizyo`.
   **Anahtar asla sohbete, e-postaya veya koda yapıştırılmaz.**
5. **Modül**: Yönetim › Einstellungen › Module › «Video-Sitzung» açık olmalı.

## Kontrol listesi

- `https://video…/herhangi-bir-oda` anahtarsız açılınca giriş istemeli (kimse giremez).
- Tarayıcıda Geliştirici Araçları › Ağ: görüşme sırasında `jitsi.net`, `google`, `gravatar` istekleri olmamalı.
- `docker compose -f /opt/jitsi/docker-compose.yml logs jvb | grep -i stun` → harici STUN sunucusu yok.
- Kurumsal ağlarda UDP 10000 kapalıysa görüntü gelmeyebilir; gerekirse sonra kendi TURN sunucumuz (coturn, 443/tcp) eklenir.

## Faturalama

Randevu görüntülü **gerçekleştiyse** (terapist veya hasta odaya girdiyse) faturada otomatik dikkate alınır (0.7.0'dan beri,
`modules/billing/repo.py` › `line_for`):

- **UV/MV/IV (kaza, askerlik, malullük):** satır otomatik **25.310 «Patientenbehandlung auf räumliche Distanz», 5 dakika başına**
  olur (randevu süresine göre; `nfizyo/knowledge/tariffs.py`), yüz yüze 25.110 yerine.
- **KVG:** mevcut 312 yapısında ayrı pozisyon yok → satır tedavi türünün normal seans pozisyonu kalır, faturada uyarı çıkar:
  «Video-Sitzung: … vor dem Versand mit dem Versicherer klären» (kesinleştirmeyi engellemez). Kullanmadan önce sigortayla yazılı
  netleştirin. Sunulan yeni KVG yapısında (henüz onaysız) 27610 var – bkz. `docs/FATURA.md`.
- Kimse odaya girmediyse randevu normal (yüz yüze) gibi faturalanır. Test: `tests/test_tariff_rules.py::test_video_session_billing`.

## Teknik

- Modül: `nfizyo/modules/video/` – tablolar `video_rooms`, `video_consents`; JWT: `video/jitsi.py` (kütüphanesiz).
- Ayarlar: `NFIZYO_VIDEO_URL`, `NFIZYO_VIDEO_APP_ID`, `NFIZYO_VIDEO_SECRET` (`core/config.py`). URL veya anahtar yoksa video kapalıdır,
  sayfalar «noch nicht eingerichtet» gösterir ve hiçbir şey dışarı gitmez.
- Testler: `tests/test_video.py`, pratikler arası ayrım `tests/test_tenant_isolation.py::test_video_never_crosses_practices`.
