from datetime import date
import json
import os
from pathlib import Path
from queue import Empty
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk
import uuid
import winreg

from bridge import Bridge, ORIGIN
from answer_view import AnswerView
from controls import IconButton, SettingsPanel, WindowResize, rounded
from diagnostics import record, record_callback_error, start_logging
from windows import APP_ASSETS, InputMethod, Integration, application, enable_high_dpi, open_edge, single_instance


LIGHT = dict(window="#FFFFFF", chrome="#F8F8FC", ink="#1B1E2C", muted="#606477",
             line="#E3E5EF", soft="#F1F1F8", accent="#4F46BD", tint="#EEECFE",
             action="#5147C0", action_ink="#FFFFFF", input="#F4F4FA", input_border="#A9A0D7")
DARK = dict(window="#181A24", chrome="#1C1E2A", ink="#F1F0F8", muted="#A9ACC0",
            line="#343649", soft="#252736", accent="#B9AEFF", tint="#322953",
            action="#AD9EFF", action_ink="#211842", input="#232635", input_border="#796DB3")
PROMPT_LIMIT = 18000
INPUT_FONT_SIZE = 11
REQUEST_START_TIMEOUT = 50
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
        self.scale = root.winfo_fpixels("1i") / 96
        self.colors = palette()
        self.settings_file = None if demo else Path(os.environ["LOCALAPPDATA"]) / "DesktopAI" / "settings.json"
        settings = self.load_settings()
        self.search_on = tk.BooleanVar(root, settings.get("search", True) is not False)
        self.thinking_on = tk.BooleanVar(root, settings.get("thinking", False) is True)
        self.bridge = self.windows = None
        self.ime = None
        self.active = None
        self.pending_prompt = ""
        self.answer = ""
        self.thought = ""
        self.started = self.needs_login = self.thought_open = False
        self.ready = self.demo
        self.request_started = 0
        self.first_delta = False
        self.stop_requested = False
        self.deadline = 0
        self.focus_grace = 0
        self.drag_offset = (0, 0)
        self.build()
        self.resize = WindowResize(root, self.scale)
        root.update_idletasks()
        self.ime = InputMethod(root, self.input, INPUT_FONT_SIZE, self.font(INPUT_FONT_SIZE)[0])
        if not demo:
            root.report_callback_exception = lambda kind, _value, stack: record_callback_error(kind, stack, self.active)
            try:
                self.bridge = Bridge()
            except OSError as error:
                record("bridge.start_failed", error=type(error).__name__, errno=error.errno)
                self.set_notice("无法启动本地连接。请检查端口 41692 是否被占用。")
            else:
                self.show_setup()
        if not smoke:
            self.windows = Integration(root, self.show, self.show_menu)
            if not self.windows.hotkey:
                record("desktop.hotkey_unavailable")
                self.set_notice("快捷键已被占用，可以双击系统托盘图标打开。")
            self.root.after(75, self.tick)

    def font(self, size, weight="normal"):
        return ("Microsoft YaHei UI", size, weight)

    def dp(self, value):
        return round(value * self.scale)

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
        frame.pack(fill="both", expand=True, padx=self.dp(1), pady=self.dp(1))
        title = tk.Frame(frame, bg=c["chrome"], padx=self.dp(12), pady=self.dp(5))
        title.pack(fill="x")
        try:
            avatar = tk.PhotoImage(master=r, file=str(APP_ASSETS / "desktop-ai.png"))
        except tk.TclError:
            mark = self.label(title, "✦", 14, "accent", "tint", width=3)
        else:
            r.iconphoto(True, avatar)
            sample = max(1, round(avatar.width() / self.dp(24)))
            self.header_icon = avatar.subsample(sample)
            mark = self.label(title, image=self.header_icon, bg="chrome")
        mark.pack(side="left")
        brand = self.label(title, "DeepSeek", 11, bg="chrome")
        brand.pack(side="left", padx=self.dp(7))
        self.settings_button = IconButton(title, "settings", self.toggle_settings, c, self.scale, "chrome")
        self.settings_button.pack(side="right")
        for widget in (title, mark, brand):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)
        tk.Frame(frame, bg=c["line"], height=self.dp(1)).pack(fill="x")

        self.settings = SettingsPanel(frame, c, self.scale,
                                      (("智能搜索", self.search_on), ("深度思考", self.thinking_on)),
                                      self.save_settings)
        r.bind("<ButtonPress-1>", self.dismiss_settings, add="+")
        center = tk.Frame(frame, bg=c["window"])
        center.pack(fill="both", expand=True)
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(center, bg=c["window"], highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        style = ttk.Style(r)
        style.theme_use("clam")
        style.configure("Quiet.Vertical.TScrollbar", background=c["soft"], troughcolor=c["window"],
                        bordercolor=c["window"], lightcolor=c["soft"], darkcolor=c["soft"],
                        arrowcolor=c["muted"], arrowsize=self.dp(9))
        self.scrollbar = ttk.Scrollbar(center, command=self.canvas.yview, style="Quiet.Vertical.TScrollbar")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.scrollbar.grid_remove()
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=c["window"], padx=self.dp(15), pady=self.dp(11))
        self.body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self.resize_body)
        self.body.bind("<Configure>", self.fit_scroll)
        r.bind_all("<MouseWheel>", self.scroll_answer, add="+")

        self.question_row = tk.Frame(self.body, bg=c["window"])
        self.question = self.label(self.question_row, size=10, bg="tint", justify="left",
                                   anchor="w", wraplength=self.dp(400), padx=self.dp(9), pady=self.dp(5))
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

        self.reason_text = self.label(self.body, size=10, color="muted", bg="soft", justify="left",
                                      wraplength=self.dp(449), anchor="w", padx=self.dp(9), pady=self.dp(5))
        self.answer_view = AnswerView(self.body, c, self.scale)
        self.answer_view.bind("<MouseWheel>", self.scroll_answer_from_text, add="+")
        self.empty = self.label(self.body, "回答会显示在这里", 10, "muted")
        self.empty.pack(pady=(self.dp(34), self.dp(6)))

        self.notice = tk.Frame(frame, bg=c["soft"], padx=self.dp(12), pady=self.dp(6))
        self.notice_label = self.label(self.notice, size=9, bg="soft", justify="left",
                                       anchor="w", wraplength=self.dp(460))
        self.notice_label.pack(anchor="w")
        self.notice_actions = tk.Frame(self.notice, bg=c["soft"])
        self.notice_actions.pack(anchor="w")

        self.footer = tk.Frame(frame, bg=c["chrome"])
        self.footer.pack(fill="x", side="bottom", before=center)
        tk.Frame(self.footer, bg=c["line"], height=self.dp(1)).pack(fill="x")
        composer = tk.Frame(self.footer, bg=c["chrome"], padx=self.dp(8), pady=self.dp(5))
        composer.pack(fill="x", padx=self.dp(11), pady=self.dp(8))
        composer.grid_columnconfigure(0, weight=1)
        self.input_shell = tk.Canvas(composer, background=c["chrome"], borderwidth=0, highlightthickness=0)
        self.input_shell.place(relwidth=1, relheight=1, bordermode="outside")
        self.input_shell.bind("<Configure>", self.paint_input_shell)
        self.input = tk.Text(composer, height=1, width=1, wrap="word", bg=c["input"], fg=c["ink"],
                             insertbackground=c["accent"], font=self.font(INPUT_FONT_SIZE),
                             undo=True, relief="flat", bd=0, padx=self.dp(3), pady=self.dp(3))
        self.input.grid(row=0, column=0, sticky="ew", padx=(0, self.dp(5)))
        self.placeholder = self.label(self.input, "输入问题…", INPUT_FONT_SIZE, "muted", "input", anchor="w")
        self.placeholder.bind("<Button-1>", lambda event: self.input.focus_set())
        self.input.bind("<<Modified>>", self.update_input)
        self.input.bind("<Return>", self.on_enter)
        self.input.bind("<Shift-Return>", lambda event: None)
        self.input.bind("<FocusIn>", self.input_focused)
        self.input.bind("<FocusOut>", lambda event: r.after_idle(self.paint_input_shell))
        self.send_button = IconButton(composer, "send", self.submit, c, self.scale, "input")
        self.send_button.grid(row=0, column=1, sticky="s")
        self.stop_button = IconButton(composer, "stop", self.stop, c, self.scale, "input")
        self.stop_button.grid(row=0, column=1, sticky="s")
        self.stop_button.grid_remove()
        self.update_input()

        self.menu = tk.Menu(r, tearoff=False)
        self.menu.add_command(label="打开弹窗", command=self.show)
        self.menu.add_command(label="打开 Edge 扩展设置", command=self.open_setup)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.exit)

    def resize_body(self, event):
        self.canvas.itemconfigure(self.body_window, width=event.width)
        wrap = max(self.dp(210), event.width - self.dp(34))
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
        self.root.geometry(f"{event.x_root - x:+d}{event.y_root - y:+d}")

    def show(self):
        self.focus_grace = time.monotonic() + .3
        self.root.deiconify()
        self.root.lift()
        self.root.after_idle(self.input.focus_force)

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
        if self.active and self.bridge:
            self.cancel_browser_request()
        if self.windows:
            self.windows.close()
        if self.bridge:
            self.bridge.close()
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

    def show_setup(self):
        self.set_notice("等待 Edge 连接。首次使用请加载配套扩展，再粘贴连接码。",
                        (("打开扩展设置", self.open_setup), ("复制连接码", self.copy_code),
                         ("打开连接页", self.open_pair)))

    def copy_code(self):
        if self.bridge:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.bridge.token)
            self.open_pair()

    def open_setup(self):
        self.open_page("edge://extensions/")

    def open_pair(self):
        self.open_page(ORIGIN + "/pair.html")

    def open_page(self, url):
        try:
            open_edge(url)
            self.root.withdraw()
        except OSError:
            self.set_notice("无法启动 Microsoft Edge。请手动打开浏览器。")

    def open_login(self):
        self.root.withdraw()
        if self.bridge and self.bridge.connected:
            self.bridge.jobs.put({"type": "open"})
        else:
            self.open_page("https://chat.deepseek.com/")

    def read_input(self):
        return self.input.get("1.0", "end-1c")

    def update_input(self, event=None):
        self.input.edit_modified(False)
        value = self.read_input()
        self.input.configure(height=min(3, 1 + value.count("\n")))
        if value or (self.ime and self.ime.composing):
            self.placeholder.place_forget()
            self.send_button.set_enabled(bool(value.strip()))
        else:
            self.placeholder.configure(text="输入下一个问题…" if self.question_row.winfo_manager()
                                       else "输入问题…")
            self.placeholder.place(x=self.dp(3), y=self.dp(3))
            self.send_button.set_enabled(False)

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
        if self.active or not text:
            return
        if len(text) > PROMPT_LIMIT:
            self.set_notice(f"单次最多可发送 {PROMPT_LIMIT} 个字符，请缩短后重试。")
            return
        if not self.ready:
            record("desktop.submit_blocked", reason="bridge_unavailable")
            if self.bridge:
                self.show_setup()
            return
        self.active = uuid.uuid4().hex
        self.pending_prompt = text
        self.started = self.needs_login = self.first_delta = False
        self.request_started = time.monotonic()
        self.deadline = self.request_started + 325
        self.reset()
        self.set_notice()
        self.question.configure(text=text)
        self.question_row.pack(fill="x", pady=(0, self.dp(8)), before=self.answer_bar)
        self.replace_input("")
        self.status.configure(text="●  已发送")
        self.empty.configure(text="等待 DeepSeek 回复…")
        self.send_button.grid_remove()
        self.stop_button.set_enabled(True)
        self.stop_button.grid()
        self.canvas.yview_moveto(0)
        if self.demo:
            self.run_demo(self.active)
        else:
            record("desktop.queued", self.active, search=self.search_on.get(), thinking=self.thinking_on.get())
            self.bridge.active_request = self.active
            self.bridge.jobs.put(dict(type="ask", id=self.active, day=date.today().isoformat(),
                                      prompt=text, search=self.search_on.get(), thinking=self.thinking_on.get()))

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
            self.cancel_browser_request()
            self.root.after(STOP_TIMEOUT_MS, self.stop_timed_out, self.active)

    def cancel_browser_request(self):
        self.bridge.active_request = None
        self.bridge.jobs.put({"type": "stop", "id": self.active})

    def stop_timed_out(self, request):
        if self.active == request and self.stop_requested:
            self.finish_unconfirmed_stop("stop_timeout")

    def finish_unconfirmed_stop(self, stage):
        self.record_request(stage)
        self.finish("已停止等待")
        self.set_notice("未收到网页的停止确认。可以打开 Edge 查看生成状态。", (("打开 Edge 查看", self.open_login),))

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
        if self.bridge:
            self.bridge.active_request = None
        self.active = None
        self.stop_requested = False
        self.pending_prompt = ""
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
        restore = login or not self.started
        self.needs_login = login
        self.finish("需要登录" if login else "请求中断")
        if restore and prompt and not self.read_input().strip():
            self.replace_input(prompt)
        self.set_notice(description, (("去 Edge 登录", self.open_login),) if login else ())

    def record_request(self, stage, **fields):
        elapsed_ms = round((time.monotonic() - self.request_started) * 1000)
        record("desktop." + stage, self.active, elapsed_ms=elapsed_ms, **fields)

    def accept(self, event):
        if event.get("id") != self.active or not self.active:
            return
        kind = event.get("type")
        if kind == "started":
            self.started = True
        elif kind == "delta":
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
        if self.windows:
            self.windows.poll()
            if (self.root.state() != "withdrawn" and time.monotonic() >= self.focus_grace
                    and not self.windows.foreground()):
                self.hide()
        self.input_composing()
        if self.bridge:
            connected = self.bridge.connected
            if self.ready != connected:
                self.ready = connected
                record("desktop.edge_connected" if connected else "desktop.edge_disconnected", self.active)
                if connected and not self.needs_login:
                    self.set_notice()
                elif not connected and not self.active and not self.needs_login:
                    self.show_setup()
            while True:
                try:
                    self.accept(self.bridge.events.get_nowait())
                except Empty:
                    break
            now = time.monotonic()
            if self.active and not self.stop_requested and now >= self.deadline:
                self.record_request("reply_timeout", started=self.started, received_delta=self.first_delta)
                self.cancel_browser_request()
                self.fail("等待网页回答超时，请在 Edge 查看。")
            elif self.active and not self.stop_requested and not connected:
                self.record_request("request_lost_connection", started=self.started, received_delta=self.first_delta)
                self.cancel_browser_request()
                self.fail("与 Edge 的连接中断，请确认扩展和浏览器仍在运行。")
            elif (self.active and not self.stop_requested and not (self.started or self.first_delta)
                  and now - self.request_started >= REQUEST_START_TIMEOUT):
                self.record_request("start_timeout")
                self.cancel_browser_request()
                self.fail("未收到 DeepSeek 网页开始回复的信号，请在 Edge 查看页面状态。")
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
        if background:
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
