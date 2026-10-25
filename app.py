#!/usr/bin/env python3
"""Kacou Audio — télécharge les prédications audio (Kacou 1 → dernier)
de https://www.philippekacou.org, dans la langue de son choix.

Lancement :  python3 app.py
Aucune dépendance externe : uniquement la bibliothèque standard de Python.
"""

import json
import os
import queue
import re
import shutil
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

SITE = "https://www.philippekacou.org"
API = "https://api.philippekacou.org/api"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) KacouAudio/2.0"
# Une fois empaqueté (PyInstaller), les fichiers sont dans sys._MEIPASS
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
DEFAULT_DIR = Path.home() / "Music" / "Kacou Philippe - Prédications"
CONFIG_FILE = Path.home() / ".kacou_audio.json"
DEFAULT_LANG = "fr-fr"
SCAN_MAX_AGE = 12 * 3600  # re-vérifier les audios disponibles toutes les 12 h
CHUNK = 64 * 1024

# --- Palette -----------------------------------------------------------------
BG = "#0f1115"
CARD = "#181b22"
CARD_2 = "#1f232c"
ROW_ALT = "#1c2029"
BORDER = "#2a2f3a"
TEXT = "#e9e6df"
MUTED = "#8b909c"
DIM = "#5c616d"
GOLD = "#d4a64a"
GOLD_HOVER = "#e2b85f"
GREEN = "#5fbf7f"
RED = "#e06c6c"
BLUE = "#6ca8e0"

FONT = "Segoe UI" if os.name == "nt" else "Helvetica Neue"


# --- Réglages ----------------------------------------------------------------
def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


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


def http_json(url):
    with http_open(url) as r:
        return json.loads(r.read().decode("utf-8"))


def api_list(path):
    """Lit toutes les pages d'une liste de l'API du site."""
    base = f"{API}/{path}?per_page=500"
    first = http_json(base)
    items = list(first.get("data") or [])
    last = int((first.get("meta") or {}).get("last_page") or 1)
    for page in range(2, min(last, 50) + 1):
        items += http_json(f"{base}&page={page}").get("data") or []
    return items


def safe_name(text):
    text = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", text)
    return re.sub(r"\s+", " ", text).strip(" .")


def fetch_languages():
    langs = []
    for item in api_list(f"{DEFAULT_LANG}/langues"):
        code = (item.get("initial") or "").strip()
        if not code:
            continue
        name = (item.get("libelle") or code).strip()
        name = name[:1].upper() + name[1:]
        langs.append({
            "code": code,
            "name": name,
            "country": ((item.get("country") or {}).get("name") or "").strip(),
            "main": bool(item.get("main")),
            "order": item.get("order") or 999,
        })
    # Nom de sous-dossier unique pour chaque langue
    seen = {}
    for lang in langs:
        seen[lang["name"].casefold()] = seen.get(lang["name"].casefold(), 0) + 1
    for lang in langs:
        folder = lang["name"]
        if seen[lang["name"].casefold()] > 1:
            folder += f" ({lang['code']})"
        lang["folder"] = safe_name(folder) or lang["code"]
    return langs


def _normalize(raw):
    sermons = []
    for s in raw:
        if s.get("number") is None:
            continue
        url = (s.get("audio") or s.get("cloud_audio") or "").strip()
        sermons.append({
            "number": int(s["number"]),
            "chapter": s.get("chapter") or f"Kacou {s['number']}",
            "title": (s.get("title") or "").strip(),
            "time": s.get("time") or "",
            "url": url if url.startswith("http") else "",
        })
    sermons.sort(key=lambda s: s["number"])
    for i, s in enumerate(sermons):
        s["iid"] = f"r{i}"
    return sermons


def _sermons_from_page(code):
    """Secours : liste intégrée dans la page web /<langue>/sermons."""
    with http_open(f"{SITE}/{code}/sermons") as r:
        html = r.read().decode("utf-8", errors="replace")
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    data = "".join(json.loads('"' + c + '"') for c in chunks)
    idx = data.find('"defaultSermons":')
    if idx < 0:
        raise RuntimeError("Liste des prédications introuvable sur la page.")
    arr, _ = json.JSONDecoder().raw_decode(data[data.index("[", idx):])
    return arr


def fetch_sermons(code):
    try:
        raw = api_list(f"{code}/sermons")
    except Exception:
        raw = _sermons_from_page(code)
    return _normalize(raw)


def audio_count(code):
    sermons = fetch_sermons(code)
    return sum(1 for s in sermons if s["url"]), len(sermons)


def filename_for(sermon):
    name = f"Kacou {sermon['number']:03d}"
    title = safe_name(sermon["title"])
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

    def __init__(self, parent, text, command, primary=True, width=170, height=42,
                 bg=CARD, anchor="center"):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command, self.primary, self.anchor = text, command, primary, anchor
        self.w, self.h = width, height
        self.enabled, self.hover = True, False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _colors(self):
        if not self.enabled:
            return (CARD_2, DIM, BORDER)
        if self.primary:
            return (GOLD_HOVER if self.hover else GOLD, "#1a1408", GOLD)
        return ("#272c37" if self.hover else CARD_2, TEXT, GOLD if self.hover else BORDER)

    def _draw(self):
        self.delete("all")
        fill, fg, outline = self._colors()
        r, w, h = 12, self.w - 2, self.h - 2
        pts = [1 + r, 1, w - r, 1, w, 1, w, 1 + r, w, h - r, w, h, w - r, h,
               1 + r, h, 1, h, 1, h - r, 1, 1 + r, 1, 1]
        self.create_polygon(pts, smooth=True, fill=fill, outline=outline, width=1)
        if self.anchor == "w":
            self.create_text(18, self.h / 2, text=self.text, fill=fg, anchor="w",
                             font=(FONT, 13, "bold"))
            self.create_text(self.w - 18, self.h / 2, text="▾", fill=GOLD if self.enabled else DIM,
                             anchor="e", font=(FONT, 14, "bold"))
        else:
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


class LanguagePicker(tk.Toplevel):
    """Fenêtre de choix de la langue, avec recherche."""

    def __init__(self, app):
        super().__init__(app.root, bg=BG)
        self.app = app
        self.title("Choisir la langue")
        self.transient(app.root)
        self.geometry(self._centered(560, 620))
        self.minsize(460, 420)
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.refresh())

        wrap = tk.Frame(self, bg=BG, padx=22, pady=20)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text="Choisir la langue", bg=BG, fg=TEXT,
                 font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(wrap, text="Seules les langues qui ont des audios sur le site sont affichées.",
                 bg=BG, fg=MUTED, font=(FONT, 12)).pack(anchor="w", pady=(2, 14))

        box = tk.Frame(wrap, bg=CARD_2, highlightbackground=BORDER,
                       highlightcolor=GOLD, highlightthickness=1)
        box.pack(fill="x")
        tk.Label(box, text="⌕", bg=CARD_2, fg=MUTED, font=(FONT, 16)).pack(side="left", padx=(12, 4))
        self.entry = tk.Entry(box, textvariable=self.query, bg=CARD_2, fg=TEXT,
                              insertbackground=TEXT, relief="flat", font=(FONT, 13),
                              highlightthickness=0, bd=0)
        self.entry.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 12))

        self.info = tk.Label(wrap, text="", bg=BG, fg=MUTED, anchor="w", font=(FONT, 11))
        self.info.pack(fill="x", pady=(10, 6))

        btns = tk.Frame(wrap, bg=BG)
        btns.pack(side="bottom", fill="x", pady=(14, 0))
        RoundButton(btns, "Choisir", self.choose, width=130, bg=BG).pack(side="right")
        RoundButton(btns, "Annuler", self.destroy, primary=False, width=110, bg=BG).pack(
            side="right", padx=10)

        lst = tk.Frame(wrap, bg=BORDER)
        lst.pack(fill="both", expand=True)
        inner = tk.Frame(lst, bg=CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.tree = ttk.Treeview(inner, columns=("name", "country", "audios"),
                                 show="headings", selectmode="browse")
        for col, text, w, stretch in (("name", "LANGUE", 210, True),
                                      ("country", "PAYS", 160, True),
                                      ("audios", "AUDIOS", 90, False)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=w, anchor="w", stretch=stretch)
        self.tree.tag_configure("odd", background=ROW_ALT)
        self.tree.tag_configure("current", foreground=GOLD)
        self.tree.tag_configure("pending", foreground=MUTED)
        sb = tk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", lambda e: self.choose())
        self.bind("<Return>", lambda e: self.choose())
        self.bind("<Escape>", lambda e: self.destroy())
        self.entry.bind("<Down>", self._focus_list)

        self.refresh()
        if self.tree.exists(app.lang_code):
            self.tree.see(app.lang_code)
        self.entry.focus_set()
        self.grab_set()

    def _centered(self, w, h):
        r = self.app.root
        r.update_idletasks()
        x = r.winfo_rootx() + (r.winfo_width() - w) // 2
        y = r.winfo_rooty() + (r.winfo_height() - h) // 3
        return f"{w}x{h}+{max(x, 0)}+{max(y, 0)}"

    def _focus_list(self, _):
        kids = self.tree.get_children()
        if kids:
            self.tree.focus_set()
            target = self.tree.selection() or (kids[0],)
            self.tree.selection_set(target)
            self.tree.focus(target[0])

    def refresh(self):
        app = self.app
        q = self.query.get().strip().casefold()
        selected = self.tree.selection()
        keep = selected[0] if selected else app.lang_code
        self.tree.delete(*self.tree.get_children())
        langs = sorted(app.languages, key=lambda l: (not l["main"], l["order"] if l["main"] else 0,
                                                      l["name"].casefold()))
        shown = 0
        for lang in langs:
            count = app.counts.get(lang["code"])
            if count is not None and count[0] == 0 and lang["code"] != app.lang_code:
                continue
            hay = f"{lang['name']} {lang['country']} {lang['code']}".casefold()
            if q and q not in hay:
                continue
            tags = ["odd"] if shown % 2 else []
            if lang["code"] == app.lang_code:
                tags.append("current")
            elif count is None:
                tags.append("pending")
            audios = "…" if count is None else str(count[0])
            self.tree.insert("", "end", iid=lang["code"], tags=tags,
                             values=(lang["name"], lang["country"], audios))
            shown += 1
        if self.tree.exists(keep):
            self.tree.selection_set(keep)
        if app.scan_total and app.scan_done < app.scan_total:
            self.info.config(text=f"Vérification des audios disponibles… "
                                  f"{app.scan_done}/{app.scan_total} langues", fg=GOLD)
        else:
            with_audio = sum(1 for c in app.counts.values() if c[0] > 0)
            self.info.config(text=f"{with_audio} langues avec des audios  ·  {shown} affichée(s)",
                             fg=MUTED)

    def choose(self):
        sel = self.tree.selection()
        if not sel:
            return
        code = sel[0]
        self.destroy()
        self.app.select_language(code)


# --- Application -------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        # Liste gardée en mémoire : les noms de langues (et donc les dossiers)
        # sont connus tout de suite, même si le site répond lentement.
        self.languages = self.cfg.get("languages") or []
        self.counts = {k: tuple(v) for k, v in (self.cfg.get("counts") or {}).items()}
        self.scan_total = self.scan_done = 0
        self.lang_code = self.cfg.get("lang") or DEFAULT_LANG
        self.sermons = []
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.picker = None
        self.folder = tk.StringVar(value=self.cfg.get("folder") or str(DEFAULT_DIR))
        self.lang_path = tk.StringVar()
        self.range_from = tk.IntVar(value=1)
        self.range_to = tk.IntVar(value=1)
        self.parallel = tk.IntVar(value=3)
        self.total = self.finished = self.failed = 0
        self.partial = {}
        self.bytes_done = 0
        self.speed_mark = (time.time(), 0)
        self.speed = 0.0

        root.title("Kacou Audio — Téléchargeur de prédications")
        root.configure(bg=BG)
        root.geometry("1340x820")
        root.minsize(1180, 700)
        self._set_icon()
        self._styles()
        self._build()
        self._update_lang_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)
        root.after(200, self.load_languages)
        root.after(300, self.load_list)

    def _set_icon(self):
        try:
            self.icon_img = tk.PhotoImage(file=str(ASSETS / "icon.png"))
            self.root.iconphoto(True, self.icon_img)
        except tk.TclError:
            pass

    # ---- style ---------------------------------------------------------------
    def _styles(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Gold.Horizontal.TProgressbar", troughcolor=CARD_2,
                     background=GOLD, bordercolor=CARD_2, lightcolor=GOLD,
                     darkcolor=GOLD, thickness=12)
        st.configure("Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, rowheight=30, borderwidth=0, font=(FONT, 12))
        st.map("Treeview", background=[("selected", "#3a3220")],
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

        self.status = tk.Label(wrap, text="Prêt.", bg=BG, fg=MUTED, anchor="w", font=(FONT, 11))
        self.status.pack(side="bottom", fill="x", pady=(10, 0))

        side = tk.Frame(wrap, bg=BG)
        side.pack(side="left", fill="y", padx=(0, 22))
        self._build_sidebar(side)

        main = tk.Frame(wrap, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        # En-tête
        head = tk.Frame(main, bg=BG)
        head.pack(fill="x", pady=(0, 18))
        logo = tk.Canvas(head, width=52, height=52, bg=BG, highlightthickness=0)
        logo.create_oval(2, 2, 50, 50, fill=GOLD, outline="")
        logo.create_text(26, 27, text="♪", fill="#1a1408", font=(FONT, 24, "bold"))
        logo.pack(side="left")
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left", padx=14)
        tk.Label(titles, text="Kacou Audio", bg=BG, fg=TEXT,
                 font=(FONT, 24, "bold")).pack(anchor="w")
        self.lbl_subtitle = tk.Label(titles, text="", bg=BG, fg=MUTED, font=(FONT, 12))
        self.lbl_subtitle.pack(anchor="w")

        # Réglages
        c = card(main, fill="x")
        c.columnconfigure(1, weight=1)

        tk.Label(c, text="LANGUE DES AUDIOS", bg=CARD, fg=MUTED,
                 font=(FONT, 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        lang_row = tk.Frame(c, bg=CARD)
        lang_row.grid(row=1, column=0, columnspan=3, sticky="we", pady=(6, 14))
        self.btn_lang = RoundButton(lang_row, "", self.open_picker, primary=False,
                                    width=340, height=44, anchor="w")
        self.btn_lang.pack(side="left")
        self.lbl_lang_info = tk.Label(lang_row, text="", bg=CARD, fg=MUTED, font=(FONT, 12))
        self.lbl_lang_info.pack(side="left", padx=14)

        tk.Label(c, text="DOSSIER DE DESTINATION", bg=CARD, fg=MUTED,
                 font=(FONT, 10, "bold")).grid(row=2, column=0, columnspan=3, sticky="w")
        path_box = tk.Frame(c, bg=CARD_2, highlightbackground=BORDER, highlightthickness=1)
        path_box.grid(row=3, column=0, columnspan=2, sticky="we", pady=(6, 14))
        tk.Label(path_box, textvariable=self.lang_path, bg=CARD_2, fg=TEXT, anchor="w",
                 font=(FONT, 12), padx=12, pady=10).pack(fill="x")
        self.btn_folder = RoundButton(c, "Changer…", self.choose_folder, primary=False, width=120)
        self.btn_folder.grid(row=3, column=2, padx=(10, 0), pady=(6, 14))

        opts = tk.Frame(c, bg=CARD)
        opts.grid(row=4, column=0, columnspan=3, sticky="w")

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

        # Actions
        c2 = card(main, fill="x", pady=14)
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

        # Liste
        c3 = card(main, fill="both", expand=True)
        tree_box = tk.Frame(c3, bg=CARD)
        tree_box.pack(fill="both", expand=True)
        cols = ("num", "title", "time", "status")
        self.tree = ttk.Treeview(tree_box, columns=cols, show="headings", selectmode="none")
        for col, text, w, stretch in (
            ("num", "N°", 90, False), ("title", "TITRE", 460, True),
            ("time", "DURÉE", 100, False), ("status", "STATUT", 150, False),
        ):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=w, anchor="w", stretch=stretch)
        self.tree.tag_configure("odd", background=ROW_ALT)
        self.tree.tag_configure("done", foreground=GREEN)
        self.tree.tag_configure("active", foreground=GOLD)
        self.tree.tag_configure("error", foreground=RED)
        self.tree.tag_configure("skip", foreground=BLUE)
        self.tree.tag_configure("none", foreground=DIM)
        sb = tk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_sidebar(self, side):
        """Colonne de gauche : l'image du prophète et la progression."""
        try:
            self.banner_img = tk.PhotoImage(file=str(ASSETS / "banner.png"))
            tk.Label(side, image=self.banner_img, bg=BG, bd=0).pack(anchor="n")
        except tk.TclError:
            pass

        c = card(side, fill="x", pady=(18, 0))
        tk.Label(c, text="PROGRESSION", bg=CARD, fg=MUTED,
                 font=(FONT, 10, "bold")).pack(anchor="w")
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x", pady=(6, 10))
        self.lbl_count = tk.Label(row, text="0 / 0", bg=CARD, fg=TEXT, font=(FONT, 24, "bold"))
        self.lbl_count.pack(side="left")
        self.lbl_pct = tk.Label(row, text="0 %", bg=CARD, fg=GOLD, font=(FONT, 24, "bold"))
        self.lbl_pct.pack(side="right")
        self.progress = ttk.Progressbar(c, style="Gold.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.lbl_detail = tk.Label(c, text="", bg=CARD, fg=MUTED, font=(FONT, 12),
                                   anchor="w", justify="left", wraplength=280)
        self.lbl_detail.pack(fill="x", pady=(10, 0))

    # ---- langue --------------------------------------------------------------
    def current_language(self):
        for lang in self.languages:
            if lang["code"] == self.lang_code:
                return lang
        known = {DEFAULT_LANG: ("Français", "France")}
        name, country = known.get(self.lang_code, (self.lang_code, ""))
        return {"code": self.lang_code, "name": name, "country": country,
                "folder": safe_name(name) or self.lang_code}

    def lang_dir(self):
        return Path(self.folder.get()) / self.current_language()["folder"]

    def _update_lang_ui(self):
        lang = self.current_language()
        label = lang["name"] + (f"  ·  {lang['country']}" if lang["country"] else "")
        self.btn_lang.configure_state(text=label)
        self.lang_path.set(str(self.lang_dir()))
        self.lbl_subtitle.config(
            text=f"Prédications de philippekacou.org en {lang['name']}, du premier au dernier Kacou")
        count = self.counts.get(self.lang_code)
        if self.sermons:
            n = sum(1 for s in self.sermons if s["url"])
            self.lbl_lang_info.config(text=f"{n} audio(s) sur {len(self.sermons)} prédications")
        elif count:
            self.lbl_lang_info.config(text=f"{count[0]} audio(s) sur {count[1]} prédications")
        else:
            self.lbl_lang_info.config(text="")

    def load_languages(self):
        def work():
            try:
                self.events.put(("langs", fetch_languages()))
            except Exception as e:
                self.events.put(("langs_error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _scan_counts(self):
        """Vérifie en arrière-plan quelles langues ont des audios."""
        fresh = time.time() - float(self.cfg.get("counts_time") or 0) < SCAN_MAX_AGE
        codes = [l["code"] for l in self.languages]
        if fresh and all(c in self.counts for c in codes):
            return
        self.scan_total, self.scan_done = len(codes), 0

        def one(code):
            try:
                a, n = audio_count(code)
                self.events.put(("count", (code, a, n)))
            except Exception:
                self.events.put(("count", (code, None, None)))

        def run():
            with ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(one, codes))
            self.events.put(("scan_done", None))

        threading.Thread(target=run, daemon=True).start()

    def open_picker(self):
        if self.running:
            return
        if not self.languages:
            self.set_status("La liste des langues est encore en cours de chargement…", GOLD)
            return
        if self.picker and self.picker.winfo_exists():
            self.picker.lift()
            return
        self.picker = LanguagePicker(self)

    def select_language(self, code):
        if self.running or code == self.lang_code:
            return
        self.lang_code = code
        self.cfg["lang"] = code
        save_config(self.cfg)
        self.sermons = []
        self.tree.delete(*self.tree.get_children())
        self._update_lang_ui()
        self.load_list()

    def _migrate_old_french_files(self):
        """Avant les langues, les audios français étaient à la racine du dossier :
        on les range une seule fois dans le sous-dossier « Français »."""
        if self.lang_code != "fr-fr" or self.cfg.get("migrated_fr"):
            return
        root, target = Path(self.folder.get()), self.lang_dir()
        moved = 0
        try:
            old = sorted(root.glob("Kacou [0-9][0-9][0-9]*.mp3"))
            if old:
                target.mkdir(parents=True, exist_ok=True)
            for f in old:
                if not (target / f.name).exists():
                    shutil.move(str(f), str(target / f.name))
                    moved += 1
        except OSError:
            return
        self.cfg["migrated_fr"] = True
        save_config(self.cfg)
        if moved:
            self.set_status(f"{moved} fichier(s) français déjà téléchargés rangés dans « {target.name} ».",
                            GREEN)

    # ---- actions -------------------------------------------------------------
    def set_status(self, text, color=MUTED):
        self.status.config(text=text, fg=color)

    def choose_folder(self):
        if self.running:
            return
        d = filedialog.askdirectory(initialdir=self.folder.get(), title="Choisir le dossier")
        if d:
            self.folder.set(d)
            self.cfg["folder"] = d
            save_config(self.cfg)
            self._update_lang_ui()
            self._mark_existing()

    def open_folder(self):
        folder = self.lang_dir()
        folder.mkdir(parents=True, exist_ok=True)
        opener = "open" if sys.platform == "darwin" else ("explorer" if os.name == "nt" else "xdg-open")
        subprocess.Popen([opener, str(folder)])

    def load_list(self):
        if self.running:
            return
        code = self.lang_code
        name = self.current_language()["name"]
        self.set_status(f"Chargement des prédications en {name}…", GOLD)
        self.btn_start.configure_state(enabled=False)
        self.btn_reload.configure_state(enabled=False)

        def work():
            try:
                self.events.put(("list", (code, fetch_sermons(code))))
            except Exception as e:
                self.events.put(("list_error", (code, str(e))))

        threading.Thread(target=work, daemon=True).start()

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.sermons):
            self.tree.insert("", "end", iid=s["iid"],
                             values=(s["chapter"], s["title"], s["time"], "En attente"),
                             tags=("odd",) if i % 2 else ())
        self._mark_existing()
        kids = self.tree.get_children()
        if kids:
            self.tree.see(kids[0])

    def _mark_existing(self):
        folder = self.lang_dir()
        have = n = 0
        for s in self.sermons:
            if not s["url"]:
                self._row(s["iid"], "Pas d'audio", "none")
                continue
            n += 1
            exists = (folder / filename_for(s)).exists()
            have += exists
            self._row(s["iid"], "✓ Déjà présent" if exists else "En attente",
                      "skip" if exists else None)
        self.lbl_count.config(text=f"{have} / {n}")
        self.lbl_detail.config(text="déjà dans le dossier" if have else "audios disponibles")
        pct = have * 100 / n if n else 0
        self.progress["value"] = pct
        self.lbl_pct.config(text=f"{pct:.0f} %")

    def _row(self, iid, status, tag):
        if not self.tree.exists(iid):
            return
        vals = list(self.tree.item(iid, "values"))
        vals[3] = status
        tags = ["odd"] if self.tree.index(iid) % 2 else []
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
        return [s for s in self.sermons if s["url"] and a <= s["number"] <= b]

    def _set_controls(self, enabled):
        for b in (self.btn_start, self.btn_reload, self.btn_folder, self.btn_lang):
            b.configure_state(enabled=enabled)
        self.btn_stop.configure_state(enabled=not enabled)

    def start(self):
        if self.running or not self.sermons:
            return
        try:
            todo = self._selection()
        except ValueError as e:
            messagebox.showerror("Kacou Audio", str(e))
            return
        if not todo:
            messagebox.showinfo("Kacou Audio", "Aucun audio disponible dans cet intervalle.")
            return
        folder = self.lang_dir()
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
        self.finished = self.failed = 0
        self.bytes_done = 0
        self.speed_mark = (time.time(), 0)
        self.speed = 0.0
        self.partial = {}
        for s in todo:
            self._row(s["iid"], "En attente", None)
        self._update_progress()
        self._set_controls(False)
        self.set_status(f"Téléchargement de {len(todo)} audio(s) vers {folder}", GOLD)

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
        iid = s["iid"]
        if self.stop_event.is_set():
            self.events.put(("item", (iid, "Annulé", None, 0)))
            return
        target = folder / filename_for(s)
        if target.exists() and target.stat().st_size > 0:
            self.events.put(("item", (iid, "✓ Déjà présent", "skip", 1)))
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
                            self.events.put(("item", (iid, txt, "active", pct)))
                if size and got < size:
                    raise IOError("fichier incomplet")
                os.replace(tmp, target)
                self.events.put(("item", (iid, f"✓ {human_size(got)}", "done", 1)))
                return
            except InterruptedError:
                tmp.unlink(missing_ok=True)
                self.events.put(("item", (iid, "Annulé", None, 0)))
                return
            except Exception as e:
                last_err = e
                tmp.unlink(missing_ok=True)
                if self.stop_event.wait(2 * attempt):
                    self.events.put(("item", (iid, "Annulé", None, 0)))
                    return
        self.events.put(("item", (iid, "✗ Erreur", "error", -1)))
        self.events.put(("log", f"{s['chapter']} : {last_err}"))

    # ---- boucle UI -------------------------------------------------------------
    def _poll(self):
        refresh_picker = False
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "bytes":
                    self.bytes_done += data
                elif kind == "item":
                    iid, text, tag, frac = data
                    self._row(iid, text, tag)
                    if tag in ("done", "skip") and frac == 1:
                        self.finished += 1
                        self.partial.pop(iid, None)
                    elif tag == "error":
                        self.failed += 1
                        self.partial.pop(iid, None)
                    elif tag == "active":
                        self.partial[iid] = frac
                    else:
                        self.partial.pop(iid, None)
                elif kind == "log":
                    self.set_status(data, RED)
                elif kind == "list":
                    code, sermons = data
                    if code == self.lang_code:
                        self._on_list(sermons)
                elif kind == "list_error":
                    code, err = data
                    if code == self.lang_code:
                        self._on_list_error(err)
                elif kind == "langs":
                    self.languages = data
                    self.cfg["languages"] = data
                    save_config(self.cfg)
                    self._update_lang_ui()
                    # La liste a pu arriver avant les noms : remplacer le code par le nom
                    text = self.status.cget("text")
                    if f" en {self.lang_code} " in text:
                        name = self.current_language()["name"]
                        self.status.config(text=text.replace(f" en {self.lang_code} ", f" en {name} "))
                    self._scan_counts()
                    refresh_picker = True
                elif kind == "langs_error":
                    if self.languages:
                        self._scan_counts()  # on garde la liste enregistrée
                    else:
                        self.set_status("Impossible de charger la liste des langues : " + data, RED)
                elif kind == "count":
                    code, a, n = data
                    self.scan_done += 1
                    if a is not None:
                        self.counts[code] = (a, n)
                    refresh_picker = True
                elif kind == "scan_done":
                    self.scan_total = self.scan_done = 0
                    self.cfg["counts"] = {k: list(v) for k, v in self.counts.items()}
                    self.cfg["counts_time"] = time.time()
                    save_config(self.cfg)
                    refresh_picker = True
                elif kind == "all_done":
                    self._on_all_done()
        except queue.Empty:
            pass
        if refresh_picker and self.picker and self.picker.winfo_exists():
            self.picker.refresh()
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
        name = self.current_language()["name"]
        with_audio = [s for s in sermons if s["url"]]
        self.counts[self.lang_code] = (len(with_audio), len(sermons))
        self._update_lang_ui()
        self._migrate_old_french_files()
        self._fill_tree()
        if not with_audio:
            self.set_status(f"Aucun audio n'est disponible en {name} sur le site pour le moment.", RED)
            return
        first, last = with_audio[0]["number"], with_audio[-1]["number"]
        for sp in (self.sp_from, self.sp_to):
            sp.config(from_=first, to=last)
        self.range_from.set(first)
        self.range_to.set(last)
        self.btn_start.configure_state(enabled=True)
        if not self.status.cget("text").startswith(tuple("0123456789")):
            self.set_status(f"{len(with_audio)} audio(s) en {name} "
                            f"(Kacou {first} → Kacou {last}).", GREEN)

    def _on_list_error(self, err):
        self.btn_reload.configure_state(enabled=True)
        self.set_status("Erreur de chargement : " + err, RED)
        messagebox.showerror("Kacou Audio",
                             f"Impossible de récupérer la liste :\n{err}\n\n"
                             "Vérifie ta connexion Internet puis clique sur « Actualiser ».")

    def _on_all_done(self):
        self.running = False
        self._update_progress()
        stopped = self.stop_event.is_set()
        self._set_controls(True)
        if stopped:
            msg, color = f"Arrêté. {self.finished} / {self.total} terminés.", MUTED
        elif self.failed:
            msg, color = (f"Terminé avec {self.failed} erreur(s). Relance « Tout télécharger » "
                          "pour réessayer uniquement les manquants."), RED
        else:
            msg, color = f"Terminé ! {self.finished} audio(s) dans {self.lang_dir()}", GREEN
        self.set_status(msg, color)
        if not stopped:
            self.root.bell()

    def _on_close(self):
        if self.running and not messagebox.askyesno(
                "Kacou Audio", "Un téléchargement est en cours. Quitter quand même ?"):
            return
        self.stop_event.set()
        self.cfg["counts"] = {k: list(v) for k, v in self.counts.items()}
        save_config(self.cfg)
        self.root.after(300, self.root.destroy)


def main():
    if os.name == "nt":
        try:  # texte net sur les écrans Windows haute résolution
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
