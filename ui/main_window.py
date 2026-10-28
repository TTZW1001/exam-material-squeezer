"""Tkinter main window for 报考资料瘦身器."""

from __future__ import annotations

import ctypes
import math
import os
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from core import (
    CancelledError,
    CompressionResult,
    MaterialCompressor,
    find_ghostscript,
    ghostscript_version,
    human_size,
)
from .theme import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_SOFT,
    APP_TITLE,
    APP_VERSION,
    BG,
    BORDER,
    CARD,
    GREEN,
    HEADER,
    HEADER_SOFT,
    INK,
    MUTED,
    RED,
)


INPUT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


@dataclass
class QueueItem:
    path: Path
    rel: Path
    display: str
    size: int
    iid: str
    origin_root: Path | None = None
    status: str = "pending"
    done_signature: tuple[str, int, str] | None = None


def _enable_dpi_awareness() -> None:
    # Tk 8.6 does not scale pixel-based geometry when it becomes DPI aware,
    # which makes the layout tiny on 200% displays.  Keep Tk in logical
    # coordinates and let Windows scale the window; this is the most robust
    # behaviour across laptops, desktops and remote sessions.
    return


class ExamMaterialSqueezerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE}  v{APP_VERSION}")
        self.configure(bg=BG)
        self.geometry(self._centered_geometry(1010, 790))
        self.minsize(880, 660)

        try:
            icon = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
            if icon.is_file():
                self.iconbitmap(default=str(icon))
        except Exception:
            pass

        families = set(tkfont.families(self))
        self.ui_font = next(
            (name for name in ("Microsoft YaHei UI", "Microsoft YaHei",
                               "PingFang SC", "Noto Sans CJK SC", "Segoe UI")
             if name in families),
            "Segoe UI",
        )
        self.mono_font = next(
            (name for name in ("Cascadia Mono", "Consolas", "Courier New")
             if name in families),
            "Courier New",
        )

        self._gs_path: str | None = None
        self._items: list[QueueItem] = []
        self._items_by_iid: dict[str, QueueItem] = {}
        self._row_seq = 0
        self._running = False
        self._auto_output = True
        self._last_output: Path | None = None
        self._last_dir: Path | None = None
        self._queue: queue.Queue | None = None
        self._engine: MaterialCompressor | None = None
        self._cancel_event: threading.Event | None = None
        self._total = 0
        self._current_index = 0
        self._batch_ts = ""

        self.output_var = tk.StringVar()
        self.output_format = tk.StringVar(value="PDF")
        self.target_value = tk.StringVar(value="200")
        self.target_unit = tk.StringVar(value="KB")
        self.target_text_var = tk.StringVar(value="200 KB")
        self.per_file_hint_var = tk.StringVar(
            value="每个文件独立压到 200 KB 以下"
        )
        self.include_subfolders = tk.BooleanVar(value=True)
        self.count_var = tk.StringVar(value="0 个文件")
        self.total_var = tk.StringVar(value="合计 0 B")
        self.summary_count_var = tk.StringVar(value="0")
        self.summary_size_var = tk.StringVar(value="0 B")
        self.status_var = tk.StringVar(value="准备就绪")
        self.badge_var = tk.StringVar(value="● 正在检测 Ghostscript…")
        self.note_var = tk.StringVar(
            value="多页时保持 PDF 页数；页数过多会拼成 1 页缩略图。"
        )

        self._build_styles()
        self._build_header()
        self._build_body()
        self._on_target_change()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._detect_ghostscript)
        self.after(120, self._append_log,
                   "info", "就绪：支持 PDF / JPG / PNG，输出 PDF / JPG / PNG，均低于设定上限。")

    # ------------------------------------------------------------------
    # layout

    def _centered_geometry(self, width: int, height: int) -> str:
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(width, max(880, screen_w - 48))
        height = min(height, max(640, screen_h - 88))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 16)
        return f"{width}x{height}+{x}+{y}"

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Files.Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=INK,
            rowheight=31,
            borderwidth=0,
            font=(self.ui_font, 9),
        )
        style.configure(
            "Files.Treeview.Heading",
            background="#FAF9F6",
            foreground=MUTED,
            font=(self.ui_font, 8, "bold"),
            relief="flat",
            borderwidth=0,
            padding=(8, 7),
        )
        style.map(
            "Files.Treeview",
            background=[("selected", "#FFE8DF")],
            foreground=[("selected", INK)],
        )
        style.map("Files.Treeview.Heading", background=[("active", "#F3F0E9")])
        style.configure(
            "Slim.Horizontal.TProgressbar",
            troughcolor="#F0ECE4",
            background=ACCENT,
            bordercolor="#F0ECE4",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=7,
        )
        style.configure(
            "Target.TCombobox",
            fieldbackground=ACCENT_SOFT,
            background=ACCENT_SOFT,
            foreground=ACCENT,
            arrowcolor=ACCENT,
            borderwidth=0,
            padding=(2, 0),
        )
        style.map(
            "Target.TCombobox",
            fieldbackground=[("readonly", ACCENT_SOFT)],
            selectbackground=[("readonly", ACCENT_SOFT)],
            selectforeground=[("readonly", ACCENT)],
        )

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=HEADER, height=96)
        header.pack(fill="x")
        header.pack_propagate(False)

        inner = tk.Frame(header, bg=HEADER)
        inner.pack(fill="both", expand=True, padx=26, pady=17)

        logo = tk.Canvas(inner, width=52, height=58, bg=HEADER,
                         highlightthickness=0)
        logo.pack(side="left", padx=(0, 13))
        logo.create_polygon(6, 4, 33, 4, 44, 15, 44, 52, 6, 52,
                            fill="#FFFFFF", outline="")
        logo.create_polygon(33, 4, 44, 15, 33, 15, fill="#D8DFEA", outline="")
        logo.create_line(25, 22, 25, 37, arrow=tk.LAST, fill=ACCENT, width=4)
        logo.create_line(18, 31, 25, 38, 32, 31, fill=ACCENT, width=4)

        titles = tk.Frame(inner, bg=HEADER)
        titles.pack(side="left", fill="y")
        title_row = tk.Frame(titles, bg=HEADER)
        title_row.pack(anchor="w")
        tk.Label(title_row, text=APP_TITLE, bg=HEADER, fg="#FFFFFF",
                 font=(self.ui_font, 19, "bold")).pack(side="left")
        tk.Label(
            title_row, text=f"v{APP_VERSION}", bg="#26344C", fg="#C9D4E5",
            font=(self.ui_font, 7, "bold"), padx=6, pady=2,
        ).pack(side="left", padx=(9, 0), pady=(4, 0))
        tk.Label(
            titles,
            text="PDF / JPG / PNG → PDF / JPG / PNG  ·  自定义大小上限  ·  本地处理",
            bg=HEADER, fg=HEADER_SOFT, font=(self.ui_font, 9),
        ).pack(anchor="w", pady=(5, 0))

        badge = tk.Label(
            inner, textvariable=self.badge_var, bg="#1E2B42", fg="#8FE3BD",
            font=(self.ui_font, 8), padx=12, pady=7, cursor="hand2",
        )
        badge.pack(side="right", pady=8)
        badge.bind("<Button-1>", lambda _event: self._detect_ghostscript())
        self.badge_label = badge

    def _card(self, parent: tk.Misc) -> tk.Frame:
        return tk.Frame(
            parent, bg=CARD, highlightbackground=BORDER,
            highlightcolor=BORDER, highlightthickness=1,
        )

    def _build_body(self) -> None:
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=22, pady=(16, 16))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0, minsize=318)
        body.grid_rowconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=0, minsize=112)

        left = self._card(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self._build_file_card(left)

        right = self._card(body)
        right.grid(row=0, column=1, sticky="nsew")
        self._build_settings_card(right)

        log_card = self._card(body)
        log_card.grid(row=1, column=0, columnspan=2, sticky="nsew",
                      pady=(14, 0))
        self._build_log_card(log_card)

    def _build_file_card(self, card: tk.Frame) -> None:
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=17, pady=14)

        heading = tk.Frame(inner, bg=CARD)
        heading.pack(fill="x")
        tk.Label(heading, text="待处理文件", bg=CARD, fg=INK,
                 font=(self.ui_font, 12, "bold")).pack(side="left")
        tk.Label(heading, textvariable=self.count_var, bg=CARD, fg=MUTED,
                 font=(self.ui_font, 9)).pack(side="right")

        actions = tk.Frame(inner, bg=CARD)
        actions.pack(fill="x", pady=(11, 2))
        self.pick_files_btn = self._button(
            actions, "＋  选择文件", self._pick_files, kind="dark"
        )
        self.pick_files_btn.pack(side="left")
        self.pick_folder_btn = self._button(
            actions, "选择文件夹", self._pick_folder, kind="soft"
        )
        self.pick_folder_btn.pack(side="left", padx=(8, 0))
        self.clear_btn = self._button(
            actions, "清空", self._clear_files, kind="ghost", padx=10
        )
        self.clear_btn.pack(side="right")
        self.remove_btn = self._button(
            actions, "移除选中", self._remove_selected, kind="soft", padx=8
        )
        self.remove_btn.pack(side="right", padx=(0, 4))
        self.remove_btn.configure(state="disabled")

        tk.Label(
            inner,
            text="支持 PDF / JPG / PNG，可多选文件或直接扫描文件夹。",
            bg=CARD, fg=MUTED, font=(self.ui_font, 8),
        ).pack(anchor="w", pady=(5, 0))

        summary = tk.Frame(inner, bg=CARD)
        summary.pack(side="bottom", fill="x", pady=(9, 0))
        tk.Label(summary, textvariable=self.total_var, bg=CARD, fg=MUTED,
                 font=(self.ui_font, 8)).pack(side="left")
        tk.Label(summary, textvariable=self.per_file_hint_var, bg=CARD,
                 fg="#9A958A", font=(self.ui_font, 8)).pack(side="right")

        holder = tk.Frame(inner, bg=CARD)
        holder.pack(fill="both", expand=True, pady=(10, 0))
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            holder, columns=("name", "size", "status"), show="headings",
            selectmode="browse", style="Files.Treeview", height=9,
        )
        self.tree.heading("name", text="文件名", anchor="w")
        self.tree.heading("size", text="大小", anchor="e")
        self.tree.heading("status", text="状态", anchor="center")
        self.tree.column("name", width=330, minwidth=170, stretch=True,
                         anchor="w")
        self.tree.column("size", width=80, minwidth=64, stretch=False,
                         anchor="e")
        self.tree.column("status", width=150, minwidth=110, stretch=False,
                         anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.tag_configure("wait", foreground=MUTED)
        self.tree.tag_configure("run", foreground=ACCENT)
        self.tree.tag_configure("done", foreground=GREEN)
        self.tree.tag_configure("fail", foreground=RED)
        self.tree.bind("<Double-1>", self._open_selected_file)
        self.tree.bind("<Delete>", lambda _event: self._remove_selected())
        self.tree.bind("<<TreeviewSelect>>",
                       lambda _event: self._refresh_controls())

        scroll = ttk.Scrollbar(holder, orient="vertical",
                               command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.tree.configure(yscrollcommand=scroll.set)

        self.empty_label = tk.Label(
            holder,
            text="还没有待处理文件\n点击「选择文件」或「选择文件夹」开始",
            bg=CARD, fg="#9A958A", justify="center",
            font=(self.ui_font, 9), cursor="hand2",
        )
        self.empty_label.place(relx=0.5, rely=0.52, anchor="center")
        self.empty_label.bind("<Button-1>", lambda _event: self._pick_files())

    def _build_settings_card(self, card: tk.Frame) -> None:
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=17, pady=14)

        tk.Label(inner, text="输出设置", bg=CARD, fg=INK,
                 font=(self.ui_font, 12, "bold")).pack(anchor="w")

        target = tk.Frame(inner, bg=ACCENT_SOFT)
        target.pack(fill="x", pady=(10, 11))
        target_inner = tk.Frame(target, bg=ACCENT_SOFT)
        target_inner.pack(fill="x", padx=13, pady=9)
        self.target_entry = tk.Entry(
            target_inner, textvariable=self.target_value, width=5,
            bg=ACCENT_SOFT, fg=ACCENT, relief="flat", bd=0,
            highlightthickness=0, insertbackground=ACCENT,
            justify="left", font=(self.ui_font, 20, "bold"),
        )
        self.target_entry.pack(side="left")
        self.target_unit_box = ttk.Combobox(
            target_inner, textvariable=self.target_unit, width=3,
            values=("KB", "MB"), state="readonly",
            style="Target.TCombobox", font=(self.ui_font, 8),
        )
        self.target_unit_box.pack(side="left", padx=(3, 8))
        text = tk.Frame(target_inner, bg=ACCENT_SOFT)
        text.pack(side="left", pady=(3, 0))
        tk.Label(text, text="硬性上限", bg=ACCENT_SOFT, fg="#9A3A26",
                 font=(self.ui_font, 8, "bold")).pack(anchor="w")
        tk.Label(text, text="低于此值立即停止", bg=ACCENT_SOFT,
                 fg="#A65B48", font=(self.ui_font, 8)).pack(anchor="w")
        self.target_entry.bind("<KeyRelease>", self._on_target_change)
        self.target_entry.bind("<FocusOut>", self._on_target_change)
        self.target_unit_box.bind("<<ComboboxSelected>>",
                                  self._on_target_change)

        format_row = tk.Frame(inner, bg=CARD)
        format_row.pack(fill="x", pady=(0, 7))
        tk.Label(format_row, text="输出格式", bg=CARD, fg=MUTED,
                 font=(self.ui_font, 8, "bold")).pack(side="left")
        format_switch = tk.Frame(format_row, bg="#F6F3EC")
        format_switch.pack(side="right")
        self.format_buttons: dict[str, tk.Button] = {}
        for code in ("PDF", "JPG", "PNG"):
            selected = self.output_format.get() == code
            btn = tk.Button(
                format_switch, text=code, width=4,
                command=lambda c=code: self._select_format(c),
                bg=ACCENT if selected else "#F6F3EC",
                fg="#FFFFFF" if selected else INK,
                activebackground=ACCENT_HOVER if selected else "#ECE7DC",
                activeforeground="#FFFFFF" if selected else INK,
                relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                font=(self.ui_font, 8, "bold"), padx=5, pady=4,
            )
            btn.pack(side="left", padx=(1, 1))
            self.format_buttons[code] = btn

        tk.Label(inner, text="保存到", bg=CARD, fg=MUTED,
                 font=(self.ui_font, 8, "bold")).pack(anchor="w")
        output_row = tk.Frame(inner, bg=CARD)
        output_row.pack(fill="x", pady=(4, 7))
        self.output_entry = tk.Entry(
            output_row, textvariable=self.output_var, bg="#F8F7F4",
            fg=INK, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            insertbackground=INK, font=(self.ui_font, 8),
        )
        self.output_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.output_entry.bind("<KeyPress>", self._mark_manual_output)
        self.browse_btn = self._button(
            output_row, "…", self._pick_output_folder, kind="soft",
            padx=9, pady=4,
        )
        self.browse_btn.pack(side="left", padx=(6, 0))

        self.include_check = tk.Checkbutton(
            inner, text="包含子文件夹，并保留目录结构",
            variable=self.include_subfolders, bg=CARD, fg=INK,
            activebackground=CARD, activeforeground=INK,
            selectcolor="#FFFFFF", font=(self.ui_font, 8),
            anchor="w", bd=0, highlightthickness=0, padx=0, pady=2,
            cursor="hand2",
        )
        self.include_check.pack(anchor="w")

        summary = tk.Frame(inner, bg="#F8F7F4")
        summary.pack(fill="x", pady=(10, 10))
        left_summary = tk.Frame(summary, bg="#F8F7F4")
        left_summary.pack(side="left", padx=11, pady=7)
        tk.Label(left_summary, text="文件数量", bg="#F8F7F4", fg=MUTED,
                 font=(self.ui_font, 7)).pack(anchor="w")
        tk.Label(left_summary, textvariable=self.summary_count_var,
                 bg="#F8F7F4", fg=INK,
                 font=(self.ui_font, 10, "bold")).pack(anchor="w")
        right_summary = tk.Frame(summary, bg="#F8F7F4")
        right_summary.pack(side="right", padx=11, pady=7)
        tk.Label(right_summary, text="原始合计", bg="#F8F7F4", fg=MUTED,
                 font=(self.ui_font, 7)).pack(anchor="e")
        tk.Label(right_summary, textvariable=self.summary_size_var,
                 bg="#F8F7F4", fg=INK,
                 font=(self.ui_font, 10, "bold")).pack(anchor="e")

        self.start_btn = self._button(
            inner, "开始压缩", self._on_primary, kind="primary",
            pady=9, font_size=11,
        )
        self.start_btn.pack(fill="x", pady=(0, 6))

        self.open_btn = self._button(
            inner, "打开输出文件夹", self._open_output, kind="soft",
            pady=6, font_size=8,
        )
        self.open_btn.pack(fill="x")
        self.open_btn.configure(state="disabled")

        progress_area = tk.Frame(inner, bg=CARD)
        progress_area.pack(fill="x", pady=(12, 0))
        self.progress = ttk.Progressbar(
            progress_area, style="Slim.Horizontal.TProgressbar",
            mode="determinate", maximum=100, value=0,
        )
        self.progress.pack(fill="x")
        self.status_label = tk.Label(
            progress_area, textvariable=self.status_var, bg=CARD, fg=MUTED,
            font=(self.ui_font, 8), anchor="w", justify="left",
            wraplength=274,
        )
        self.status_label.pack(fill="x", pady=(6, 0))

        tk.Label(
            inner,
            textvariable=self.note_var,
            bg=CARD, fg="#9A958A", font=(self.ui_font, 7),
            justify="left", wraplength=274,
        ).pack(side="bottom", anchor="w", pady=(6, 0))

    def _build_log_card(self, card: tk.Frame) -> None:
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", expand=True, padx=14, pady=10)

        heading = tk.Frame(inner, bg=CARD)
        heading.pack(fill="x")
        tk.Label(heading, text="处理日志", bg=CARD, fg=INK,
                 font=(self.ui_font, 9, "bold")).pack(side="left")
        tk.Label(heading, text="所有处理均在本机完成 · 不会上传",
                 bg=CARD, fg="#9A958A",
                 font=(self.ui_font, 7)).pack(side="right")
        clear_log = tk.Label(heading, text="清空", bg=CARD, fg=MUTED,
                             font=(self.ui_font, 7), cursor="hand2")
        clear_log.pack(side="right", padx=(0, 12))
        clear_log.bind("<Button-1>", lambda _event: self._clear_log())

        text_wrap = tk.Frame(inner, bg="#141C2B")
        text_wrap.pack(fill="both", expand=True, pady=(7, 0))
        self.log_text = tk.Text(
            text_wrap, height=4, bg="#141C2B", fg="#C9D4E5",
            insertbackground="#C9D4E5", relief="flat", bd=0,
            padx=10, pady=6, wrap="word", font=(self.mono_font, 8),
            state="disabled",
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(text_wrap, orient="vertical",
                                   command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.tag_configure("info", foreground="#C9D4E5")
        self.log_text.tag_configure("muted", foreground="#94A3B8")
        self.log_text.tag_configure("success", foreground="#86EFAC")
        self.log_text.tag_configure("warn", foreground="#FCD34D")
        self.log_text.tag_configure("error", foreground="#FCA5A5")

    def _button(
        self, parent: tk.Misc, text: str, command, kind: str = "soft",
        padx: int = 14, pady: int = 6, font_size: int = 9,
    ) -> tk.Button:
        palette = {
            "primary": (ACCENT, "#FFFFFF", ACCENT_HOVER),
            "dark": (INK, "#FFFFFF", "#27324A"),
            "soft": ("#F6F3EC", INK, "#ECE7DC"),
            "ghost": (CARD, MUTED, "#F3F0E9"),
        }
        bg, fg, hover = palette[kind]
        button = tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=hover, activeforeground=fg, disabledforeground="#B8B2A7",
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            font=(self.ui_font, font_size, "bold"), padx=padx, pady=pady,
        )
        self._bind_hover(button, bg, hover)
        return button

    def _bind_hover(self, button: tk.Button, normal: str, hover: str) -> None:
        button.bind(
            "<Enter>",
            lambda _event, b=button, h=hover: b.configure(bg=h)
            if str(b["state"]) != "disabled" else None,
        )
        button.bind(
            "<Leave>",
            lambda _event, b=button, n=normal: b.configure(bg=n)
            if str(b["state"]) != "disabled" else None,
        )

    # ------------------------------------------------------------------
    # files and settings

    def _pick_files(self) -> None:
        if self._running:
            return
        initial = self._last_dir or Path.home()
        paths = filedialog.askopenfilenames(
            parent=self, title="选择 PDF / JPG / PNG",
            initialdir=str(initial),
            filetypes=[
                ("资料文件", "*.pdf *.jpg *.jpeg *.png"),
                ("PDF 文件", "*.pdf"),
                ("图片", "*.jpg *.jpeg *.png"),
                ("所有文件", "*.*"),
            ],
        )
        if not paths:
            return
        chosen = [Path(p) for p in paths]
        added = self._add_files(chosen, None)
        self._last_dir = chosen[0].parent
        self._append_log("info", f"已添加 {added} 个文件。")

    def _pick_folder(self) -> None:
        if self._running:
            return
        initial = self._last_dir or Path.home()
        path = filedialog.askdirectory(
            parent=self, title="选择包含资料的文件夹", initialdir=str(initial)
        )
        if not path:
            return
        root = Path(path).resolve()
        self._last_dir = root
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            if self.include_subfolders.get():
                candidates = sorted(root.rglob("*"), key=lambda p: str(p).lower())
            else:
                candidates = sorted(root.iterdir(), key=lambda p: str(p).lower())
            files = [p for p in candidates
                     if p.is_file() and p.suffix.lower() in INPUT_EXTENSIONS]
        except OSError as exc:
            self.config(cursor="")
            messagebox.showerror("读取失败", f"无法扫描文件夹：\n{exc}", parent=self)
            return
        finally:
            self.config(cursor="")

        if not files:
            messagebox.showinfo(
                "没有找到资料",
                "这个文件夹里没有 PDF / JPG / PNG 文件。\n"
                "如果是子文件夹中的文件，请勾选「包含子文件夹」。",
                parent=self,
            )
            return
        added = self._add_files(files, root)
        self._append_log(
            "info", f"从「{root.name}」扫描到 {len(files)} 个文件，新增 {added} 个。"
        )

    def _add_files(self, paths: list[Path], root: Path | None) -> int:
        added = 0
        existing = {item.path for item in self._items}
        for path in paths:
            try:
                resolved = path.resolve()
                if (resolved.suffix.lower() not in INPUT_EXTENSIONS
                        or not resolved.is_file()):
                    continue
                if resolved in existing:
                    continue
                size = resolved.stat().st_size
            except OSError:
                continue
            if root is not None:
                try:
                    rel = resolved.relative_to(root)
                except ValueError:
                    rel = Path(resolved.name)
                display = f"{root.name}/{rel.as_posix()}"
            else:
                rel = Path(resolved.name)
                display = resolved.name
            self._row_seq += 1
            iid = f"row{self._row_seq}"
            item = QueueItem(resolved, rel, display, size, iid, root)
            self._items.append(item)
            self._items_by_iid[iid] = item
            existing.add(resolved)
            self.tree.insert(
                "", "end", iid=iid,
                values=(display, human_size(size), "等待中"),
                tags=("wait",),
            )
            added += 1

        if added:
            self.empty_label.place_forget()
            self._refresh_summary()
            # Keep the current batch folder when adding more files after a
            # completed run; otherwise results would be split across folders.
            if not self.output_var.get().strip() and self._last_output is None:
                self.output_var.set(str(self._suggest_output(self._now_ts())))
                self.after_idle(self._show_output_tail)
        return added

    def _show_output_tail(self) -> None:
        try:
            self.output_entry.xview_moveto(1.0)
        except tk.TclError:
            pass

    def _clear_files(self) -> None:
        if self._running:
            return
        self._items.clear()
        self._items_by_iid.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._refresh_summary()
        self._auto_output = True
        self.output_var.set("")
        self._last_output = None
        self._open_btn_state()
        self.status_var.set("准备就绪")
        self.progress["value"] = 0
        self._append_log("muted", "已清空文件列表。")

    def _remove_selected(self) -> None:
        if self._running:
            return
        selected = set(self.tree.selection())
        if not selected:
            return
        for iid in selected:
            self.tree.delete(iid)
            self._items_by_iid.pop(iid, None)
        self._items = [item for item in self._items if item.iid not in selected]
        if not self._items:
            self.empty_label.place(relx=0.5, rely=0.52, anchor="center")
            if self._auto_output:
                self.output_var.set("")
        self._refresh_summary()
        self._append_log("muted", f"已从列表移除 {len(selected)} 个文件。")

    def _select_format(self, code: str) -> None:
        if self._running:
            return
        self.output_format.set(code)
        for name, button in self.format_buttons.items():
            selected = name == code
            button.configure(
                bg=ACCENT if selected else "#F6F3EC",
                fg="#FFFFFF" if selected else INK,
                activebackground=ACCENT_HOVER if selected else "#ECE7DC",
                activeforeground="#FFFFFF" if selected else INK,
            )
        self._update_note()
        self._append_log("muted", f"输出格式：{code}")

    def _parse_target(self) -> int:
        raw = self.target_value.get().strip().replace(",", "")
        if not raw:
            raise ValueError("请输入大小上限，例如 200")
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("大小上限必须是数字，例如 200 或 0.2") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError("大小上限必须大于 0")
        unit = self.target_unit.get().upper()
        multiplier = 1024 if unit == "KB" else 1024 * 1024
        target = int(round(value * multiplier))
        if target < 1024:
            raise ValueError("大小上限不能小于 1 KB")
        return target

    def _on_target_change(self, _event=None) -> None:
        try:
            label = human_size(self._parse_target())
        except ValueError:
            label = "—"
        self.target_text_var.set(label)
        if label == "—":
            self.per_file_hint_var.set("每个文件独立压到目标值以下")
        else:
            self.per_file_hint_var.set(f"每个文件独立压到 {label} 以下")
        self._update_note()

    def _update_note(self) -> None:
        code = self.output_format.get()
        label = self.target_text_var.get()
        if label == "—":
            label = "目标值"
        if code == "PDF":
            self.note_var.set(
                "图片转 1 页 PDF；PDF 保持页数，页数过多拼缩略图。"
            )
        else:
            self.note_var.set(
                f"图片输出 1 张；PDF 按页输出；每张 < {label}。"
            )

    def _refresh_summary(self) -> None:
        count = len(self._items)
        total = sum(item.size for item in self._items)
        self.count_var.set(f"{count} 个文件")
        self.total_var.set(f"合计 {human_size(total)}")
        self.summary_count_var.set(str(count))
        self.summary_size_var.set(human_size(total))
        self.empty_label.place(relx=0.5, rely=0.52, anchor="center") \
            if count == 0 else self.empty_label.place_forget()
        self._refresh_controls()

    def _mark_manual_output(self, _event=None) -> None:
        self._auto_output = False

    def _pick_output_folder(self) -> None:
        if self._running:
            return
        current = self.output_var.get().strip()
        initial = Path(current) if current else (self._last_dir or Path.home())
        path = filedialog.askdirectory(
            parent=self, title="选择输出文件夹", initialdir=str(initial)
        )
        if path:
            self._auto_output = False
            self.output_var.set(path)
            self.after_idle(self._show_output_tail)
            self._append_log("muted", f"输出文件夹：{path}")

    def _suggest_output(self, timestamp: str) -> Path:
        if not self._items:
            return Path.home() / f"PDF压缩输出_{timestamp}"
        first = self._items[0]
        if first.origin_root is not None:
            return first.origin_root.parent / f"{first.origin_root.name}_压缩输出_{timestamp}"
        return first.path.parent / f"PDF压缩输出_{timestamp}"

    @staticmethod
    def _now_ts() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    # ------------------------------------------------------------------
    # Ghostscript

    def _detect_ghostscript(self) -> None:
        path = find_ghostscript()
        if path:
            version = ghostscript_version(path)
            self._gs_path = path
            version_text = f" {version}" if version else ""
            self.badge_var.set(f"● Ghostscript{version_text} 已就绪")
            self.badge_label.configure(fg="#8FE3BD")
            self._append_log("success", f"Ghostscript 已就绪：{path}")
            self.status_var.set("准备就绪")
        else:
            self._gs_path = None
            self.badge_var.set("● 未找到 Ghostscript · 点击重试")
            self.badge_label.configure(fg="#FCA5A5")
            self._append_log(
                "warn",
                "未检测到 Ghostscript：PDF 输入暂不可用，JPG/PNG 图片输入仍可处理。"
                "安装后点击右上角状态重试。",
            )
            self.status_var.set("未找到 Ghostscript（图片仍可用）")
        self._refresh_controls()

    # ------------------------------------------------------------------
    # run

    def _on_primary(self) -> None:
        if self._running:
            self._cancel()
        else:
            self._start()

    def _start(self) -> None:
        if not self._items:
            messagebox.showinfo(
                "还没有文件",
                "请先选择 PDF / JPG / PNG 文件或文件夹。",
                parent=self,
            )
            return
        if not self._gs_path and any(
            item.path.suffix.lower() == ".pdf" for item in self._items
        ):
            messagebox.showerror(
                "需要 Ghostscript",
                "处理 PDF 输入需要 Ghostscript。\n\n"
                "JPG/PNG 图片输入不需要 Ghostscript。\n"
                "安装后点击右上角的状态标签重新检测。",
                parent=self,
            )
            return

        try:
            target_bytes = self._parse_target()
        except ValueError as exc:
            messagebox.showerror("大小上限无效", str(exc), parent=self)
            self.target_entry.focus_set()
            return

        timestamp = self._now_ts()
        # Keep the current batch folder when adding files after a completed
        # run.  A new folder is only generated for a fresh list.
        if not self.output_var.get().strip():
            self.output_var.set(str(self._suggest_output(timestamp)))
            self.after_idle(self._show_output_tail)
        elif self._auto_output and self._last_output is None:
            self.output_var.set(str(self._suggest_output(timestamp)))
            self.after_idle(self._show_output_tail)
        output_text = self.output_var.get().strip().strip('"')
        output_dir = Path(output_text).expanduser()
        try:
            output_dir = output_dir.resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            if not output_dir.is_dir():
                raise OSError("目标不是文件夹")
        except OSError as exc:
            messagebox.showerror(
                "输出文件夹不可用",
                f"无法使用输出文件夹：\n{output_dir}\n\n{exc}",
                parent=self,
            )
            return

        output_format = self.output_format.get()
        signature = (output_format, target_bytes, str(output_dir))
        pending: list[QueueItem] = []
        skipped = 0
        for item in self._items:
            if item.status == "done" and item.done_signature == signature:
                skipped += 1
                continue
            item.status = "pending"
            item.done_signature = None
            pending.append(item)
            self._set_row(item, "等待中", "wait")

        if not pending:
            self.status_var.set("没有待处理文件")
            self._append_log(
                "muted",
                "所有文件都已按当前设置完成，无需重复压缩。"
                "直接添加新文件即可，只会处理新增文件。",
            )
            return

        self._batch_ts = timestamp
        self._last_output = output_dir
        self._running = True
        self._total = len(pending)
        self._current_index = 0
        self._queue = queue.Queue()
        self._cancel_event = threading.Event()
        self.progress["value"] = 0
        self.status_var.set("准备压缩…")
        self._append_log(
            "info",
            f"开始处理 {len(pending)} 个文件 → 输出 {output_format} → "
            f"目标 {human_size(target_bytes)} → {output_dir}",
        )
        if skipped:
            self._append_log(
                "muted",
                f"已跳过 {skipped} 个设置相同的已完成文件。",
            )
        self._refresh_controls()

        self._engine = MaterialCompressor(
            self._gs_path or "",
            target_bytes=target_bytes,
            progress=self._engine_progress,
            log=self._engine_log,
            cancel_event=self._cancel_event,
        )
        thread = threading.Thread(
            target=self._worker,
            args=(
                pending, output_dir, timestamp, output_format, signature,
            ),
            daemon=True,
        )
        thread.start()
        self.after(60, self._poll_queue)

    def _engine_progress(self, fraction: float, text: str) -> None:
        if self._queue is None:
            return
        overall = (self._current_index + fraction) / max(1, self._total)
        self._queue.put(("progress", overall, text))

    def _engine_log(self, level: str, text: str) -> None:
        if self._queue is not None:
            self._queue.put(("log", level, text))

    def _worker(
        self, items: list[QueueItem], output_dir: Path, timestamp: str,
        output_format: str, signature: tuple[str, int, str],
    ) -> None:
        results: list[CompressionResult] = []
        failures: list[tuple[QueueItem, str]] = []
        fatal = ""
        cancelled = False
        try:
            for index, item in enumerate(items):
                if self._cancel_event is not None and self._cancel_event.is_set():
                    cancelled = True
                    break
                self._current_index = index
                item.status = "running"
                self._queue.put(("row", item.iid, "压缩中…", "run"))
                try:
                    relative_parent = (
                        item.rel.parent if item.origin_root is not None
                        else Path(".")
                    )
                    result = self._engine.compress_any(
                        item.path, output_dir, timestamp,
                        output_format, relative_parent,
                    )
                except CancelledError:
                    item.status = "pending"
                    self._queue.put(("row", item.iid, "已停止", "wait"))
                    cancelled = True
                    break
                except Exception as exc:  # show a useful per-file failure
                    message = str(exc) or "未知错误"
                    item.status = "failed"
                    failures.append((item, message))
                    self._queue.put(("row", item.iid, "失败", "fail"))
                    self._queue.put((
                        "log", "error",
                        f"[{index + 1}/{len(items)}] {item.display}：{message}",
                    ))
                    self._queue.put((
                        "progress", (index + 1) / len(items),
                        f"已处理 {index + 1}/{len(items)}",
                    ))
                    continue

                results.append(result)
                item.status = "done"
                item.done_signature = signature
                saved = f" -{result.saved_percent}%" if result.original_size else ""
                if result.method == "原文件已达标":
                    saved = ""
                if result.output_count > 1:
                    row_status = (
                        f"完成 · {result.output_count} 张 · "
                        f"{human_size(result.final_size)}"
                    )
                    produced = (
                        f"{human_size(result.original_size)} → "
                        f"{result.output_count} 张（最大 "
                        f"{human_size(result.final_size)}，合计 "
                        f"{human_size(result.total_output_size)}）{saved}"
                    )
                else:
                    row_status = f"完成 · {human_size(result.final_size)}"
                    produced = (
                        f"{human_size(result.original_size)} → "
                        f"{human_size(result.final_size)}{saved}"
                    )
                self._queue.put(("row", item.iid, row_status, "done"))
                self._queue.put((
                    "log", "success",
                    f"[{index + 1}/{len(items)}] {item.display}  "
                    f"{produced}  ({result.method})",
                ))
                self._queue.put((
                    "progress", (index + 1) / len(items),
                    f"已处理 {index + 1}/{len(items)}",
                ))
        except Exception as exc:
            fatal = f"{type(exc).__name__}: {exc}"
        finally:
            if self._cancel_event is not None and self._cancel_event.is_set():
                cancelled = True
            self._queue.put((
                "finished",
                {
                    "results": results,
                    "failures": failures,
                    "cancelled": cancelled,
                    "fatal": fatal,
                    "output": output_dir,
                },
            ))

    def _poll_queue(self) -> None:
        if not self._running:
            return
        assert self._queue is not None
        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "progress":
                    _, fraction, text = message
                    self.progress["value"] = max(0.0, min(1.0, fraction)) * 100
                    self.status_var.set(text)
                elif kind == "row":
                    _, iid, text, tag = message
                    item = self._items_by_iid.get(iid)
                    if item:
                        self._set_row(item, text, tag)
                elif kind == "log":
                    _, level, text = message
                    self._append_log(level, text)
                elif kind == "finished":
                    self._on_finished(message[1])
                    return
        except queue.Empty:
            pass
        self.after(70, self._poll_queue)

    def _on_finished(self, payload: dict) -> None:
        self._running = False
        results: list[CompressionResult] = payload.get("results", [])
        failures = payload.get("failures", [])
        fatal: str = payload.get("fatal", "")
        cancelled: bool = payload.get("cancelled", False)
        output_dir: Path = payload.get("output", self._last_output)
        self._last_output = output_dir
        self._refresh_controls()

        if cancelled:
            self.status_var.set(f"已停止 · 已完成 {len(results)} 个")
            self._append_log("warn", "任务已停止。已生成的文件会保留。")
            for item in self._items:
                if "wait" in (self.tree.item(item.iid, "tags") or ()):
                    item.status = "pending"
                    self._set_row(item, "已跳过", "wait")
        elif fatal:
            self.status_var.set("处理中断")
            self.progress["value"] = 0
            self._append_log("error", f"处理中断：{fatal}")
            messagebox.showerror(
                "处理中断", f"发生了意外错误：\n\n{fatal}", parent=self
            )
        elif failures:
            self.status_var.set(
                f"完成 · 成功 {len(results)} 个，失败 {len(failures)} 个"
            )
            self._append_log(
                "warn", f"处理结束：成功 {len(results)} 个，失败 {len(failures)} 个。"
            )
            detail = "\n".join(
                f"• {item.display}：{message}" for item, message in failures[:4]
            )
            if len(failures) > 4:
                detail += f"\n… 另有 {len(failures) - 4} 个失败"
            messagebox.showwarning(
                "部分文件未完成",
                f"成功 {len(results)} 个，失败 {len(failures)} 个。\n\n{detail}",
                parent=self,
            )
        else:
            self.status_var.set(f"全部完成 · {len(results)} 个文件")
            self.progress["value"] = 100
            self._append_log(
                "success", f"全部完成，共 {len(results)} 个文件。输出：{output_dir}"
            )
            if results:
                total_before = sum(r.original_size for r in results)
                total_after = sum(
                    r.total_output_size or r.final_size for r in results
                )
                output_total = sum(r.output_count for r in results)
                if messagebox.askyesno(
                    "压缩完成",
                    f"{len(results)} 个源文件已处理完成，"
                    f"共生成 {output_total} 个文件。\n\n"
                    f"{human_size(total_before)} → {human_size(total_after)}\n\n"
                    f"输出文件夹：\n{output_dir}\n\n"
                    "现在打开输出文件夹吗？",
                    parent=self,
                ):
                    self._open_output()

    def _cancel(self) -> None:
        if not self._running:
            return
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._engine is not None:
            self._engine.cancel()
        self.status_var.set("正在停止…")
        self._append_log("warn", "正在停止当前任务…")
        self.start_btn.configure(state="disabled")

    # ------------------------------------------------------------------
    # helpers

    def _set_row(self, item: QueueItem, status: str, tag: str) -> None:
        if not self.tree.exists(item.iid):
            return
        self.tree.item(
            item.iid,
            values=(item.display, human_size(item.size), status),
            tags=(tag,),
        )

    def _refresh_controls(self) -> None:
        if not hasattr(self, "start_btn"):
            return
        can_select = not self._running
        for button in (self.pick_files_btn, self.pick_folder_btn,
                       self.browse_btn):
            button.configure(state="normal" if can_select else "disabled")
        self.clear_btn.configure(
            state="normal" if (self._items and not self._running) else "disabled"
        )
        has_selection = bool(self.tree.selection())
        self.remove_btn.configure(
            state="normal" if (has_selection and not self._running) else "disabled"
        )
        for button in self.format_buttons.values():
            button.configure(state="normal" if not self._running else "disabled")
        self.include_check.configure(state="normal" if not self._running else "disabled")
        self.output_entry.configure(state="normal" if not self._running else "disabled")
        self.target_entry.configure(state="normal" if not self._running else "disabled")
        self.target_unit_box.configure(
            state="readonly" if not self._running else "disabled"
        )

        if self._running:
            self.start_btn.configure(
                text="停止压缩", bg="#334155", fg="#FFFFFF",
                activebackground="#1F2937", state="normal",
            )
            self._bind_hover(self.start_btn, "#334155", "#1F2937")
        else:
            has_pdf = any(
                item.path.suffix.lower() == ".pdf" for item in self._items
            )
            enabled = bool(self._items and (self._gs_path or not has_pdf))
            if enabled:
                self.start_btn.configure(
                    text="开始压缩", bg=ACCENT, fg="#FFFFFF",
                    activebackground=ACCENT_HOVER, state="normal",
                )
                self._bind_hover(self.start_btn, ACCENT, ACCENT_HOVER)
            else:
                self.start_btn.configure(
                    text="开始压缩", bg="#E9E2D8", fg="#8C8478",
                    activebackground="#E9E2D8", state="disabled",
                    disabledforeground="#8C8478",
                )
                self._bind_hover(self.start_btn, "#E9E2D8", "#E9E2D8")
        self._open_btn_state()

    def _open_btn_state(self) -> None:
        if not hasattr(self, "open_btn"):
            return
        usable = (
            not self._running and self._last_output is not None
            and self._last_output.exists()
        )
        self.open_btn.configure(
            state="normal" if usable else "disabled",
            fg=ACCENT if usable else MUTED,
            disabledforeground="#B8B2A7",
        )

    def _open_output(self) -> None:
        path = self._last_output
        if path is None:
            text = self.output_var.get().strip()
            if text:
                path = Path(text)
        if path is None or not path.exists():
            messagebox.showinfo("还没有输出", "完成一次压缩后即可打开输出文件夹。",
                                parent=self)
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _open_selected_file(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        item = self._items_by_iid.get(selected[0])
        if item and item.path.exists():
            try:
                os.startfile(str(item.path))  # type: ignore[attr-defined]
            except Exception:
                pass

    def _append_log(self, level: str, text: str) -> None:
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"{timestamp}  {text}\n", level)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 400:
            self.log_text.delete("1.0", f"{lines - 300}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-o>", lambda _event: self._pick_files())
        self.bind("<Control-O>", lambda _event: self._pick_folder())
        self.bind("<Control-Return>", lambda _event: self._on_primary())
        self.bind("<Escape>", lambda _event: self._cancel() if self._running else None)

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                "正在处理", "任务还在进行中。\n停止并退出吗？", parent=self
            ):
                return
            if self._engine is not None:
                self._engine.cancel()
            self._running = False
        self.destroy()


def main() -> None:
    _enable_dpi_awareness()
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "ExamMaterialSqueezer.App.1"
            )
        except Exception:
            pass
    app = ExamMaterialSqueezerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
