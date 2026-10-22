"""Desktop water / deep-river / bridge annotator for the 25 official maps.

Run from the project root:
    python tools/annotate_maps.py

Masks are written at 2048x2048:
    annotations/<slug>/water.png
    annotations/<slug>/bridge.png
    annotations/<slug>/meta.json

The build script downsamples these to the app's 1024 grid.
"""

from __future__ import annotations

import json
import os
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

ROOT = Path(os.environ.get("SQUAD_MAP_ROOT", str(Path(__file__).resolve().parents[1])))
CATALOG = json.loads((ROOT / "tools" / "maps_catalog.json").read_text(encoding="utf-8"))
SOURCE = ROOT / ".tmp" / "maps_source"
OUT = ROOT / "annotations"
SIZE = 2048
FONT = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 9, "bold")

LAYERS = [
    ("水域", "water", (35, 110, 235), 0.36),
    ("桥梁", "bridge", (255, 170, 25), 0.72),
    ("地图边界内", "boundary", (35, 210, 120), 0.28),
    ("通畅道路", "clear", (60, 230, 100), 0.58),
    ("不可通行障碍", "obstacle", (255, 55, 110), 0.78),
    ("水面舰艇航道", "boat", (80, 190, 255), 0.48),
]
LABEL_BY_KEY = {k: label for label, k, _c, _a in LAYERS}
KEY_BY_LABEL = {label: k for label, k, _c, _a in LAYERS}
COLOR_BY_KEY = {k: (c, a) for _label, k, c, a in LAYERS}


class Annotator(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Squad 地图区域标注器（水域 / 桥梁 / 边界 / 道路 / 障碍）")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1340, sw - 80), min(800, sh - 80)
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(1080, 700)
        self.configure(bg="#0d1117")
        self.option_add("*Font", FONT)

        self.maps = CATALOG
        self.slug = None
        self.base = None
        self.topo = None
        self.water = Image.new("L", (SIZE, SIZE), 0)
        self.bridge = Image.new("L", (SIZE, SIZE), 0)
        self.boundary = Image.new("L", (SIZE, SIZE), 0)
        self.clear = Image.new("L", (SIZE, SIZE), 0)
        self.obstacle = Image.new("L", (SIZE, SIZE), 0)
        self.boat = Image.new("L", (SIZE, SIZE), 0)
        self.layer = "water"
        self.tool = "polygon"
        self.brush = 4
        self.zoom = 1.0
        self.cx = SIZE / 2
        self.cy = SIZE / 2
        self.dirty = False
        self.poly = []
        self.pan = None
        self.last_brush = None
        self.undo_stack = []
        self.redo_stack = []
        self._render_job = None

        self._build_ui()
        self._bind_keys()
        self._fill_map_list()
        first = "yehorivka" if any(m["slug"] == "yehorivka" for m in self.maps) else self.maps[0]["slug"]
        self.select_map(first, force=True)
        self.after(250, self.fit)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- UI ----------------
    def _build_ui(self):
        header = tk.Frame(self, bg="#161b22", height=46)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        self.map_label = tk.Label(header, text="", bg="#161b22", fg="#f0f6fc", font=("Microsoft YaHei UI", 13, "bold"))
        self.map_label.pack(side="left", padx=(14, 8))

        tk.Label(header, text="底图", bg="#161b22", fg="#8b949e").pack(side="left", padx=(4, 2))
        self.topo_var = tk.BooleanVar(value=False)
        tk.Checkbutton(header, text="地形图", variable=self.topo_var, command=self.render,
                       bg="#161b22", fg="#c9d1d9", selectcolor="#161b22",
                       activebackground="#161b22", activeforeground="#fff").pack(side="left")

        tk.Frame(header, bg="#30363d", width=1, height=26).pack(side="left", padx=10, fill="y")

        tk.Label(header, text="图层", bg="#161b22", fg="#8b949e").pack(side="left", padx=(2, 2))
        self.layer_var = tk.StringVar(value="水域")
        self.layer_combo = ttk.Combobox(header, textvariable=self.layer_var, state="readonly",
                                        values=[label for label, _k, _c, _a in LAYERS],
                                        width=13, font=FONT)
        self.layer_combo.set("水域")
        self.layer_combo.pack(side="left", padx=2)
        self.layer_combo.bind("<<ComboboxSelected>>", self.on_layer_combo)

        tk.Frame(header, bg="#30363d", width=1, height=26).pack(side="left", padx=10, fill="y")

        tk.Label(header, text="工具", bg="#161b22", fg="#8b949e").pack(side="left", padx=(2, 2))
        self.tool_var = tk.StringVar(value="polygon")
        for text, value in (("多边形", "polygon"), ("画笔", "brush"), ("橡皮", "erase")):
            tk.Radiobutton(header, text=text, value=value, variable=self.tool_var,
                           command=lambda v=value: self.set_tool(v), bg="#161b22", fg="#c9d1d9",
                           selectcolor="#161b22", activebackground="#161b22",
                           activeforeground="#fff", indicatoron=False, width=6,
                           font=FONT).pack(side="left", padx=2)

        tk.Label(header, text="笔刷", bg="#161b22", fg="#8b949e").pack(side="left", padx=(10, 2))
        self.brush_scale = tk.Scale(header, from_=1, to=40, orient="horizontal", length=120,
                                    showvalue=False, command=self.set_brush,
                                    bg="#161b22", fg="#c9d1d9", highlightthickness=0,
                                    troughcolor="#30363d")
        self.brush_scale.set(4)
        self.brush_scale.pack(side="left")

        actions = tk.Frame(self, bg="#11161d")
        actions.pack(fill="x", side="top")
        for text, cmd, fg in (("保存当前地图", self.save_current, "#0d1117"),
                              ("撤销", self.undo, "#c9d1d9"),
                              ("重做", self.redo, "#c9d1d9"),
                              ("完成多边形", self.close_poly, "#0d1117"),
                              ("取消多边形", self.cancel_poly, "#c9d1d9"),
                              ("水域→舰艇", self.copy_water_to_boat, "#c9d1d9"),
                              ("适应窗口", self.fit, "#c9d1d9"),
                              ("清空当前图层", self.clear_layer, "#f85149")):
            bg = "#f0b429" if text == "保存当前地图" else ("#3fb950" if text == "完成多边形" else "#21262d")
            tk.Button(actions, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground="#30363d", activeforeground="#fff",
                      relief="flat", padx=9, pady=3, font=FONT).pack(side="left", padx=(8 if text == "保存当前地图" else 3, 3), pady=3)
        tk.Label(actions, text="多边形：逐点左键；撤销删上一个点；双击/Enter/完成按钮闭合。",
                 bg="#11161d", fg="#6e7681").pack(side="left", padx=10)

        body = tk.Frame(self, bg="#0d1117")
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg="#161b22", width=230)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="地图（✓ 已标记）", bg="#161b22", fg="#8b949e",
                 anchor="w", font=FONT_B).pack(fill="x", padx=10, pady=(10, 4))
        self.listbox = tk.Listbox(left, bg="#0d1117", fg="#c9d1d9",
                                  selectbackground="#1f6feb", selectforeground="#fff",
                                  activestyle="none", exportselection=False,
                                  relief="flat", highlightthickness=0, font=FONT)
        self.listbox.pack(fill="both", expand=True, padx=8, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self.on_list_select)
        self.progress = tk.Label(left, text="", bg="#161b22", fg="#8b949e", anchor="w")
        self.progress.pack(fill="x", padx=10, pady=(0, 8))

        right = tk.Frame(body, bg="#0d1117")
        right.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(right, bg="#0b0f14", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.on_resize)
        self.canvas.bind("<ButtonPress-1>", self.on_left_down)
        self.canvas.bind("<B1-Motion>", self.on_left_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_left_up)
        self.canvas.bind("<Double-Button-1>", self.on_double)
        self.canvas.bind("<Return>", lambda e: self.close_poly())
        self.canvas.bind("<ButtonPress-2>", self.on_pan_start)
        self.canvas.bind("<B2-Motion>", self.on_pan_move)
        self.canvas.bind("<ButtonRelease-2>", self.on_pan_end)
        self.canvas.bind("<ButtonPress-3>", self.on_pan_start)
        self.canvas.bind("<B3-Motion>", self.on_pan_move)
        self.canvas.bind("<ButtonRelease-3>", self.on_pan_end)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Motion>", self.on_motion)

        self.status = tk.Label(self, text="", bg="#161b22", fg="#8b949e", anchor="w", padx=10)
        self.status.pack(fill="x", side="bottom")
        self.hint = tk.Label(self, text="图层：W水域 B桥梁 N边界内 R通畅道路 O障碍 V舰艇航道 · 多边形左键逐点，双击/Enter/完成按钮闭合，撤销删点 · 中键/右键拖动 · 滚轮缩放 · Ctrl+S 保存",
                             bg="#0d1117", fg="#6e7681", anchor="w", padx=10, pady=3)
        self.hint.pack(fill="x", side="bottom")

    def _bind_keys(self):
        self.bind("<Control-s>", lambda e: self.save_current())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-y>", lambda e: self.redo())
        self.bind("<Escape>", lambda e: self.cancel_poly())
        self.bind("<Return>", lambda e: self.close_poly())
        self.bind("w", lambda e: self.set_layer("water"))
        self.bind("b", lambda e: self.set_layer("bridge"))
        self.bind("n", lambda e: self.set_layer("boundary"))
        self.bind("r", lambda e: self.set_layer("clear"))
        self.bind("o", lambda e: self.set_layer("obstacle"))
        self.bind("v", lambda e: self.set_layer("boat"))
        self.bind("p", lambda e: self.set_tool("polygon"))
        self.bind("g", lambda e: self.set_tool("brush"))
        self.bind("e", lambda e: self.set_tool("erase"))

    # ---------------- maps ----------------
    def _fill_map_list(self):
        self.listbox.delete(0, tk.END)
        names = ("water.png", "bridge.png", "boundary.png", "clear_road.png", "obstacle.png", "boat.png")
        def done(slug):
            return any((OUT / slug / n).exists() for n in names)
        for m in self.maps:
            self.listbox.insert(tk.END, ("✓ " if done(m["slug"]) else "  ") + m["label"])
        self.progress.config(text=f"已标记 {sum(1 for m in self.maps if done(m['slug']))} / {len(self.maps)}")

    def on_list_select(self, _event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        slug = self.maps[sel[0]]["slug"]
        if slug == self.slug:
            return
        self.select_map(slug)

    def select_map(self, slug, force=False):
        if not force:
            if not self.save_current(silent=True):
                return
        m = next((x for x in self.maps if x["slug"] == slug), None)
        if not m:
            return
        src = SOURCE / slug
        try:
            base = Image.open(src / "basemap.webp").convert("RGB").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
            topo_path = src / "topomap.webp"
            topo = Image.open(topo_path).convert("RGB").resize((SIZE, SIZE), Image.Resampling.LANCZOS) if topo_path.exists() else base
        except Exception as exc:
            messagebox.showerror("地图加载失败", f"{slug}\n{exc}\n\n需要 .tmp/maps_source（先运行 tools/download_maps.py）。")
            return

        self.slug = slug
        self.base, self.topo = base, topo
        self.water = Image.new("L", (SIZE, SIZE), 0)
        self.bridge = Image.new("L", (SIZE, SIZE), 0)
        self.boundary = Image.new("L", (SIZE, SIZE), 0)
        self.clear = Image.new("L", (SIZE, SIZE), 0)
        self.obstacle = Image.new("L", (SIZE, SIZE), 0)
        mp = OUT / slug
        for name, target in (("water.png", "water"), ("bridge.png", "bridge"),
                             ("boundary.png", "boundary"), ("clear_road.png", "clear"),
                             ("obstacle.png", "obstacle"), ("boat.png", "boat")):
            p = mp / name
            if p.exists():
                im = Image.open(p).convert("L")
                if im.size != (SIZE, SIZE):
                    im = im.resize((SIZE, SIZE), Image.Resampling.NEAREST)
                setattr(self, target, im)
        self.poly = []
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = False
        self.map_label.config(text=m["label"])
        self.zoom = 1.0
        self.cx = self.cy = SIZE / 2
        self.fit()
        self.listbox.selection_clear(0, tk.END)
        idx = self.maps.index(m)
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self.update_status()

    # ---------------- view ----------------
    def on_resize(self, _event=None):
        if self._render_job:
            self.after_cancel(self._render_job)
        self._render_job = self.after(40, self.render)

    def fit(self):
        w = max(200, self.canvas.winfo_width())
        h = max(200, self.canvas.winfo_height())
        self.zoom = max(0.35, min(2.0, min((w - 30) / SIZE, (h - 30) / SIZE)))
        self.cx = self.cy = SIZE / 2
        self.render()

    def view_rect(self):
        w = max(100, self.canvas.winfo_width())
        h = max(100, self.canvas.winfo_height())
        vw, vh = w / self.zoom, h / self.zoom
        return self.cx - vw / 2, self.cy - vh / 2, w, h

    def map_to_screen(self, x, y):
        x0, y0, _w, _h = self.view_rect()
        return (x - x0) * self.zoom, (y - y0) * self.zoom

    def screen_to_map(self, sx, sy):
        x0, y0, _w, _h = self.view_rect()
        return x0 + sx / self.zoom, y0 + sy / self.zoom

    def render(self):
        self._render_job = None
        if self.slug is None:
            return
        w = max(100, self.canvas.winfo_width())
        h = max(100, self.canvas.winfo_height())
        x0, y0, _w, _h = self.view_rect()
        src = self.topo if self.topo_var.get() else self.base
        box = (x0, y0, x0 + w / self.zoom, y0 + h / self.zoom)
        try:
            view = src.transform((w, h), Image.Transform.EXTENT, box, Image.Resampling.BILINEAR).convert("RGB")
        except Exception:
            view = Image.new("RGB", (w, h), (11, 15, 20))

        for _label, key, color, alpha in LAYERS:
            mask = getattr(self, key)
            if mask.getbbox() is None:
                continue
            m = mask.transform((w, h), Image.Transform.EXTENT, box, Image.Resampling.NEAREST)
            solid = Image.new("RGB", (w, h), color)
            a = m.point(lambda v: int(v * alpha))
            view = Image.composite(solid, view, a)

        self.photo = ImageTk.PhotoImage(view)
        self.canvas.delete("map")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo, tags="map")
        self.canvas.tag_lower("map")
        self.draw_poly_preview()

    def draw_poly_preview(self):
        self.canvas.delete("poly")
        if not self.poly:
            return
        pts = [self.map_to_screen(x, y) for x, y in self.poly]
        if len(pts) > 1:
            self.canvas.create_line(*[c for p in pts for c in p], fill="#67e8f9", width=2, tags="poly")
        for x, y in pts:
            self.canvas.create_oval(x-3, y-3, x+3, y+3, fill="#67e8f9", outline="", tags="poly")

    # ---------------- tools ----------------
    def set_layer(self, layer):
        self.layer = layer
        label = LABEL_BY_KEY.get(layer, "水域")
        self.layer_var.set(label)
        try:
            self.layer_combo.set(label)
        except Exception:
            pass
        self.update_status()

    def on_layer_combo(self, _event=None):
        label = self.layer_var.get()
        self.set_layer(KEY_BY_LABEL.get(label, "water"))

    def set_tool(self, tool):
        self.tool = tool
        self.tool_var.set(tool)
        if tool != "polygon":
            self.cancel_poly()
        self.update_status()

    def set_brush(self, value):
        self.brush = max(1, int(float(value)))

    def current_mask(self):
        return getattr(self, self.layer)

    def snapshot(self):
        layer = self.layer
        self.undo_stack.append((layer, self.current_mask().tobytes()))
        if len(self.undo_stack) > 40:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self):
        # While drawing a polygon, undo removes the last vertex instead of
        # restoring a mask snapshot (there is no mask yet).
        if self.tool == "polygon" and self.poly:
            self.poly.pop()
            self.draw_poly_preview()
            self.update_status()
            return
        if not self.undo_stack:
            return
        layer, raw = self.undo_stack.pop()
        self.redo_stack.append((layer, getattr(self, layer).tobytes()))
        setattr(self, layer, Image.frombytes("L", (SIZE, SIZE), raw))
        self.dirty = True
        self.render()

    def redo(self):
        if not self.redo_stack:
            return
        layer, raw = self.redo_stack.pop()
        self.undo_stack.append((layer, getattr(self, layer).tobytes()))
        setattr(self, layer, Image.frombytes("L", (SIZE, SIZE), raw))
        self.dirty = True
        self.render()

    def on_left_down(self, e):
        self.canvas.focus_set()
        if self.tool == "polygon":
            self.poly.append(self.screen_to_map(e.x, e.y))
            self.draw_poly_preview()
            self.update_status()
        else:
            self.snapshot()
            self.dirty = True
            self.last_brush = (e.x, e.y)
            self.paint_at(e.x, e.y)

    def on_left_move(self, e):
        if self.tool in ("brush", "erase") and self.last_brush is not None:
            self.paint_at(e.x, e.y, self.last_brush)
            self.last_brush = (e.x, e.y)

    def on_left_up(self, _e):
        self.last_brush = None

    def paint_at(self, sx, sy, prev=None):
        mask = self.current_mask()
        draw = ImageDraw.Draw(mask)
        x, y = self.screen_to_map(sx, sy)
        value = 0 if self.tool == "erase" else 255
        r = self.brush
        if prev is not None:
            px, py = self.screen_to_map(*prev)
            draw.line([(px, py), (x, y)], fill=value, width=max(1, r * 2))
        draw.ellipse([x - r, y - r, x + r, y + r], fill=value)
        self.render()
        self.update_status(x, y)

    def on_double(self, e):
        if self.tool == "polygon":
            # A double click also generates two Button-1 events; drop the
            # duplicate last vertex before closing.
            if len(self.poly) >= 2:
                x, y = self.screen_to_map(e.x, e.y)
                lx, ly = self.poly[-1]
                if (x - lx) ** 2 + (y - ly) ** 2 < 25:
                    self.poly.pop()
            self.close_poly()

    def close_poly(self, _event=None):
        if self.tool != "polygon":
            return
        if len(self.poly) < 3:
            self.status.config(text="多边形至少需要 3 个点")
            return
        self.snapshot()
        ImageDraw.Draw(self.current_mask()).polygon(self.poly, fill=255)
        self.dirty = True
        self.poly = []
        self.render()
        self.update_status()
        self.status.config(text="多边形已闭合并写入当前图层；可撤销。")

    def cancel_poly(self):
        self.poly = []
        self.draw_poly_preview()

    def copy_water_to_boat(self):
        if not messagebox.askyesno("生成舰艇航道", "用当前水域掩膜覆盖水面舰艇航道？\n覆盖后可以继续增删修改。"):
            return
        self.set_layer("boat")
        self.snapshot()
        self.boat = self.water.copy()
        self.dirty = True
        self.render()
        self.status.config(text="已从水域复制到舰艇航道；现在可以继续修改。")

    def clear_layer(self):
        if not messagebox.askyesno("清空图层", f"清空当前地图的{'水域' if self.layer == 'water' else '桥梁'}标记？"):
            return
        self.snapshot()
        setattr(self, self.layer, Image.new("L", (SIZE, SIZE), 0))
        self.dirty = True
        self.render()

    # ---------------- pan / zoom ----------------
    def on_pan_start(self, e):
        self.pan = (e.x, e.y, self.cx, self.cy)
        self.canvas.config(cursor="fleur")

    def on_pan_move(self, e):
        if not self.pan:
            return
        sx, sy, cx0, cy0 = self.pan
        self.cx = cx0 - (e.x - sx) / self.zoom
        self.cy = cy0 - (e.y - sy) / self.zoom
        self.render()

    def on_pan_end(self, _e):
        self.pan = None
        self.canvas.config(cursor="crosshair")

    def on_wheel(self, e):
        if not self.slug:
            return
        mx, my = self.screen_to_map(e.x, e.y)
        factor = 1.18 if e.delta > 0 else 1 / 1.18
        self.zoom = max(0.35, min(8.0, self.zoom * factor))
        self.cx = mx - (e.x - self.canvas.winfo_width() / 2) / self.zoom
        self.cy = my - (e.y - self.canvas.winfo_height() / 2) / self.zoom
        self.render()

    # ---------------- save ----------------
    def save_current(self, silent=False):
        if self.slug is None:
            return True
        if not self.dirty:
            if not silent:
                self.update_status()
            return True
        mp = OUT / self.slug
        try:
            mp.mkdir(parents=True, exist_ok=True)
            for name, img in (("water.png", self.water), ("bridge.png", self.bridge),
                              ("clear_road.png", self.clear), ("obstacle.png", self.obstacle),
                              ("boat.png", self.boat)):
                tmp = mp / (name + ".tmp")
                img.save(tmp, format="PNG", optimize=True)
                tmp.replace(mp / name)
            # Boundary is optional: no file means "entire map is inside".
            bp = mp / "boundary.png"
            if self.boundary.getbbox() is not None:
                tmp = mp / "boundary.png.tmp"
                self.boundary.save(tmp, format="PNG", optimize=True)
                tmp.replace(bp)
            elif bp.exists():
                bp.unlink()
            meta = {
                "slug": self.slug,
                "label": next(m["label"] for m in self.maps if m["slug"] == self.slug),
                "size": SIZE,
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": "annotate_maps.py/2",
                "layers": ["water", "bridge", "boundary", "clear", "obstacle", "boat"],
            }
            (mp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            self.dirty = False
            self._fill_map_list()
            if not silent:
                self.status.config(text=f"已保存 {mp}  ({time.strftime('%H:%M:%S')})")
            return True
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return False

    def on_close(self):
        if not self.save_current(silent=True):
            return
        self.destroy()

    def update_status(self, x=None, y=None):
        if not self.slug:
            return
        m = next((m for m in self.maps if m["slug"] == self.slug), None)
        pos = ""
        if x is not None and y is not None:
            pos = f" · 像素 {int(x)}, {int(y)}"
        self.status.config(text=f"{m['label'] if m else ''} · {SIZE}×{SIZE} · 缩放 {self.zoom:.2f} · "
                                f"图层 {LABEL_BY_KEY.get(self.layer, self.layer)} · 工具 {self.tool}{pos}")

    def on_motion(self, e):
        if self.slug:
            x, y = self.screen_to_map(e.x, e.y)
            self.update_status(x, y)


if __name__ == "__main__":
    app = Annotator()
    app.mainloop()
