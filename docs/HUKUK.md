# Hukuki belgeler (taslak)

Hepsi **taslak**; kullanmadan önce bir hukukçuya kontrol ettirilmeli. Almanca yazıldı (muayenehanelerle sözleşme dili).

- `docs/rechtliches/AVV_Entwurf.md` – NSystem ile muayenehane arasındaki veri işleme sözleşmesi (nDSG md. 9, StGB md. 321
  meslek sırrı), ek A: sistemde gerçekten uygulanan güvenlik önlemleri, ek B: alt işleyiciler (sunucu, SMS, MediData, HIN).
- `docs/rechtliches/Bearbeitungsverzeichnis_Vorlage.md` – muayenehanenin işleme kaydı şablonu (nDSG md. 12).
- `docs/rechtliches/Medizinprodukt_Abklaerung.md` – "tıbbi cihaz yazılımı mı?" sorusu için uzmana sorulacaklar
  (özellikle MTT artış önerisi). Karar değil, soru listesi.
- Hasta uygulamasında gizlilik metni: `/p/datenschutz` (11 dil, muayenehane bilgileriyle; giriş, davet, profil, video ve
  randevu teklif sayfalarından link var).
- Vertragsanhänge A (Preisblatt) ve C (Leistungsbeschreibung) + muayenehane için DSFA şablonu (nDSG md. 22): Claude Docs
  belgesi «N-Fizyo – Vertragsanhänge A und C, DSFA-Vorlage (Entwurf)» (12.11.2026). AVV 0.9.0'a göre güncellendi: destek
  erişimi (md. 4.5a), imzalı güncellemeler, silme süreleri, «App abmelden».
- **Datenauskunft (nDSG md. 25)**: Yönetim › Einstellungen › Export › «Datenauskunft für eine Person». Hasta numarası +
  Yönetim şifresi → o kişinin tüm verileri ZIP olarak doğrudan iner (tablolar CSV, belgeler, kendi egzersiz videoları;
  sunucuda kalmaz; şifreler, kodlar ve iç erişim protokolü yok). İşlem protokole yazılır. Cevap süresi 30 gün, ücretsiz.
