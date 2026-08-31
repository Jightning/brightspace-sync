"""One place for colors + named ttk styles, so light and dark both stay legible."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk


def _is_dark(root: tk.Misc) -> bool:
    try:
        r, g, b = root.winfo_rgb(ttk.Style(root).lookup("TFrame", "background") or "SystemWindow")
    except tk.TclError:
        return False
    return (r + g + b) / 3 < 32768


def install(root: tk.Misc) -> dict:
    """Configure shared styles on ``root`` and return the palette dict."""
    dark = _is_dark(root)
    pal = {
        "fg": "#f0f0f0" if dark else "#1b1b1b",
        "muted": "#b6bcc4" if dark else "#4a4f56",   # secondary text - still high contrast
        "err": "#ff8a80" if dark else "#c0392b",
        "warn": "#f0b429" if dark else "#b26a00",
        "ok": "#7bd88f" if dark else "#1e7e34",
        "accent": "#8ab4f8" if dark else "#1a63d8",
    }
    try:
        family = tkfont.nametofont("TkDefaultFont").actual("family")
    except tk.TclError:
        family = "TkDefaultFont"

    st = ttk.Style(root)
    st.configure("Heading.TLabel", font=(family, 16, "bold"), foreground=pal["fg"])
    st.configure("Sub.TLabel", font=(family, 13, "bold"), foreground=pal["fg"])
    st.configure("Muted.TLabel", foreground=pal["muted"])
    st.configure("Err.TLabel", foreground=pal["err"])
    st.configure("OK.TLabel", foreground=pal["ok"])
    st.configure("Disclosure.TButton", anchor="w")
    # macOS renders the native group-box caption tiny; make section titles read as headers.
    st.configure("TLabelframe.Label", font=(family, 13, "bold"), foreground=pal["fg"])
    return pal
