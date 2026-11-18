# Yerel yapay zekâ

Her şey muayenehanenin kutusunda çalışır. Bulut servisi yok. Yapay zekâ adresi sadece yerel olabilir
(`NFIZYO_ALLOW_REMOTE_AI=0`), aksi halde program başlamaz.
Tek istisna: **sadece sunum** için bulut servisi (`NFIZYO_AI_MODE=cloud_demo`, aşağıda «Sunum için bulut YZ») –
yalnızca demo modunda ve kurgusal verilerle çalışır.

Kod: `nfizyo/ai/`

Kurulum betiği hepsini kurar ve ayarlar. Bir parça çalışmazsa program durmaz; yönetici ve terapistler her sayfanın üstünde turuncu bir uyarı görür, ayrıntı «Einstellungen → KI» sayfasında:

| Parça | Ayar (`/etc/n-fizyo/n-fizyo.env`) | Motor | Geçici olarak çalışmazsa |
|---|---|---|---|
| Konuşma → metin | `NFIZYO_STT=faster_whisper`, `NFIZYO_STT_MODEL`, `NFIZYO_STT_DEVICE` | faster-whisper (Whisper large-v3-turbo, GPU) | Diktat kaydedilir ama yazıya dökülmez; YZ gelince otomatik işlenir, istenirse yazarak devam |
| Not taslağı, rapor okuma | `NFIZYO_LLM=ollama`, `NFIZYO_LLM_MODEL` | Ollama (örn. qwen2.5:7b-instruct) | Kural motoru çalışır (DE/FR anahtar kelimeler, tarih, ölçüm) |
| Çeviri | `NFIZYO_TRANSLATE=ollama`, `NFIZYO_TRANSLATE_MODEL` | Ollama + translategemma:12b (55 dil) | Sadece hazır cümleler ve daha önce kaydedilmiş çeviriler |
| Tarama okuma | – | pypdf + Tesseract (deu, fra, ita, eng) | Sadece metin içeren PDF'ler |

Durum: `Einstellungen → KI` veya komut satırında:

```bash
cd /opt/n-fizyo/app && sudo -u nfizyo /opt/n-fizyo/venv/bin/python -m nfizyo --env /etc/n-fizyo/n-fizyo.env check-ai
```

## Güvenlik kuralları (kodda zorunlu)

- Yapay zekâ karar vermez. Her şey öneri; terapist onaylar.
- Not taslağında, transkriptte geçmeyen sayı varsa model çıktısı reddedilir (`ai/safety.py`).
- Rapordan çıkan her öneri, orijinal metinde birebir geçen bir alıntıya dayanmak zorunda; yoksa atılır.
- Olumsuzlama tanınır: «Nachtschmerz verneint», «pas de fièvre» → kırmızı bayrak sayılmaz.
- Çeviride sayılar farklıysa uyarı gösterilir.
- Hastaya giden metinler terapist onayından geçer. Hasta uygulaması tıbbi tavsiye vermez.
- Ses kayıtları not onaylanınca silinir. Hastanın şikâyet kaydındaki ses hiç saklanmaz.
- Hasta bilgisi: aynı diktattan iki kısa bölüm önerilir («evde ne yapmalı», «ne zaman haber vermeli»). YZ yeni sayı/egzersiz/tavsiye uydurursa öneri atılır, cümleler diktattan aynen alınır. Terapist onaylamadan hastaya hiçbir şey gitmez. Sesli okuma hastanın telefonundaki yerel sesle yapılır (metin internete gitmez).
- Ölçümlerde **diktatta söylenen eklem/taraf geçerlidir**; dosyadaki bulgu (örn. «Rotatorenmanschette links») sadece diktatta hiçbir şey
  söylenmemişse ve aynı eklemse kullanılır. Taraf bir ölçüme kesin bağlanamıyorsa (örn. «beidseits») taraf boş kalır – yanlış taraf asla.
  Diktat dosyadan farklıysa onay kartında kısa uyarı: «Im Diktat: Schulter rechts – in der Akte: Schulter links».
- Kural motoru: «Zu Hause …», «Melden Sie sich, wenn …» cümleleri Befund'a değil Plan'a (ve hasta bilgisine) gider; «Plan:», «Zu Hause:»
  gibi başlıklar hasta metnine kopyalanmaz; başlıklı diktatta Befund/Behandlung cümleleri hasta bilgisine girmez.
- Çeviri motoru yoksa onay kartı bunu söyler («nicht automatisch übersetzt»); uygulamada «çeviri yakında» sözü sadece çeviri işi
  beklerken görünür, sonra hastanın dilinde «Bu metin çevrilmedi – anlaşılmayan bir şey varsa sorun» notu.

## Donanım (muayenehane kutusu)

Yapay zekâ zorunlu. Kurulum betiği NVIDIA kartı ve en az 16 GB ekran kartı hafızası olmadan durur.

| Model | Ekran kartı hafızası |
|---|---|
| Whisper large-v3-turbo (ses → yazı) | ~2 GB |
| Qwen 2.5 7B (not, rapor okuma) | ~5 GB |
| TranslateGemma 12B (çeviri) | ~8 GB |

- **16 GB** (örn. RTX 5060 Ti 16 GB): hepsi çalışır; diktat, hasta bilgisi ve çeviri arasında modeller değişir, birkaç saniye bekleme olabilir.
- **24 GB** (örn. RTX PRO 4000 Blackwell SFF, 70 W, sessiz): hepsi aynı anda yüklü kalır.
- Diğer: 8 çekirdekli işlemci, 32 GB RAM, 2× 1 TB SSD aynalı (RAID 1), kesintisiz güç kaynağı (UPS), disk şifreleme (Ubuntu kurulumunda seçilir).
- Mac ve AMD kutular uygun değil: ses → yazı NVIDIA (CUDA) ister.

## Demo modu

Yapay zekâ kurulu değilse demo; diktat, hasta bilgisi ve çevirileri **açıkça etiketli** örneklerle gösterir
(«Demo – Spracherkennung simuliert», «Demo – simuliert»). Gerçek muayenehanede bu örnekler yoktur.

## Sunum için bulut YZ (`cloud_demo`) – sadece kurgusal verilerle

Satış sunumunda ekran kartı olmadan **gerçek** YZ göstermek için: konuşma → yazı (canlı diktat), not taslağı,
rapor okuma, hasta bilgisi önerisi ve çeviri, OpenAI uyumlu bir bulut servisi üzerinden çalışır. Aynı promptlar ve
**aynı güvenlik kontrolleri** kullanılır (diktatta olmayan sayı → taslak reddedilir, alıntı orijinalde yoksa öneri
atılır, hastaya giden her şey terapist onayından geçer). Kod: `nfizyo/ai/cloud.py`.

- **Sadece sunum, sadece kurgusal veri.** `NFIZYO_DEMO=1` değilse program başlamaz («N-Fizyo startet nicht:
  NFIZYO_AI_MODE=cloud_demo ist nur im Demo-Modus erlaubt …»). Veritabanında demo olmayan bir muayenehane varsa da
  başlamaz. Bunu kodda değiştirmeyin.
- **Gerçek hastalarla pilot için bu mod kullanılmaz:** İsviçre'deki GPU sunucusu kiralanır ve mevcut YZ sunucusu modu
  (`NFIZYO_AI_MODE=remote`) kullanılır.
- Anahtar (`NFIZYO_CLOUD_AI_KEY`) sadece laptoptaki `.env` dosyasında durur; log'a, ekrana ve `check-ai` çıktısına
  yazılmaz (`check-ai` sadece «gesetzt (…son 4 karakter)» gösterir). Sunumdan sonra anahtarı servis panelinden silin.
- Ekranda görünür: her sayfanın üstündeki demo şeridinde ve `Einstellungen › KI`'da «Demo: KI über externen Dienst
  <host> – nur für Vorführungen mit fiktiven Daten», ayrıca servis ve model adları. Bu modda «simuliert» örnekleri
  kullanılmaz; sonuçlar gerçektir.
- Servis yavaşsa veya cevap vermezse (`NFIZYO_CLOUD_AI_TIMEOUT`, varsayılan 60 sn): diktat kaydedilmiş kalır,
  servis geri gelince otomatik işlenir (2 dakikada bir kontrol); not taslağı kural motoruyla yapılır ve bu açıkça yazılır.

| Ayar | Anlamı |
|---|---|
| `NFIZYO_AI_MODE=cloud_demo` | Bu modu açar (sadece `NFIZYO_DEMO=1` ile) |
| `NFIZYO_CLOUD_AI_URL` | Servisin temel adresi (sadece HTTPS) |
| `NFIZYO_CLOUD_AI_KEY` | API anahtarı (sadece `.env`) |
| `NFIZYO_CLOUD_AI_STT_MODEL` | Konuşma → yazı modeli (`POST {URL}/audio/transcriptions`, dil ipucu de/fr) |
| `NFIZYO_CLOUD_AI_LLM_MODEL` | Not taslağı, rapor okuma, hasta bilgisi (`POST {URL}/chat/completions`) |
| `NFIZYO_CLOUD_AI_TRANSLATE_MODEL` | Çeviri modeli; boş = LLM modeli |
| `NFIZYO_CLOUD_AI_REASONING` | İsteğe bağlı `reasoning_effort` (hız için `none`); model tanımazsa parametresiz tekrar denenir |
| `NFIZYO_CLOUD_AI_TIMEOUT` | Saniye, istek başına (varsayılan 60) |

### Hazır ayarlar (11.11.2026'da resmî dokümanlardan kontrol edildi)

**Mistral (AB, Paris)** – önerilen:

```
NFIZYO_CLOUD_AI_URL=https://api.mistral.ai/v1
NFIZYO_CLOUD_AI_STT_MODEL=voxtral-mini-transcribe-3
NFIZYO_CLOUD_AI_LLM_MODEL=mistral-small-latest
NFIZYO_CLOUD_AI_REASONING=none
```

Kaynaklar (11.11.2026): https://docs.mistral.ai/studio/audio/speech-to-text/transcribe-files ,
https://docs.mistral.ai/api/endpoint/audio/transcriptions , https://docs.mistral.ai/models/mistral-small-4-0-26-03.md ,
https://docs.mistral.ai/capabilities/reasoning/adjustable , https://docs.mistral.ai/api/endpoint/chat

**OpenAI:**

```
NFIZYO_CLOUD_AI_URL=https://api.openai.com/v1
NFIZYO_CLOUD_AI_STT_MODEL=gpt-transcribe
NFIZYO_CLOUD_AI_LLM_MODEL=gpt-6-luna
NFIZYO_CLOUD_AI_REASONING=none
```

`gpt-transcribe` dil ipucunu `languages[]` alanıyla alır (N-Fizyo bunu kendisi seçer). `whisper-1`,
`gpt-4o-transcribe`, `gpt-4o-mini-transcribe` de çalışır ama OpenAI'ye göre 26.02.2027'de kapanacak.
Kaynaklar (11.11.2026): https://developers.openai.com/api/docs/guides/speech-to-text ,
https://developers.openai.com/api/reference/python/resources/audio/subresources/transcriptions/methods/create ,
https://developers.openai.com/api/docs/models , https://developers.openai.com/api/docs/models/gpt-6-luna ,
https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create

Model adları sık değişir: sunumdan önce `check-ai` çalıştırın; model yoksa hata mesajında görünür.

### Sahibinin laptopu: `.env` ve başlatma

Ayrı bir klasörde, örn. `~/n-fizyo/sunum.env` (sadece kurgusal demo verisi olan ayrı bir veri klasörü):

```
NFIZYO_DEMO=1
NFIZYO_SECRET_KEY=<python3 -c "import secrets; print(secrets.token_hex(32))" çıktısı>
NFIZYO_DATA_DIR=./data/sunum
NFIZYO_HOST=127.0.0.1
NFIZYO_PORT=8080
NFIZYO_BASE_URL=http://localhost:8080
NFIZYO_AI_MODE=cloud_demo
NFIZYO_CLOUD_AI_URL=https://api.mistral.ai/v1
NFIZYO_CLOUD_AI_KEY=<API anahtarı – sadece bu dosyada>
NFIZYO_CLOUD_AI_STT_MODEL=voxtral-mini-transcribe-3
NFIZYO_CLOUD_AI_LLM_MODEL=mistral-small-latest
NFIZYO_CLOUD_AI_REASONING=none
NFIZYO_CLOUD_AI_TIMEOUT=60
```

```bash
cd ~/n-fizyo
python3 -m nfizyo --env sunum.env reset-demo   # temiz demo verisi
python3 -m nfizyo --env sunum.env check-ai     # "ok": true görülmeli (llm, translate, stt)
python3 -m nfizyo --env sunum.env run          # tarayıcı: http://localhost:8080
```

Giriş: `therapie@demo.n-fizyo.ch` / `leitung@demo.n-fizyo.ch`, şifre `demo-praxis`.

### 5 dakikalık prova listesi

1. `check-ai`: `llm`, `translate`, `stt` hepsi `"ok": true`; anahtar sadece «gesetzt (…)» olarak görünür.
2. Tarayıcıyı laptopta **http://localhost:8080** ile açın (mikrofon sadece localhost veya HTTPS'te çalışır; başka
   cihazdan IP ile açılırsa mikrofon engellenir). `leitung@` ile `Einstellungen › KI`: «Demo: KI über externen Dienst …»
   ve model adları görünmeli.
3. Mikrofon izni: `therapie@` ile Heute › Ayşe'nin randevusu › Diktieren → tarayıcı izin sorar → «Zulassen/Allow».
   Sorulmazsa adres çubuğundaki kilit simgesi › Mikrofon › İzin ver.
4. Almanca diktat (~20 sn), örn. «Knie rechts Flexion 100 Grad, Schmerz 3. Mobilisation durchgeführt. Zu Hause
   zweimal täglich üben. Melden, wenn das Knie heiss wird.» → 10–30 sn içinde not taslağı ve hasta bilgisi önerisi.
   Diktatta olmayan bir sayı taslakta olmamalı.
5. «Mit der Notiz freigeben und an die Patienten-App senden (Türkisch)» işaretli olarak onaylayın → Ayşe için
   **Türkçe** çeviri birkaç saniyede gelir (hasta dosyası › Info; hasta uygulamasında da). «simuliert» etiketi yoktur.
6. Fransızca diktat: çıkış yapın, giriş sayfasında «Français» seçin, tekrar giriş, başka bir hastaya kısa bir
   Fransızca diktat (örn. «Genou gauche flexion 95 degrés …»).
7. Servis yavaşsa: diktat kaybolmaz, «Entwürfe zum Bestätigen» listesinde bekler ve servis cevap verince
   kendiliğinden işlenir. Beklemek istemezseniz «Text eingeben statt sprechen» ile yazarak devam edin.
