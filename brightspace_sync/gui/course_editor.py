"""Pick courses, and (optionally) rename or merge their folders."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .model import MergeGroup, SetupModel
from .widgets import Collapsible, Tooltip

_KEEP = "keep separate"
_NEW = "＋ new merge group…"


class CourseEditor(ttk.Frame):
    def __init__(self, parent, model: SetupModel, on_change=lambda: None):
        super().__init__(parent)
        self.model = model
        self._on_change = on_change
        self._vars: dict[int, tk.BooleanVar] = {}
        self._filter = tk.StringVar()
        self._filter.trace_add("write", lambda *_: self._rebuild_courses())

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        b_all = ttk.Button(bar, text="Select all", command=lambda: self._set_all(True))
        b_all.pack(side="left")
        b_none = ttk.Button(bar, text="Select none", command=lambda: self._set_all(False))
        b_none.pack(side="left", padx=4)
        Tooltip(b_all, "Check every course in the list.")
        Tooltip(b_none, "Uncheck every course.")
        ttk.Label(bar, text="Filter:").pack(side="left", padx=(12, 2))
        f_ent = ttk.Entry(bar, textvariable=self._filter, width=18)
        f_ent.pack(side="left")
        Tooltip(f_ent, "Type part of a course name or code to shorten the list. "
                       "It only hides rows — it doesn't change what's selected.")
        self._count = ttk.Label(bar, text="", style="Muted.TLabel")
        self._count.pack(side="right")

        self._list = ttk.Frame(self)
        self._list.pack(fill="both", expand=True)

        start_open = bool(model.merges) or any(c.folder for c in model.courses)
        self._adv = Collapsible(self, "Rename folders / merge courses  (optional)",
                                open=start_open)
        self._adv.pack(fill="x", pady=(6, 0))
        ttk.Label(self._adv.body, justify="left", style="Muted.TLabel", wraplength=600,
                  text="Every selected course gets its own folder named after it. "
                       "Change a name below, or use a merge group to put related "
                       "courses (say a lecture and its lab) inside one shared folder.").pack(
            anchor="w", pady=(0, 8))
        self._folders = ttk.Frame(self._adv.body)
        self._folders.pack(fill="x")
        add_btn = ttk.Button(self._adv.body, text=_NEW, command=self._new_group)
        add_btn.pack(anchor="w", pady=(8, 4))
        Tooltip(add_btn, "Create an empty merge group, then put courses in it with "
                         "each course's “Merge” dropdown.")
        self._groups = ttk.Frame(self._adv.body)
        self._groups.pack(fill="x")

        self.refresh()

    # --- public -------------------------------------------------------
    def refresh(self) -> None:
        self._vars = {c.id: tk.BooleanVar(value=c.selected) for c in self.model.courses}
        self._rebuild_courses()
        self._rebuild_advanced()

    # --- course checklist -------------------------------------------
    def _rebuild_courses(self) -> None:
        for w in self._list.winfo_children():
            w.destroy()
        needle = self._filter.get().casefold()
        shown = 0
        for c in self.model.courses:
            label = f"{c.code}  {c.name}".strip()
            if needle and needle not in label.casefold():
                continue
            ttk.Checkbutton(self._list, text=label, variable=self._vars[c.id],
                            command=lambda cid=c.id: self._toggle(cid)).pack(anchor="w")
            shown += 1
        if not shown:
            ttk.Label(self._list, style="Muted.TLabel",
                      text="No courses match the filter.").pack(anchor="w")
        self._update_count()

    def _toggle(self, cid: int) -> None:
        next(c for c in self.model.courses if c.id == cid).selected = self._vars[cid].get()
        self._after_change()

    def _set_all(self, value: bool) -> None:
        for c in self.model.courses:
            c.selected = value
            self._vars[c.id].set(value)
        self._after_change()

    def _after_change(self) -> None:
        self._update_count()
        self._rebuild_advanced()
        self._on_change()

    def _update_count(self) -> None:
        n = sum(1 for c in self.model.courses if c.selected)
        self._count.configure(text=f"{n} of {len(self.model.courses)} selected")

    # --- rename / merge -------------------------------------------
    def _group_label(self, g: MergeGroup, i: int) -> str:
        return f"{i + 1}. {g.folder.strip() or 'unnamed'}"

    def _group(self, key: str) -> MergeGroup | None:
        return next((g for g in self.model.merges if g.key == key), None)

    def _new_group(self) -> str:
        keys = {g.key for g in self.model.merges}
        key = next(f"g{i}" for i in range(1000) if f"g{i}" not in keys)
        self.model.merges.append(MergeGroup(key=key))
        self._rebuild_advanced()
        self._on_change()
        return key

    def _remove_group(self, key: str) -> None:
        for c in self.model.courses:
            if c.merge_key == key:
                c.merge_key = ""
        self.model.merges = [g for g in self.model.merges if g.key != key]
        self._rebuild_advanced()
        self._on_change()

    def _rebuild_advanced(self) -> None:
        for frame in (self._folders, self._groups):
            for w in frame.winfo_children():
                w.destroy()

        selected = [c for c in self.model.courses if c.selected]
        labels = [self._group_label(g, i) for i, g in enumerate(self.model.merges)]
        label_to_key = {lb: g.key for lb, g in zip(labels, self.model.merges)}
        combo_values = [_KEEP, *labels, _NEW]

        ttk.Label(self._folders, text="Course", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Label(self._folders, text="Folder / subfolder name", style="Muted.TLabel").grid(
            row=0, column=1, sticky="w", padx=6)
        ttk.Label(self._folders, text="Merge into", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w")

        if not selected:
            ttk.Label(self._folders, style="Muted.TLabel", text="Select some courses first.").grid(
                row=1, column=0, columnspan=3, sticky="w")

        for r, c in enumerate(selected, start=1):
            lbl = ttk.Label(self._folders, text=(c.code or c.name)[:28])
            lbl.grid(row=r, column=0, sticky="w", padx=(0, 8))
            Tooltip(lbl, c.name)

            fvar = tk.StringVar(value=(c.subfolder if c.merge_key else c.folder) or c.default_folder)
            ent = ttk.Entry(self._folders, textvariable=fvar, width=34)
            ent.grid(row=r, column=1, sticky="w", padx=6, pady=1)
            fvar.trace_add("write", lambda *_, cc=c, v=fvar: self._set_folder(cc, v.get()))
            Tooltip(ent, "The folder this course is saved in. When the course is in a "
                         "merge group, this is its subfolder inside the shared folder.")

            cur = next((lb for lb, k in label_to_key.items() if k == c.merge_key), _KEEP)
            gvar = tk.StringVar(value=cur)
            cmb = ttk.Combobox(self._folders, values=combo_values, textvariable=gvar,
                               state="readonly", width=18)
            cmb.grid(row=r, column=2, sticky="w")
            cmb.bind("<<ComboboxSelected>>",
                     lambda _e, cc=c, v=gvar, m=dict(label_to_key): self._pick_group(cc, v.get(), m))
            Tooltip(cmb, "Put this course in the same folder as other courses. "
                         "Each merged course keeps its own subfolder.")

        for i, g in enumerate(self.model.merges):
            n = sum(1 for c in selected if c.merge_key == g.key)
            row = ttk.Frame(self._groups)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"Merge group {i + 1}").pack(side="left")
            gv = tk.StringVar(value=g.folder)
            e = ttk.Entry(row, textvariable=gv, width=28)
            e.pack(side="left", padx=6)
            gv.trace_add("write", lambda *_, gg=g, v=gv: self._set_group_folder(gg, v.get()))
            Tooltip(e, "Name of the shared folder these merged courses go into.")
            ttk.Label(row, text=f"{n} course(s)", style="Muted.TLabel").pack(side="left")
            rm = ttk.Button(row, text="Remove", command=lambda k=g.key: self._remove_group(k))
            rm.pack(side="left", padx=6)
            Tooltip(rm, "Delete this group. Its courses go back to their own folders.")

    def _set_folder(self, course, text: str) -> None:
        if course.merge_key:
            course.subfolder = text
        else:
            course.folder = text
        self._on_change()

    def _set_group_folder(self, group: MergeGroup, text: str) -> None:
        group.folder = text
        self._on_change()

    def _pick_group(self, course, choice: str, label_to_key: dict) -> None:
        if choice == _KEEP:
            course.merge_key = ""
        else:
            key = self._new_group() if choice == _NEW else label_to_key.get(choice, "")
            course.merge_key = key
            g = self._group(key)
            if g is not None and not g.folder.strip():
                g.folder = course.folder.strip() or course.default_folder
        self._rebuild_advanced()
        self._on_change()
