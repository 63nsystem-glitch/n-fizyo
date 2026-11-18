# GitHub üzerinden sunucuya kurulum

Her `main` yüklemesinde GitHub önce testleri çalıştırır (`Tests`). Testler yeşilse `Deploy` paketi doğrudan depodan yapar
(`git archive`, GitHub artifact kullanılmaz) ve sunucuya kurar. Elle başlatmak: GitHub › Actions › Deploy › «Run workflow».

N-Gastro'dan tamamen ayrı: ayrı depo, ayrı sunucu, ayrı SSH anahtarı, ayrı Secrets.

## Bir kez: anahtar (Windows, PowerShell)

```powershell
ssh-keygen -t ed25519 -C "github-deploy-fizyo" -f "$HOME\.ssh\fizyo_deploy"
```

Şifre sorarsa boş bırak (GitHub otomatik kullanacak). İki dosya oluşur:
- `fizyo_deploy.pub` → **açık** anahtar, gizli değil (sunucuya gider)
- `fizyo_deploy` → **özel** anahtar, sadece GitHub Secrets'a girer; sohbete, e-postaya, depoya asla

## Bir kez: sunucu (Netcup, Ubuntu 24.04, root)

```bash
scp deploy/server-bootstrap.sh root@<IP>:
ssh root@<IP> 'bash server-bootstrap.sh --pubkey "<fizyo_deploy.pub içeriği>" --domain fizyo.nsystem.ch --demo'
```

Betik: «deploy» kullanıcısı (şifresiz, sadece bu anahtarla, terminal/yönlendirme yok), `/usr/local/sbin/n-fizyo-deploy`
ve sadece bu programa sudo. Alan adı ve mod (`--demo`) sunucuda `/etc/n-fizyo-deploy.conf` içinde sabit; GitHub değiştiremez.
Sonunda `FIZYO_SSH_KNOWN_HOSTS` için gereken satırı yazar.

## Bir kez: GitHub Secrets (depo › Settings › Secrets and variables › Actions)

| Secret | İçerik |
|---|---|
| `FIZYO_SSH_HOST` | sunucunun IP'si |
| `FIZYO_SSH_KEY` | `fizyo_deploy` dosyasının tamamı (BEGIN … END dahil) |
| `FIZYO_SSH_KNOWN_HOSTS` | bootstrap'ın yazdığı satır: `<IP> ssh-ed25519 AAAA…` |

## Bir kez: DNS

`fizyo.nsystem.ch` için A kaydı → sunucunun IP'si (nsystem.ch'nin DNS'i neredeyse orada). Kayıt yokken de kurulum
yapılır, sertifika kayıt gelince kendiliğinden alınır.

## Sunucu ne yapar

| Durum | Ne çalışır |
|---|---|
| Henüz kurulu değil | `deploy/install.sh --demo --no-ai --domain <ad>` (demo) |
| Aynı sürüm numarası (0.9.0 kalırken her yükleme) | `deploy/update.sh --force` |
| Yeni sürüm | `deploy/update.sh` |
| Eski sürüm | `update.sh` reddeder |

`update.sh`: önce yedek, sonra geçiş, sağlık kontrolü; hata olursa kendiliğinden eski sürüme döner.

## Güvenlik notları

- Depoya yazabilen herkes sunucuya kod gönderebilir → `main` dalını koru (Settings › Branches: pull request ve yeşil test şartı).
- Özel anahtar sızarsa: sunucuda `/home/deploy/.ssh/authorized_keys` satırını sil, yeni anahtar, bootstrap'ı tekrar çalıştır.
- Demo sunucusunda gerçek hasta verisi asla olmaz. Gerçek muayenehane kutusu bu yolla güncellenmez
  (orada merkez panel veya servis kablosu: `docs/MERKEZ.md`, `docs/SERVIS_KABLOSU.md`).
