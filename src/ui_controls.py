"""Small drawn controls with consistent DPI, keyboard and popup behavior."""
import tkinter as tk
from tkinter import font as tkfont

from controls import FONT_FAMILY, font_pixels, rounded
from windows import monitor_at


class ActionButton(tk.Canvas):
    def __init__(self, parent, text, command, colors, scale, primary=False, width=120):
        self.text, self.command, self.colors, self.scale = text, command, colors, scale
        self.primary, self.logical_width = primary, width
        self.enabled, self.hover, self.focused = True, False, False
        super().__init__(parent, bg=colors['window'], highlightthickness=0, bd=0, takefocus=True, cursor='hand2')
        self.set_scale(scale)
        self.bind('<Configure>', self.paint)
        self.bind('<Enter>', lambda e: self.flag('hover', True))
        self.bind('<Leave>', lambda e: self.flag('hover', False))
        self.bind('<FocusIn>', lambda e: self.flag('focused', True))
        self.bind('<FocusOut>', lambda e: self.flag('focused', False))
        self.bind('<ButtonRelease-1>', self.invoke)
        self.bind('<Return>', self.invoke)
        self.bind('<space>', self.invoke)

    def configure(self, cnf=None, **kwargs):
        for key in ('text', 'command'):
            if key in kwargs:
                setattr(self, key, kwargs.pop(key))
        if 'state' in kwargs:
            self.enabled = kwargs.pop('state') != 'disabled'
        result = super().configure(cnf, **kwargs)
        if hasattr(self, '_w'):
            self.paint()
        return result

    config = configure

    def cget(self, key):
        if key == 'text':
            return self.text
        if key == 'state':
            return 'normal' if self.enabled else 'disabled'
        return super().cget(key)

    def set_scale(self, scale):
        self.scale = scale
        self.configure(width=round(self.logical_width * scale), height=round(36 * scale))

    def flag(self, name, value):
        setattr(self, name, value)
        self.paint()

    def invoke(self, event=None):
        if self.enabled:
            self.command()
        return 'break'

    def paint(self, event=None):
        self.delete('all')
        w, h, s, c = self.winfo_width(), self.winfo_height(), self.scale, self.colors
        if min(w, h) < 2:
            return
        fill = c['action'] if self.primary and self.enabled else c['soft']
        ink = c['action_ink'] if self.primary and self.enabled else c['ink'] if self.enabled else c['muted']
        if self.hover and self.enabled and not self.primary:
            fill = c['tint']
        rounded(self, s, s, w-s, h-s, 9*s, fill=fill,
                outline=c['accent'] if self.focused else '', width=max(1, s))
        self.create_text(w/2, h/2, text=self.text, fill=ink, font=(FONT_FAMILY, font_pixels(10, s)))


class Segmented(tk.Canvas):
    def __init__(self, parent, variable, values, colors, scale):
        self.variable, self.values, self.colors, self.scale = variable, list(values), colors, scale
        self.enabled, self.hover = True, None
        super().__init__(parent, bg=colors['window'], highlightthickness=0, bd=0, takefocus=True, cursor='hand2')
        self.trace = variable.trace_add('write', lambda *_: self.paint())
        self.bind('<Configure>', self.paint)
        self.bind('<Motion>', lambda e: self.highlight(self.index_at(e.x)))
        self.bind('<Leave>', lambda e: self.highlight(None))
        self.bind('<ButtonRelease-1>', lambda e: self.select(self.index_at(e.x)))
        self.bind('<Left>', lambda e: self.select((self.current()-1) % len(self.values)))
        self.bind('<Right>', lambda e: self.select((self.current()+1) % len(self.values)))
        self.set_scale(scale)

    def configure(self, cnf=None, **kwargs):
        if 'state' in kwargs:
            self.enabled = kwargs.pop('state') != 'disabled'
        result = super().configure(cnf, **kwargs)
        self.paint()
        return result

    config = configure

    def set_scale(self, scale):
        self.scale = scale
        self.configure(height=round(42 * scale), width=round(300 * scale))

    def current(self, index=None):
        if index is not None:
            self.variable.set(self.values[index])
        return self.values.index(self.variable.get()) if self.variable.get() in self.values else 0

    def index_at(self, x):
        return min(len(self.values)-1, max(0, int(x * len(self.values) / max(1, self.winfo_width()))))

    def highlight(self, index):
        self.hover = index
        self.paint()

    def select(self, index):
        if self.enabled:
            self.current(index)
            self.event_generate('<<ComboboxSelected>>')
        return 'break'

    def paint(self, event=None):
        self.delete('all')
        w, h, s, c = self.winfo_width(), self.winfo_height(), self.scale, self.colors
        if min(w, h) < 2:
            return
        rounded(self, s, s, w-s, h-s, 11*s, fill=c['soft'])
        for index, label in enumerate(self.values):
            left, right = index*w/len(self.values), (index+1)*w/len(self.values)
            selected = index == self.current()
            if selected:
                rounded(self, left+4*s, 4*s, right-4*s, h-4*s, 8*s, fill=c['window'], outline=c['line'], width=s)
            self.create_text((left+right)/2, h/2, text=label, fill=c['accent'] if selected else c['muted'],
                             font=(FONT_FAMILY, font_pixels(10, s), 'bold' if selected else 'normal'))

    def destroy(self):
        self.variable.trace_remove('write', self.trace)
        super().destroy()


class ChoiceField(tk.Canvas):
    def __init__(self, parent, variable, colors, scale, values=(), descriptions=()):
        self.variable, self.colors, self.scale = variable, colors, scale
        self.values, self.descriptions = list(values), list(descriptions)
        self.enabled, self.hover, self.focused = True, False, False
        self.popup = self.list_canvas = None
        self.focus_timer = self.blur_timer = None
        self.row = 0
        super().__init__(parent, bg=colors['window'], highlightthickness=0, bd=0, takefocus=True, cursor='hand2')
        self.trace = variable.trace_add('write', lambda *_: self.paint())
        self.bind('<Configure>', self.paint)
        self.bind('<Enter>', lambda e: self.flag('hover', True))
        self.bind('<Leave>', lambda e: self.flag('hover', False))
        self.bind('<FocusIn>', lambda e: self.flag('focused', True))
        self.bind('<FocusOut>', lambda e: self.flag('focused', False))
        self.bind('<ButtonRelease-1>', self.open)
        self.bind('<Return>', self.open)
        self.bind('<space>', self.open)
        self.bind('<Down>', self.open)
        self.bind('<Escape>', lambda e: self.close())
        self.set_scale(scale)

    def configure(self, cnf=None, **kwargs):
        for key in ('values', 'descriptions'):
            if key in kwargs:
                setattr(self, key, list(kwargs.pop(key)))
        if 'state' in kwargs:
            self.enabled = kwargs.pop('state') != 'disabled'
            if not self.enabled:
                self.close()
        result = super().configure(cnf, **kwargs)
        self.paint()
        return result

    config = configure

    def cget(self, key):
        if key == 'state':
            return 'readonly' if self.enabled else 'disabled'
        return super().cget(key)

    def set_scale(self, scale):
        self.close()
        self.scale = scale
        self.configure(height=round(40 * scale), width=round(240 * scale))

    def current(self, index=None):
        if index is not None and 0 <= index < len(self.values):
            self.variable.set(self.values[index])
        return self.values.index(self.variable.get()) if self.variable.get() in self.values else -1

    def get(self):
        return self.variable.get()

    def flag(self, name, value):
        setattr(self, name, value)
        self.paint()

    def paint(self, event=None):
        self.delete('all')
        w, h, s, c = self.winfo_width(), self.winfo_height(), self.scale, self.colors
        if min(w, h) < 2:
            return
        rounded(self, s, s, w-s, h-s, 9*s, fill=c['input'],
                outline=c['accent'] if self.focused or self.popup else c['input_border'] if self.hover else c['line'], width=max(1, s))
        font = tkfont.Font(self, family=FONT_FAMILY, size=font_pixels(10, s))
        text, available = self.variable.get(), max(1, w-48*s)
        while text and font.measure(text) > available:
            text = text[:-2]
            if text:
                text += '…'
        self.create_text(13*s, h/2, text=text, anchor='w', fill=c['ink'] if self.enabled else c['muted'], font=font)
        x = w-18*s
        self.create_line(x-3*s, h/2-1.5*s, x, h/2+1.5*s, x+3*s, h/2-1.5*s,
                         fill=c['muted'], width=max(1, 1.4*s), capstyle='round', joinstyle='round')

    def open(self, event=None):
        if self.popup:
            self.close()
            return 'break'
        if not self.enabled or not self.values:
            return 'break'
        c, s = self.colors, self.scale
        self.row = max(0, self.current())
        width = max(self.winfo_width(), round(250*s))
        row_height = round((48 if any(self.descriptions) else 38)*s)
        height = min(len(self.values), 5)*row_height+round(8*s)
        x, y = self.winfo_rootx(), self.winfo_rooty()+self.winfo_height()+round(5*s)
        monitor = monitor_at(x, y)
        if monitor:
            left, top, right, bottom = monitor['work']
            x = max(left, min(x, right-width))
            if y+height > bottom:
                y = max(top, self.winfo_rooty()-height-round(5*s))
        popup = self.popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.attributes('-topmost', True)
        popup.configure(bg=c['line'])
        popup.geometry(f'{width}x{height}+{x}+{y}')
        canvas = self.list_canvas = tk.Canvas(popup, bg=c['chrome'], bd=0, highlightthickness=0,
                                               scrollregion=(0, 0, width, row_height*len(self.values)+8*s))
        canvas.pack(fill='both', expand=True, padx=1, pady=1)
        self.row_height = row_height
        canvas.bind('<Motion>', self.popup_motion)
        canvas.bind('<ButtonRelease-1>', self.popup_select)
        canvas.bind('<MouseWheel>', self.popup_scroll)
        popup.bind('<Escape>', lambda e: self.close())
        popup.bind('<Up>', lambda e: self.move(-1))
        popup.bind('<Down>', lambda e: self.move(1))
        popup.bind('<Return>', lambda e: self.choose(self.row))
        popup.bind('<FocusOut>', self.popup_blurred)
        self.paint_list()
        self.focus_timer = popup.after_idle(canvas.focus_force)
        self.paint()
        return 'break'

    def paint_list(self):
        canvas = self.list_canvas
        if not canvas:
            return
        canvas.delete('all')
        s, c = self.scale, self.colors
        width = max(self.winfo_width(), round(250*s))-2
        for index, label in enumerate(self.values):
            y = 4*s+index*self.row_height
            if index == self.row:
                rounded(canvas, 4*s, y, width-4*s, y+self.row_height-2*s, 7*s, fill=c['tint'])
            desc = self.descriptions[index] if index < len(self.descriptions) else ''
            canvas.create_text(13*s, y+(15 if desc else 18)*s, text=label, anchor='w', fill=c['ink'],
                               font=(FONT_FAMILY, font_pixels(10, s)))
            if desc:
                canvas.create_text(13*s, y+33*s, text=desc, anchor='w', fill=c['muted'],
                                   font=(FONT_FAMILY, font_pixels(8, s)))
            if index == self.current():
                canvas.create_text(width-19*s, y+19*s, text='✓', fill=c['accent'], font=(FONT_FAMILY, font_pixels(11, s)))

    def popup_motion(self, event):
        row = int((self.list_canvas.canvasy(event.y)-4*self.scale)//self.row_height)
        if 0 <= row < len(self.values) and row != self.row:
            self.row = row
            self.paint_list()

    def popup_select(self, event):
        row = int((self.list_canvas.canvasy(event.y)-4*self.scale)//self.row_height)
        if 0 <= row < len(self.values):
            self.choose(row)
        return 'break'

    def popup_scroll(self, event):
        self.list_canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    def move(self, direction):
        self.row = (self.row+direction) % len(self.values)
        self.list_canvas.yview_moveto(max(0, self.row-2)/max(1, len(self.values)))
        self.paint_list()
        return 'break'

    def choose(self, row):
        self.current(row)
        self.close()
        self.focus_set()
        self.event_generate('<<ComboboxSelected>>')
        return 'break'

    def close_if_blurred(self):
        self.blur_timer = None
        if self.popup and self.focus_displayof() is None:
            self.close()
        elif self.popup:
            widget = self.focus_get()
            if widget and widget.winfo_toplevel() is not self.popup:
                self.close()

    def popup_blurred(self, event):
        if self.popup:
            if self.blur_timer:
                self.popup.after_cancel(self.blur_timer)
            self.blur_timer = self.popup.after_idle(self.close_if_blurred)

    def close(self):
        if self.popup:
            popup, self.popup, self.list_canvas = self.popup, None, None
            for timer in (self.focus_timer, self.blur_timer):
                if timer:
                    popup.after_cancel(timer)
            self.focus_timer = self.blur_timer = None
            popup.destroy()
            self.paint()
        return 'break'

    def destroy(self):
        self.close()
        self.variable.trace_remove('write', self.trace)
        super().destroy()


class ToggleRow(tk.Canvas):
    def __init__(self, parent, title, description, variable, command, colors, scale):
        self.title, self.description, self.variable, self.command = title, description, variable, command
        self.colors, self.scale, self.hover = colors, scale, False
        super().__init__(parent, bg=colors['window'], bd=0, highlightthickness=0, cursor='hand2', takefocus=True)
        self.trace = variable.trace_add('write', lambda *_: self.paint())
        self.bind('<Configure>', self.paint)
        self.bind('<ButtonRelease-1>', self.toggle)
        self.bind('<Return>', self.toggle)
        self.bind('<space>', self.toggle)
        self.set_scale(scale)

    def set_scale(self, scale):
        self.scale = scale
        self.configure(height=round(52*scale))

    def toggle(self, event=None):
        self.variable.set(not self.variable.get())
        self.command()
        return 'break'

    def paint(self, event=None):
        self.delete('all')
        w, h, s, c = self.winfo_width(), self.winfo_height(), self.scale, self.colors
        if min(w, h) < 2:
            return
        self.create_text(0, 15*s, text=self.title, anchor='w', fill=c['ink'], font=(FONT_FAMILY, font_pixels(10, s)))
        self.create_text(0, 35*s, text=self.description, anchor='w', fill=c['muted'], font=(FONT_FAMILY, font_pixels(8, s)))
        x, y = w-22*s, 24*s
        rounded(self, x-17*s, y-10*s, x+17*s, y+10*s, 10*s, fill=c['action'] if self.variable.get() else c['line'])
        cx = x+(7 if self.variable.get() else -7)*s
        self.create_oval(cx-6*s, y-6*s, cx+6*s, y+6*s, fill=c['action_ink'] if self.variable.get() else c['muted'], outline='')

    def destroy(self):
        self.variable.trace_remove('write', self.trace)
        super().destroy()
