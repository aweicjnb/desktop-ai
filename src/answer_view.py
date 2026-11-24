import math
from itertools import groupby
from pathlib import Path
import re
import sys
import tkinter as tk
from tkinter import font, filedialog, messagebox
from queue import Empty
import webbrowser

from controls import FONT_FAMILY, font_pixels
from image_codec import photo_image
from reply_images import ReplyImages
from rich_content import IMAGE_SCHEME, MAX_REPLY_IMAGES, https_url, image_key, copyable_text
from windows import monitor_at

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
        self.colors = colors
        self.media = {}
        self.photos = {}
        self.image_widgets = []
        self.image_loader = ReplyImages()
        self.image_fetch = self.open_page = None
        self.image_poll_job = None
        self.preview = None
        self.press_point = None
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
        self.press_point = (event.x,event.y)

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

    def set_source(self, source, images=(), fetch=None, open_page=None):
        self.source = source
        self.image_fetch, self.open_page = fetch, open_page
        images=images if isinstance(images,(list,tuple)) else ()
        for descriptor in images[:MAX_REPLY_IMAGES]:
            self.register_image(descriptor)
        self.schedule()

    def clear(self):
        self.source = ""
        self.image_fetch = self.open_page = None
        self.image_loader.clear()
        self.media.clear()
        self.photos.clear()
        self.close_preview()
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
                for widget in self.image_widgets:
                    widget.destroy()
                self.image_widgets.clear()
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
            self.inline(children, tags)
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

    def inline(self, tokens, tags=()):
        emphasis = {'strong':'strong','emphasis':'emphasis','strikethrough':'strike'}
        for text_group, group in groupby(tokens,key=lambda item:item['type']=='text'):
            items=list(group)
            if text_group:
                for text, inline_tags in spans(items,tags):
                    self.put(text,inline_tags)
                continue
            for item in items:
                kind=item['type']
                if kind=='image':
                    self.draw_image(item.get('attrs',{}).get('url',''), ''.join(text for text,_ in spans(item.get('children',[]))))
                elif kind in emphasis:
                    self.inline(item.get('children',[]),tags+(emphasis[kind],))
                elif kind=='link':
                    url=item.get('attrs',{}).get('url','')
                    name='url_'+image_key(url)
                    self.tag_configure(name,foreground=self.colors['accent'],underline=True)
                    self.tag_bind(name,'<ButtonRelease-1>',lambda event,target=url:self.click_link(event,target))
                    self.inline(item.get('children',[]),tags+(name,))
                else:
                    for text, inline_tags in spans([item],tags):
                        self.put(text,inline_tags)

    def click_link(self, event, url):
        if self.tag_ranges('sel') or self.press_point and abs(event.x-self.press_point[0])+abs(event.y-self.press_point[1])>5:
            return
        if https_url(url):
            webbrowser.open(url)

    def register_image(self, descriptor):
        key=descriptor.get('key')
        if not isinstance(key,str) or len(self.media)>=MAX_REPLY_IMAGES and key not in self.media:
            return None
        state=self.media.get(key)
        if state is None:
            state=self.media[key]={'descriptor':descriptor,'revision':0,'loaded':None,'error':None,'submitted':False}
        elif state['descriptor']!=descriptor:
            state.update(descriptor=descriptor,revision=state['revision']+1,loaded=None,error=None,submitted=False)
            self.photos.pop(key,None)
        if self.image_fetch and not state['submitted'] and not (descriptor.get('kind')=='pending' and descriptor.get('asset_pointer')):
            state['submitted']=True
            self.image_loader.submit(key,state['revision'],descriptor,self.image_fetch)
            if self.image_poll_job is None:
                self.image_poll_job=self.after(70,self.poll_images)
        return state

    def poll_images(self):
        self.image_poll_job=None
        changed=False
        while True:
            try:
                generation,key,revision,loaded,error=self.image_loader.results.get_nowait()
            except Empty:
                break
            state=self.media.get(key)
            if generation==self.image_loader.generation and state and state['revision']==revision:
                state.update(loaded=loaded,error=error)
                self.photos.pop(key,None)
                changed=True
        if changed:
            self.rendered_width=0
            self.schedule()
        if self.image_loader.pending():
            self.image_poll_job=self.after(70,self.poll_images)

    def draw_image(self, url, alt):
        key=url[len(IMAGE_SCHEME):] if url.startswith(IMAGE_SCHEME) else image_key(url)
        state=self.media.get(key)
        if state is None and https_url(url):
            if not self.image_fetch:
                self.put(alt or '[图片]')
                return
            state=self.register_image({'key':key,'kind':'remote','url':url,'title':alt or '图片'})
        if state is None:
            self.put(alt or '[图片]')
            return
        c=self.colors
        width=max(self.dp(80),self.winfo_width()-self.dp(4))
        loaded=state['loaded']
        photo=None
        if loaded:
            cached=self.photos.get(key)
            size=(width,self.dp(360))
            if not cached or cached[0]!=size:
                cached=self.photos[key]=(size,photo_image(loaded,*size,master=self))
            photo=cached[1]
        height=photo.height() if photo else self.dp(70)
        frame=tk.Frame(self,bg=c['window'],width=width,height=height+self.dp(29))
        frame.pack_propagate(False)
        canvas=tk.Canvas(frame,width=width,height=height,bg=c['soft'],bd=0,highlightthickness=0)
        canvas.pack(fill='x')
        if photo:
            canvas.create_image(width/2,0,image=photo,anchor='n')
            canvas.configure(cursor='hand2')
            canvas.bind('<Button-1>',lambda event,item=loaded:self.show_preview(item))
        else:
            text=state['error'] or ('图片生成中…' if state['descriptor'].get('asset_pointer') else '图片加载中…')
            canvas.create_text(width/2,height/2,text=text,fill=c['muted'],font=self.regular,width=width-self.dp(20))
        footer=tk.Frame(frame,bg=c['window'])
        footer.pack(fill='x',pady=(self.dp(3),0))
        def action(text,command,side='right'):
            label=tk.Label(footer,text=text,font=self.regular,bg=c['window'],fg=c['accent'],cursor='hand2')
            label.pack(side=side,padx=(self.dp(8),0))
            label.bind('<Button-1>',lambda event:command())
        if loaded:
            source_url=state['descriptor'].get('source_url')
            if https_url(source_url):
                action('图片来源',lambda:webbrowser.open(source_url),side='left')
            action('保存图片',lambda:self.save_image(loaded))
            action('查看大图',lambda:self.show_preview(loaded))
        elif state['error']:
            action('重试',lambda:self.retry_image(key))
            if self.open_page:
                action('查看网页',self.open_page)
        self.image_widgets.append(frame)
        self.window_create('end',window=frame,align='top')

    def retry_image(self,key):
        state=self.media.get(key)
        if state:
            state.update(submitted=False,error=None)
            self.register_image(state['descriptor'])
            self.rendered_width=0
            self.schedule()

    def save_image(self,loaded):
        filename=filedialog.asksaveasfilename(parent=self.winfo_toplevel(),title='保存图片',
            initialfile='ChatGPT 图片'+loaded.extension,defaultextension=loaded.extension,
            filetypes=[('图片','*'+loaded.extension),('所有文件','*.*')])
        if filename:
            try:
                Path(filename).write_bytes(loaded.raw)
            except OSError:
                messagebox.showerror('保存图片','图片未能保存，请选择其他位置。',parent=self.winfo_toplevel())

    def close_preview(self):
        if self.preview is not None:
            self.preview.destroy()
            self.preview=None

    def show_preview(self,loaded):
        self.close_preview()
        c=self.colors
        view=self.preview=tk.Toplevel(self)
        view.title('查看图片')
        view.configure(bg=c['window'])
        view.attributes('-topmost',True)
        view.transient(self.winfo_toplevel())
        screen=monitor_at(self.winfo_rootx(),self.winfo_rooty())
        left,top,right,bottom=screen['work'] if screen else (0,0,self.winfo_screenwidth(),self.winfo_screenheight())
        width=min(round((right-left)*.8),max(self.dp(360),loaded.image.width))
        height=min(round((bottom-top)*.8),max(self.dp(260),loaded.image.height+self.dp(45)))
        view.geometry(f'{width}x{height}+{left+(right-left-width)//2}+{top+(bottom-top-height)//2}')
        action=tk.Button(view,text='保存图片',command=lambda:self.save_image(loaded),relief='flat',bd=0,
                         font=self.regular,bg=c['soft'],fg=c['ink'],cursor='hand2')
        action.pack(side='bottom',pady=self.dp(6))
        canvas=tk.Canvas(view,bg=c['window'],bd=0,highlightthickness=0)
        canvas.pack(fill='both',expand=True)
        def draw(event=None):
            size=canvas.winfo_width(),canvas.winfo_height()
            if getattr(view,'photo_size',None)==size:
                return
            view.photo_size=size
            view.photo=photo_image(loaded,*size,master=view)
            canvas.delete('all');canvas.create_image(size[0]/2,size[1]/2,image=view.photo)
        canvas.bind('<Configure>',draw)
        def destroy(event):
            if event.widget is view and self.preview is view:
                self.preview=None
        view.bind('<Destroy>',destroy)
        view.bind('<Escape>',lambda event:(view.destroy(),'break')[1])

    def copyable_text(self):
        return copyable_text(self.source)

    def destroy(self):
        self.image_loader.close()
        self.close_preview()
        for timer in (self.render_job,self.image_poll_job):
            if timer is not None:
                self.after_cancel(timer)
        self.photos.clear()
        super().destroy()

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
