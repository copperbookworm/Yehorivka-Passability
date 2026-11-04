# -*- coding: utf-8 -*-
"""地图数据：底图 + 1024 网格高程（米）。与页面 hAtM 同口径。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

import core
import paths

DATA = paths.resource_dir() / "data"
N = 1024


class MapData:
    def __init__(self, meta: dict):
        self.meta = meta
        self.slug = meta["slug"]
        self.label = meta["label"]
        self.name = meta["name"]
        self.minx = meta["minx"]
        self.minz = meta["minz"]
        self.spanx = meta["spanx"]
        self.spanz = meta["spanz"]
        self.cellx = meta["cellx"]
        self.cellz = meta["cellz"]
        self.elev_lo = meta["elevLo"]
        self.elev_hi = meta["elevHi"]
        self._basemap = None
        self._elev = None

    # ---- 资源（惰性加载，切图不占内存） ----
    @property
    def basemap_path(self) -> Path:
        return DATA / self.meta["base"]

    @property
    def elev(self) -> np.ndarray:
        if self._elev is None:
            img = Image.open(DATA / self.meta["elev"])
            q = np.asarray(img).astype(np.float32)
            self._elev = self.elev_lo + q / 65535.0 * (self.elev_hi - self.elev_lo)
        return self._elev

    def drop_cache(self):
        self._elev = None
        self._basemap = None

    def basemap(self) -> Image.Image:
        if self._basemap is None:
            from PIL import ImageEnhance

            img = Image.open(self.basemap_path).convert("RGB")
            # 卫星底图偏暗，提亮一点更容易看清网格与地形（不改几何）
            img = ImageEnhance.Brightness(img).enhance(1.32)
            img = ImageEnhance.Contrast(img).enhance(1.06)
            self._basemap = img
        return self._basemap

    # ---- 坐标换算：相对米 <-> 底图像素 ----
    def rel_to_px(self, rx: float, rz: float, size: float) -> tuple[float, float]:
        return rx / self.spanx * size, rz / self.spanz * size

    def px_to_rel(self, px: float, pz: float, size: float) -> tuple[float, float]:
        return px / size * self.spanx, pz / size * self.spanz

    # ---- 高程（双线性，双轴分别换算，和页面一致） ----
    def h_at(self, rx: float, rz: float) -> float:
        fx = rx / self.cellx - 0.5
        fz = rz / self.cellz - 0.5
        fx = min(N - 1.001, max(0.0, fx))
        fz = min(N - 1.001, max(0.0, fz))
        x0 = int(np.floor(fx)); z0 = int(np.floor(fz))
        tx = fx - x0; tz = fz - z0
        e = self.elev
        a = e[z0, x0]; b = e[z0, x0 + 1]
        c = e[z0 + 1, x0]; dd = e[z0 + 1, x0 + 1]
        return (a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + dd * tx) * tz

    def grid_at(self, rx: float, rz: float) -> str:
        return core.text_at(rx, rz, 3)

    def locate(self, grid_text: str) -> tuple[float, float]:
        """网格文本 -> 相对米（格心）。越界时抛 ParseError。"""
        rx, rz = core.parse(grid_text)
        if rx > self.spanx + 1 or rz > self.spanz + 1:
            raise core.ParseError(f"{grid_text} 不在这张地图上（图幅 {self.spanx:.0f}×{self.spanz:.0f} m）")
        return min(rx, self.spanx), min(rz, self.spanz)


def load_maps() -> list[MapData]:
    return [MapData(m) for m in json.loads((DATA / "maps.json").read_text(encoding="utf-8"))]
