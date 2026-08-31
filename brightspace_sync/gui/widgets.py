"""Small reusable tk pieces."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class ScrollFrame(ttk.Frame):
    """A vertically scrolling area. Put content in ``.body``.

    Only one of these is alive at a time in this app, so it can own the mouse
    wheel globally without fighting another instance.
    """

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self.body = ttk.Frame(self._canvas)
        self._win = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(
            self._win, width=e.width))
        # Standard cross-platform wheel handling. Only one ScrollFrame is alive
        # at a time, so binding globally is fine. (macOS trackpad scrolling can
        # be flaky when run from source; the scrollbar always works, and the
        # packaged build ships its own Tk where this behaves normally.)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(seq, self._on_wheel, add="+")

    def _on_wheel(self, event) -> None:
        delta = getattr(event, "delta", 0)
        if getattr(event, "num", 0) == 4 or delta > 0:
            self._canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", 0) == 5 or delta < 0:
            self._canvas.yview_scroll(1, "units")


class Collapsible(ttk.Frame):
    """A header the user clicks to reveal ``.body``."""

    def __init__(self, parent, title: str, *, open: bool = False):
        super().__init__(parent)
        self._open = open
        self._btn = ttk.Button(self, command=self.toggle, style="Disclosure.TButton")
        self._btn.pack(fill="x")
        self.body = ttk.Frame(self, padding=(16, 4, 0, 4))
        self._title = title
        self._render()

    def toggle(self) -> None:
        self._open = not self._open
        self._render()

    def _render(self) -> None:
        self._btn.configure(text=("▼  " if self._open else "▶  ") + self._title)
        if self._open:
            self.body.pack(fill="x")
        else:
            self.body.forget()


class Tooltip:
    """A hover hint. ``Tooltip(widget, "text")`` — that's the whole API."""

    def __init__(self, widget, text: str, *, delay: int = 400, wrap: int = 320):
        self.widget, self.text, self.delay, self.wrap = widget, text, delay, wrap
        self._after = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self) -> None:
        if self._tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 14
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", background="#2b2b2b",
                 foreground="#f5f5f5", relief="solid", borderwidth=1,
                 wraplength=self.wrap, padx=8, pady=5).pack()

    def _hide(self, _=None) -> None:
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


def tip(widget, text: str):
    """Attach a tooltip and return the widget, so it chains in a layout call."""
    Tooltip(widget, text)
    return widget
