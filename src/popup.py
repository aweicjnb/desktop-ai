from base64 import b64encode
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import sys
import time
import threading
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk
import uuid
import winreg

from web_account import import_edge_login
from web_client import WebClient, WebError
from chatgpt_account import ChatGPTError
from chatgpt_web_account import import_session, update_registration
from chatgpt_web_client import ChatGPTWebClient as ChatGPTClient
from setup_view import SetupView
from answer_view import AnswerView
from clipboard_image import ClipboardImageError, read_clipboard_image
from controls import FONT_FAMILY, IconButton, WindowResize, font_pixels, rounded
from ui_controls import ActionButton
from window_layout import choose_monitor, clamp_rect, remember_rect, restore_rect
from diagnostics import record, record_callback_error, start_logging
from windows import (
    APP_ASSETS, InputMethod, Integration, application, enable_high_dpi, monitor_work_area, monitor_at, monitors,
    open_chrome, open_edge, single_instance, window_dpi,
)


LIGHT = dict(window="#FFFFFF", chrome="#F8F8FC", ink="#1B1E2C", muted="#606477",
             line="#E3E5EF", soft="#F1F1F8", accent="#4F46BD", tint="#EEECFE",
             action="#5147C0", action_ink="#FFFFFF", input="#F4F4FA", input_border="#A9A0D7")
DARK = dict(window="#181A24", chrome="#1C1E2A", ink="#F1F0F8", muted="#A9ACC0",
            line="#343649", soft="#252736", accent="#B9AEFF", tint="#322953",
            action="#AD9EFF", action_ink="#211842", input="#232635", input_border="#796DB3")
PROMPT_LIMIT = 18000
INPUT_FONT_SIZE = 11
REQUEST_START_TIMEOUT = 50
IMAGE_START_TIMEOUT = 95
STOP_TIMEOUT_MS = 4000


def palette():
    path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return DARK if not winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else LIGHT
    except OSError:
        return LIGHT


class Popup:
    def __init__(self, root, demo=False, smoke=False):
        self.root, self.demo = root, demo
        self.scale = window_dpi(root.winfo_id()) / 96
        self.fonts = {}
        self.layout_options = []
        self.colors = palette()
        self.settings_file = None if demo else Path(os.environ["LOCALAPPDATA"]) / "DesktopAI" / "settings.json"
        settings = self.load_settings()
        self.provider = "chatgpt" if settings.get("provider") == "chatgpt" else "deepseek"
        self.chatgpt_model = settings.get("chatgpt_model")
        self.pinned_device = settings.get('pinned_monitor') if isinstance(settings.get('pinned_monitor'), str) else None
        geometry = settings.get('pinned_geometry')
        self.pin_geometry = geometry if isinstance(geometry, dict) and all(
            isinstance(geometry.get(key), (int, float)) and abs(geometry[key]) < 100000 for key in ('x', 'y', 'width', 'height')) and geometry['width'] >= 200 and geometry['height'] >= 160 else None
        self.fullscreen = False
        self.normal_layout = self.fullscreen_device = self.settings_home = None
        self._placing = False
        self._pin_save_timer = None
        self.search_on = tk.BooleanVar(root, settings.get("search", True) is not False)
        self.thinking_on = tk.BooleanVar(root, settings.get("thinking", False) is True)
        self.client = self.windows = None
        self.setup_results = Queue()
        self.setup_task = self.authorization = None
        self.ime = None
        self.active = None
        self.pending_prompt = ""
        self.draft_image = self.pending_image = None
        self.draft_preview = self.question_preview = None
        self.draft_preview_source = self.question_preview_source = None
        self.header_source = None
        self.answer = ""
        self.thought = ""
        self.started = self.needs_login = self.thought_open = False
        self.ready = self.demo
        self.request_started = 0
        self.first_delta = False
        self.stop_requested = False
        self.deadline = 0
        self.focus_grace = 0
        self.positioned = False
        self.drag_offset = (0, 0)
        self.build()
        self.resize = WindowResize(root, self.scale)
        self.resize.constrain = self.constrain_resize
        root.bind('<Configure>', self.window_configured, add='+')
        root.update_idletasks()
        self.ime = InputMethod(root, self.input, INPUT_FONT_SIZE, FONT_FAMILY)
        if not demo:
            root.report_callback_exception = lambda kind, _value, stack: record_callback_error(kind, stack, self.active)
            try:
                self.client = ChatGPTClient(model=self.chatgpt_model) if self.provider == "chatgpt" else WebClient()
                self.ready = True
            except FileNotFoundError:
                self.show_setup()
            except (OSError, ValueError) as error:
                record("web.start_failed", error=type(error).__name__)
                self.show_setup("本机登录数据无法读取，请重新连接。")
            self.update_provider_ui()
            self.prepare_client()
        if not smoke:
            self.windows = Integration(root, self.show, self.show_menu)
            if not self.windows.hotkey:
                record("desktop.hotkey_unavailable")
                self.set_notice("快捷键已被占用，可以双击系统托盘图标打开。")
            self.root.after(75, self.tick)

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = tkfont.Font(self.root, family=FONT_FAMILY, size=font_pixels(size, self.scale))
        return self.fonts[size]

    def dp(self, value):
        return round(value * self.scale)

    def scale_options(self, configure, **options):
        self.layout_options.append((configure, options))
        self.apply_scaled_options(configure, options)

    def apply_scaled_options(self, configure, options):
        configure(**{name: tuple(map(self.dp, value)) if isinstance(value, tuple) else self.dp(value)
                     for name, value in options.items()})

    def set_scale(self, scale):
        if scale == self.scale:
            return
        self.scale = scale
        for size, face in self.fonts.items():
            face.configure(size=font_pixels(size, scale))
        for configure, options in self.layout_options:
            self.apply_scaled_options(configure, options)
        for widget in (self.settings_button, self.pin_button, self.fullscreen_button, self.model_button, self.send_button, self.stop_button,
                       self.answer_view, self.resize, self.setup_panel):
            widget.set_scale(scale)
        for widget, padding in ((self.empty, (34, 6)), (self.question_row, (0, 8)),
                                (self.reason_text, (4, 0)), (self.answer_view, (5, 0))):
            if widget.winfo_manager():
                widget.pack_configure(pady=tuple(map(self.dp, padding)))
        for button in self.notice_actions.winfo_children():
            button.configure(padx=self.dp(7), pady=self.dp(3))
            button.pack_configure(padx=(0, self.dp(6)), pady=(self.dp(5), 0))
        if self.placeholder.winfo_manager():
            self.placeholder.place_configure(x=self.dp(3), y=self.dp(3))
        if self.header_source:
            self.header_icon = self.thumbnail(self.header_source, self.dp(24))
            self.header_mark.configure(image=self.header_icon)
        if self.draft_preview_source:
            self.refresh_draft_preview()
        if self.question_preview_source:
            self.question_preview = self.thumbnail(self.question_preview_source, self.dp(38))
            self.question.configure(image=self.question_preview)
        self.resize_body()
        self.root.after_idle(self.paint_input_shell)
        self.root.after_idle(self.ime.apply)
        self.root.after_idle(self.fit_setup)

    def load_settings(self):
        if self.settings_file:
            try:
                settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if isinstance(settings, dict):
                    return settings
            except (OSError, ValueError):
                pass
        return {}

    def save_settings(self):
        if not self.settings_file:
            return
        settings = {"search": self.search_on.get(), "thinking": self.thinking_on.get(),
                    "provider": self.provider, "chatgpt_model": self.chatgpt_model,
                    'pinned_monitor': self.pinned_device, 'pinned_geometry': self.pin_geometry}
        path = self.settings_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(settings), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            self.set_notice("设置已用于本次运行，但未能保存。")

    def label(self, parent, text="", size=10, color="ink", bg="window", **options):
        return tk.Label(parent, text=text, font=self.font(size), bg=self.colors[bg],
                        fg=self.colors[color], **options)

    def button(self, parent, text, action, fill="soft", color="muted", size=10, **options):
        return tk.Button(parent, text=text, command=action, font=self.font(size),
                         bg=self.colors[fill], fg=self.colors[color], activebackground=self.colors["tint"],
                         activeforeground=self.colors["accent"], disabledforeground=self.colors["muted"],
                         relief="flat", borderwidth=0, cursor="hand2", padx=self.dp(7), pady=self.dp(3), **options)

    def build(self):
        r, c = self.root, self.colors
        r.title("Desktop AI")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=c["line"])
        width = min(self.dp(510), r.winfo_screenwidth() - self.dp(24))
        height = min(self.dp(384), r.winfo_screenheight() - self.dp(50))
        left = max(0, (r.winfo_screenwidth() - width) // 2)
        top = max(0, (r.winfo_screenheight() - height) // 2)
        r.geometry(f"{width}x{height}+{left}+{top}")
        r.bind("<Escape>", self.on_escape)
        r.bind("<Alt-F4>", lambda event: self.exit())
        r.protocol("WM_DELETE_WINDOW", self.hide)

        frame = tk.Frame(r, bg=c["window"])
        frame.pack(fill="both", expand=True)
        self.scale_options(frame.pack_configure, padx=1, pady=1)
        title = tk.Frame(frame, bg=c["chrome"])
        self.scale_options(title.configure, padx=12, pady=5)
        title.pack(fill="x")
        try:
            avatar = tk.PhotoImage(master=r, file=str(APP_ASSETS / "desktop-ai.png"))
        except tk.TclError:
            mark = self.label(title, "✦", 14, "accent", "tint", width=3)
        else:
            r.iconphoto(True, avatar)
            self.header_source = avatar
            self.header_icon = self.thumbnail(avatar, self.dp(24))
            mark = self.label(title, image=self.header_icon, bg="chrome")
        self.header_mark = mark
        mark.pack(side="left")
        brand = self.label(title, "DeepSeek", 11, bg="chrome")
        self.brand = brand
        brand.pack(side="left")
        self.scale_options(brand.pack_configure, padx=7)
        self.settings_button = IconButton(title, "settings", self.toggle_settings, c, self.scale, "chrome", tooltip='设置')
        self.settings_button.pack(side="right")
        self.fullscreen_button = IconButton(title, 'fullscreen', self.toggle_fullscreen, c, self.scale, 'chrome', tooltip='全屏')
        self.fullscreen_button.pack(side='right')
        self.pin_button = IconButton(title, 'pin', self.toggle_pin, c, self.scale, 'chrome', tooltip='固定当前屏幕')
        self.pin_button.pack(side='right')
        self.pin_button.set_flag('selected', bool(self.pinned_device))
        self.model_button = ActionButton(title, '默认 ▾', self.toggle_settings, c, self.scale, width=90)
        self.model_button.configure(bg=c['chrome'])
        self.model_button.pack(side='left')
        self.scale_options(self.model_button.pack_configure, padx=(5, 0))
        for widget in (title, mark, brand):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.end_drag)
            widget.bind('<Double-Button-1>', lambda event: self.toggle_fullscreen())
        divider = tk.Frame(frame, bg=c["line"])
        divider.pack(fill="x")
        self.scale_options(divider.configure, height=1)

        center = self.center = tk.Frame(frame, bg=c["window"])
        center.pack(fill="both", expand=True)
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(center, bg=c["window"], highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        style = ttk.Style(r)
        style.theme_use("clam")
        style.configure("Quiet.Vertical.TScrollbar", background=c["soft"], troughcolor=c["window"],
                        bordercolor=c["window"], lightcolor=c["soft"], darkcolor=c["soft"],
                        arrowcolor=c["muted"])
        self.scale_options(lambda **options: style.configure("Quiet.Vertical.TScrollbar", **options), arrowsize=9)
        self.scrollbar = ttk.Scrollbar(center, command=self.canvas.yview, style="Quiet.Vertical.TScrollbar")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.scrollbar.grid_remove()
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=c["window"])
        self.scale_options(self.body.configure, padx=15, pady=11)
        self.body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self.resize_body)
        self.body.bind("<Configure>", self.fit_scroll)
        r.bind_all("<MouseWheel>", self.scroll_answer, add="+")

        self.question_row = tk.Frame(self.body, bg=c["window"])
        self.question = self.label(self.question_row, size=10, bg="tint", justify="left",
                                   anchor="w", wraplength=self.dp(400))
        self.scale_options(self.question.configure, padx=9, pady=5)
        self.question.pack(side="right")
        bar = tk.Frame(self.body, bg=c["window"])
        bar.pack(fill="x")
        self.answer_bar = bar
        heading = self.label(bar, "回答", 9, "muted")
        heading.pack(side="left")
        self.status = self.label(bar, size=9, color="accent")
        self.status.pack(side="right")
        self.copy_button = self.button(bar, "复制", self.copy_answer, "window", size=9)
        self.reason_button = self.button(bar, "查看思考", self.toggle_thought, "window", size=9)
        for button in (self.copy_button, self.reason_button):
            self.scale_options(button.configure, padx=7, pady=3)

        self.reason_text = self.label(self.body, size=10, color="muted", bg="soft", justify="left",
                                      wraplength=self.dp(449), anchor="w")
        self.scale_options(self.reason_text.configure, padx=9, pady=5)
        self.answer_view = AnswerView(self.body, c, self.scale)
        self.answer_view.bind("<MouseWheel>", self.scroll_answer_from_text, add="+")
        self.empty = self.label(self.body, "回答会显示在这里", 10, "muted")
        self.empty.pack(pady=(self.dp(34), self.dp(6)))

        self.notice = tk.Frame(frame, bg=c["soft"])
        self.scale_options(self.notice.configure, padx=12, pady=6)
        self.notice_label = self.label(self.notice, size=9, bg="soft", justify="left",
                                       anchor="w")
        self.scale_options(self.notice_label.configure, wraplength=460)
        self.notice_label.pack(anchor="w")
        self.notice_actions = tk.Frame(self.notice, bg=c["soft"])
        self.notice_actions.pack(anchor="w")

        self.footer = tk.Frame(frame, bg=c["chrome"])
        self.footer.pack(fill="x", side="bottom", before=center)
        divider = tk.Frame(self.footer, bg=c["line"])
        divider.pack(fill="x")
        self.scale_options(divider.configure, height=1)
        composer = tk.Frame(self.footer, bg=c["chrome"])
        self.scale_options(composer.configure, padx=8, pady=5)
        composer.pack(fill="x")
        self.scale_options(composer.pack_configure, padx=11, pady=8)
        composer.grid_columnconfigure(0, weight=1)
        self.input_shell = tk.Canvas(composer, background=c["chrome"], borderwidth=0, highlightthickness=0)
        self.input_shell.place(relwidth=1, relheight=1, bordermode="outside")
        self.input_shell.bind("<Configure>", self.paint_input_shell)
        self.attachment = tk.Frame(composer, bg=c["input"])
        self.attachment.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.attachment.grid_remove()
        self.attachment_thumb = self.label(self.attachment, bg="input")
        self.attachment_thumb.pack(side="left")
        self.scale_options(self.attachment_thumb.pack_configure, padx=(3, 6))
        self.attachment_label = self.label(self.attachment, size=9, color="muted", bg="input")
        self.attachment_label.pack(side="left")
        remove = self.button(self.attachment, "移除", self.remove_draft_image, "input", size=9)
        remove.pack(side="right")
        self.scale_options(remove.configure, padx=7, pady=3)
        self.input = tk.Text(composer, height=1, width=1, wrap="word", bg=c["input"], fg=c["ink"],
                             insertbackground=c["accent"], font=self.font(INPUT_FONT_SIZE),
                             undo=True, relief="flat", bd=0)
        self.scale_options(self.input.configure, padx=3, pady=3)
        self.input.grid(row=1, column=0, sticky="ew")
        self.scale_options(self.input.grid_configure, padx=(0, 5))
        self.placeholder = self.label(self.input, "输入问题…", INPUT_FONT_SIZE, "muted", "input", anchor="w")
        self.placeholder.bind("<Button-1>", lambda event: self.input.focus_set())
        self.input.bind("<<Modified>>", self.update_input)
        self.input.bind("<<Paste>>", self.paste_image)
        self.input.bind("<Return>", self.on_enter)
        self.input.bind("<Shift-Return>", lambda event: None)
        self.input.bind("<FocusIn>", self.input_focused)
        self.input.bind("<FocusOut>", lambda event: r.after_idle(self.paint_input_shell))
        self.send_button = IconButton(composer, "send", self.submit, c, self.scale, "input")
        self.send_button.grid(row=1, column=1, sticky="s")
        self.stop_button = IconButton(composer, "stop", self.stop, c, self.scale, "input")
        self.stop_button.grid(row=1, column=1, sticky="s")
        self.stop_button.grid_remove()
        self.update_input()
        self.setup_panel = SetupView(center, self)
        self.settings = self.setup_panel

        self.menu = tk.Menu(r, tearoff=False)
        self.menu.add_command(label="打开弹窗", command=self.show)
        self.menu.add_command(label="打开网页版", command=self.open_login)
        self.menu.add_command(label="模型与账号", command=self.refresh_login)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.exit)

    def resize_body(self, event=None):
        width = event.width if event else self.canvas.winfo_width()
        self.canvas.itemconfigure(self.body_window, width=width)
        wrap = max(self.dp(210), width - self.dp(34))
        self.reason_text.configure(wraplength=wrap - self.dp(20))
        self.question.configure(wraplength=wrap - self.dp(42))

    def fit_scroll(self, event=None):
        was_at_bottom = self.canvas.yview()[1] >= .98
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.body.winfo_reqheight() > self.canvas.winfo_height() + 2:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
        if was_at_bottom and self.active and self.answer:
            self.canvas.yview_moveto(1)

    def scroll_answer(self, event):
        if self.setup_panel.winfo_manager():
            return self.setup_panel.scroll(event)
        if event.widget is not self.input and self.scrollbar.winfo_ismapped():
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def scroll_answer_from_text(self, event):
        self.scroll_answer(event)
        return "break"

    def paint_input_shell(self, event=None):
        view, s, c = self.input_shell, self.scale, self.colors
        width, height = view.winfo_width(), view.winfo_height()
        if min(width, height) <= 1:
            return
        view.delete("all")
        color = c["input_border"] if self.root.focus_get() is self.input else c["line"]
        rounded(view, s, s, width - s, height - s, 12*s, fill=c["input"], outline=color, width=s)

    def input_focused(self, event):
        self.root.after_idle(self.paint_input_shell)
        if self.ime:
            self.root.after_idle(self.ime.apply)

    def start_drag(self, event):
        if self.fullscreen:
            self.drag_offset = None
            return 'break'
        self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event):
        if self.drag_offset is None or self.fullscreen:
            return 'break'
        x, y = self.drag_offset
        rect = (event.x_root-x, event.y_root-y, self.root.winfo_width(), self.root.winfo_height())
        if self.pinned_device:
            screen = self.target_monitor()
            if screen:
                rect = clamp_rect(rect, screen['work'])
        self.put_rect(rect)

    def end_drag(self, event):
        if self.fullscreen or self.drag_offset is None:
            return
        self.place_on_pointer_monitor(keep_position=True)
        self.remember_pin()

    def window_rect(self):
        size = re.match(r'(\d+)x(\d+)', self.root.geometry())
        width, height = map(int, size.groups()) if size else (self.dp(510), self.dp(384))
        return self.root.winfo_rootx(), self.root.winfo_rooty(), width, height

    def put_rect(self, rect):
        x, y, width, height = map(round, rect)
        self.root.geometry(f'{width}x{height}+{x}+{y}')

    def current_monitor(self):
        x, y, width, height = self.window_rect()
        return monitor_at(x+width/2, y+height/2)

    def constrain_resize(self, rect):
        if self.pinned_device:
            screen = self.target_monitor()
            if screen:
                return clamp_rect(rect, screen['work'])
        return rect

    def target_monitor(self, keep_position=False):
        x, y, width, height = self.window_rect()
        point = (x+width/2, y+height/2) if keep_position else self.root.winfo_pointerxy()
        return choose_monitor(monitors(), self.pinned_device, point)

    def move_to_monitor(self, screen, saved=None, keep_position=False):
        if not screen:
            return
        self._placing = True
        try:
            self.root.update_idletasks()
            x, y, width, height = self.window_rect()
            old_scale = self.scale
            left, top, right, bottom = screen['work']
            same = self.positioned and left <= x+width/2 < right and top <= y+height/2 < bottom
            if not same:
                probe_w, probe_h = min(width, right-left), min(height, bottom-top)
                self.put_rect((left+(right-left-probe_w)//2, top+(bottom-top-probe_h)//2, probe_w, probe_h))
                self.root.update_idletasks()
            scale = window_dpi(self.root.winfo_id()) / 96
            self.set_scale(scale)
            if saved:
                rect = restore_rect(saved, screen['work'], scale)
            else:
                width, height = round(width*scale/old_scale), round(height*scale/old_scale)
                if not (same or keep_position):
                    x, y = left+(right-left-width)//2, top+(bottom-top-height)//2
                rect = clamp_rect((x, y, width, height), screen['work'])
            self.put_rect(rect)
            self.root.update_idletasks()
            self.positioned = True
        finally:
            self._placing = False

    def place_on_pointer_monitor(self, keep_position=False):
        screen = self.target_monitor(keep_position)
        if self.fullscreen:
            self.apply_fullscreen(screen)
        else:
            saved = self.pin_geometry if self.pinned_device and not keep_position and not self.setup_panel.winfo_manager() else None
            self.move_to_monitor(screen, saved, keep_position)

    def toggle_pin(self):
        screen = self.current_monitor()
        self.pin_to_monitor(None if self.pinned_device else screen['id'] if screen else None)

    def pin_to_monitor(self, device):
        if not device:
            self.pinned_device = None
            self.pin_geometry = None
        else:
            screen = next((item for item in monitors() if item['id'] == device), None)
            if not screen:
                return
            current = self.current_monitor()
            if self.fullscreen:
                saved = self.settings_home or self.normal_layout
            elif self.settings_home:
                saved = self.settings_home
            else:
                saved = remember_rect(self.window_rect(), current['work'], self.scale) if current else None
            self.pinned_device = device
            self.pin_geometry = dict(saved) if saved else None
            if self.fullscreen:
                self.apply_fullscreen(screen)
            else:
                self.move_to_monitor(screen, keep_position=bool(current and current['id'] == device))
                if not self.setup_panel.winfo_manager():
                    self.pin_geometry = remember_rect(self.window_rect(), screen['work'], self.scale)
        self.pin_button.set_flag('selected', bool(device))
        self.pin_button.tooltip = '取消固定' if device else '固定当前屏幕'
        self.save_settings()

    def remember_pin(self):
        self._pin_save_timer = None
        if not self.pinned_device or self.fullscreen or self._placing or self.setup_panel.winfo_manager():
            return
        screen = self.current_monitor()
        if screen and screen['id'] == self.pinned_device:
            self.pin_geometry = remember_rect(self.window_rect(), screen['work'], self.scale)
            self.save_settings()

    def window_configured(self, event):
        if event.widget is not self.root or self._placing or not self.pinned_device or self.fullscreen:
            return
        if self._pin_save_timer:
            self.root.after_cancel(self._pin_save_timer)
        self._pin_save_timer = self.root.after(250, self.remember_pin)

    def apply_fullscreen(self, screen):
        if not screen:
            return
        self.move_to_monitor(screen)
        self.fullscreen_device = screen['id']
        left, top, right, bottom = screen['bounds']
        self.put_rect((left, top, right-left, bottom-top))

    def toggle_fullscreen(self):
        screen = self.current_monitor() or self.target_monitor()
        if not screen:
            return
        self.setup_panel.close_choices()
        if not self.fullscreen:
            self.normal_layout = remember_rect(self.window_rect(), screen['work'], self.scale)
            self.fullscreen = True
            self.resize.cancel()
            self.resize.enabled = False
            self.fullscreen_button.set_icon('restore', '退出全屏')
            self.fullscreen_button.set_flag('selected', True)
            self.apply_fullscreen(screen)
        else:
            self.fullscreen = False
            self.resize.enabled = True
            self.fullscreen_button.set_icon('fullscreen', '全屏')
            self.fullscreen_button.set_flag('selected', False)
            self.move_to_monitor(screen, self.normal_layout)
            self.normal_layout = self.fullscreen_device = None
            self.remember_pin()
            if self.setup_panel.winfo_manager():
                self.root.after_idle(self.fit_setup)

    def prepare_client(self):
        if not self.demo and self.provider == 'chatgpt' and self.client:
            self.client.start_preparation()

    def show(self):
        self.prepare_client()
        self.place_on_pointer_monitor()
        self.focus_grace = time.monotonic() + .3
        self.root.deiconify()
        self.root.lift()
        target = (self.setup_panel.back if self.setup_panel.busy else self.setup_panel.source_picker) if self.setup_panel.winfo_manager() else self.input
        self.root.after_idle(target.focus_force)

    def hide(self, event=None):
        self.resize.cancel()
        self.answer_view.close_preview()
        self.close_settings()
        self.root.withdraw()
        return "break"

    def on_escape(self, event):
        if self.input_composing():
            return "break"
        if self.settings.winfo_manager():
            self.close_settings()
            self.input.focus_set()
            return "break"
        if self.fullscreen:
            self.toggle_fullscreen()
            return 'break'
        return self.hide()

    def close_settings(self):
        if hasattr(self, 'setup_panel') and self.setup_panel.winfo_manager():
            self.close_setup()

    def toggle_settings(self):
        if self.setup_panel.winfo_manager() and (self.client or self.demo):
            self.close_settings()
            self.input.focus_set()
        else:
            self.show_setup()

    def dismiss_settings(self, event):
        pass

    def show_menu(self):
        self.menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def exit(self):
        if self.authorization:
            self.authorization.cancel()
        self.setup_task = None
        if not self.demo:
            record("desktop.exit", self.active)
        if self.active and self.client:
            self.cancel_request()
        if self.windows:
            self.windows.close()
        if self.client:
            self.client.close()
        for timer in self.root.tk.call("after", "info"):
            # Each widget owns its registered Tcl callback. Cancel the timer
            # here and let that widget remove its own command during destroy.
            self.root.tk.call("after", "cancel", timer)
        self.root.destroy()

    def set_notice(self, text="", actions=()):
        for button in self.notice_actions.winfo_children():
            button.destroy()
        if not text:
            self.notice.pack_forget()
            return
        self.notice_label.configure(text=text)
        for label, action in actions:
            button = self.button(self.notice_actions, label, action, "tint", "accent", size=9)
            self.resize.attach(button)
            button.pack(side="left", padx=(0, self.dp(6)), pady=(self.dp(5), 0))
        self.notice.pack(fill="x", side="bottom", after=self.footer)

    def show_setup(self, message=""):
        if not self.setup_panel.winfo_manager():
            if self.fullscreen:
                self.settings_home = dict(self.normal_layout) if self.normal_layout else None
            else:
                screen = self.current_monitor()
                if screen:
                    self.settings_home = remember_rect(self.window_rect(), screen['work'], self.scale)
        self.set_notice()
        preferred = self.client.account.get("edge_profile", "Default") if self.client else None
        self.setup_panel.show(preferred, can_return=self.client is not None or self.demo, message=message, provider=self.provider)
        self.settings_button.set_flag('selected', True)
        if self.active:
            self.setup_panel.connect.configure(state='disabled')
            self.setup_panel.status.configure(text='回答结束后可切换模型，窗口和回答偏好可随时调整。')
        self.footer.pack_forget()
        self.root.after_idle(self.fit_setup)

    def fit_setup(self):
        if not self.setup_panel.winfo_manager() or self.fullscreen or self._placing:
            return
        width = self.dp(390)
        header = self.brand.master.winfo_reqheight() + self.dp(3)
        height = self.setup_panel.winfo_reqheight() + header
        self.resize.min_width = width
        self.resize.min_height = height
        geometry = re.match(r"(\d+)x(\d+)", self.root.geometry())
        current_width, current_height = map(int, geometry.groups()) if geometry else (0, 0)
        width = max(width, self.root.winfo_width(), current_width)
        height = max(height, self.root.winfo_height(), current_height)
        screen = self.current_monitor()
        if screen:
            x, y, width, height = clamp_rect((self.root.winfo_rootx(), self.root.winfo_rooty(), width, height), screen['work'])
            self.resize.min_width = min(self.resize.min_width, screen['work'][2]-screen['work'][0])
            self.resize.min_height = min(self.resize.min_height, screen['work'][3]-screen['work'][1])
            if (x, y, width, height) != self.window_rect():
                self.put_rect((x, y, width, height))
        elif width != self.root.winfo_width() or height != self.root.winfo_height():
            self.root.geometry(f"{width}x{height}")

    @property
    def provider_name(self):
        return "ChatGPT" if self.provider == "chatgpt" else "DeepSeek"

    def update_provider_ui(self):
        self.brand.configure(text=self.provider_name)
        name = '默认'
        if self.provider == 'chatgpt' and self.client:
            name = next((item.get('display_name') or item['slug'] for item in self.client.models if item['slug'] == self.client.model), self.client.model)
        text = name+' ▾'
        while name and self.font(10).measure(text) > self.dp(self.model_button.logical_width-16):
            name = name[:-1]
            text = name.rstrip()+'… ▾'
        self.model_button.configure(text=text)

    def new_chatgpt_session(self):
        self.close_settings()
        if self.active:
            self.set_notice("请先结束当前请求，再开始新会话。")
            return
        if self.provider != "chatgpt" or not self.client:
            return
        try:
            self.client.reset_session()
        except (OSError, ValueError, ChatGPTError):
            self.set_notice("本机会话未能重置，请稍后重试。")
        else:
            self.reset()
            self.set_notice("已开始新的 ChatGPT 会话。")

    def refresh_login(self):
        if self.active:
            self.set_notice("请先结束当前请求，再连接其他账号。")
            return
        self.show_setup()

    def close_setup(self):
        self.setup_panel.close_choices()
        self.setup_panel.grid_remove()
        self.settings_button.set_flag('selected', False)
        self.resize.set_scale(self.scale)
        self.footer.pack(fill="x", side="bottom", before=self.center)
        if self.settings_home:
            if self.fullscreen:
                self.normal_layout = dict(self.settings_home)
            else:
                screen = self.current_monitor()
                if screen:
                    self.move_to_monitor(screen, self.pin_geometry if self.pinned_device and self.pin_geometry else self.settings_home)
        self.settings_home = None
        self.root.after_idle(self.input.focus_set)

    def cancel_setup_login(self):
        if self.authorization:
            self.authorization.cancel()
            self.authorization = None
        self.setup_task = None
        self.setup_panel.set_busy(False)
        if self.client or self.demo:
            self.close_setup()
        else:
            self.hide()

    def import_login(self):
        if self.setup_panel.busy or self.active:
            return
        profile = self.setup_panel.profile()
        if (self.client and self.provider == 'deepseek' and not self.setup_panel.login_visible
                and self.client.account.get('edge_profile', 'Default') == profile):
            self.close_setup()
            return
        task = self.setup_task = uuid.uuid4().hex
        self.setup_panel.set_busy(True)
        threading.Thread(target=self.read_login, args=(profile, task), daemon=True).start()

    def read_login(self, profile, task):
        client = None
        try:
            account = import_edge_login(profile)
            client = WebClient(account=account)
            client.connect_account()
        except (OSError, ValueError, WebError) as error:
            record("web.account_import_failed", error=type(error).__name__)
            if isinstance(error, (ValueError, WebError)):
                text = str(error)
            else:
                text = "无法读取或保存登录，请确认 Edge 已完成登录，稍后再试。"
            if client:
                client.close()
            self.setup_results.put((task, None, text))
        except Exception as error:
            record("web.account_import_failed", error=type(error).__name__)
            if client:
                client.close()
            self.setup_results.put((task, None, "读取登录时发生错误，请重新登录后再试。"))
        else:
            self.setup_results.put((task, client, ""))

    def start_chatgpt_login(self):
        if self.setup_panel.busy or self.active:
            return
        try:
            open_chrome('https://chatgpt.com/api/auth/session')
            self.hide()
        except OSError:
            self.setup_panel.show_error('无法打开 Chrome，请检查浏览器安装。')

    def import_chatgpt_login(self):
        if self.setup_panel.busy or self.active:
            return
        try:
            account = import_session(self.root.clipboard_get())
        except (ValueError, tk.TclError) as error:
            self.setup_panel.show_error(str(error) if isinstance(error, ValueError) else '请先在 Chrome 的网页会话页面复制完整 JSON。')
            return
        task = self.setup_task = uuid.uuid4().hex
        self.setup_panel.set_busy(True, '正在验证网页登录并连接 ChatGPT…')
        threading.Thread(target=self.read_chatgpt_login, args=(task, None, account, None), daemon=True).start()

    def use_chatgpt_account(self):
        if self.setup_panel.busy or self.active:
            return
        account = self.setup_panel.account()
        if not account:
            return
        if self.client and self.provider == 'chatgpt' and self.client.client_id == account['client_id']:
            try:
                model = self.setup_panel.model() or self.client.model
                self.client.set_model(model)
                update_registration(self.client.client_id, {'model': model})
            except (OSError, ValueError, ChatGPTError):
                self.setup_panel.show_error('未能保存模型选择，请稍后重试。')
                return
            self.chatgpt_model = model
            self.update_provider_ui()
            self.save_settings()
            self.close_setup()
            return
        task = self.setup_task = uuid.uuid4().hex
        self.setup_panel.set_busy(True, "正在验证 ChatGPT 登录并获取可用模型…")
        threading.Thread(target=self.read_chatgpt_login,
                         args=(task, None, account, self.setup_panel.model()), daemon=True).start()

    def read_chatgpt_login(self, task, authorization, account, model):
        client = None
        try:
            client = ChatGPTClient(account=account, model=model)
            client.connect_account()
        except (OSError, ValueError, ChatGPTError) as error:
            if client:
                client.close()
            record("chatgpt.connect_failed", error=type(error).__name__)
            description = str(error) if isinstance(error, ChatGPTError) else "无法读取或保存 ChatGPT 登录数据，请重试。"
            self.setup_results.put((task, None, description))
        except Exception as error:
            if client:
                client.close()
            record("chatgpt.connect_failed", error=type(error).__name__)
            self.setup_results.put((task, None, "ChatGPT 连接失败，请重新登录后再试。"))
        else:
            self.setup_results.put((task, client, ""))

    def open_page(self, url, profile=None):
        try:
            open_edge(url, profile)
            self.root.withdraw()
        except OSError:
            text = "未找到可用的 Microsoft Edge，请先安装 Edge 后重试。"
            if self.setup_panel.winfo_manager():
                self.setup_panel.show_error(text)
            else:
                self.set_notice(text)

    def open_login(self):
        provider = self.setup_panel.provider() if self.setup_panel.winfo_manager() else self.provider
        if provider == "chatgpt":
            if self.setup_panel.winfo_manager():
                self.start_chatgpt_login()
            else:
                try:
                    open_chrome(self.client.current_url() if self.client else 'https://chatgpt.com/')
                    self.hide()
                except OSError:
                    self.set_notice("未找到 Google Chrome。")
            return
        url = "https://chat.deepseek.com/"
        if self.setup_panel.winfo_manager():
            profile = self.setup_panel.profile()
        else:
            profile = self.client.account.get("edge_profile") if self.client else None
        if self.client and not self.setup_panel.winfo_manager():
            url += "a/chat/s/" + self.client.account["session_id"]
        self.open_page(url, profile)

    def open_chatgpt_usage(self):
        try:
            open_chrome("https://chatgpt.com/settings/usage")
            self.hide()
        except OSError:
            self.set_notice("未找到 Google Chrome。")

    def acknowledge_chatgpt_plan(self):
        try:
            update_registration(self.client.client_id, {"plan_notice_seen": True})
        except (OSError, ValueError, KeyError):
            self.set_notice("未能保存提示状态，下次连接时会再次提示。")
        else:
            self.set_notice()

    def read_input(self):
        return self.input.get("1.0", "end-1c")

    @staticmethod
    def thumbnail(source, size):
        sample = (max(source.width(), source.height()) + size - 1) // size
        return source.subsample(sample) if sample > 1 else source

    def paste_image(self, event=None):
        try:
            image = read_clipboard_image(self.root.winfo_id())
            if image is None:
                return None
            source = tk.PhotoImage(master=self.root, data=b64encode(image.png), format="png")
        except ClipboardImageError as error:
            self.set_notice(str(error))
            return "break"
        except tk.TclError:
            self.set_notice("无法打开剪贴板图片，请重新复制。")
            return "break"
        self.set_draft_image(image, self.thumbnail(source, 192))
        if self.ready:
            self.set_notice()
        return "break"

    def refresh_draft_preview(self):
        source = self.draft_preview_source
        self.draft_preview = self.thumbnail(source, self.dp(38)) if source else None
        self.attachment_thumb.configure(image=self.draft_preview or "")
        if self.attachment.winfo_manager():
            self.attachment.grid_configure(pady=(0, self.dp(3)))

    def set_draft_image(self, image, preview_source=None):
        self.draft_image, self.draft_preview_source = image, preview_source
        if image:
            self.attachment_label.configure(text=f"图片 · {image.width} × {image.height}")
            self.attachment.grid()
        else:
            self.attachment.grid_remove()
        self.refresh_draft_preview()
        self.update_input()

    def remove_draft_image(self):
        self.set_draft_image(None)
        self.input.focus_set()

    def update_input(self, event=None):
        self.input.edit_modified(False)
        value = self.read_input()
        self.input.configure(height=min(3, 1 + value.count("\n")))
        if value or (self.ime and self.ime.composing):
            self.placeholder.place_forget()
        else:
            self.placeholder.configure(text="输入下一个问题…" if self.question_row.winfo_manager()
                                       else "输入问题…")
            self.placeholder.place(x=self.dp(3), y=self.dp(3))
        self.send_button.set_enabled(bool(value.strip() or self.draft_image))

    def replace_input(self, text):
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self.input.edit_reset()
        self.update_input()

    def on_enter(self, event):
        if not self.input_composing():
            self.submit()
        return "break"

    def input_composing(self):
        if not self.ime:
            return False
        focused = self.root.focus_get() is self.input and self.root.state() != "withdrawn"
        if self.ime.refresh(focused):
            self.update_input()
        return self.ime.composing

    def copy_answer(self):
        text = self.answer_view.selected_text() or self.answer_view.copyable_text()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def toggle_thought(self):
        self.thought_open = not self.thought_open
        self.paint_thought()

    def paint_thought(self):
        if not (self.answer and self.thought):
            self.reason_button.pack_forget()
            self.reason_text.pack_forget()
            return
        self.reason_button.configure(text="收起思考" if self.thought_open else "查看思考")
        self.reason_button.pack(side="right")
        if self.thought_open:
            self.reason_text.configure(text=self.thought)
            self.reason_text.pack(fill="x", pady=(self.dp(4), 0), after=self.answer_bar)
        else:
            self.reason_text.pack_forget()

    def reset(self):
        self.answer = self.thought = ""
        self.thought_open = False
        self.answer_view.clear()
        for widget in (self.question_row, self.reason_text, self.reason_button, self.answer_view, self.copy_button):
            widget.pack_forget()
        self.status.configure(text="")
        self.empty.configure(text="回答会显示在这里")
        self.empty.pack(pady=(self.dp(34), self.dp(6)))

    def submit(self):
        text = self.read_input().strip()
        image = self.draft_image
        if self.active or not (text or image):
            return
        if len(text) > PROMPT_LIMIT:
            self.set_notice(f"单次最多可发送 {PROMPT_LIMIT} 个字符，请缩短后重试。")
            return
        if not self.ready:
            record("desktop.submit_blocked", reason="login_unavailable")
            self.show_setup()
            return
        self.active = uuid.uuid4().hex
        self.pending_prompt = text
        self.pending_image = image
        self.question_preview = self.draft_preview
        self.question_preview_source = self.draft_preview_source
        self.started = self.needs_login = self.first_delta = False
        self.request_started = time.monotonic()
        self.deadline = self.request_started + 325
        self.reset()
        self.set_notice()
        self.question.configure(text=text or "图片", image=self.question_preview or "", compound="left")
        self.question_row.pack(fill="x", pady=(0, self.dp(8)), before=self.answer_bar)
        self.set_draft_image(None)
        self.replace_input("")
        self.status.configure(text="●  已发送" if self.demo else "●  正在连接")
        self.empty.configure(text=f"等待 {self.provider_name} 回复…" if self.demo else f"正在连接 {self.provider_name}…")
        self.send_button.grid_remove()
        self.stop_button.set_enabled(True)
        self.stop_button.grid()
        self.canvas.yview_moveto(0)
        if self.demo:
            self.run_demo(self.active)
        else:
            search = self.search_on.get() and (image is None or self.provider == "chatgpt")
            record("desktop.queued", self.active, provider=self.provider, search=search, thinking=self.thinking_on.get(), image=image is not None)
            self.client.active_request = self.active
            job = dict(type="ask", id=self.active,
                       prompt=text, search=search, thinking=self.thinking_on.get())
            if image is not None:
                job.update(type="ask_image", prompt=text or "请说明这张图片的内容。",
                           image=b64encode(image.png).decode("ascii"))
            self.client.enqueue(job)

    def run_demo(self, request):
        sample = "这是流式输出示例。DeepSeek 正文会在这里逐段显示。"
        thought = "先确认问题中的重点，再用简洁的方式回答。" if self.thinking_on.get() else ""
        searching = self.search_on.get()

        def chunk(index=0):
            if self.active != request:
                return
            if index == len(sample):
                self.finish("●  已完成")
                return
            self.delta(sample[:index + 1], thought, "done" if searching else "")
            self.root.after(42, chunk, index + 1)

        def begin():
            if self.active == request:
                self.delta("", thought, "searching" if searching else "")
                self.root.after(950 if thought or searching else 50, chunk)

        self.root.after(500, begin)

    def stop(self):
        if not self.active or self.stop_requested:
            return
        if self.demo:
            self.finish("已停止")
        else:
            self.record_request("stop_requested")
            self.stop_requested = True
            self.stop_button.set_enabled(False)
            self.status.configure(text="正在停止…")
            self.cancel_request()
            self.root.after(STOP_TIMEOUT_MS, self.stop_timed_out, self.active)

    def cancel_request(self):
        self.client.active_request = None
        self.client.enqueue({"type": "stop", "id": self.active})

    def stop_timed_out(self, request):
        if self.active == request and self.stop_requested:
            self.finish_unconfirmed_stop("stop_timeout")

    def finish_unconfirmed_stop(self, stage):
        self.record_request(stage)
        self.finish("已停止等待")
        if self.provider == "chatgpt":
            self.set_notice("已停止等待，连接正在关闭；已收到的内容保留。")
        else:
            self.set_notice("已停止等待，服务端停止状态尚未确认。", (("查看当前会话", self.open_login),))

    def delta(self, answer, thought, search, images=()):
        self.answer, self.thought = answer, thought
        if answer:
            self.answer_view.pack(fill="x", pady=(self.dp(5), 0))
            fetch = self.client.load_reply_image if self.client and self.provider=='chatgpt' else None
            self.answer_view.set_source(answer,images=images,fetch=fetch,open_page=self.open_login if fetch else None)
            self.copy_button.pack(side="right")
            if not self.stop_requested:
                self.status.configure(text="●  回答中")
            self.empty.pack_forget()
        else:
            self.answer_view.pack_forget()
            self.copy_button.pack_forget()
            if not self.stop_requested:
                phase = "搜索中" if search == "searching" else "思考中" if thought else "已发送"
                self.status.configure(text="●  " + phase)
        self.paint_thought()

    def finish(self, status):
        if self.client:
            self.client.active_request = None
        self.active = None
        self.stop_requested = False
        self.pending_prompt = ""
        self.pending_image = None
        self.status.configure(text=status)
        if self.empty.winfo_manager():
            self.empty.configure(text=status.strip("● "))
        self.stop_button.grid_remove()
        self.send_button.grid()
        self.update_input()
        if self.setup_panel.winfo_manager() and not self.setup_panel.busy:
            self.setup_panel.connect.configure(state='normal')
            self.setup_panel.status.configure(text='')
        focus = self.root.focus_get()
        if self.root.state() != "withdrawn" and not self.settings.winfo_manager() and focus is not self.answer_view:
            self.input.focus_set()

    def fail(self, description, login=False):
        prompt = self.pending_prompt
        image = self.pending_image
        restore = login or not self.started
        self.needs_login = login
        self.finish("需要登录" if login else "请求中断")
        if restore and (prompt or image) and not self.read_input().strip() and self.draft_image is None:
            self.replace_input(prompt)
            if image:
                self.set_draft_image(image, self.question_preview_source)
        actions = (("重新连接 ChatGPT", self.refresh_login),) if self.provider == "chatgpt" else (("去 Edge 登录", self.open_login), ("重新读取登录", self.refresh_login))
        if self.provider == "chatgpt" and any(word in description for word in ("额度", "频率")):
            actions = (("管理 ChatGPT 用量", self.open_chatgpt_usage),)
        elif not login:
            actions = ()
        self.set_notice(description, actions)

    def record_request(self, stage, **fields):
        elapsed_ms = round((time.monotonic() - self.request_started) * 1000)
        record("desktop." + stage, self.active, elapsed_ms=elapsed_ms, **fields)

    def accept(self, event):
        if event.get("id") != self.active or not self.active:
            return
        kind = event.get("type")
        if kind == "progress":
            if not self.started and not self.stop_requested:
                self.status.configure(text="●  " + event["text"])
                self.empty.configure(text=event["text"] + "…")
        elif kind == "started":
            self.started = True
            if not self.first_delta and not self.stop_requested:
                self.status.configure(text="●  已发送")
                self.empty.configure(text=f"等待 {self.provider_name} 回复…")
        elif kind == "delta":
            self.started = True
            answer = str(event.get("answer", ""))
            if not self.first_delta:
                self.first_delta = True
                self.record_request("first_delta", has_answer=bool(answer))
            if answer and not self.answer:
                self.record_request("first_answer")
            self.delta(answer, str(event.get("thought", "")), event.get("search", ""),event.get('images',[]))
        elif kind == "complete":
            answer = event.get("answer")
            if isinstance(answer, str) and answer:
                self.delta(answer, str(event.get("thought", "")), event.get("search", ""),event.get('images',[]))
            failed = event.get('failed', False)
            self.record_request("generation_failed" if failed else "complete", has_answer=bool(self.answer))
            self.finish("生成失败" if failed else "●  已完成")
            if failed:
                self.set_notice(str(event.get('notice') or '生成失败，具体原因见上方回复。'))
        elif kind == "stopped":
            if event.get("confirmed") is False:
                self.finish_unconfirmed_stop("stop_unconfirmed")
            else:
                self.record_request("stopped")
                self.finish("已停止")
        elif kind in ("login", "error"):
            self.record_request(kind, started=self.started)
            self.fail(str(event.get("text") or f"{self.provider_name} 请求失败，请稍后重试。"), login=kind == "login")

    def tick(self):
        while True:
            try:
                task, client, error = self.setup_results.get_nowait()
            except Empty:
                break
            if task != self.setup_task:
                if client:
                    client.close()
                continue
            self.setup_task = self.authorization = None
            self.setup_panel.set_busy(False)
            if error:
                self.setup_panel.source_changed()
                self.setup_panel.show_error(error)
                if self.root.state() == "withdrawn":
                    self.show()
            else:
                if client.provider == "chatgpt":
                    try:
                        update_registration(client.client_id, activate=True)
                    except (OSError, ValueError, KeyError):
                        client.close()
                        self.setup_panel.show_error("未能保存 ChatGPT 账号选择，原有连接已保留。")
                        continue
                if self.client:
                    self.client.close()
                self.client = client
                self.provider = client.provider
                if self.provider == "chatgpt":
                    self.chatgpt_model = client.model
                self.update_provider_ui()
                self.save_settings()
                self.ready = True
                self.needs_login = False
                self.reset()
                self.set_notice()
                self.close_setup()
                self.show()
        if self.windows:
            self.windows.poll()
            if (not self.pinned_device and self.root.state() != "withdrawn" and time.monotonic() >= self.focus_grace
                    and not self.windows.foreground()):
                self.hide()
        self.input_composing()
        if self.client:
            connected = self.client.connected
            if self.ready != connected:
                self.ready = connected
                record("desktop.web_available" if connected else "desktop.web_unavailable", self.active)
                if connected and not self.needs_login:
                    self.set_notice()
                elif not connected and not self.active and not self.needs_login:
                    self.show_setup()
            while True:
                try:
                    self.accept(self.client.events.get_nowait())
                except Empty:
                    break
            now = time.monotonic()
            if self.active and not self.stop_requested and now >= self.deadline:
                self.record_request("reply_timeout", started=self.started, received_delta=self.first_delta)
                self.cancel_request()
                self.fail(f"等待 {self.provider_name} 回复超时，已保留收到的内容。")
            elif (self.active and not self.stop_requested and not (self.started or self.first_delta)
                  and now - self.request_started >= (IMAGE_START_TIMEOUT if self.pending_image else REQUEST_START_TIMEOUT)):
                self.record_request("start_timeout")
                self.cancel_request()
                self.fail("图片上传或发送超时，请稍后重试。" if self.pending_image else f"连接 {self.provider_name} 超时，请稍后重试。")
        self.root.after(75, self.tick)


def main():
    enable_high_dpi()
    demo = "--demo" in sys.argv
    smoke = "--smoke" in sys.argv
    background = "--background" in sys.argv
    mutex = None
    if not (demo or smoke):
        mutex, created = single_instance()
        if not created:
            if not background:
                root = tk.Tk()
                root.withdraw()
                messagebox.showinfo("Desktop AI", "Desktop AI 已在托盘运行。")
                root.destroy()
            if mutex:
                application.CloseHandle(mutex)
            return
        start_logging()
        record("desktop.started", background=background)
    try:
        root = tk.Tk()
        root.withdraw()
        popup = Popup(root, demo=demo or smoke, smoke=smoke)
        if smoke:
            popup.exit()
            return
        if not background:
            popup.show()
        root.mainloop()
    finally:
        if mutex:
            application.CloseHandle(mutex)
