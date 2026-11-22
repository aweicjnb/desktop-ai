import tkinter as tk


def rounded(canvas, x1, y1, x2, y2, radius, **style):
    radius = min(radius, (y2 - y1) / 2, (x2 - x1) / 2)
    return canvas.create_polygon(
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        smooth=True, splinesteps=18, **style,
    )


class WindowResize:
    CURSORS = {
        "n": "size_ns", "s": "size_ns", "w": "size_we", "e": "size_we",
        "nw": "top_left_corner", "ne": "top_right_corner",
        "sw": "bottom_left_corner", "se": "bottom_right_corner",
    }

    def __init__(self, root, scale):
        self.root = root
        self.edge = round(6 * scale)
        self.min_width = min(round(350 * scale), root.winfo_screenwidth())
        self.min_height = min(round(240 * scale), root.winfo_screenheight())
        self.drag = None
        self.cursor = None
        self.tag = "DesktopAiResize" + str(id(self))
        root.bind_class(self.tag, "<Motion>", self.hover)
        root.bind_class(self.tag, "<Leave>", self.leave)
        root.bind_class(self.tag, "<ButtonPress-1>", self.start)
        root.bind_class(self.tag, "<B1-Motion>", self.move)
        root.bind_class(self.tag, "<ButtonRelease-1>", self.end)
        self.attach(root)

    def attach(self, widget):
        if isinstance(widget, tk.Menu):
            return
        tags = widget.bindtags()
        if self.tag not in tags:
            widget.bindtags((self.tag, *tags))
        for child in widget.winfo_children():
            self.attach(child)

    def direction(self, x, y):
        x -= self.root.winfo_rootx()
        y -= self.root.winfo_rooty()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if not (0 <= x < width and 0 <= y < height):
            return ""
        vertical = "n" if y < self.edge else "s" if y >= height - self.edge else ""
        horizontal = "w" if x < self.edge else "e" if x >= width - self.edge else ""
        return vertical + horizontal

    def set_cursor(self, widget, direction):
        if self.cursor and self.cursor[0] is widget and self.cursor[2] == direction:
            return
        self.clear_cursor()
        if direction:
            previous = widget.cget("cursor")
            widget.configure(cursor=self.CURSORS[direction])
            self.cursor = (widget, previous, direction)

    def clear_cursor(self):
        if self.cursor:
            widget, previous, direction = self.cursor
            if widget.winfo_exists() and widget.cget("cursor") == self.CURSORS[direction]:
                widget.configure(cursor=previous)
            self.cursor = None

    def hover(self, event):
        if not self.drag:
            self.set_cursor(event.widget, self.direction(event.x_root, event.y_root))

    def leave(self, event):
        if not self.drag and self.cursor and self.cursor[0] is event.widget:
            self.clear_cursor()

    def start(self, event):
        direction = self.direction(event.x_root, event.y_root)
        if direction:
            root = self.root
            self.drag = (direction, event.x_root, event.y_root, root.winfo_rootx(),
                         root.winfo_rooty(), root.winfo_width(), root.winfo_height())
            self.set_cursor(event.widget, direction)
            return "break"

    def move(self, event):
        if self.drag:
            direction, start_x, start_y, left, top, width, height = self.drag
            dx, dy = event.x_root - start_x, event.y_root - start_y
            right, bottom = left + width, top + height
            if "w" in direction:
                left = min(left + dx, right - self.min_width)
            if "e" in direction:
                right = max(right + dx, left + self.min_width)
            if "n" in direction:
                top = min(top + dy, bottom - self.min_height)
            if "s" in direction:
                bottom = max(bottom + dy, top + self.min_height)
            self.root.geometry(f"{right - left}x{bottom - top}+{left}+{top}")
            return "break"

    def end(self, event):
        if self.drag:
            self.drag = None
            self.hover(event)
            return "break"

    def cancel(self):
        self.drag = None
        self.clear_cursor()


class IconButton(tk.Canvas):
    def __init__(self, parent, icon, command, colors, scale, background):
        self.icon, self.command, self.colors = icon, command, colors
        self.scale, self.background = scale, background
        self.enabled, self.hover, self.focused, self.selected = True, False, False, False
        size = round((30 if icon != "settings" else 29) * scale)
        super().__init__(parent, bg=colors[background], width=size, height=size, borderwidth=0,
                         highlightthickness=0, cursor="hand2", takefocus=True)
        self.bind("<Configure>", self.paint)
        self.bind("<Map>", self.paint)
        self.bind("<Enter>", lambda event: self.set_flag("hover", True))
        self.bind("<Leave>", lambda event: self.set_flag("hover", False))
        self.bind("<FocusIn>", lambda event: self.set_flag("focused", True))
        self.bind("<FocusOut>", lambda event: self.set_flag("focused", False))
        self.bind("<ButtonRelease-1>", self.invoke)
        self.bind("<Return>", self.invoke)
        self.bind("<space>", self.invoke)

    def set_flag(self, name, value):
        setattr(self, name, value)
        self.paint()

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if self.enabled != enabled:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "")
            self.paint()

    def invoke(self, event=None):
        if self.enabled:
            self.command()
        return "break"

    def paint(self, event=None):
        self.delete("all")
        c, s = self.colors, self.scale
        w, h = self.winfo_width(), self.winfo_height()
        if min(w, h) <= 1:
            return
        x, y = w / 2, h / 2
        if self.icon == "send":
            fill = ("accent" if self.hover else "action") if self.enabled else "soft"
            ink = "action_ink" if self.enabled else "muted"
        elif self.icon == "stop":
            fill, ink = "tint", "accent"
        else:
            fill = "tint" if self.selected else "soft" if self.hover or self.focused else self.background
            ink = "accent" if self.selected else "muted"
        rounded(self, s, s, w - s, h - s, 8 * s, fill=c[fill],
                outline=c["accent"] if self.focused else "", width=max(1, s))
        stroke = max(1, round(1.5 * s))
        if self.icon == "send":
            self.create_line(x, y + 5*s, x, y - 5*s, fill=c[ink], width=stroke, capstyle="round")
            self.create_line(x - 4*s, y - s, x, y - 5*s, x + 4*s, y - s,
                             fill=c[ink], width=stroke, joinstyle="round", capstyle="round")
        elif self.icon == "stop":
            rounded(self, x - 4*s, y - 4*s, x + 4*s, y + 4*s, 2*s, fill=c[ink])
        else:
            for offset, knob in ((-3, -2), (3, 2)):
                line_y = y + offset*s
                self.create_line(x - 6*s, line_y, x + 6*s, line_y,
                                 fill=c[ink], width=stroke, capstyle="round")
                knob_x = x + knob*s
                self.create_oval(knob_x - 1.7*s, line_y - 1.7*s, knob_x + 1.7*s, line_y + 1.7*s,
                                 fill=c[fill], outline=c[ink], width=stroke)


class SettingsPanel(tk.Canvas):
    def __init__(self, parent, colors, scale, options, save):
        self.colors, self.scale, self.options, self.save = colors, scale, options, save
        self.hover = None
        self.selected = 0
        self.focused = False
        super().__init__(parent, bg=colors["window"], width=round(202 * scale),
                         height=round(108 * scale), borderwidth=0, highlightthickness=0, takefocus=True)
        self.bind("<Configure>", self.paint)
        self.bind("<Motion>", self.motion)
        self.bind("<Leave>", lambda event: self.highlight(None))
        self.bind("<ButtonRelease-1>", self.click)
        self.bind("<FocusIn>", lambda event: self.focused_state(True))
        self.bind("<FocusOut>", lambda event: self.focused_state(False))
        self.bind("<Up>", lambda event: self.move(-1))
        self.bind("<Down>", lambda event: self.move(1))
        self.bind("<space>", lambda event: self.toggle(self.selected))
        self.bind("<Return>", lambda event: self.toggle(self.selected))

    def row_at(self, y):
        local = y / self.scale
        return int((local - 28) // 36) if 28 <= local < 100 else None

    def highlight(self, row):
        if self.hover != row:
            self.hover = row
            self.configure(cursor="hand2" if row is not None else "")
            self.paint()

    def motion(self, event):
        self.highlight(self.row_at(event.y))

    def click(self, event):
        row = self.row_at(event.y)
        if row is not None:
            self.selected = row
            self.toggle(row)

    def focused_state(self, focused):
        self.focused = focused
        self.paint()

    def move(self, offset):
        self.selected = (self.selected + offset) % len(self.options)
        self.paint()
        return "break"

    def toggle(self, row):
        variable = self.options[row][1]
        variable.set(not variable.get())
        self.save()
        self.paint()
        return "break"

    def paint(self, event=None):
        self.delete("all")
        s, c = self.scale, self.colors
        w, h = self.winfo_width(), self.winfo_height()
        if min(w, h) <= 1:
            return
        rounded(self, s, s, w - s, h - s, 12*s, fill=c["chrome"], outline=c["line"], width=max(1, s))
        self.create_text(13*s, 15*s, text="设置", anchor="w", fill=c["muted"],
                         font=("Microsoft YaHei UI", 9))
        for index, (label, value) in enumerate(self.options):
            y = (46 + index * 36) * s
            if self.hover == index or (self.focused and self.selected == index):
                rounded(self, 6*s, y - 15*s, w - 6*s, y + 15*s, 7*s, fill=c["soft"])
            self.create_text(13*s, y, text=label, anchor="w", fill=c["ink"],
                             font=("Microsoft YaHei UI", 10))
            x = w - 32*s
            rounded(self, x - 16*s, y - 9*s, x + 16*s, y + 9*s, 9*s,
                    fill=c["action"] if value.get() else c["line"])
            k = x + (7 if value.get() else -7)*s
            self.create_oval(k - 5.5*s, y - 5.5*s, k + 5.5*s, y + 5.5*s,
                             fill=c["action_ink"] if value.get() else c["muted"], outline="")
