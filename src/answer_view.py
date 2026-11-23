import math
from itertools import groupby
from pathlib import Path
import re
import sys
import tkinter as tk
from tkinter import font

from controls import FONT_FAMILY, font_pixels

sys.path.insert(0, str(Path(__file__).with_name("_vendor")))
from mistune import create_markdown


_parse = create_markdown(renderer="ast", plugins=["table", "strikethrough"])
_citations = re.compile(r"(\[citation:(\d+)\])")
_table_words = re.compile(r"\s+|[a-zA-Z0-9_./:+-]+|.", re.DOTALL)


def spans(tokens, tags=()):
    emphasis = {"strong": "strong", "emphasis": "emphasis", "strikethrough": "strike", "link": "link"}
    for is_text, group in groupby(tokens, key=lambda token: token["type"] == "text"):
        if is_text:
            raw = "".join(part["raw"] for part in group)
            for index, text in enumerate(_citations.split(raw)):
                if index % 3 == 0 and text:
                    yield text, tags
                elif index % 3 == 2:
                    yield "[" + text + "]", tags + ("citation",)
            continue
        for item in group:
            kind, raw = item["type"], item.get("raw", "")
            if kind in ("softbreak", "linebreak"):
                yield ("\n" if kind == "linebreak" else " "), tags
            elif kind == "codespan":
                yield raw, tags + ("code",)
            elif "children" in item:
                yield from spans(item["children"], tags + ((emphasis[kind],) if kind in emphasis else ()))
            elif raw:
                yield raw, tags


class AnswerView(tk.Text):
    def __init__(self, parent, colors, scale):
        self.regular = font.Font(parent, family=FONT_FAMILY)
        self.bold = font.Font(parent, family=FONT_FAMILY, weight="bold")
        self.code = font.Font(parent, family="Cascadia Mono")
        super().__init__(parent, width=1, height=1, wrap="word", font=self.regular, state="disabled",
                         bg=colors["window"], fg=colors["ink"], borderwidth=0, highlightthickness=0,
                         padx=0, pady=0, exportselection=False, takefocus=True, cursor="xterm",
                         selectbackground=colors["accent"], selectforeground=colors["action_ink"])
        self.source = self.rendered = ""
        self.rendered_width = 0
        self.render_job = None
        self.dragging = False
        self.table_number = 0
        self.measure_cache = {}
        self.scale = scale
        self.tag_configure("strong", font=self.bold)
        self.tag_configure("emphasis", font=(FONT_FAMILY, font_pixels(11, scale), "italic"))
        self.tag_configure("strike", overstrike=True)
        self.tag_configure("link", foreground=colors["accent"], underline=True)
        self.tag_configure("citation", foreground=colors["muted"])
        self.tag_configure("code", font=self.code, background=colors["soft"])
        self.tag_configure("quote", foreground=colors["muted"])
        self.tag_configure("table_header", background=colors["tint"], foreground=colors["ink"], font=self.bold)
        self.tag_configure("table_row_a", background=colors["soft"])
        self.tag_configure("table_row_b", background=colors["chrome"])
        self.set_scale(scale)
        self.tag_raise("sel")
        self.bind("<Configure>", lambda event: self.schedule(), add="+")
        self.bind("<ButtonPress-1>", self.begin_selection, add="+")
        self.bind("<ButtonRelease-1>", self.end_selection, add="+")
        self.bind("<KeyRelease>", lambda event: self.schedule(), add="+")
        self.bind("<<Copy>>", self.copy_selection)
        self.bind("<Control-a>", self.select_all)

    def dp(self, value):
        return round(value * self.scale)

    def set_scale(self, scale):
        ratio = scale / self.scale
        self.scale = scale
        px = self.dp
        self.regular.configure(size=font_pixels(11, scale))
        self.bold.configure(size=font_pixels(11, scale))
        self.code.configure(size=font_pixels(10, scale))
        self.tag_configure("emphasis", font=(FONT_FAMILY, font_pixels(11, scale), "italic"))
        self.tag_configure("citation", font=(FONT_FAMILY, font_pixels(9, scale)))
        self.tag_configure("code_block", lmargin1=px(9), lmargin2=px(9), rmargin=px(5))
        self.tag_configure("quote", lmargin1=px(12), lmargin2=px(12))
        for level, size, before, after in ((1, 15, 7, 6), (2, 13, 5, 5), (3, 11, 5, 4)):
            self.tag_configure(f"heading{level}", font=(FONT_FAMILY, font_pixels(size, scale), "bold"),
                               spacing1=px(before), spacing3=px(after))
        self.tag_configure("space", font=(FONT_FAMILY, font_pixels(6, scale)))
        self.tag_configure("table_top", spacing1=px(5))
        self.tag_configure("table_bottom", spacing3=px(5))
        # Fit existing tables to the new DPI even when a selection delays reflow.
        for number in range(1, self.table_number + 1):
            grid = f"grid{number}"
            tabs = self.tk.splitlist(self.tag_cget(grid, "tabs"))
            self.tag_configure(grid, tabs=[round(int(tab) * ratio) for tab in tabs],
                               lmargin1=px(8), lmargin2=px(8))
        self.rendered_width = 0
        self.schedule()

    def begin_selection(self, event):
        self.focus_set()
        self.dragging = True

    def end_selection(self, event):
        self.dragging = False
        self.schedule()

    def selected_text(self):
        selection = self.tag_ranges("sel")
        if not selection:
            return ""
        return self.get(*selection)

    def copy_selection(self, event=None):
        text = self.selected_text()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
        return "break"

    def select_all(self, event):
        self.tag_add("sel", "1.0", "end-1c")
        return "break"

    def set_source(self, source):
        self.source = source
        self.schedule()

    def clear(self):
        self.source = ""
        self.dragging = False
        self.tag_remove("sel", "1.0", "end")
        self.paint()

    def schedule(self):
        if self.render_job is None:
            self.render_job = self.after(55, self.paint)

    def paint(self):
        if self.render_job is not None:
            self.after_cancel(self.render_job)
            self.render_job = None
        if self.dragging or self.tag_ranges("sel"):
            return
        width = self.winfo_width()
        if self.source and width < self.dp(80):
            return
        if self.source != self.rendered or self.rendered_width != width:
            self.configure(state="normal")
            try:
                self.delete("1.0", "end")
                self.table_number = 0
                self.measure_cache.clear()
                for token in _parse(self.source):
                    self.block(token)
                if self.get("end-2c", "end-1c") == "\n":
                    self.delete("end-2c", "end-1c")
                self.rendered, self.rendered_width = self.source, width
                self.tag_raise("sel")
            finally:
                self.configure(state="disabled")
        pixels = self.count("1.0", "end", "update", "ypixels", return_ints=True) or 0
        height = max(1, math.ceil(pixels / self.regular.metrics("linespace")))
        if int(self.cget("height")) != height:
            self.configure(height=height)

    def put(self, text, tags=()):
        if text:
            self.insert("end", text, tags)

    def block(self, token, tags=()):
        kind = token["type"]
        children = token.get("children", [])
        if kind == "heading":
            tags += (f"heading{min(3, token['attrs']['level'])}",)
        if kind in ("paragraph", "block_text", "heading"):
            for text, inline_tags in spans(children, tags):
                self.put(text, inline_tags)
            self.put("\n", tags)
        elif kind == "blank_line":
            self.put("\n", ("space",))
        elif kind == "block_code":
            self.put(token.get("raw", "").rstrip("\n") + "\n", ("code", "code_block"))
        elif kind == "table":
            self.table(children)
        elif kind == "list":
            ordered = token["attrs"].get("ordered", False)
            start = token["attrs"].get("start", 1)
            for index, item in enumerate(children, start):
                self.put(f"{index}.  " if ordered else "•  ", tags)
                for child in item.get("children", []):
                    self.block(child, tags)
        elif kind == "thematic_break":
            self.put("────────\n", ("quote",))
        elif children:
            for child in children:
                self.block(child, tags + (("quote",) if kind == "block_quote" else ()))
        elif token.get("raw"):
            self.put(token["raw"], tags)

    def measure(self, text, tags):
        style = "code" if "code" in tags else "strong" if "strong" in tags else ""
        key = (text, style)
        if key not in self.measure_cache:
            face = {"code": self.code, "strong": self.bold}.get(style, self.regular)
            self.measure_cache[key] = face.measure(text)
        return self.measure_cache[key]

    def wrap_cell(self, cell, width):
        lines = [[]]
        used = 0
        for value, tags in cell:
            for piece in _table_words.findall(value):
                pieces = list(piece) if self.measure(piece, tags) > width else [piece]
                for part in pieces:
                    measure = self.measure(part, tags)
                    if part == "\n" or used and used + measure > width:
                        lines.append([])
                        used = 0
                    if not part.isspace() or used and part != "\n":
                        lines[-1].append((part, tags))
                        used += measure
        return lines

    def table(self, sections):
        rows = []
        for section in sections:
            children = section["children"]
            rows.extend([children] if section["type"] == "table_head" else (row["children"] for row in children))
        if not rows or not rows[0]:
            return
        count = len(rows[0])
        cells = [[list(spans(cell["children"])) for cell in row] for row in rows]
        padding = self.dp(8)
        available = self.winfo_width() - 2 * padding - self.dp(4)
        if available < count * self.dp(24):
            for row in cells:
                for column, cell in enumerate(row):
                    if column:
                        self.put("  ·  ")
                    for value, tags in cell:
                        self.put(value, tags)
                self.put("\n")
            return
        minimum = min(self.dp(72), available // count)
        preferred = [
            max(sum(self.measure(text, tags) for text, tags in row[i]) for row in cells) + 2 * padding
            for i in range(count)
        ]
        needs = [max(0, width - minimum) for width in preferred]
        spare = available - minimum * count
        total = sum(needs)
        widths = [minimum + min(need, round(spare * need / total)) if total else minimum for need in needs]
        widths[-1] += available - sum(widths)

        self.table_number += 1
        grid = f"grid{self.table_number}"
        tabs = [sum(widths[:index + 1]) for index in range(count)]
        self.tag_configure(grid, tabs=tabs, lmargin1=padding, lmargin2=padding, wrap="none")
        for index, row in enumerate(cells):
            shade = "table_header" if index == 0 else "table_row_a" if index % 2 else "table_row_b"
            formatted = [self.wrap_cell(cell, width - 2 * padding) for cell, width in zip(row, widths)]
            height = max(map(len, formatted))
            for line in range(height):
                tags = (grid, shade)
                if line == 0:
                    tags += ("table_top",)
                if line == height - 1:
                    tags += ("table_bottom",)
                for column in formatted:
                    if line < len(column):
                        for value, inline_tags in column[line]:
                            self.put(value, tags + inline_tags)
                    self.put("\t", tags)
                self.put("\n", tags)
        self.put("\n", ("space",))
