# NSystem merkez paneli

İki parça:

| Parça | Nerede | Kod |
|---|---|---|
| Merkez panel | NSystem sunucusu (İsviçre), örn. `https://panel.nsystem.ch` | `nfizyo/central/` |
| Kutu ajanı + runner | her muayenehane kutusunda | `nfizyo/agent/` |

## Nasıl çalışır

- Kutu **5 dakikada bir** panele durum bildirir (sadece dışarıya bağlantı, kutuda açık port yok).
  İçerik: sürümler, disk, yedek yaşı, son yedeğin ve dışarıdaki şifreli kopyanın sonucu (ok/başarısız, saat, boyut), iş kuyruğu, hata sayıları, YZ durumu, muayenehane adı, hesap sayısı, 2FA'sız hesap sayısı,
  GLN/adres/IBAN var mı. **Hasta verisi yok** (testle kontrol edilir).
- Bildirim kutunun gizli anahtarıyla imzalı (HMAC); 5 dakikadan eski veya yanlış imzalı bildirim reddedilir.
- Panel uyarı açar ve **telefona bildirim** gönderir (ntfy): bildirim gelmiyor (30 dk), yedek eski (>30 saat) veya başarısız,
  dışarıdaki şifreli kopya başarısız (`docs/YEDEK.md`), disk dolu (>%85),
  kutunun saati 20 sn'den fazla kayık (bildirimde kaç saniye olduğu yazar; 2FA kodları 30 sn geçerli – kayık saat girişi bozar;
  5 dk'yı aşınca bildirim reddedilir ama kutu «sessiz» değil «saat yanlış» diye görünür), saat eşitleme (NTP) kapalı (bildirimsiz),
  YZ hazır değil, kuyruk takıldı, runner çalışmıyor, ekran kartı sıcak, komut başarısız,
  MediData client sertifikası 60 gün içinde bitiyor / bitti / kullanılamıyor (bildirimde sadece durum ve gün sayısı; `docs/VERBINDUNGSTEST.md`).
- Yeni kutu için **kontrol listesi** (otomatik): HTTPS, demo kapalı, 2FA zorunlu, tüm hesaplarda 2FA, YZ, yedek, disk, muayenehane bilgileri, SMS/HIN/MediData, dış yedek, destek erişimi hazır, sadece imzalı sürüm (son üçü isteğe bağlı).
- **Komutlar** (sabit liste): durum kontrolü, şimdi yedekle, N-Fizyo'yu yeniden başlat, YZ'yi yeniden başlat, yeni sürüm, eski sürüme dön,
  destek erişimini kapat (açamaz – aşağıda).
  - Panel komutu **Ed25519** ile imzalar; komut verirken o anki 2FA kodu istenir.
  - Kutu imzayı, kutu kimliğini, süreyi ve tekrarı kontrol eder; **runner (root)** aynı kontrolü bir kez daha yapar ve sadece listedekini çalıştırır.
  - Yeni sürüm: sadece `NFIZYO_RELEASE_BASE` adresinden, **NSystem release anahtarıyla imzalı manifest** (`NFIZYO_RELEASE_PUBKEY`)
    ve SHA-256 tutarsa, `update.sh` ile (yedek + otomatik geri dönüş). Ayrıntı: «Sürümler».
  - **Şifre sıfırlama yok**, veri okuma/dışa aktarma yok, keyfi komut yok.
- Muayenehane yönetimi her NSystem işlemini görür: Yönetim › Einstellungen › NSystem (komutlar + «Protokoll NSystem»:
  destek erişimi, bağlantılar, SSH komutları, talepler, cevaplar, duyurular).

Panel menüsü: **Boxen** (kutu başına «Support offen bis …»), **Support** (talepler, yeni sayısı), **Hinweise** (bakım duyuruları),
**Warnungen**, **Protokoll** (personel işlemleri). Panel metinleri Almanca.

## Kurulum (NSystem sunucusu)

```bash
sudo bash deploy/install-central.sh --domain panel.nsystem.ch --ntfy https://ntfy.sh/<gizli-konu>
cd /opt/n-fizyo-central/app
sudo -u nfizyo-central /opt/n-fizyo-central/venv/bin/python -m nfizyo.central --env /etc/n-fizyo-central/central.env add-staff --email ad@nsystem.ch --name "Ad Soyad"
```

İlk girişte 2FA kurulur (zorunlu). KI sunucusuyla aynı makinede çalışabilir (Caddy'de ayrı bölüm, port 8095). `install-ai-server.sh`
de Caddy'de sadece kendi bölümünü (`# n-fizyo-ai BEGIN/END`) yazar – panel ve release sitesi silinmez (0.9.0 QA).

## Yeni kutu bağlama

```bash
# NSystem sunucusunda (komutlar program klasöründe çalışır):
cd /opt/n-fizyo-central/app
sudo -u nfizyo-central /opt/n-fizyo-central/venv/bin/python -m nfizyo.central --env /etc/n-fizyo-central/central.env add-box --name "Physio Balance Biel"
# → /var/lib/n-fizyo-central/box-bx_XXXX.env (gizli anahtar içinde, sadece servis okur) – bunu kutuya kopyala (scp)
# Kutuda:
sudo bash /opt/n-fizyo/app/deploy/connect-central.sh /yol/box-bx_XXXX.env
```

Dosya kutuda okunduktan sonra güvenli silinir. Yeni sürümler için kutuda ayrıca: `NFIZYO_RELEASE_BASE=https://releases.nsystem.ch` (örnek).

## Telefona bildirim

`ntfy` uygulamasını kur, `NFIZYO_CENTRAL_NTFY_URL` içindeki konuyu abone ol. Konu adı tahmin edilemez olmalı
(veya kendi ntfy sunucusu + `NFIZYO_CENTRAL_NTFY_TOKEN`). Bildirimde sadece kutu adı ve sorun yazar.

## Sürümler (0.9.0)

Sürüm NSystem'in **kendi bilgisayarında** paketlenir ve imzalanır; özel anahtar sunucuda veya Git'te durmaz.

```bash
export NFIZYO_RELEASE_KEY=$HOME/.nsystem/release.pem        # projenin DIŞINDA (içindeyse araç reddeder)
python3 tools/release.py keygen                             # bir kez: Ed25519 anahtar (0600, üzerine yazılmaz) + açık anahtar
python3 tools/release.py build                              # dist/: n-fizyo-<sürüm>.tar.gz + .sha256 + imzalı .manifest.json
python3 tools/release.py publish --dir $HOME/nsystem-releases --rsync nsystem@panel.nsystem.ch:/srv/n-fizyo-releases
python3 tools/release.py verify dist/n-fizyo-0.9.0.manifest.json --pubkey <açık-anahtar>
```

- Paket: `nfizyo/`, `deploy/`, `docs/`, `third_party/`, README, `requirements*.txt/.lock`, `.env.example`. **Yok:** `tests/`, `tools/`,
  `.github/`, kökteki `data/`, `dist/`, önbellekler, `*.env`, `*.key`, `*.pem`, `*.sqlite3`, `*.gpg`. Aynı kaynak → aynı SHA-256
  (`SOURCE_DATE_EPOCH` ile sabit zaman).
- Manifest: sürüm, tarih, dosya, SHA-256, boyut, README'deki «Yeni (sürüm …)» bölümünden kısa liste, modül sürümleri, sahip başına
  son migration (veritabanı değişiyor mu). `index.json` bütün manifestleri tutar, o da imzalı. Önce dosyalar, en son index yüklenir.
- Açık anahtar (gizli değil) iki yere: panelde `NFIZYO_CENTRAL_RELEASE_PUBKEY`, kutuda `NFIZYO_RELEASE_PUBKEY`
  (`add-box` dosyaya yazar; eski kutularda `.env`'e elle).
- **Sunucu (statik, Caddy):** `install-central.sh --domain panel.nsystem.ch --releases-domain releases.nsystem.ch` →
  `/srv/n-fizyo-releases` HTTPS ile verilir (DNS: `releases.nsystem.ch` → panel sunucusu). Yükleyen kullanıcıya klasörü ver:
  `sudo chown nsystem: /srv/n-fizyo-releases`. Caddy bölümü:

  ```
  releases.nsystem.ch {
  	root * /srv/n-fizyo-releases
  	file_server
  	header Cache-Control "no-cache"
  }
  ```
  `central.env`: `NFIZYO_CENTRAL_RELEASE_DIR=/srv/n-fizyo-releases` (aynı sunucu) veya `NFIZYO_CENTRAL_RELEASE_BASE=https://releases.nsystem.ch`;
  kutular: `NFIZYO_RELEASE_BASE=https://releases.nsystem.ch`. Kontrol: `python -m nfizyo.central --env … releases`.
- **Panelde:** kutu sayfası › «Neue Version»: listeden sürüm seçilir (değişiklik özeti, SHA-256, veritabanı durumu görünür),
  dosya ve SHA-256 **otomatik** dolar, personel sadece 2FA koduyla onaylar. İstenirse **zaman** seçilir (kutu komutu ancak o zaman
  alır, 60 dk geçerli) ve otomatik **bakım duyurusu** (de/fr: «Am … um … wird N-Fizyo aktualisiert …», şimdiden güncellemeden 45 dk
  sonrasına kadar). Planlı komut zamanından önce «Abbrechen» ile iptal edilir (duyuru da geri çekilir).
- **Kutuda (runner):** önce manifest indirilir, imza kontrol edilir; dosya adı + SHA-256 komutla ve manifestle aynı mı; arşiv
  indirilir, SHA-256 tekrar; paketteki sürüm manifestle aynı mı – **ancak hepsi tutarsa** `update.sh`. Sadece https (yönlendirmede de).
  `NFIZYO_RELEASE_PUBKEY` yoksa güncelleme yapılmaz.
- **Eski sürüm yok (0.9.0 QA):** «Neue Version» sadece kurulu sürümden **yeni** bir sürüme; kurulu sürüm okunamazsa da reddedilir.
  Runner bir kez kurulan en yüksek sürümü kendi klasöründe (`/var/lib/n-fizyo-runner/release.json`, sadece root) tutar: ondan eski
  imzalı paketler asla kabul edilmez (aynısı, `rollback` sonrası tekrar kurulabilir). Geri dönüş sadece «Rollback» komutuyla
  (`update.sh --rollback`, önceki sürüm + güncelleme öncesi veritabanı). `update.sh` de aynı sürümü «nichts zu tun» ile geçer,
  eskisini reddeder; `--force` sadece kutuda elle, runner'dan asla.
- **Runner klasörleri:** `/var/lib/n-fizyo/runner/` (spool, local, results, events, status.json) uygulama kullanıcısına ait; runner (root)
  orada sadece `nfizyo/agent/safefs.py` ile çalışır: sembolik bağlara uymaz (O_NOFOLLOW, klasör tanıtıcıları), sadece yeni dosya yazar
  ve yerinde değiştirir, komut kimliği sadece `[A-Za-z0-9_-]` (dosya adı). Uygulama ele geçirilse bile root olamaz.

## Destek erişimi – Supportzugang (0.9.0)

Kutuya uzaktan bakmak için. **Sadece muayenehanenin yönetimi (Leitung) açar**: Einstellungen › NSystem › «Supportzugang öffnen»
(1–8 saat, varsayılan 4) veya «Hilfe › Support anfragen»da «Supportzugang gleich öffnen». Açıkken kalan süre geri sayılır, her Leitung
sayfasında üstte «offen bis …» yazar, istenince «Jetzt schliessen». Süre dolunca **kendiliğinden kapanır** – uygulama çalışmasa da
(runner her 20 sn bakar + ayrıca systemd zamanlayıcısı `n-fizyo-support-expire`; kutu yeniden başlarsa süre içindeyse tünel geri
kurulur, süre geçmişse kapalı kalır). NSystem paneli erişimi **açamaz** (komut yok), sadece «Supportzugang schliessen» ile kapatabilir.

**Açıkken NSystem bu kutuya teknik olarak erişebilir.** Bu yüzden muayenehanenin açık onayı gerekir ve kurallar veri işleme
sözleşmesinde (AVV) yazmalı: ne zaman, ne için, kim, neyi göremez, kayıtlar. Uygulama da bunu açma düğmesinin yanında söyler.

Nasıl çalışır:
1. Uygulama (Leitung tıklayınca) runner'a bir iş bırakır: `/var/lib/n-fizyo/runner/local/<id>.json`, `NFIZYO_SECRET_KEY`'den türetilmiş
   HMAC ile imzalı, 10 dk geçerli, bir kez. Runner (root) imzayı, süreyi (1–8 h), tekrarı kontrol eder.
2. Runner **WireGuard tünelini** NSystem hub'ına kurar (`nfz-support`, sadece dışarıya; kutuda internete açık port yok). Tünelden sadece
   hub görünür (`AllowedIPs = hub/32`) ve kutuda sadece hub'dan **port 22** kabul edilir (iptables, sadece bu arayüz).
3. NSystem'in **açık** SSH anahtarları `~nfizyo-support/.ssh/authorized_keys`'e yazılır (dosya root'un): `from="<hub>"`, `restrict`
   (terminal/pty yok), `command="/usr/local/sbin/n-fizyo-support-tool"`. Ayrıca (`deploy/support-setup.sh`): sshd bu kullanıcıyı hub
   dışındaki her adresten reddeder (`DenyUsers nfizyo-support@*,!10.77.0.1`) ve `Match User nfizyo-support` ile aracı zorlar
   (`ForceCommand`, `PermitTTY no`, yönlendirme yok); kullanıcının giriş kabuğu **aracın kendisi** (bash değil); şifresi kilitli;
   `/etc/cron.deny` ve `/etc/at.deny`'da. Kapanınca anahtarlar silinir, açık oturumlar sonlandırılır, crontab/at işleri ve «linger»
   silinir, tünel iner.
4. Her adım kayda geçer (Yönetim › Einstellungen › NSystem › Protokoll; panelde kutu sayfası): açıldı (kim, kaç saat), tünel bağlandı,
   SSH girişi/çıkışı/reddi, **her komut** (`logs n-fizyo 200` gibi), kapandı (erken / süre doldu / NSystem / hata). Panel açılınca
   telefona bildirim gelir. Panele isim gitmez, sadece tür ve saat.

**Destek kullanıcısı (`nfizyo-support`) ne yapabilir:** kabuk (shell) yok, sadece sabit araç (`deploy/n-fizyo-support-tool`; satırlar
düz okunur – `read -e`/Readline yok, editör veya pager açılamaz; izin verilmeyen her deneme de `denied <ilk kelime>` olarak kayda geçer):
`status`, `health`, `logs <n-fizyo|n-fizyo-runner|ollama|caddy|n-fizyo-backup|ssh> [satır]`, `versions`, `check-ai`,
`check-connections` (sadece okur), `backups`, `backup-test <set>` (değiştirmez), `offsite-test`, `disk`, `gpu`, `net`,
`restart <n-fizyo|ollama|caddy>`. **Yapamaz:** dosya/veritabanı okumak, veri dışa aktarmak, şifre değiştirmek, hesap açmak, keyfi komut,
root kabuğu. Teknik günlükler hasta içeriği yazmayacak şekilde tasarlandı; yine de erişim açıkken okunabilirler – AVV'ye girer.

Bir sunucuda **birden fazla muayenehane** varsa (yol 3) erişim bütün sunucuyu kapsadığı için uygulamadan açılamaz.

### Hub kurulumu (NSystem sunucusu, bir kez)

```bash
sudo bash deploy/install-support-hub.sh                     # WireGuard «nfz-hub», 10.77.0.1/24, UDP 51820 (ufw açılır)
# Ekrana çıkan açık hub anahtarını ve adresi central.env'e yaz (gizli değil):
#   NFIZYO_CENTRAL_SUPPORT_HUB_PUBKEY=…   NFIZYO_CENTRAL_SUPPORT_ENDPOINT=hub.nsystem.ch:51820   NFIZYO_CENTRAL_SUPPORT_NET=10.77.0.0/24
#   NFIZYO_CENTRAL_SUPPORT_SSH_KEYS=ssh-ed25519 AAAA… nsystem-support      (birden fazla: «;» ile)
#   NFIZYO_CENTRAL_SUPPORT_SSH_HOST=admin@hub.nsystem.ch                  (panelde «ssh -J …» ipucu için)
sudo systemctl restart n-fizyo-central
```

- DNS: `hub.nsystem.ch` → bu sunucu. Hub'ın özel anahtarı sadece `/etc/wireguard/nfz-hub.key` (root, 0600).
- SSH özel anahtarı NSystem çalışanının bilgisayarında kalır (mümkünse donanım anahtarı, `sk-ssh-ed25519`); hub'da durmaz.
- Hub kutular arasında yönlendirme yapmaz (ip_forward kapalı): kutular birbirini görmez.

### Kutu başına (bir kez)

```bash
# Kutuda (install.sh/update.sh bunu zaten çalıştırır; hiçbir şey açmaz):
sudo bash /opt/n-fizyo/app/deploy/support-setup.sh --show-key       # kutunun AÇIK WireGuard anahtarı
# NSystem sunucusunda:
cd /opt/n-fizyo-central/app && sudo -u nfizyo-central /opt/n-fizyo-central/venv/bin/python -m nfizyo.central --env /etc/n-fizyo-central/central.env \
     support-peer --box bx_XXXX --pubkey <kutunun-açık-anahtarı>
# → tünel adresi (10.77.0.10 …), hub peer dosyası /var/lib/n-fizyo-central/wg-peers.conf (hub kendisi alır: nfz-hub-sync.path)
# → /var/lib/n-fizyo-central/support-bx_XXXX.env (sadece açık değerler) – kutuya kopyala, kutuda:
sudo bash /opt/n-fizyo/app/deploy/connect-central.sh /yol/support-bx_XXXX.env
```

Bağlanmak (sadece erişim açıkken): `ssh -J admin@hub.nsystem.ch nfizyo-support@10.77.0.X` (panel kutu sayfası komutu gösterir).
Kutu ayarları: `NFIZYO_SUPPORT_HUB`, `_HUB_PUBKEY`, `_ADDRESS`, `_HUB_IP`, `_SSH_KEYS` (`/etc/n-fizyo/n-fizyo.env`).

## Destek talepleri (0.9.0)

Her personel rolü (Therapie, Empfang, Leitung): menüde **Hilfe** › «Support anfragen»: alan (Agenda, Dokumentation, Abrechnung,
Patienten-App, Technik, Anderes), aciliyet (kann warten / normal / dringend), kısa açıklama; Leitung için «Supportzugang gleich öffnen».

- **Hasta verisi çıkmaz:** formda açık uyarı. Metinde bu muayenehanenin bir hastasının **tam adı** (ad soyad / soyad ad, aksansız da),
  **hitap + soyadı** («Frau Örnek», «Herr Muster», «Mme/M./Madame/Monsieur …», «Patientin …») veya **baş harf + soyadı** («L. Muster»),
  **hasta numarası** («P00004», «Dossier 1002»), **doğum tarihi** (veya öyle görünen tarih: 1900 … geçen yıldan önceki yıl; 12.03.1985,
  12-03-1985, «12. März 1985», «Jg. 1985»), **AHV** (756.xxxx.xxxx.xx, tireli de), **sigortalı/dosya numarası** (kart 80756…,
  muayenehanedeki numaralar), **telefon** (0'sız «79 123 45 67» de), **e-posta** («ad(at)alan.ch» de) varsa gönderim **engellenir** ve
  ne bulunduğu söylenir (`core/patientdata.py`). Görünmez karakterler (yumuşak tire, sıfır genişlikte boşluk) silinir, her tür tire «-»
  sayılır, ö = oe = o, ü = ue = u, ä = ae = a. **Sadece ön ad** («Sandra …») ve **sadece soyad** (hitapsız) bilerek engellenmez (çok yanlış
  alarm) – Serhat'ın kararı. Engellenen metin hiçbir yere kaydedilmez. Panel aynı kalıpları bir kez daha kontrol eder; tutarsa metni
  **saklamaz** (sadece «blockiert» ve tür). Panel kutunun raporunu saklarken talep metinlerini çıkarır.
- **Panele bağlı değilse** (`NFIZYO_CENTRAL_*` yok, örn. demo): Hilfe sayfası bunu açıkça söyler («nicht mit NSystem verbunden … info@nsystem.ch»),
  talep kaydedilir ama «Nicht gesendet» görünür; «5 dakika içinde» sözü verilmez. Bağlantı kurulunca bekleyen talepler giderek gönderilir.
- Gönderim: bir sonraki 5 dakikalık imzalı raporla. Panelde **Support** kutusu: durum **neu / in Bearbeitung / erledigt**, yeni talepte
  telefona bildirim (sadece kutu, alan, aciliyet – metin yok). Cevap (panel cevapta da hasta verisi kalıplarını reddeder) bir sonraki
  raporla imzalı olarak kutuya gider; muayenehane Hilfe sayfasında görür, menüde rozet.
- Leitung muayenehanenin bütün taleplerini, diğerleri kendi taleplerini görür.

## Bakım duyuruları (0.9.0)

Panel › **Hinweise**: Almanca + Fransızca metin, başlangıç/bitiş (Zürih saati), bütün kutular veya seçilenler. Kutu bir sonraki
raporla alır (panel anahtarıyla, kutuya özel imzalı). Süre içinde **sadece personel** ekranlarında üstte bant (hasta uygulamasında
yok), kişi başına kapatılabilir. Panelde «Zurückziehen» → bir sonraki raporla kaybolur. Planlı güncelleme otomatik duyuru ekleyebilir.

## Panel yedeği (0.9.0)

`install-central.sh` her gece 03:15 `n-fizyo-central-backup` çalıştırır – kutudaki yedekle aynı betik (`deploy/n-fizyo-backup`):

- `/var/backups/n-fizyo-central/<tarih>/`: `central.sqlite3` (tutarlı kopya + kontrol) ve `keys.tar.gz`: `central.env` (gizli anahtar:
  kutu anahtarları ve 2FA şifreli), `command-signing.pem` (komut imzası), varsa `/etc/wireguard/nfz-hub.key` + `nfz-hub.conf`. 30 gün.
  `box-*.env` bağlantı dosyaları yedeğe girmez (kopyalanınca silinmeleri gerekir).
- Dışarıya şifreli kopya (önerilir, `docs/YEDEK.md` ile aynı): `central.env`'e `NFIZYO_OFFSITE_TARGET` (kutulardan **ayrı klasör**),
  `NFIZYO_OFFSITE_KEY_FILE=/etc/n-fizyo-central/offsite.key`; anahtar: `sudo NFIZYO_ENV_FILE=/etc/n-fizyo-central/central.env
  n-fizyo-backup --offsite-keygen` → **kâğıda**. Deneme: aynı ortamla `--offsite-test`.
- Sonuç `/var/lib/n-fizyo-central/backup-status.json` → panel ana sayfasında; başarısız, 30 saatten eski veya dış kopya yoksa
  telefona bir kez bildirim.
- Release özel anahtarı panelde değil: kendi bilgisayarında, ayrıca çevrimdışı kopya.

**Geri yükleme (panel):**

```bash
sudo systemctl stop n-fizyo-central
E="NFIZYO_ENV_FILE=/etc/n-fizyo-central/central.env NFIZYO_BACKUP_DIR=/var/backups/n-fizyo-central"
sudo env $E n-fizyo-backup --offsite-fetch latest /root/panel-restore     # veya yerel: /var/backups/n-fizyo-central/<tarih>
SET=/root/panel-restore/<tarih>                                             # son satırda yazan klasör
sudo tar -C / -xzf $SET/keys.tar.gz                                         # central.env, command-signing.pem, hub anahtarı
sudo install -o nfizyo-central -g nfizyo-central -m 0600 $SET/central.sqlite3 /var/lib/n-fizyo-central/central.sqlite3
sudo rm -f /var/lib/n-fizyo-central/central.sqlite3-wal /var/lib/n-fizyo-central/central.sqlite3-shm
sudo chown nfizyo-central /var/lib/n-fizyo-central/command-signing.pem; sudo chown root:nfizyo-central /etc/n-fizyo-central/central.env
sudo systemctl start n-fizyo-central; sudo systemctl restart wg-quick@nfz-hub 2>/dev/null || true
```

Sunucu tamamen gittiyse: yeni sunucuda `install-central.sh` (aynı alan adları), sonra yukarıdaki adımlar (dış kopya için önce
`central.env`'e `NFIZYO_OFFSITE_*` ve kâğıttaki anahtar). Aynı `command-signing.pem` ve `central.env` geri gelince kutular hiçbir
değişiklik olmadan bağlanmaya devam eder; hub anahtarı geri gelince tüneller de.
