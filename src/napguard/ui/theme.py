"""NapGuard's ttk theme: dark navy with cyan and blue accents (light variant included).

Built on ttk's portable "clam" engine, so it needs nothing beyond Tk (python3-tk).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

PALETTES: dict[str, dict[str, str]] = {
    "dark": {"bg": "#0b1426", "panel": "#111d35", "card": "#16243f", "line": "#243659",
             "text": "#e6edf7", "muted": "#8fa3c4", "accent": "#22d3ee", "accent2": "#3b82f6",
             "ok": "#34d399", "warn": "#fbbf24", "error": "#f87171", "busy": "#60a5fa",
             "info": "#7dd3fc", "select": "#1e3a6a", "entry": "#0f1a30"},
    "light": {"bg": "#eef3fa", "panel": "#ffffff", "card": "#f7faff", "line": "#c9d6ea",
              "text": "#0f1d33", "muted": "#4b5d7a", "accent": "#0891b2", "accent2": "#2563eb",
              "ok": "#047857", "warn": "#b45309", "error": "#b91c1c", "busy": "#1d4ed8",
              "info": "#0369a1", "select": "#cfe3ff", "entry": "#ffffff"},
}


def colour(mode: str, role: str) -> str:
    return PALETTES[mode].get(role, PALETTES[mode]["text"])


def apply(root: tk.Tk, mode: str = "dark") -> dict[str, str]:
    p = PALETTES[mode]
    families = set(tkfont.families(root))
    family = next((f for f in ("Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans") if f in families),
                  "TkDefaultFont")
    base = (family, 10)
    root.configure(bg=p["bg"])
    root.option_add("*Font", base)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["entry"],
                    bordercolor=p["line"], lightcolor=p["line"], darkcolor=p["line"],
                    troughcolor=p["panel"], focuscolor=p["accent"], font=base,
                    insertcolor=p["text"], selectbackground=p["select"],
                    selectforeground=p["text"])
    style.configure("TFrame", background=p["bg"])
    style.configure("Panel.TFrame", background=p["panel"])
    style.configure("Card.TFrame", background=p["card"], relief="solid", borderwidth=1)
    style.configure("Sidebar.TFrame", background=p["panel"])
    style.configure("Header.TFrame", background=p["panel"])
    for name, bg in (("TLabel", p["bg"]), ("Panel.TLabel", p["panel"]), ("Card.TLabel", p["card"]),
                     ("Header.TLabel", p["panel"])):
        style.configure(name, background=bg, foreground=p["text"])
    style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
    style.configure("CardMuted.TLabel", background=p["card"], foreground=p["muted"],
                    font=(family, 9))
    style.configure("CardValue.TLabel", background=p["card"], foreground=p["text"],
                    font=(family, 14, "bold"))
    style.configure("Title.TLabel", background=p["bg"], foreground=p["text"],
                    font=(family, 18, "bold"))
    style.configure("Subtitle.TLabel", background=p["bg"], foreground=p["muted"],
                    font=(family, 10))
    style.configure("Section.TLabel", background=p["bg"], foreground=p["accent"],
                    font=(family, 11, "bold"))
    style.configure("Brand.TLabel", background=p["panel"], foreground=p["accent"],
                    font=(family, 16, "bold"))
    style.configure("Tagline.TLabel", background=p["panel"], foreground=p["muted"],
                    font=(family, 10))
    for role in ("ok", "warn", "error", "busy", "info", "muted"):
        fg = p[role] if role != "muted" else p["muted"]
        style.configure(f"{role}.Badge.TLabel", background=p["card"], foreground=fg,
                        font=(family, 10, "bold"))
        style.configure(f"{role}.Status.TLabel", background=p["bg"], foreground=fg,
                        font=(family, 10, "bold"))
        style.configure(f"{role}.Header.TLabel", background=p["panel"], foreground=fg,
                        font=(family, 10, "bold"))
    style.configure("TButton", background=p["card"], foreground=p["text"], padding=(12, 6),
                    borderwidth=1, relief="flat")
    style.map("TButton", background=[("disabled", p["panel"]), ("active", p["select"])],
              foreground=[("disabled", p["muted"])])
    style.configure("Accent.TButton", background=p["accent2"], foreground="#ffffff",
                    font=(family, 10, "bold"))
    style.map("Accent.TButton", background=[("disabled", p["panel"]), ("active", p["accent"])],
              foreground=[("disabled", p["muted"])])
    style.configure("Danger.TButton", background=p["card"], foreground=p["error"])
    style.map("Danger.TButton", background=[("disabled", p["panel"]), ("active", p["select"])],
              foreground=[("disabled", p["muted"])])
    style.configure("Nav.TButton", background=p["panel"], foreground=p["muted"], anchor="w",
                    padding=(16, 9), borderwidth=0, font=(family, 11))
    style.map("Nav.TButton", background=[("active", p["card"])],
              foreground=[("active", p["text"])])
    style.configure("NavActive.TButton", background=p["card"], foreground=p["accent"],
                    anchor="w", padding=(16, 9), borderwidth=0, font=(family, 11, "bold"))
    style.configure("Treeview", background=p["panel"], fieldbackground=p["panel"],
                    foreground=p["text"], rowheight=28, borderwidth=0)
    style.map("Treeview", background=[("selected", p["select"])],
              foreground=[("selected", p["text"])])
    style.configure("Treeview.Heading", background=p["card"], foreground=p["muted"],
                    font=(family, 10, "bold"), relief="flat")
    style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
    style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
    style.configure("TEntry", fieldbackground=p["entry"], foreground=p["text"])
    style.configure("TSpinbox", fieldbackground=p["entry"], foreground=p["text"],
                    arrowcolor=p["text"])
    style.configure("TCombobox", fieldbackground=p["entry"], foreground=p["text"],
                    arrowcolor=p["text"])
    style.configure("Horizontal.TScale", background=p["bg"], troughcolor=p["panel"])
    style.configure("TProgressbar", background=p["accent"], troughcolor=p["panel"])
    return p
