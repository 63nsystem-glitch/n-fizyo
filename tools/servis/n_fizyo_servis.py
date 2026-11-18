"""N-Fizyo Servis – NSystem'in dizüstü programı (Windows). Kutuya servis kablosuyla bağlanır, durumu gösterir, imzalı yeni
sürümü yükler, güncellemeyi başlatır ve izler; gerekirse yedek alır veya eski sürüme döner.

Çalıştırma (geliştirme):  py tools\\servis\\n_fizyo_servis.py
Tek dosya .exe:           tools\\servis\\build_windows.bat  → dist\\N-Fizyo-Servis.exe
Kurallar ve adımlar:      docs/SERVIS_KABLOSU.md
"""
from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from servis_client import BoxClient, ServiceError, create_key, default_key_path, load_key, public_key
except ImportError:  # paket olarak çağrıldıysa
    from tools.servis.servis_client import BoxClient, ServiceError, create_key, default_key_path, load_key, public_key

STATES = {"idle": "bekliyor", "running": "çalışıyor …", "done": "bitti ✓", "failed": "HATA", "unknown": "belirsiz"}
ACTIONS = {"update": "Güncelleme", "rollback": "Eski sürüme dönüş", "backup": "Yedek"}


def _age(hours) -> str:
    if hours is None:
        return "yedek yok!"
    return f"{hours:.0f} saat önce" if hours >= 1 else "1 saatten yeni"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("N-Fizyo Servis")
        self.geometry("980x680")
        self.minsize(860, 600)
        self.key_path = default_key_path()
        self.client: BoxClient | None = None
        self.manifest_path: Path | None = None
        self.uploaded = False
        self.polling = False
        self._build()
        self.after(300, self.check_key)

    # ------------------------------------------------------------------ arayüz
    def _build(self) -> None:
        style = ttk.Style(self)
        style.configure("Big.TButton", padding=(14, 8))
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Bold.TLabel", font=("Segoe UI", 10, "bold"))

        menu = tk.Menu(self)
        keym = tk.Menu(menu, tearoff=False)
        keym.add_command(label="Anahtar oluştur (ilk sefer)", command=self.make_key)
        keym.add_command(label="Açık anahtarı göster", command=self.show_pubkey)
        menu.add_cascade(label="Anahtar", menu=keym)
        self.config(menu=menu)

        top = ttk.Frame(self, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="N-Fizyo Servis", style="Title.TLabel").pack(side="left")
        self.conn_label = ttk.Label(top, text="Bağlı değil", foreground="#9a3412")
        self.conn_label.pack(side="left", padx=16)
        ttk.Button(top, text="Bağlan", style="Big.TButton", command=self.connect).pack(side="right")

        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(1, weight=1)

        # Durum
        st = ttk.LabelFrame(body, text="Kutunun durumu", padding=10)
        st.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.status_vars = {}
        for i, (key, label) in enumerate([("version", "Sürüm"), ("health", "Program"), ("services", "Servisler"),
                                          ("backup", "Son yedek"), ("disk", "Boş disk"), ("gpu", "Ekran kartı"),
                                          ("box", "Kutu kimliği")]):
            ttk.Label(st, text=label + ":", style="Bold.TLabel").grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="–")
            ttk.Label(st, textvariable=var, wraplength=300).grid(row=i, column=1, sticky="w", padx=8, pady=2)
            self.status_vars[key] = var
        ttk.Button(st, text="Yenile", command=self.refresh).grid(row=8, column=0, sticky="w", pady=(10, 0))
        ttk.Button(st, text="Şimdi yedekle", command=self.backup).grid(row=8, column=1, sticky="w", padx=8, pady=(10, 0))

        # Yeni sürüm
        up = ttk.LabelFrame(body, text="Yeni sürüm", padding=10)
        up.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ttk.Button(up, text="1. Paket seç (…manifest.json)", command=self.choose).pack(anchor="w")
        self.pkg_label = ttk.Label(up, text="Paket seçilmedi", wraplength=420)
        self.pkg_label.pack(anchor="w", pady=(4, 8))
        self.upload_btn = ttk.Button(up, text="2. Kutuya yükle ve kontrol ettir", command=self.upload, state="disabled")
        self.upload_btn.pack(anchor="w")
        self.diff_text = tk.Text(up, height=9, width=56, wrap="word", font=("Segoe UI", 9), relief="flat", background="#f6f6f4")
        self.diff_text.pack(fill="both", expand=True, pady=8)
        self.update_btn = ttk.Button(up, text="3. Güncellemeyi başlat", style="Big.TButton", command=self.start_update, state="disabled")
        self.update_btn.pack(anchor="w")

        # Akış
        lg = ttk.LabelFrame(body, text="Akış", padding=10)
        lg.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        bar = ttk.Frame(lg)
        bar.pack(fill="x")
        self.job_label = ttk.Label(bar, text="Görev yok", style="Bold.TLabel")
        self.job_label.pack(side="left")
        ttk.Button(bar, text="Eski sürüme dön…", command=self.rollback).pack(side="right")
        self.progress = ttk.Progressbar(lg, mode="indeterminate")
        self.progress.pack(fill="x", pady=6)
        self.log = tk.Text(lg, height=10, wrap="word", font=("Consolas", 9), relief="flat", background="#14211d", foreground="#d9e4e0")
        self.log.pack(fill="both", expand=True)

    def say(self, text: str) -> None:
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")

    # ------------------------------------------------------------------ arka planda çalıştır (pencere donmasın)
    def background(self, work, done=None, error_title="Hata") -> None:
        def run():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - hiçbir hata pencereyi kapatmasın
                msg = str(exc) if isinstance(exc, ServiceError) else f"{type(exc).__name__}: {exc}"
                self.after(0, lambda: (self.progress.stop(), self.say("HATA: " + msg), messagebox.showerror(error_title, msg)))
                return
            if done:
                self.after(0, lambda: done(result))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ anahtar
    def check_key(self) -> None:
        if not self.key_path.exists():
            self.say(f"Servis anahtarı yok ({self.key_path}). Menü › Anahtar › Anahtar oluştur.")

    def make_key(self) -> None:
        try:
            pub = create_key(self.key_path)
        except ServiceError as exc:
            messagebox.showerror("Anahtar", str(exc))
            return
        self.say(f"Anahtar oluşturuldu: {self.key_path}")
        self._show_pub(pub)

    def show_pubkey(self) -> None:
        try:
            self._show_pub(public_key(self.key_path))
        except ServiceError as exc:
            messagebox.showerror("Anahtar", str(exc))

    def _show_pub(self, pub: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(pub)
        messagebox.showinfo("Açık anahtar (gizli değil)",
                            f"{pub}\n\nPanoya kopyalandı. Kutuda bir kez:\n"
                            f"sudo bash deploy/service-port-setup.sh --interface <kart> --pubkey {pub}\n\n"
                            "Özel anahtar sadece bu bilgisayarda kalır – kimseye gönderme, sohbete yapıştırma.")

    # ------------------------------------------------------------------ bağlantı ve durum
    def connect(self) -> None:
        try:
            self.client = BoxClient(load_key(self.key_path))
        except ServiceError as exc:
            messagebox.showerror("Anahtar", str(exc) + "\n\nMenü › Anahtar › Anahtar oluştur.")
            return
        self.say("Kutu aranıyor (169.254.77.1) … Windows kablo takıldıktan sonra adresini 20–60 saniyede alır.")
        self.background(self.client.hello, self._connected, "Bağlantı")

    def _connected(self, hello: dict) -> None:
        if not hello.get("ready"):
            self.conn_label.config(text="Bağlı, ama kutuda servis anahtarı yok", foreground="#9a3412")
            self.say("Kutuda NFIZYO_SERVICE_PUBKEY yok – service-port-setup.sh ile girilmeli.")
            return
        self.conn_label.config(text=f"Bağlı · N-Fizyo {hello.get('version') or '?'}", foreground="#0e6e66")
        self.status_vars["box"].set(hello.get("box_id") or "(panele bağlı değil)")
        self.say(f"Bağlandı. Kurulu sürüm: {hello.get('version')}")
        self.refresh()

    def refresh(self) -> None:
        if not self.client:
            return
        self.background(self.client.status, self._show_status, "Durum")

    def _show_status(self, s: dict) -> None:
        v = self.status_vars
        v["version"].set(f"{s.get('version')}  ({len(s.get('modules') or {})} modül)")
        h = s.get("health") or {}
        v["health"].set("çalışıyor ✓" if h.get("ok") else "CEVAP VERMİYOR")
        sv = s.get("services") or {}
        v["services"].set(", ".join(f"{k}: {'✓' if val == 'active' else val}" for k, val in sv.items()) or "–")
        v["backup"].set(_age(s.get("backup_age_h")))
        v["disk"].set(f"{s.get('disk_free_gb')} GB" if s.get("disk_free_gb") is not None else "–")
        g = s.get("gpu")
        v["gpu"].set(f"{g.get('name')} · {g.get('mem_used_mb')}/{g.get('mem_total_mb')} MB · {g.get('temp_c')} °C"
                     if isinstance(g, dict) and g.get("name") else "yok / okunamadı")
        if s.get("pending"):
            p = s["pending"]
            self.say(f"Kutuda bekleyen kontrol edilmiş paket: {p.get('version')}")
            self.uploaded = True
            self.update_btn.config(state="normal")
        self._show_job(s.get("job") or {})

    # ------------------------------------------------------------------ paket
    def choose(self) -> None:
        name = filedialog.askopenfilename(title="Manifest seç", filetypes=[("N-Fizyo manifest", "*.manifest.json")])
        if not name:
            return
        self.manifest_path = Path(name)
        self.uploaded = False
        self.update_btn.config(state="disabled")
        self.pkg_label.config(text=str(self.manifest_path))
        self.upload_btn.config(state="normal" if self.client else "disabled")
        if not self.client:
            self.say("Önce «Bağlan».")

    def upload(self) -> None:
        if not (self.client and self.manifest_path):
            return
        self.say("Paket gönderiliyor ve kutuda kontrol ediliyor (imza, sağlama toplamı, sürüm) …")
        self.progress.start(12)
        path = self.manifest_path
        self.background(lambda: self.client.upload(path), self._uploaded, "Yükleme")

    def _uploaded(self, out: dict) -> None:
        self.progress.stop()
        d = out.get("diff") or {}
        lines = [f"Sürüm: {d.get('from')}  →  {d.get('to')}", ""]
        mods = d.get("modules") or {}
        lines.append(f"Değişen modüller ({len(mods)}):")
        lines += [f"  {k}: {a or 'yeni'} → {b or 'kaldırıldı'}" for k, (a, b) in mods.items()] or ["  yok"]
        db = d.get("db_changes") or []
        lines += ["", "VERİTABANI DEĞİŞİYOR: " + ", ".join(db) if db else "Veritabanı değişmiyor."]
        if d.get("changelog"):
            lines += ["", "Yenilikler:"] + [f"  • {c}" for c in d["changelog"]]
        self.diff_text.delete("1.0", "end")
        self.diff_text.insert("1.0", "\n".join(lines))
        self.uploaded = True
        self.update_btn.config(state="normal")
        self.say("Paket kontrol edildi ve kutuda bekliyor.")

    # ------------------------------------------------------------------ görevler
    def start_update(self) -> None:
        if not (self.client and self.uploaded):
            return
        if not messagebox.askyesno("Güncelleme", "Güncelleme başlasın mı?\n\n• Önce otomatik yedek alınır.\n"
                                   "• Program yaklaşık 1 dakika kapalı kalır – muayenehanede kimse çalışmıyor olmalı.\n"
                                   "• Hata olursa kutu kendiliğinden eski sürüme döner.\n\nKablo bitene kadar takılı kalsın."):
            return
        self.background(self.client.update, self._started, "Güncelleme")

    def rollback(self) -> None:
        if not self.client:
            return
        if not messagebox.askyesno("Eski sürüme dön", "DİKKAT: Önceki sürüme ve güncelleme ÖNCESİ veritabanına dönülür.\n\n"
                                   "Güncellemeden sonra girilen randevu, not ve faturalar KAYBOLUR.\n\nYine de dönülsün mü?",
                                   icon="warning"):
            return
        self.background(self.client.rollback, self._started, "Geri dönüş")

    def backup(self) -> None:
        if self.client:
            self.background(self.client.backup, self._started, "Yedek")

    def _started(self, out: dict) -> None:
        job = out.get("job") or {}
        self.say(f"{ACTIONS.get(job.get('action'), job.get('action'))} başladı.")
        self.update_btn.config(state="disabled")
        self._show_job(job)
        if not self.polling:
            self.polling = True
            self.after(3000, self.poll)

    def poll(self) -> None:
        def done(out):
            job = out.get("job") or {}
            self._show_job(job)
            if job.get("state") == "running":
                self.after(3000, self.poll)
            else:
                self.polling = False
                self._finished(job, out.get("version"))

        def fail_soft():
            try:
                return self.client.progress()
            except ServiceError:
                return {"job": {"state": "running", "action": "?", "log": ""}}   # kutu servisi yeniden başlıyor olabilir
        self.background(fail_soft, done, "Akış")

    def _show_job(self, job: dict) -> None:
        state = job.get("state") or "idle"
        name = ACTIONS.get(job.get("action"), "Görev")
        self.job_label.config(text=f"{name}: {STATES.get(state, state)}" if state != "idle" else "Görev yok",
                              foreground={"failed": "#b91c1c", "done": "#0e6e66"}.get(state, ""))
        if state == "running":
            self.progress.start(12)
        else:
            self.progress.stop()

    def _finished(self, job: dict, version) -> None:
        if job.get("log"):
            self.say("— kutunun çıktısı —\n" + str(job["log"])[-3000:])
        if job.get("state") == "done":
            self.say(f"{ACTIONS.get(job.get('action'), 'Görev')} bitti. Kurulu sürüm: {version}")
            messagebox.showinfo("Bitti", f"{ACTIONS.get(job.get('action'), 'Görev')} başarılı.\nKurulu sürüm: {version}\n\n"
                                "Şimdi 4 ekrana kısa bir giriş yapıp kontrol et.")
        else:
            messagebox.showerror("Olmadı", "Görev başarısız. Güncellemeyse kutu eski sürüme kendiliğinden döndü.\n"
                                 "Ayrıntı alttaki akışta.")
        self.refresh()


if __name__ == "__main__":
    App().mainloop()
