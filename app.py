#!/usr/bin/env python3
"""Kacou Audio — télécharge toutes les prédications audio (Kacou 1 → dernier)
depuis https://www.philippekacou.org/fr-fr/sermons.

Lancement :  python3 app.py
Aucune dépendance externe : uniquement la bibliothèque standard de Python.
"""

import json
import os
import queue
import re
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

SERMONS_URL = "https://www.philippekacou.org/fr-fr/sermons"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) KacouAudio/1.0"
DEFAULT_DIR = Path.home() / "Music" / "Kacou Philippe - Prédications"
CHUNK = 64 * 1024

# --- Palette -----------------------------------------------------------------
BG = "#0f1115"
CARD = "#181b22"
CARD_2 = "#1f232c"
BORDER = "#2a2f3a"
TEXT = "#e9e6df"
MUTED = "#8b909c"
GOLD = "#d4a64a"
GOLD_HOVER = "#e2b85f"
GREEN = "#5fbf7f"
RED = "#e06c6c"
BLUE = "#6ca8e0"

FONT = "Helvetica Neue"


# --- Réseau ------------------------------------------------------------------
def _ssl_context():
    """Python installé depuis python.org n'a pas toujours de certificats :
    on essaie certifi, puis les certificats système de macOS."""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()))


def http_open(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return OPENER.open(req, timeout=timeout)


def fetch_sermons():
    """Récupère la liste des prédications intégrée dans la page du site."""
    with http_open(SERMONS_URL) as r:
        html = r.read().decode("utf-8", errors="replace")
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    data = "".join(json.loads('"' + c + '"') for c in chunks)
    idx = data.find('"defaultSermons":')
    if idx < 0:
        raise RuntimeError("Liste des prédications introuvable sur la page.")
    arr, _ = json.JSONDecoder().raw_decode(data[data.index("[", idx):])
    sermons = []
    for s in arr:
        url = s.get("audio") or s.get("cloud_audio")
        if not url or s.get("number") is None:
            continue
        sermons.append({
            "number": int(s["number"]),
            "chapter": s.get("chapter") or f"Kacou {s['number']}",
            "title": (s.get("title") or "").strip(),
            "time": s.get("time") or "",
            "url": url,
        })
    sermons.sort(key=lambda s: s["number"])
    return sermons


def safe_filename(sermon):
    title = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", sermon["title"])
    title = re.sub(r"\s+", " ", title).strip(" .")
    name = f"Kacou {sermon['number']:03d}"
    if title:
        name += f" - {title}"
    return name[:150].rstrip(" .") + ".mp3"


def human_size(n):
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024 or unit == "Go":
            return f"{n:.0f} {unit}" if unit in ("o", "Ko") else f"{n:.1f} {unit}"
        n /= 1024


# --- Widgets -----------------------------------------------------------------
class RoundButton(tk.Canvas):
    """Bouton arrondi (les tk.Button natifs de macOS ignorent les couleurs)."""

    def __init__(self, parent, text, command, primary=True, width=170, height=42, bg=CARD):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command, self.primary = text, command, primary
        self.w, self.h = width, height
        self.enabled, self.hover = True, False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _colors(self):
        if not self.enabled:
            return (CARD_2, "#555a66", BORDER)
        if self.primary:
            return (GOLD_HOVER if self.hover else GOLD, "#1a1408", GOLD)
        return (CARD_2 if not self.hover else "#272c37", TEXT, BORDER if not self.hover else GOLD)

    def _draw(self):
        self.delete("all")
        fill, fg, outline = self._colors()
        r, w, h = 12, self.w - 2, self.h - 2
        pts = [1 + r, 1, w - r, 1, w, 1, w, 1 + r, w, h - r, w, h, w - r, h,
               1 + r, h, 1, h, 1, h - r, 1, 1 + r, 1, 1]
        self.create_polygon(pts, smooth=True, fill=fill, outline=outline, width=1)
        self.create_text(self.w / 2, self.h / 2, text=self.text, fill=fg,
                         font=(FONT, 13, "bold"))

    def _set_hover(self, value):
        self.hover = value
        self._draw()

    def _click(self, _):
        if self.enabled and self.command:
            self.command()

    def configure_state(self, enabled=None, text=None):
        if enabled is not None:
            self.enabled = enabled
            self.config(cursor="hand2" if enabled else "arrow")
        if text is not None:
            self.text = text
        self._draw()


def card(parent, **pack):
    outer = tk.Frame(parent, bg=BORDER)
    outer.pack(**pack)
    inner = tk.Frame(outer, bg=CARD, padx=20, pady=16)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    return inner


# --- Application -------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.sermons = []
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.folder = tk.StringVar(value=str(DEFAULT_DIR))
        self.range_from = tk.IntVar(value=1)
        self.range_to = tk.IntVar(value=1)
        self.parallel = tk.IntVar(value=3)
        self.bytes_done = 0
        self.speed_mark = (time.time(), 0)
        self.speed = 0.0

        root.title("Kacou Audio — Téléchargeur de prédications")
        root.configure(bg=BG)
        root.geometry("980x760")
        root.minsize(820, 620)
        self._styles()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)
        root.after(300, self.load_list)

    # ---- style ---------------------------------------------------------------
    def _styles(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Gold.Horizontal.TProgressbar", troughcolor=CARD_2,
                     background=GOLD, bordercolor=CARD_2, lightcolor=GOLD,
                     darkcolor=GOLD, thickness=12)
        st.configure("Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, rowheight=30, borderwidth=0, font=(FONT, 12))
        st.map("Treeview", background=[("selected", "#2b2f3a")],
               foreground=[("selected", TEXT)])
        st.configure("Treeview.Heading", background=CARD_2, foreground=MUTED,
                     relief="flat", borderwidth=0, font=(FONT, 11, "bold"), padding=(8, 8))
        st.map("Treeview.Heading", background=[("active", CARD_2)])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("Dark.TSpinbox", fieldbackground=CARD_2, background=CARD_2,
                     foreground=TEXT, arrowcolor=GOLD, bordercolor=BORDER,
                     lightcolor=CARD_2, darkcolor=CARD_2, insertcolor=TEXT, padding=6)

    # ---- interface -----------------------------------------------------------
    def _build(self):
        wrap = tk.Frame(self.root, bg=BG, padx=28, pady=22)
        wrap.pack(fill="both", expand=True)

        # En-tête
        head = tk.Frame(wrap, bg=BG)
        head.pack(fill="x", pady=(0, 18))
        logo = tk.Canvas(head, width=52, height=52, bg=BG, highlightthickness=0)
        logo.create_oval(2, 2, 50, 50, fill=GOLD, outline="")
        logo.create_text(26, 27, text="♪", fill="#1a1408", font=(FONT, 24, "bold"))
        logo.pack(side="left")
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left", padx=14)
        tk.Label(titles, text="Kacou Audio", bg=BG, fg=TEXT,
                 font=(FONT, 24, "bold")).pack(anchor="w")
        tk.Label(titles, text="Toutes les prédications de philippekacou.org, du Kacou 1 au dernier",
                 bg=BG, fg=MUTED, font=(FONT, 12)).pack(anchor="w")

        # Réglages
        c = card(wrap, fill="x")
        tk.Label(c, text="DOSSIER DE DESTINATION", bg=CARD, fg=MUTED,
                 font=(FONT, 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        path_box = tk.Frame(c, bg=CARD_2, highlightbackground=BORDER, highlightthickness=1)
        path_box.grid(row=1, column=0, columnspan=2, sticky="we", pady=(6, 14))
        tk.Label(path_box, textvariable=self.folder, bg=CARD_2, fg=TEXT, anchor="w",
                 font=(FONT, 12), padx=12, pady=10).pack(fill="x")
        self.btn_folder = RoundButton(c, "Changer…", self.choose_folder, primary=False, width=120)
        self.btn_folder.grid(row=1, column=2, padx=(10, 0), pady=(6, 14))
        c.columnconfigure(1, weight=1)

        opts = tk.Frame(c, bg=CARD)
        opts.grid(row=2, column=0, columnspan=3, sticky="w")

        def label(t):
            return tk.Label(opts, text=t, bg=CARD, fg=TEXT, font=(FONT, 12))

        label("Du Kacou").pack(side="left")
        self.sp_from = ttk.Spinbox(opts, from_=1, to=1, width=5, textvariable=self.range_from,
                                   style="Dark.TSpinbox", font=(FONT, 12))
        self.sp_from.pack(side="left", padx=8)
        label("au Kacou").pack(side="left")
        self.sp_to = ttk.Spinbox(opts, from_=1, to=1, width=5, textvariable=self.range_to,
                                 style="Dark.TSpinbox", font=(FONT, 12))
        self.sp_to.pack(side="left", padx=8)
        label("    Téléchargements simultanés").pack(side="left")
        self.sp_par = ttk.Spinbox(opts, from_=1, to=8, width=3, textvariable=self.parallel,
                                  style="Dark.TSpinbox", font=(FONT, 12))
        self.sp_par.pack(side="left", padx=8)

        # Actions + progression
        c2 = card(wrap, fill="x", pady=14)
        top = tk.Frame(c2, bg=CARD)
        top.pack(fill="x")
        self.btn_start = RoundButton(top, "⬇  Tout télécharger", self.start, width=200)
        self.btn_start.pack(side="left")
        self.btn_stop = RoundButton(top, "Arrêter", self.stop, primary=False, width=110)
        self.btn_stop.pack(side="left", padx=10)
        self.btn_open = RoundButton(top, "Ouvrir le dossier", self.open_folder, primary=False, width=160)
        self.btn_open.pack(side="right")
        self.btn_reload = RoundButton(top, "↻ Actualiser", self.load_list, primary=False, width=130)
        self.btn_reload.pack(side="right", padx=10)
        self.btn_start.configure_state(enabled=False)
        self.btn_stop.configure_state(enabled=False)

        stats = tk.Frame(c2, bg=CARD)
        stats.pack(fill="x", pady=(16, 8))
        self.lbl_count = tk.Label(stats, text="0 / 0", bg=CARD, fg=TEXT, font=(FONT, 20, "bold"))
        self.lbl_count.pack(side="left")
        self.lbl_detail = tk.Label(stats, text="", bg=CARD, fg=MUTED, font=(FONT, 12))
        self.lbl_detail.pack(side="left", padx=12, pady=(6, 0))
        self.lbl_pct = tk.Label(stats, text="0 %", bg=CARD, fg=GOLD, font=(FONT, 20, "bold"))
        self.lbl_pct.pack(side="right")
        self.progress = ttk.Progressbar(c2, style="Gold.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.pack(fill="x")

        self.status = tk.Label(wrap, text="Prêt.", bg=BG, fg=MUTED, anchor="w", font=(FONT, 11))
        self.status.pack(side="bottom", fill="x", pady=(10, 0))

        # Liste
        c3 = card(wrap, fill="both", expand=True)
        tree_box = tk.Frame(c3, bg=CARD)
        tree_box.pack(fill="both", expand=True)
        cols = ("num", "title", "time", "status")
        self.tree = ttk.Treeview(tree_box, columns=cols, show="headings", selectmode="none")
        for col, text, w, anchor, stretch in (
            ("num", "N°", 90, "w", False), ("title", "TITRE", 460, "w", True),
            ("time", "DURÉE", 100, "w", False), ("status", "STATUT", 150, "w", False),
        ):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=w, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("odd", background="#1b1e26")
        self.tree.tag_configure("done", foreground=GREEN)
        self.tree.tag_configure("active", foreground=GOLD)
        self.tree.tag_configure("error", foreground=RED)
        self.tree.tag_configure("skip", foreground=BLUE)
        sb = tk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ---- actions -------------------------------------------------------------
    def set_status(self, text, color=MUTED):
        self.status.config(text=text, fg=color)

    def choose_folder(self):
        if self.running:
            return
        d = filedialog.askdirectory(initialdir=self.folder.get(), title="Choisir le dossier")
        if d:
            self.folder.set(d)
            self._mark_existing()

    def open_folder(self):
        folder = Path(self.folder.get())
        folder.mkdir(parents=True, exist_ok=True)
        opener = "open" if sys.platform == "darwin" else ("explorer" if os.name == "nt" else "xdg-open")
        subprocess.Popen([opener, str(folder)])

    def load_list(self):
        if self.running:
            return
        self.set_status("Chargement de la liste depuis philippekacou.org…", GOLD)
        self.btn_start.configure_state(enabled=False)
        self.btn_reload.configure_state(enabled=False)

        def work():
            try:
                self.events.put(("list", fetch_sermons()))
            except Exception as e:
                self.events.put(("list_error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.sermons):
            self.tree.insert("", "end", iid=str(s["number"]),
                             values=(s["chapter"], s["title"], s["time"], "En attente"),
                             tags=("odd",) if i % 2 else ())
        self._mark_existing()

    def _mark_existing(self):
        folder = Path(self.folder.get())
        have = 0
        for s in self.sermons:
            exists = (folder / safe_filename(s)).exists()
            have += exists
            self._row(s["number"], "✓ Déjà présent" if exists else "En attente",
                      "skip" if exists else None)
        n = len(self.sermons)
        self.lbl_count.config(text=f"{have} / {n}")
        self.lbl_detail.config(text="déjà dans le dossier" if have else "prédications disponibles")
        pct = have * 100 / n if n else 0
        self.progress["value"] = pct
        self.lbl_pct.config(text=f"{pct:.0f} %")

    def _row(self, number, status, tag):
        iid = str(number)
        if not self.tree.exists(iid):
            return
        vals = list(self.tree.item(iid, "values"))
        vals[3] = status
        idx = self.tree.index(iid)
        tags = ["odd"] if idx % 2 else []
        if tag:
            tags.append(tag)
        self.tree.item(iid, values=vals, tags=tags)

    def _selection(self):
        try:
            a, b = int(self.range_from.get()), int(self.range_to.get())
        except (tk.TclError, ValueError):
            raise ValueError("Les numéros de début et de fin doivent être des nombres.")
        if a > b:
            a, b = b, a
        return [s for s in self.sermons if a <= s["number"] <= b]

    def start(self):
        if self.running or not self.sermons:
            return
        try:
            todo = self._selection()
        except ValueError as e:
            messagebox.showerror("Kacou Audio", str(e))
            return
        if not todo:
            messagebox.showinfo("Kacou Audio", "Aucune prédication dans cet intervalle.")
            return
        folder = Path(self.folder.get())
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Kacou Audio", f"Impossible de créer le dossier :\n{e}")
            return

        try:
            workers = max(1, min(8, int(self.parallel.get())))
        except (tk.TclError, ValueError):
            workers = 3
        self.running = True
        self.stop_event.clear()
        self.total = len(todo)
        self.finished = 0
        self.failed = 0
        self.bytes_done = 0
        self.speed_mark = (time.time(), 0)
        self.speed = 0.0
        self.partial = {}
        for s in todo:
            self._row(s["number"], "En attente", None)
        self._update_progress()
        for b in (self.btn_start, self.btn_reload, self.btn_folder):
            b.configure_state(enabled=False)
        self.btn_stop.configure_state(enabled=True)
        self.set_status(f"Téléchargement de {len(todo)} prédication(s) vers {folder}", GOLD)

        def run():
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for s in todo:
                    pool.submit(self._download, s, folder)
            self.events.put(("all_done", None))

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        if self.running:
            self.stop_event.set()
            self.btn_stop.configure_state(enabled=False)
            self.set_status("Arrêt en cours… (les fichiers incomplets sont supprimés)", RED)

    # ---- téléchargement (threads) ---------------------------------------------
    def _download(self, s, folder):
        n = s["number"]
        if self.stop_event.is_set():
            self.events.put(("item", (n, "Annulé", None, 0)))
            return
        target = folder / safe_filename(s)
        if target.exists() and target.stat().st_size > 0:
            self.events.put(("item", (n, "✓ Déjà présent", "skip", 1)))
            return
        tmp = target.with_suffix(".mp3.part")
        last_err = None
        for attempt in range(1, 4):
            try:
                with http_open(s["url"], timeout=60) as r, open(tmp, "wb") as f:
                    size = int(r.headers.get("Content-Length") or 0)
                    got, last_ui = 0, 0.0
                    while True:
                        if self.stop_event.is_set():
                            raise InterruptedError
                        buf = r.read(CHUNK)
                        if not buf:
                            break
                        f.write(buf)
                        got += len(buf)
                        self.events.put(("bytes", len(buf)))
                        now = time.time()
                        if now - last_ui > 0.3:
                            last_ui = now
                            pct = got / size if size else 0
                            txt = f"↓ {pct * 100:.0f} %" if size else f"↓ {human_size(got)}"
                            self.events.put(("item", (n, txt, "active", pct)))
                if size and got < size:
                    raise IOError("fichier incomplet")
                os.replace(tmp, target)
                self.events.put(("item", (n, f"✓ {human_size(got)}", "done", 1)))
                return
            except InterruptedError:
                tmp.unlink(missing_ok=True)
                self.events.put(("item", (n, "Annulé", None, 0)))
                return
            except Exception as e:
                last_err = e
                tmp.unlink(missing_ok=True)
                if self.stop_event.wait(2 * attempt):
                    self.events.put(("item", (n, "Annulé", None, 0)))
                    return
        self.events.put(("item", (n, "✗ Erreur", "error", -1)))
        self.events.put(("log", f"Kacou {n} : {last_err}"))

    # ---- boucle UI -------------------------------------------------------------
    def _poll(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "bytes":
                    self.bytes_done += data
                elif kind == "item":
                    n, text, tag, frac = data
                    self._row(n, text, tag)
                    if tag in ("done", "skip") and frac == 1:
                        self.finished += 1
                        self.partial.pop(n, None)
                    elif tag == "error":
                        self.failed += 1
                        self.partial.pop(n, None)
                    elif tag == "active":
                        self.partial[n] = frac
                    else:
                        self.partial.pop(n, None)
                elif kind == "log":
                    self.set_status(data, RED)
                elif kind == "list":
                    self._on_list(data)
                elif kind == "list_error":
                    self.btn_reload.configure_state(enabled=True)
                    self.set_status("Erreur de chargement : " + data, RED)
                    messagebox.showerror("Kacou Audio",
                                         f"Impossible de récupérer la liste :\n{data}\n\n"
                                         "Vérifie ta connexion Internet puis clique sur « Actualiser ».")
                elif kind == "all_done":
                    self._on_all_done()
        except queue.Empty:
            pass
        if self.running:
            self._update_progress()
        self.root.after(150, self._poll)

    def _update_progress(self):
        now = time.time()
        t0, b0 = self.speed_mark
        if now - t0 >= 1:
            inst = (self.bytes_done - b0) / (now - t0)
            self.speed = inst if self.speed == 0 else 0.6 * self.speed + 0.4 * inst
            self.speed_mark = (now, self.bytes_done)
        done = self.finished + self.failed
        frac = (done + sum(self.partial.values())) / self.total if self.total else 0
        self.progress["value"] = frac * 100
        self.lbl_pct.config(text=f"{frac * 100:.0f} %")
        self.lbl_count.config(text=f"{self.finished} / {self.total}")
        detail = f"{human_size(self.bytes_done)} téléchargés"
        if self.running and self.speed > 0:
            detail += f"  ·  {human_size(self.speed)}/s"
        if self.failed:
            detail += f"  ·  {self.failed} erreur(s)"
        self.lbl_detail.config(text=detail)

    def _on_list(self, sermons):
        self.sermons = sermons
        self.btn_reload.configure_state(enabled=True)
        if not sermons:
            self.set_status("Aucune prédication trouvée.", RED)
            return
        first, last = sermons[0]["number"], sermons[-1]["number"]
        for sp in (self.sp_from, self.sp_to):
            sp.config(from_=first, to=last)
        self.range_from.set(first)
        self.range_to.set(last)
        self._fill_tree()
        self.btn_start.configure_state(enabled=True)
        self.set_status(f"{len(sermons)} prédications trouvées (Kacou {first} → Kacou {last}).", GREEN)

    def _on_all_done(self):
        self.running = False
        self._update_progress()
        stopped = self.stop_event.is_set()
        for b in (self.btn_start, self.btn_reload, self.btn_folder):
            b.configure_state(enabled=True)
        self.btn_stop.configure_state(enabled=False)
        if stopped:
            msg, color = f"Arrêté. {self.finished} / {self.total} terminés.", MUTED
        elif self.failed:
            msg, color = (f"Terminé avec {self.failed} erreur(s). Relance « Tout télécharger » "
                          "pour réessayer uniquement les manquants."), RED
        else:
            msg, color = f"Terminé ! {self.finished} prédications dans {self.folder.get()}", GREEN
        self.set_status(msg, color)
        if not stopped:
            self.root.bell()

    def _on_close(self):
        if self.running and not messagebox.askyesno(
                "Kacou Audio", "Un téléchargement est en cours. Quitter quand même ?"):
            return
        self.stop_event.set()
        self.root.after(300, self.root.destroy)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
