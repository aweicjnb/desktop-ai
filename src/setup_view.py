import tkinter as tk
from tkinter import ttk

from web_account import edge_profiles


class SetupView(tk.Frame):
    def __init__(self, parent, popup):
        super().__init__(parent, bg=popup.colors["window"])
        self.popup = popup
        self.profiles = []
        self.busy = False
        self.preferred_profile = None
        self.selected = tk.StringVar(self)
        self.heading = popup.label(self, "连接 DeepSeek", size=15)
        self.heading.pack(anchor="w")
        self.description = popup.label(
            self, "选择你的 Edge 用户配置，完成网页登录后，点击「导入登录」。",
            color="muted", size=10, justify="left", anchor="w",
        )
        self.description.pack(fill="x")
        popup.scale_options(self.description.pack_configure, pady=(8, 20))

        popup.label(self, "Edge 用户配置", size=9, color="muted").pack(anchor="w")
        style = ttk.Style(self)
        colors = popup.colors
        style.configure("Connect.TCombobox", fieldbackground=colors["input"], background=colors["soft"],
                        foreground=colors["ink"], arrowcolor=colors["muted"], bordercolor=colors["line"],
                        lightcolor=colors["line"], darkcolor=colors["line"], relief="flat")
        style.map("Connect.TCombobox",
                  fieldbackground=[("readonly", colors["input"]), ("disabled", colors["soft"])],
                  foreground=[("readonly", colors["ink"]), ("disabled", colors["muted"])],
                  selectbackground=[("readonly", colors["input"])],
                  selectforeground=[("readonly", colors["ink"])])
        self.picker = ttk.Combobox(self, textvariable=self.selected, state="readonly",
                                   font=popup.font(10), style="Connect.TCombobox",
                                   postcommand=self.refresh_profiles, width=22)
        self.picker.pack(fill="x")
        popup.scale_options(self.picker.pack_configure, pady=(5, 18))
        popup.scale_options(lambda **options: style.configure("Connect.TCombobox", **options), padding=8)
        self.actions = tk.Frame(self, bg=colors["window"])
        self.actions.pack(fill="x")
        self.login = popup.button(self.actions, "登录 DeepSeek", popup.open_login)
        self.login.pack(side="left")
        popup.scale_options(self.login.configure, padx=12, pady=7)
        self.connect = popup.button(self.actions, "导入登录", popup.import_login, "action", "action_ink")
        self.connect.pack(side="left")
        popup.scale_options(self.connect.configure, padx=14, pady=7)
        popup.scale_options(self.connect.pack_configure, padx=(8, 0))
        self.back = popup.button(self.actions, "返回", popup.close_setup, "window")
        self.status = popup.label(self, size=9, color="muted", anchor="w", justify="left")
        self.status.pack(fill="x")
        popup.scale_options(self.status.pack_configure, pady=(12, 0))
        self.bind("<Configure>", self.wrap_text)
        self.set_scale(popup.scale)

    def set_scale(self, scale):
        self.configure(padx=round(24 * scale), pady=round(24 * scale))
        self.wrap_text()

    def wrap_text(self, event=None):
        width = max(self.popup.dp(190), self.winfo_width() - self.popup.dp(48))
        self.description.configure(wraplength=width)
        self.status.configure(wraplength=width)

    def refresh_profiles(self):
        if self.busy:
            return
        current = self.profile() if self.profiles else self.preferred_profile
        self.profiles = edge_profiles()
        labels = [f"{name} · {directory}" for directory, name in self.profiles]
        self.picker.configure(values=labels)
        index = next((index for index, (directory, _) in enumerate(self.profiles) if directory == current), 0)
        self.picker.current(index)

    def profile(self):
        index = self.picker.current()
        return self.profiles[index][0] if 0 <= index < len(self.profiles) else "Default"

    def show(self, preferred=None, can_return=False, message=""):
        if not self.busy:
            self.preferred_profile = preferred
            self.profiles = []
            self.refresh_profiles()
            self.status.configure(text=message)
        if can_return:
            self.back.pack(side="right")
        else:
            self.back.pack_forget()
        self.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.tk.call("raise", str(self))

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.login, self.connect, self.back):
            button.configure(state=state)
        self.picker.configure(state="disabled" if busy else "readonly")
        if busy:
            self.status.configure(text="正在读取登录并连接 DeepSeek…", fg=self.popup.colors["muted"])

    def show_error(self, text):
        self.status.configure(text=text, fg=self.popup.colors["accent"])
