import tkinter as tk

from chatgpt_web_account import saved_accounts
from ui_controls import ActionButton, ChoiceField, Segmented, ToggleRow
from web_account import edge_profiles


class SetupView(tk.Frame):
    """A single settings surface, with login details shown only when needed."""
    def __init__(self, parent, popup):
        super().__init__(parent, bg=popup.colors['window'])
        self.popup = popup
        self.profiles, self.accounts, self.models = [], [], []
        self.busy, self.login_visible = False, False
        self.preferred_profile = None
        self.source = tk.StringVar(self, 'DeepSeek')
        self.selected, self.model_name = [tk.StringVar(self) for _ in range(2)]
        self.controls = []
        c = popup.colors
        scroller = tk.Frame(self, bg=c['window'])
        scroller.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(scroller, bg=c['window'], bd=0, highlightthickness=0)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.scrollbar = tk.Canvas(scroller, bg=c['window'], width=3, bd=0, highlightthickness=0)
        self.canvas.configure(yscrollcommand=self.scroll_status)
        self.scrollbar.bind('<Configure>', lambda event: self.scroll_status(*self.canvas.yview()))
        self.scrollbar.bind('<Button-1>', lambda event: self.canvas.yview_moveto(event.y/max(1, self.scrollbar.winfo_height())))
        self.content = tk.Frame(self.canvas, bg=c['window'])
        self.content_window = self.canvas.create_window(0, 0, window=self.content, anchor='n')
        self.canvas.bind('<Configure>', self.fit_content)
        self.content.bind('<Configure>', self.fit_content)

        self.heading = popup.label(self.content, '设置', size=15)
        self.heading.pack(anchor='w')
        self.description = popup.label(self.content, '模型、账号与偏好', size=9, color='muted', anchor='w', justify='left')
        self.description.pack(fill='x')
        popup.scale_options(self.description.pack_configure, pady=(4, 16))
        self.source_picker = Segmented(self.content, self.source, ('ChatGPT', 'DeepSeek'), c, popup.scale)
        self.source_picker.pack(fill='x')
        self.source_picker.bind('<<ComboboxSelected>>', self.source_changed)
        self.controls.append(self.source_picker)

        self.model_row = tk.Frame(self.content, bg=c['window'])
        self.caption(self.model_row, '模型')
        self.model_picker = ChoiceField(self.model_row, self.model_name, c, popup.scale)
        self.model_picker.pack(fill='x')
        self.controls.append(self.model_picker)

        account_heading = tk.Frame(self.content, bg=c['window'])
        account_heading.pack(fill='x')
        popup.scale_options(account_heading.pack_configure, pady=(14, 5))
        self.account_label = popup.label(account_heading, '账号', size=9, color='muted')
        self.account_label.pack(side='left')
        self.manage = popup.button(account_heading, '管理登录', self.manage_login, 'window', 'accent', size=9)
        self.manage.pack(side='right')
        self.picker = ChoiceField(self.content, self.selected, c, popup.scale)
        self.picker.pack(fill='x')
        self.picker.bind('<<ComboboxSelected>>', self.account_changed)
        self.controls.append(self.picker)

        self.login_row = tk.Frame(self.content, bg=c['window'])
        self.login_help = popup.label(self.login_row, size=9, color='muted', anchor='w', justify='left')
        self.login_help.pack(fill='x')
        popup.scale_options(self.login_help.pack_configure, pady=(0, 8))
        self.login = ActionButton(self.login_row, '登录 DeepSeek', popup.open_login, c, popup.scale, width=130)
        self.login.pack(anchor='w')
        self.controls.append(self.login)

        preferences = tk.Frame(self.content, bg=c['window'])
        preferences.pack(fill='x')
        popup.scale_options(preferences.pack_configure, pady=(14, 0))
        preferences.columnconfigure((0, 1), weight=1, uniform='preference')
        self.search = ToggleRow(preferences, '智能搜索', '需要时检索网页', popup.search_on, popup.save_settings, c, popup.scale)
        self.thinking = ToggleRow(preferences, '深度思考', '用于复杂问题', popup.thinking_on, popup.save_settings, c, popup.scale)
        self.search.grid(row=0, column=0, sticky='ew')
        self.thinking.grid(row=0, column=1, sticky='ew')
        popup.scale_options(self.thinking.grid_configure, padx=(14, 0))
        self.controls.extend((self.search, self.thinking))

        self.links = tk.Frame(self.content, bg=c['window'])
        self.new_chat = popup.button(self.links, '开始新会话', popup.new_chatgpt_session, 'window', size=9)
        self.new_chat.pack(side='left')
        self.usage = popup.button(self.links, '查看用量', popup.open_chatgpt_usage, 'window', size=9)
        self.usage.pack(side='right')
        self.status = popup.label(self.content, size=9, color='muted', anchor='w', justify='left')
        self.status.pack(fill='x')
        popup.scale_options(self.status.pack_configure, pady=(8, 1))

        self.actions = tk.Frame(self, bg=c['window'], height=popup.dp(42))
        # Reserve the actions before the scrollable body on shorter screens.
        self.actions.pack(fill='x', side='bottom', before=scroller)
        popup.scale_options(self.actions.pack_configure, pady=(12, 0))
        self.back = ActionButton(self.actions, '返回对话', popup.cancel_setup_login, c, popup.scale, width=96)
        self.back.pack(side='left')
        self.connect = ActionButton(self.actions, '应用并返回', popup.import_login, c, popup.scale, primary=True, width=142)
        self.connect.pack(side='right')
        self.controls.extend((self.back, self.connect))
        self.set_scale(popup.scale)

    def caption(self, parent, text):
        label = self.popup.label(parent, text, size=9, color='muted')
        label.pack(anchor='w')
        self.popup.scale_options(label.pack_configure, pady=(14, 5))
        return label

    def fit_content(self, event=None):
        s = self.popup.scale
        width = max(1, self.canvas.winfo_width())
        target = min(width, self.popup.dp(540))
        self.canvas.coords(self.content_window, width/2, 0)
        if self.content.winfo_width() != target:
            self.canvas.itemconfigure(self.content_window, width=target)
        height = self.content.winfo_reqheight()
        self.canvas.configure(scrollregion=(0, 0, width, height), height=min(self.popup.dp(520), max(self.popup.dp(270), height)))
        for label in (self.description, self.login_help, self.status):
            label.configure(wraplength=max(1, target))

    def set_scale(self, scale):
        self.configure(padx=round(22*scale), pady=round(18*scale))
        for control in getattr(self, 'controls', []):
            control.set_scale(scale)
        if hasattr(self, 'canvas'):
            self.scrollbar.configure(width=max(3, round(3*scale)))
            self.fit_content()

    def scroll_status(self, first, last):
        first, last = float(first), float(last)
        if first > 0 or last < .999:
            if not self.scrollbar.winfo_manager():
                self.scrollbar.pack(side='right', fill='y', padx=(self.popup.dp(5), 0))
            self.scrollbar.delete('all')
            width, height = self.scrollbar.winfo_width(), self.scrollbar.winfo_height()
            self.scrollbar.create_rectangle(0, first*height, width, max(first*height+12, last*height), fill=self.popup.colors['line'], outline='')
        else:
            self.scrollbar.pack_forget()

    def scroll(self, event):
        if self.content.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(-2 if event.delta > 0 else 2, 'units')
        return 'break'

    def provider(self):
        return 'chatgpt' if self.source.get() == 'ChatGPT' else 'deepseek'

    def source_changed(self, event=None):
        self.close_choices()
        self.status.configure(text='', fg=self.popup.colors['muted'])
        self.model_row.pack_forget()
        self.links.pack_forget()
        self.login_row.pack_forget()
        self.login_visible = False
        if self.provider() == 'deepseek':
            self.account_label.configure(text='Edge 用户配置')
            self.login.configure(text='登录 DeepSeek', command=self.popup.open_login)
            self.login_help.configure(text='完成 DeepSeek 网页登录后，点击下方「导入并连接」。')
            self.connect.configure(text='应用并返回', command=self.popup.import_login)
            self.profiles = []
            self.refresh_profiles()
            if not self.popup.client or self.popup.provider != 'deepseek':
                self.expand_login(True)
                self.connect.configure(text='导入并连接')
        else:
            self.account_label.configure(text='ChatGPT 网页账号')
            self.login.configure(text='读取网页会话', command=self.popup.start_chatgpt_login)
            self.login_help.configure(text='在 Chrome 中复制网页会话的完整 JSON，再回到这里导入并连接。')
            try:
                self.accounts = saved_accounts()
            except (OSError, ValueError):
                self.accounts = []
                self.status.configure(text='本机登录数据无法读取，可重新导入。')
            self.picker.configure(values=[account.get('email', '网页登录') for account in self.accounts]+['导入其他账号…'],
                                  descriptions=['已保存在本机' for _ in self.accounts]+['使用 Chrome 中的网页登录'])
            preferred = self.popup.client.account.get('client_id') if self.popup.client and self.popup.provider == 'chatgpt' else None
            self.picker.current(next((index for index, account in enumerate(self.accounts) if account['client_id'] == preferred), 0))
            self.account_changed()
        self.fit_content()
        self.after_idle(self.popup.fit_setup)

    def refresh_profiles(self):
        if self.busy or self.provider() != 'deepseek':
            return
        current = self.profile() if self.profiles else self.preferred_profile
        self.profiles = edge_profiles()
        self.picker.configure(values=[name for _, name in self.profiles], descriptions=[directory for directory, _ in self.profiles])
        self.picker.current(next((index for index, (directory, _) in enumerate(self.profiles) if directory == current), 0))

    def profile(self):
        index = self.picker.current()
        return self.profiles[index][0] if 0 <= index < len(self.profiles) else 'Default'

    def account(self):
        index = self.picker.current()
        return self.accounts[index] if 0 <= index < len(self.accounts) else None

    def account_changed(self, event=None):
        if self.provider() != 'chatgpt':
            return
        account = self.account()
        self.model_row.pack_forget()
        self.models = account.get('models', []) if account else []
        self.connect.configure(text='应用并返回' if account else '导入并连接',
                               command=self.popup.use_chatgpt_account if account else self.popup.import_chatgpt_login)
        self.expand_login(account is None)
        if self.models:
            descriptions = []
            for item in self.models:
                name = (item.get('display_name') or item['slug']).lower()
                descriptions.append('按问题自动选择' if item['slug'] == 'auto' else
                                    '适合复杂问题与推理' if 'thinking' in name or '-t-' in item['slug'] else '适合日常提问与快速回答')
            self.model_picker.configure(values=[item.get('display_name') or item['slug'] for item in self.models], descriptions=descriptions)
            active = self.popup.client.model if self.popup.provider == 'chatgpt' and self.popup.client and self.popup.client.client_id == account['client_id'] else None
            selected = active or account.get('model')
            self.model_picker.current(next((index for index, item in enumerate(self.models) if item['slug'] == selected), 0))
            self.model_row.pack(fill='x', after=self.source_picker)
        self.links.pack_forget()
        if account and self.popup.provider == 'chatgpt' and self.popup.client and self.popup.client.client_id == account['client_id']:
            self.links.pack(fill='x', before=self.status, pady=(self.popup.dp(12), 0))
        self.after_idle(self.popup.fit_setup)

    def model(self):
        index = self.model_picker.current()
        return self.models[index]['slug'] if 0 <= index < len(self.models) else None

    def expand_login(self, show):
        self.login_visible = show
        self.login_row.pack_forget()
        if show:
            self.login_row.pack(fill='x', after=self.picker, pady=(self.popup.dp(9), 0))
        self.fit_content()

    def manage_login(self):
        if self.busy:
            return
        if self.provider() == 'chatgpt':
            self.picker.current(len(self.accounts))
            self.account_changed()
        else:
            self.expand_login(not self.login_visible)
            self.connect.configure(text='导入并连接' if self.login_visible else '应用并返回')
        self.after_idle(self.popup.fit_setup)

    def close_choices(self):
        for widget in (self.picker, self.model_picker):
            widget.close()

    def show(self, preferred=None, can_return=False, message='', provider='deepseek'):
        if not self.busy:
            self.preferred_profile = preferred
            self.source.set('ChatGPT' if provider == 'chatgpt' else 'DeepSeek')
            self.source_changed()
            self.status.configure(text=message)
            self.canvas.yview_moveto(0)
        self.can_return = can_return
        self.back.configure(text='返回对话' if can_return else '收起')
        self.grid(row=0, column=0, columnspan=2, sticky='nsew')
        self.tk.call('raise', str(self))

    def set_busy(self, busy, text=None):
        self.busy = busy
        state = 'disabled' if busy else 'normal'
        for control in (self.login, self.connect, self.source_picker, self.picker, self.model_picker):
            control.configure(state=state)
        self.manage.configure(state=state)
        self.back.configure(text='取消连接' if busy else '返回对话' if getattr(self, 'can_return', False) else '收起')
        if busy:
            self.status.configure(text=text or '正在连接账号…', fg=self.popup.colors['muted'])

    def show_error(self, text):
        self.status.configure(text=text, fg=self.popup.colors['accent'])
        self.fit_content()
        self.canvas.yview_moveto(1)
