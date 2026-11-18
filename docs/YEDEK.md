# Yedekleme ve geri yükleme

## Ne yedekleniyor (her gece 02:30, 30 gün)

`/usr/local/sbin/n-fizyo-backup` (kaynak: `deploy/n-fizyo-backup`) → `/var/backups/n-fizyo/<tarih>/`

- Veritabanı (çalışırken tutarlı kopya, sonra `integrity_check` – bozuksa yedek "başarısız" sayılır)
- `documents.tar.gz` – posta kutusu dosyaları (raporlar, taramalar)
- `videos.tar.gz` – hastaların kendi egzersiz videoları (**0.7.1'den önce yedeklenmiyordu**)
- `audio.tar.gz` – henüz onaylanmamış diktatlar (**0.7.1'den önce yedeklenmiyordu**)

Güncellemeden önce de aynı yedek alınır (`pre-update-<zaman>`, servis durmadan önce; servis durduktan sonra veritabanı bir kez
daha); `update.sh` yedek betiğini de yeniler. `--rollback` için gereken son `pre-update-…` (yolu `/opt/n-fizyo/.last-update-backup`)
günlük temizlikte 30 günden sonra da **silinmez**; daha eski `pre-update-…` setleri normal silinir.
Her gecenin sonucu `/var/backups/n-fizyo/status.json`'a yazılır (başarılı/başarısız, saat, boyut) – runner bunu NSystem
paneline iletir (aşağıda «Durum»).

## Geri yükleme

```bash
sudo bash deploy/restore.sh --list                                   # yedekleri listele
sudo bash deploy/restore.sh --test /var/backups/n-fizyo/2026-11-01_0230   # sadece kontrol, hiçbir şey değişmez
sudo bash deploy/restore.sh /var/backups/n-fizyo/2026-11-01_0230          # geri yükle
```

Geri yüklemeden önce mevcut durum `before-restore-<zaman>` olarak yedeklenir, yani geri yükleme de geri alınabilir.
Servis durdurulur, veriler yerine konur, servis başlatılır ve `/health` kontrol edilir. Test: `tests/test_backup_restore.py`
(silme → geri yükleme → her şey yerinde).

## Dışarıda şifreli kopya (0.9.0, isteğe bağlı)

Yerel yedek aynı kutuda: disk bozulması, yangın, hırsızlık veya fidye yazılımında o da gider. Bu yüzden her gece yedeği
**şifreleyip başka bir yere** de gönderebilirsiniz.

**Nasıl çalışır:** gece yedeği bitince bütün set tek dosya olur (`n-fizyo-<tarih>.tar.gz.gpg`), **kutuda** GnuPG ile
şifrelenir (AES-256, parola dosyası sadece root'ta), sonra hedefe yüklenir (yanında `.sha256` kontrol dosyası). Hedefte
son **N set** kalır (`NFIZYO_OFFSITE_KEEP`, varsayılan 14), daha eskileri orada silinir. Hedefteki başka dosyalara dokunulmaz.
Sağlayıcı sadece şifreli dosya görür; parola sağlayıcıya ve NSystem'e gitmez.

**Hangi sağlayıcı:** muayenehane sahibinin kararı (açık). Aranacaklar: veri merkezi İsviçre'de, sağlayıcıyla veri işleme
sözleşmesi (nDSG), mümkünse **sürüm geçmişi / silme kilidi** (object lock, «immutable»). Kilit önemli: kutu ele geçirilirse
saldırgan hedefe de ulaşabilir; kilitli kopyaları silemez. Üç bağlantı yolu var:

| Yol | Ne zaman | `NFIZYO_OFFSITE_TARGET` örneği |
|---|---|---|
| rclone | bulut depolama: S3, OpenStack Swift, WebDAV, SFTP … | `rclone:yedek:n-fizyo/balance` |
| rsync + SSH | kendi sunucu / NAS (başka binada) | `yedek@yedek.ornek.ch:/yedek/n-fizyo/balance` |
| bağlı disk | ağ diski veya USB disk, önceden bağlanmış | `/mnt/yedek/n-fizyo` |

Her kutuya **ayrı klasör** (aynı klasörü iki kutu paylaşırsa birbirinin setlerini siler). Bağlı diskte klasör yoksa
(disk bağlı değil) kopya **yapılmaz** ve hata verir – yanlışlıkla kendi diske yazılmaz.

### Kurulum (bir kez, kutuda)

1. **Parola dosyası:** `sudo n-fizyo-backup --offsite-keygen` → `/etc/n-fizyo/offsite.key` (sadece root, `chmod 600`).
   Ekranda gösterilmez. **Kâğıda yazın:** `sudo cat /etc/n-fizyo/offsite.key` → kâğıda, muayenehanenin kasasına.
   Bu parola olmadan dışarıdaki kopya **açılamaz** (NSystem'de kopyası yok). Sohbete, e-postaya, Git'e asla yazılmaz.
   Dosya hiçbir zaman üzerine yazılmaz; parolayı değiştirirseniz eskisini de saklayın (eski setler onunla şifreli).
2. **Hedefe erişim** – birini seçin:
   - rclone: `sudo apt install rclone`, sonra `sudo rclone config` → yeni remote (örn. adı `yedek`, sağlayıcının verdiği
     bilgilerle). Erişim bilgileri `/root/.config/rclone/rclone.conf`'ta kalır (sadece root).
   - rsync + SSH: `sudo ssh-keygen -t ed25519 -f /root/.ssh/n-fizyo-offsite -N ''` → `.pub` dosyasını hedef sunucuya ekletin
     (mümkünse sadece rsync'e izin veren kısıtlamayla, örn. `rrsync`). Sunucunun anahtarı ilk bağlantıda kaydedilir.
   - bağlı disk: diski `/etc/fstab` ile kalıcı bağlayın, içinde klasör açın (`sudo install -d -m 0700 /mnt/yedek/n-fizyo`).
3. **Ayarlar** `/etc/n-fizyo/n-fizyo.env`'e (`sudo nano /etc/n-fizyo/n-fizyo.env`):

   ```
   NFIZYO_OFFSITE_TARGET=rclone:yedek:n-fizyo/balance
   NFIZYO_OFFSITE_KEY_FILE=/etc/n-fizyo/offsite.key
   NFIZYO_OFFSITE_KEEP=14
   # sadece rsync + SSH:
   NFIZYO_OFFSITE_SSH_KEY=/root/.ssh/n-fizyo-offsite
   # sadece rclone, başka bir yerdeyse:
   NFIZYO_OFFSITE_RCLONE_CONFIG=/root/.config/rclone/rclone.conf
   ```
4. **Deneme:** `sudo n-fizyo-backup --offsite-test` → küçük rastgele bir dosya (hasta verisi değil) şifrelenir, yüklenir,
   indirilir, açılır, silinir; dört satırda «ok» görünmeli.
5. **İlk kopya:** `sudo systemctl start n-fizyo-backup` (veya NSystem panelinden «Sicherung jetzt erstellen»), sonra
   `sudo bash /opt/n-fizyo/app/deploy/restore.sh --test --offsite latest` → indirip açar, kontrol eder, hiçbir şey değiştirmez.

### Dışarıdan geri yükleme

```bash
sudo bash deploy/restore.sh --offsite-list                                  # hedefteki setler (yeniden eskiye)
sudo bash deploy/restore.sh --test --offsite latest                         # indir, kontrol et, aç, kontrol et – değişiklik yok
sudo bash deploy/restore.sh --offsite n-fizyo-2026-11-01_0230.tar.gz.gpg   # indir ve geri yükle
```

İndirilen dosya önce `.sha256` ile, sonra şifrenin kendi bütünlük kontrolüyle denetlenir; yanlış parola veya değiştirilmiş
dosya → durur, hiçbir şey değişmez. Açılan set `/var/backups/n-fizyo/offsite-<zaman>-…/` altında kalır (sadece root;
test modunda hemen silinir, diğerleri 30 gün sonra). Geri kalan akış normal geri yüklemeyle aynı (önce `before-restore-…`).

**Kutu tamamen gittiyse:** yeni kutuya `deploy/install.sh` → `.env`'e yukarıdaki `NFIZYO_OFFSITE_*` satırları → parolayı
kâğıttan `/etc/n-fizyo/offsite.key`'e yazın (`sudo chmod 600 /etc/n-fizyo/offsite.key`) → hedef erişimi (rclone/SSH) →
`restore.sh --test --offsite latest` → `restore.sh --offsite latest`.

### Durum ve uyarılar

- NSystem paneline giden: yerel ve dış kopya için sadece durum (ok / başarısız / kapalı), saat, boyut (MB), hedefteki set
  sayısı ve kısa hata kodu. Hasta verisi, dosya adı, hedef adresi veya parola **gitmez** (`nfizyo/agent/runner.py`, testli).
- Dış kopya başarısız olursa yerel yedek yine tamamdır; servis hata koduyla biter, panelde uyarı açılır
  («Externe Sicherung fehlgeschlagen (…), letzte gelungene vor … h»), kontrol listesinde isteğe bağlı madde.

| Hata kodu | Anlamı |
|---|---|
| `key_missing` / `key_perm` | parola dosyası yok / root dışında okunabilir (`chmod 600`) |
| `gpg_missing` / `rclone_missing` | `sudo apt install gnupg` / `sudo apt install rclone` |
| `upload` / `list` / `verify` | hedefe yazılamadı (ağ, erişim, disk bağlı değil) / liste okunamadı / yüklenen set bulunamadı |
| `prune` | eski setler hedefte silinemedi (silme izni?) |
| `encrypt` / `config` | şifreleme hatası / ayar eksik |

- Elle tekrar göndermek: `sudo n-fizyo-backup --offsite-upload /var/backups/n-fizyo/<tarih>`.
- Sadece gece yedeği dışarı gider. `pre-update-…` ve `before-restore-…` kutuda kalır.
- Boyut: set ≈ veritabanı + dokümanlar + videolar (sıkıştırılmış). İlk ayda sağlayıcı faturasına bakın.
- Test: `tests/test_backup_offsite.py` – yerel klasör «hedef» rolünde (rsync yolu) ve sahte rclone ile: şifreli mi, N set,
  yabancı dosyalar kalıyor mu, geri yükleme, yanlış parola, değiştirilmiş dosya, hatalar, panele giden alanlar.

## NSystem paneli yedeği (0.9.0)

Merkez panel (NSystem sunucusu) de her gece aynı betikle yedeklenir: veritabanı + anahtar dosyaları (`keys.tar.gz`), isteğe bağlı
dışarıya şifreli kopya (aynı GnuPG yöntemi, **ayrı klasör**). Kurulum ve geri yükleme: `docs/MERKEZ.md` › «Panel yedeği».
Betiğin yeni ayarları: `NFIZYO_BACKUP_FILES` (sete eklenecek dosyalar), `NFIZYO_BACKUP_STATUS_OWNER` (durum dosyasını servis okusun).
Test: `tests/test_panel_backup.py`.

## Tam veri dışa aktarımı

Yönetim › Export › «vollständiger Datenexport» ZIP'i artık kendi egzersiz videolarını da içerir (`videos/`).
