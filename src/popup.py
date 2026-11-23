from base64 import b64encode
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
import time
import threading
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk
import uuid
import winreg

from web_account import import_edge_login
from web_client import WebClient, WebError
from setup_view import SetupView
from answer_view import AnswerView
from clipboard_image import ClipboardImageError, read_clipboard_image
from controls import FONT_FAMILY, IconButton, SettingsPanel, WindowResize, font_pixels, rounded
from diagnostics import record, record_callback_error, start_logging
from windows import (
    APP_ASSETS, InputMethod, Integration, application, enable_high_dpi, monitor_work_area,
    open_edge, single_instance, window_dpi,
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
        self.search_on = tk.BooleanVar(root, settings.get("search", True) is not False)
        self.thinking_on = tk.BooleanVar(root, settings.get("thinking", False) is True)
        self.client = self.windows = None
        self.setup_results = Queue()
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
        root.update_idletasks()
        self.ime = InputMethod(root, self.input, INPUT_FONT_SIZE, FONT_FAMILY)
        if not demo:
            root.report_callback_exception = lambda kind, _value, stack: record_callback_error(kind, stack, self.active)
            try:
                self.client = WebClient()
                self.ready = True
            except FileNotFoundError:
                self.show_setup()
            except (OSError, ValueError) as error:
                record("web.start_failed", error=type(error).__name__)
                self.show_setup("本机登录数据无法读取，请重新导入。")
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
        for widget in (self.settings_button, self.settings, self.send_button, self.stop_button,
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
        if self.settings.winfo_manager():
            self.settings.place_configure(x=-self.dp(12), y=self.dp(44))
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
        settings = {"search": self.search_on.get(), "thinking": self.thinking_on.get()}
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
        brand.pack(side="left")
        self.scale_options(brand.pack_configure, padx=7)
        self.settings_button = IconButton(title, "settings", self.toggle_settings, c, self.scale, "chrome")
        self.settings_button.pack(side="right")
        for widget in (title, mark, brand):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.end_drag)
        divider = tk.Frame(frame, bg=c["line"])
        divider.pack(fill="x")
        self.scale_options(divider.configure, height=1)

        self.settings = SettingsPanel(frame, c, self.scale,
                                      (("智能搜索", self.search_on), ("深度思考", self.thinking_on)),
                                      self.save_settings)
        r.bind("<ButtonPress-1>", self.dismiss_settings, add="+")
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

        self.menu = tk.Menu(r, tearoff=False)
        self.menu.add_command(label="打开弹窗", command=self.show)
        self.menu.add_command(label="打开当前会话", command=self.open_login)
        self.menu.add_command(label="连接或切换账号", command=self.refresh_login)
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
        self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event):
        x, y = self.drag_offset
        self.root.geometry(f"+{event.x_root - x}+{event.y_root - y}")

    def end_drag(self, event):
        self.place_on_pointer_monitor(keep_position=True)

    def place_on_pointer_monitor(self, keep_position=False):
        root = self.root
        area = monitor_work_area(*root.winfo_pointerxy())
        if area is None:
            return
        work_left, work_top, work_right, work_bottom = area
        root.update_idletasks()
        left, top = root.winfo_rootx(), root.winfo_rooty()
        width, height = root.winfo_width(), root.winfo_height()
        same_monitor = (self.positioned and work_left <= left + width // 2 < work_right
                        and work_top <= top + height // 2 < work_bottom)
        work_width, work_height = work_right - work_left, work_bottom - work_top
        if not same_monitor:
            # Windows updates the HWND's DPI as it moves, including while withdrawn.
            probe_width, probe_height = min(width, work_width), min(height, work_height)
            x = work_left + (work_width - probe_width) // 2
            y = work_top + (work_height - probe_height) // 2
            root.geometry(f"{probe_width}x{probe_height}+{x}+{y}")
            root.update_idletasks()
        scale = window_dpi(root.winfo_id()) / 96
        width = min(round(width * scale / self.scale), work_width)
        height = min(round(height * scale / self.scale), work_height)
        self.set_scale(scale)
        if same_monitor or keep_position:
            left = min(max(left, work_left), work_right - width)
            top = min(max(top, work_top), work_bottom - height)
        else:
            left = work_left + (work_width - width) // 2
            top = work_top + (work_height - height) // 2
        # Tk's '+' position marker keeps a negative screen coordinate absolute.
        root.geometry(f"{width}x{height}+{left}+{top}")
        self.positioned = True

    def show(self):
        self.place_on_pointer_monitor()
        self.focus_grace = time.monotonic() + .3
        self.root.deiconify()
        self.root.lift()
        target = self.setup_panel.picker if self.setup_panel.winfo_manager() else self.input
        self.root.after_idle(target.focus_force)

    def hide(self, event=None):
        self.resize.cancel()
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
        return self.hide()

    def close_settings(self):
        self.settings.place_forget()
        self.settings_button.set_flag("selected", False)

    def toggle_settings(self):
        if self.settings.winfo_manager():
            self.close_settings()
            self.input.focus_set()
        else:
            self.settings.place(relx=1, x=-self.dp(12), y=self.dp(44), anchor="ne")
            self.settings.tk.call("raise", str(self.settings))
            self.settings_button.set_flag("selected", True)
            self.settings.focus_set()

    def dismiss_settings(self, event):
        if self.settings.winfo_manager() and event.widget not in (self.settings, self.settings_button):
            self.close_settings()

    def show_menu(self):
        self.menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())

    def exit(self):
        if not self.demo:
            record("desktop.exit", self.active)
        if self.active and self.client:
            self.cancel_request()
        if self.windows:
            self.windows.close()
        if self.client:
            self.client.close()
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
        self.close_settings()
        self.set_notice()
        preferred = self.client.account.get("edge_profile", "Default") if self.client else None
        self.setup_panel.show(preferred, can_return=self.client is not None, message=message)
        self.footer.pack_forget()

    def refresh_login(self):
        if self.active:
            self.set_notice("请先结束当前请求，再连接其他账号。")
            return
        self.show_setup()

    def close_setup(self):
        self.setup_panel.grid_remove()
        self.footer.pack(fill="x", side="bottom", before=self.center)
        self.root.after_idle(self.input.focus_set)

    def import_login(self):
        if self.setup_panel.busy or self.active:
            return
        profile = self.setup_panel.profile()
        self.setup_panel.set_busy(True)
        threading.Thread(target=self.read_login, args=(profile,), daemon=True).start()

    def read_login(self, profile):
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
            self.setup_results.put((None, text))
        except Exception as error:
            record("web.account_import_failed", error=type(error).__name__)
            self.setup_results.put((None, "读取登录时发生错误，请重新登录后再试。"))
        else:
            self.setup_results.put((client, ""))

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
        url = "https://chat.deepseek.com/"
        if self.setup_panel.winfo_manager():
            profile = self.setup_panel.profile()
        else:
            profile = self.client.account.get("edge_profile") if self.client else None
        if self.client and not self.setup_panel.winfo_manager():
            url += "a/chat/s/" + self.client.account["session_id"]
        self.open_page(url, profile)

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
        text = self.answer_view.selected_text() or self.answer
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
        self.empty.configure(text="等待 DeepSeek 回复…" if self.demo else "正在连接 DeepSeek…")
        self.send_button.grid_remove()
        self.stop_button.set_enabled(True)
        self.stop_button.grid()
        self.canvas.yview_moveto(0)
        if self.demo:
            self.run_demo(self.active)
        else:
            search = self.search_on.get() and image is None
            record("desktop.queued", self.active, search=search, thinking=self.thinking_on.get(), image=image is not None)
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
        self.set_notice("已停止等待，服务端停止状态尚未确认。", (("查看当前会话", self.open_login),))

    def delta(self, answer, thought, search):
        self.answer, self.thought = answer, thought
        if answer:
            self.answer_view.pack(fill="x", pady=(self.dp(5), 0))
            self.answer_view.set_source(answer)
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
        self.set_notice(description, (("去 Edge 登录", self.open_login), ("重新读取登录", self.refresh_login)) if login else ())

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
                self.empty.configure(text="等待 DeepSeek 回复…")
        elif kind == "delta":
            self.started = True
            answer = str(event.get("answer", ""))
            if not self.first_delta:
                self.first_delta = True
                self.record_request("first_delta", has_answer=bool(answer))
            if answer and not self.answer:
                self.record_request("first_answer")
            self.delta(answer, str(event.get("thought", "")), event.get("search", ""))
        elif kind == "complete":
            answer = event.get("answer")
            if isinstance(answer, str) and answer:
                self.delta(answer, str(event.get("thought", "")), event.get("search", ""))
            self.record_request("complete", has_answer=bool(self.answer))
            self.finish("●  已完成")
        elif kind == "stopped":
            if event.get("confirmed") is False:
                self.finish_unconfirmed_stop("stop_unconfirmed")
            else:
                self.record_request("stopped")
                self.finish("已停止")
        elif kind in ("login", "error"):
            self.record_request(kind, started=self.started)
            self.fail(str(event.get("text") or "网页请求失败，请在 Edge 查看。"), login=kind == "login")

    def tick(self):
        while True:
            try:
                client, error = self.setup_results.get_nowait()
            except Empty:
                break
            self.setup_panel.set_busy(False)
            if error:
                self.setup_panel.show_error(error)
            else:
                self.client = client
                self.ready = True
                self.needs_login = False
                self.set_notice()
                self.close_setup()
        if self.windows:
            self.windows.poll()
            if (self.root.state() != "withdrawn" and time.monotonic() >= self.focus_grace
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
                self.fail("等待 DeepSeek 回复超时，已保留收到的内容。")
            elif (self.active and not self.stop_requested and not (self.started or self.first_delta)
                  and now - self.request_started >= (IMAGE_START_TIMEOUT if self.pending_image else REQUEST_START_TIMEOUT)):
                self.record_request("start_timeout")
                self.cancel_request()
                self.fail("图片上传或发送超时，请稍后重试。" if self.pending_image else "连接 DeepSeek 超时，请稍后重试。")
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
