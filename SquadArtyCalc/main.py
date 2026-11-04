# -*- coding: utf-8 -*-
"""Squad 间瞄火力助手（桌面版）

流程：选地图/武器 -> 在地图上点出阵地 -> 框选游戏里 CURRENT GRID 区域
     -> 按识别热键（默认 F1，可自定义/组合键）截屏识图 -> 浮窗显示 阵地/目标/方位/密位。
"""
from __future__ import annotations

import ctypes
import json
import math
import random
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction, QColor, QFont, QGuiApplication, QImage, QKeySequence, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QKeySequenceEdit, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

import core
import mapsdata
import paths
import vision
from overlay import Overlay

HERE = paths.app_dir()                 # exe（或 main.py）所在目录：自测截图/日志写这里
CONFIG_PATH = paths.config_path()      # 便携优先，程序目录不可写时退回 LOCALAPPDATA
# 开发机便利：源码运行时可从本地密钥文件预填；打包后的 exe 绝不读它
KEY_FALLBACK = None  # 公开版不预填任何密钥；密钥由界面输入并保存在 config.json

PEND_TOTAL = 3          # 目标坐标连截几次（取三点外心）
CIRCUM_NEAR = 60.0          # 外心离三点重心的最小容忍距离（米）
CIRCUM_SPAN_K = 6.0         # 容忍距离的另一半：最多是三点最大边长的这么多倍

# ---- 剪贴板：Qt 在 Windows 上连续写会偶发 CLIPBRD_E_CANT_OPEN（OleSetClipboard 0x800401d0），
#      所以直接用 Win32 API 写，失败再退回 Qt。两个都失败时只是不复制，不影响计算。 ----
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_w32 = None


def _win32_clipboard():
    """惰性初始化 Win32 剪贴板 API（只在 Windows 上可用）。"""
    global _w32
    if _w32 is not None:
        return _w32
    if sys.platform != "win32":
        _w32 = False
        return _w32
    try:
        from ctypes import wintypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.restype = wintypes.BOOL
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k.GlobalFree.argtypes = [ctypes.c_void_p]
        _w32 = (u, k)
    except Exception as e:            # 极老/受限的系统，退回 Qt
        print("[clip] Win32 剪贴板不可用:", e)
        _w32 = False
    return _w32


def set_clipboard_text(text: str, tries: int = 10) -> bool:
    """把 text 写进系统剪贴板（别人正占用时重试 ~0.5 s）。"""
    api = _win32_clipboard()
    if api:
        u, k = api
        for _ in range(tries):
            if u.OpenClipboard(None):
                try:
                    u.EmptyClipboard()
                    data = text.encode("utf-16-le") + b"\x00\x00"
                    h = k.GlobalAlloc(_GMEM_MOVEABLE, len(data))
                    if not h:
                        return False
                    p = k.GlobalLock(h)
                    ctypes.memmove(p, data, len(data))
                    k.GlobalUnlock(h)
                    if not u.SetClipboardData(_CF_UNICODETEXT, h):
                        k.GlobalFree(h)
                        return False
                    # 让系统接管数据，程序退出后剪贴板内容仍然有效
                    try:
                        ctypes.windll.ole32.OleFlushClipboard()
                    except Exception:
                        pass
                    return True
                finally:
                    u.CloseClipboard()
            time.sleep(0.05)
        print("[clip] 剪贴板被别的程序占用，重试后仍失败")
    try:                              # Qt 兜底（非 Windows 或 Win32 初始化失败时）
        QApplication.clipboard().setText(text)
        return QApplication.clipboard().text() == text
    except Exception as e:
        print("[clip] 复制失败:", e)
        return False


MODE_TEXT = {
    "circum": "三点外心",
    "centroid": "三点重心",
    "mid": "两点中点",
    "single1": "单次定位",
    "single3": "三次一致",
    "single2": "两次一致",
    "grid": "坐标",
}


def combine_targets(pts):
    '''把 2~3 个截到的坐标合成一个目标点。

    pts: [(rx, rz, 原文), ...]
    返回 (target, mode, msg)：
      * 三点互不相同 -> 三点外心（近似共线时退回三点重心）
      * 有两点的坐标相同 -> 相同的那两个算一个，与剩下的取中点
      * 三个都相同 -> 按单点
    '''
    uniq = []
    for q in pts:
        if not any(abs(q[0] - u[0]) < 1e-6 and abs(q[1] - u[1]) < 1e-6 for u in uniq):
            uniq.append(q)
    codes = [q[2] for q in pts]

    if len(uniq) >= 3:
        (x1, z1), (x2, z2), (x3, z3) = [(u[0], u[1]) for u in uniq[:3]]
        d = 2.0 * (x1 * (z2 - z3) + x2 * (z3 - z1) + x3 * (z1 - z2))
        cx = cz = None
        if abs(d) > 1e-6:
            a1, a2, a3 = x1 * x1 + z1 * z1, x2 * x2 + z2 * z2, x3 * x3 + z3 * z3
            cx = (a1 * (z2 - z3) + a2 * (z3 - z1) + a3 * (z1 - z2)) / d
            cz = (a1 * (x3 - x2) + a2 * (x1 - x3) + a3 * (x2 - x1)) / d
        gx, gz = (x1 + x2 + x3) / 3.0, (z1 + z2 + z3) / 3.0
        side = max(math.hypot(x1 - x2, z1 - z2),
                   math.hypot(x2 - x3, z2 - z3),
                   math.hypot(x3 - x1, z3 - z1))
        if cx is not None and math.hypot(cx - gx, cz - gz) <= max(CIRCUM_NEAR, CIRCUM_SPAN_K * side):
            return (cx, cz), "circum", "三点外心：" + " + ".join(codes)
        return (gx, gz), "centroid", "三点近似共线，外心过远，改用重心：" + " + ".join(codes)

    if len(uniq) == 2:
        (x1, z1), (x2, z2) = [(u[0], u[1]) for u in uniq]
        return ((x1 + x2) / 2.0, (z1 + z2) / 2.0), "mid", "两点中点：" + " + ".join(codes)

    u = uniq[0]
    if len(pts) >= 3:
        return (u[0], u[1]), "single3", "{0} 个读数都是 {1}，按单点算".format(len(pts), u[2])
    return (u[0], u[1]), "single2", "两次都是 {0}，按单点算".format(u[2])


# ---------------------------------------------------------------- 配置
def load_config() -> dict:
    cfg = {
        "api_key": "",
        "region": [100, 100, 320, 110],
        "map": "yehorivka",
        "weapon": "Mortar#default",
        "hotkey": "F1",            # 快速定位（截 1 次）
        "hotkey_exact": "F2",      # 准确定位（截 3 次取外心）
        "hotkey_clear": "F3",      # 清除已截坐标
        "hotkey_overlay": "F4",    # 浮窗显隐
        "overlay": None,
        "regions": [],
        "region_name": "",
    }
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    if not cfg.get("api_key") and KEY_FALLBACK is not None and KEY_FALLBACK.exists():
        try:
            cfg["api_key"] = KEY_FALLBACK.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 地图视图
class MapView(QWidget):
    gun_set = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 420)
        self.setCursor(Qt.CrossCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.map: mapsdata.MapData | None = None
        self.img: QImage | None = None
        self.scale = 0.1          # 屏幕像素 / 米
        self.ox = 0.0
        self.oy = 0.0
        self.gun = None           # (rx, rz)
        self.target = None        # (rx, rz)
        self._press = None
        self._moved = False
        self._fit_pending = True
        self._user_zoom = False

    # --- 坐标 ---
    def w2s(self, rx, rz):
        return self.ox + rx * self.scale, self.oy + rz * self.scale

    def s2w(self, sx, sy):
        return (sx - self.ox) / self.scale, (sy - self.oy) / self.scale

    # --- 地图 ---
    def set_map(self, m: mapsdata.MapData):
        self.map = m
        img = m.basemap()
        self.img = QImage(img.tobytes(), img.width, img.height, QImage.Format_RGB888).copy()
        self._fit_pending = True
        self._user_zoom = False
        self.gun = None
        self.target = None
        self.fit()
        self.update()

    def fit(self):
        if not self.map:
            return
        s = min(self.width() / self.map.spanx, self.height() / self.map.spanz) * 0.98
        self.scale = max(0.01, s)
        self.ox = (self.width() - self.map.spanx * self.scale) / 2
        self.oy = (self.height() - self.map.spanz * self.scale) / 2
        self._fit_pending = False
        self.update()

    def zoom_at(self, sx, sy, factor):
        if not self.map:
            return
        rx, rz = self.s2w(sx, sy)
        self._user_zoom = True
        self.scale = min(max(self.scale * factor, 0.02), 6.0)
        self.ox = sx - rx * self.scale
        self.oy = sy - rz * self.scale
        self.update()

    # --- 绘制 ---
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(10, 12, 16))
        if not self.map or self.img is None:
            p.setPen(QColor(120, 130, 145))
            p.setFont(QFont("Microsoft YaHei UI", 12))
            p.drawText(self.rect(), Qt.AlignCenter, "选择地图后在这里点击设置阵地")
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        iw, ih = self.img.width(), self.img.height()
        kw = self.map.spanx * self.scale / iw
        kh = self.map.spanz * self.scale / ih
        dst = QRectF(self.ox, self.oy, iw * kw, ih * kh)
        src = QRectF(0, 0, iw, ih)
        p.drawImage(dst, self.img, src)

        # 炮位 / 目标
        if self.gun:
            gx, gy = self.w2s(*self.gun)
            if self.target:
                tx, ty = self.w2s(*self.target)
                pen = QPen(QColor(255, 178, 90, 190), 2, Qt.DashLine)
                p.setPen(pen)
                p.drawLine(QPointF(gx, gy), QPointF(tx, ty))
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(90, 220, 240))
                p.drawEllipse(QPointF(tx, ty), 6, 6)
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(90, 220, 240), 1.5))
                p.drawEllipse(QPointF(tx, ty), 12, 12)
                self._tag(p, tx + 12, ty - 26, "目标 " + self.map.grid_at(*self.target), QColor(90, 220, 240))
            p.setPen(QPen(QColor(255, 209, 102), 2))
            p.setBrush(QColor(255, 209, 102))
            p.drawEllipse(QPointF(gx, gy), 6, 6)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(gx, gy), 13, 13)
            self._tag(p, gx + 12, gy + 6,
                      f"阵地 {self.map.grid_at(*self.gun)} · {self.map.h_at(*self.gun):.0f} m",
                      QColor(255, 209, 102))

        # 比例尺
        self._scalebar(p)

    def _tag(self, p: QPainter, x, y, text, color):
        f = QFont("Microsoft YaHei UI", 9)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(text) + 14
        r = QRectF(x, y, w, 22)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 12, 18, 210))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(color)
        p.drawText(r, Qt.AlignCenter, text)

    def _scalebar(self, p: QPainter):
        target_px = 110.0
        meters = target_px / self.scale
        nice = 100
        for cand in (50, 100, 200, 250, 500, 1000, 2000, 5000):
            if cand <= meters * 1.6:
                nice = cand
        length = nice * self.scale
        x = self.width() - length - 24
        y = self.height() - 26
        p.setPen(QPen(QColor(255, 255, 255, 200), 2))
        p.drawLine(QPointF(x, y), QPointF(x + length, y))
        p.drawLine(QPointF(x, y - 4), QPointF(x, y + 4))
        p.drawLine(QPointF(x + length, y - 4), QPointF(x + length, y + 4))
        p.setPen(QColor(235, 240, 245))
        p.setFont(QFont("Microsoft YaHei UI", 9))
        label = f"{nice} m" if nice < 1000 else f"{nice / 1000:g} km"
        p.drawText(QRectF(x, y - 22, length, 18), Qt.AlignCenter, label)

    # --- 交互 ---
    def wheelEvent(self, ev):
        self.zoom_at(ev.position().x(), ev.position().y(), 1.25 if ev.angleDelta().y() > 0 else 0.8)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.position()
            self._moved = False
        elif ev.button() == Qt.RightButton and self.gun:
            self.gun = None
            self.update()
            self.gun_set.emit(-1.0, -1.0)

    def mouseMoveEvent(self, ev):
        if self._press is None or not self.map:
            return
        pos = ev.position()
        dx = pos.x() - self._press.x()
        dy = pos.y() - self._press.y()
        if not self._moved and abs(dx) + abs(dy) > 4:
            self._moved = True
        if self._moved:
            self.ox += dx
            self.oy += dy
            self._press = pos
            self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.LeftButton or self._press is None:
            return
        if not self._moved and self.map:
            rx, rz = self.s2w(ev.position().x(), ev.position().y())
            if 0 <= rx <= self.map.spanx and 0 <= rz <= self.map.spanz:
                self.gun = (rx, rz)
                self.gun_set.emit(rx, rz)
                self.update()
        self._press = None

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if not self._user_zoom:
            self.fit()


# ---------------------------------------------------------------- 区域框选
class RegionSelector(QWidget):
    picked = Signal(list)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        geo = QRect()
        for s in QGuiApplication.screens():
            geo = geo.united(s.geometry())
        self._geo = geo
        self.setGeometry(geo)
        self._start = None
        self._cur = None

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Microsoft YaHei UI", 12))
        p.drawText(QRect(0, 24, self.width(), 30), Qt.AlignCenter,
                   "拖拽框选游戏里 CURRENT GRID 那一小块（只框坐标文字）· 右键取消")
        if self._start and self._cur:
            r = QRectF(QPointF(self._start), QPointF(self._cur)).normalized()
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(r, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setPen(QPen(QColor(255, 178, 90), 2))
            p.drawRect(r)
            p.setPen(QColor(255, 209, 102))
            p.drawText(r.adjusted(4, -22, 0, 0), Qt.AlignLeft, f"{int(r.width())} × {int(r.height())}")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.RightButton:
            self.close()
            return
        self._start = ev.position().toPoint()
        self._cur = self._start
        self.update()

    def mouseMoveEvent(self, ev):
        if self._start:
            self._cur = ev.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, ev):
        if not self._start or not self._cur:
            return
        r = QRect(QPoint(self._start), QPoint(self._cur)).normalized()
        self.close()
        if r.width() < 8 or r.height() < 8:
            return
        dpr = (QGuiApplication.screenAt(r.center()) or QGuiApplication.primaryScreen()).devicePixelRatio()
        gx, gy = r.x() + self._geo.x(), r.y() + self._geo.y()
        self.picked.emit([round(gx * dpr), round(gy * dpr), round(r.width() * dpr), round(r.height() * dpr)])


# ---------------------------------------------------------------- 演练窗（假 HUD）
class DrillWindow(QWidget):
    """把一块假的 CURRENT GRID 摆进识别区域，用来端到端自测（不需要开游戏）。"""

    def __init__(self, region_phys, text, dpr):
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        x, y, w, h = region_phys
        self.setGeometry(round(x / dpr), round(y / dpr), max(60, round(w / dpr)), max(40, round(h / dpr)))
        self.text = text
        self._bg = self._make_bg()

    def _make_bg(self):
        pm = QPixmap(self.width(), self.height())
        p = QPainter(pm)
        for i in range(0, self.height(), 14):
            c = QColor(96, 104, 66) if (i // 14) % 2 else QColor(112, 118, 78)
            p.fillRect(0, i, self.width(), 14, c)
        p.end()
        return pm

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)
        f = QFont("Arial", max(9, int(self.height() * 0.16)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(0, 0, 0))
        p.drawText(self.rect().adjusted(3, 0, 3, 0), Qt.AlignHCenter | Qt.AlignTop, "CURRENT GRID")
        f2 = QFont("Arial", max(10, int(self.height() * 0.2)))
        f2.setBold(True)
        p.setFont(f2)
        p.drawText(self.rect().adjusted(3, 0, 3, 0), Qt.AlignCenter, self.text)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._bg = self._make_bg()


# ---------------------------------------------------------------- 全局热键
class Hotkeys(QThread):
    """四个可自定义热键：1=快速定位 2=准确定位 3=清除坐标 4=浮窗显隐（都支持 Ctrl/Shift/Alt/Win 组合）。"""

    pressed = Signal(int)
    failed = Signal(int, int, int)      # hid, 错误码, 热键描述（用 err 常量给界面）

    def __init__(self, bindings, parent=None):
        super().__init__(parent)
        self._tid = 0
        self._stop = False
        self.bindings = [tuple(b) for b in bindings]

    def run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._tid = kernel32.GetCurrentThreadId()
        bindings = list(self.bindings)
        for hid, mods, vk in bindings:
            if not user32.RegisterHotKey(None, hid, mods, vk):
                err = kernel32.GetLastError()
                print(f"[hotkey] id{hid} mods={mods:#x} vk={vk:#x} 注册失败（可能被别的程序占用），错误码 {err}", flush=True)
                self.failed.emit(hid, err, mods)
        msg = ctypes.wintypes.MSG() if hasattr(ctypes, "wintypes") else None
        if msg is None:
            import ctypes.wintypes as wt

            msg = wt.MSG()
        while not self._stop:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == 0x0312:
                self.pressed.emit(int(msg.wParam))
        for hid, _mods, _vk in bindings:
            user32.UnregisterHotKey(None, hid)

    def stop(self):
        self._stop = True
        if self._tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass


# ---------------------------------------------------------------- 识别线程
class OcrWorker(QThread):
    ok = Signal(str, str, int)
    fail = Signal(str)

    def __init__(self, region, key, img=None, parent=None):
        super().__init__(parent)
        self.region = region
        self.key = key
        self.img = img

    def run(self):
        try:
            img = self.img if self.img is not None else vision.grab(tuple(self.region))
            coord, raw, ms = vision.ask(img, self.key)
            self.ok.emit(coord or "", raw, ms)
        except Exception as e:      # noqa: BLE001
            self.fail.emit(str(e))


# ---------------------------------------------------------------- 热键换算
# RegisterHotKey 用的 VK 码
_VK_SPECIAL = {
    Qt.Key_Space: 0x20,
    Qt.Key_Tab: 0x09,
    Qt.Key_Return: 0x0D,
    Qt.Key_Enter: 0x0D,
    Qt.Key_Insert: 0x2D,
    Qt.Key_Delete: 0x2E,
    Qt.Key_Home: 0x24,
    Qt.Key_End: 0x23,
    Qt.Key_PageUp: 0x21,
    Qt.Key_PageDown: 0x22,
    Qt.Key_Pause: 0x13,
    Qt.Key_Print: 0x2C,
    Qt.Key_ScrollLock: 0x91,
    Qt.Key_Backspace: 0x08,
}


def seq_to_hotkey(seq: QKeySequence):
    """QKeySequence -> (mods, vk)。不可用（空/纯修饰键/不支持的键）返回 None。"""
    if seq is None or seq.isEmpty():
        return None
    combo = seq[0]
    key = combo.key()
    km = combo.keyboardModifiers()

    mods = 0
    if km & Qt.ControlModifier:
        mods |= 0x0002      # MOD_CONTROL
    if km & Qt.ShiftModifier:
        mods |= 0x0004      # MOD_SHIFT
    if km & Qt.AltModifier:
        mods |= 0x0001      # MOD_ALT
    if km & Qt.MetaModifier:
        mods |= 0x0008      # MOD_WIN

    vk = None
    if Qt.Key_A <= key <= Qt.Key_Z:
        vk = 0x41 + (key - Qt.Key_A)
    elif Qt.Key_0 <= key <= Qt.Key_9:
        vk = 0x30 + (key - Qt.Key_0)
    elif Qt.Key_F1 <= key <= Qt.Key_F24:
        vk = 0x70 + (key - Qt.Key_F1)
    else:
        vk = _VK_SPECIAL.get(key)
    if vk is None:
        return None
    return mods, vk


def hotkey_to_text(text: str) -> str:
    """配置里的字符串规范成给人看的写法（F1 / Ctrl+Shift+D）。"""
    seq = QKeySequence(text)
    if seq.isEmpty():
        return "F1"
    return seq.toString(QKeySequence.NativeText) or text


def parse_hotkey_text(text: str):
    seq = QKeySequence(text or "F1")
    got = seq_to_hotkey(seq)
    if got is None:
        got = seq_to_hotkey(QKeySequence("F1"))
    return got, (seq.toString(QKeySequence.PortableText) if got else "F1")


# 四个热键：(热键 id, 配置键, 默认键)
HOTKEY_DEFS = (
    (1, "hotkey", "F1"),           # 快速定位：截 1 次直接当目标
    (2, "hotkey_exact", "F2"),     # 准确定位：截 3 次取三点外心
    (3, "hotkey_clear", "F3"),     # 清除已截但还没算成目标的坐标
    (4, "hotkey_overlay", "F4"),   # 浮窗显隐
)


# ---------------------------------------------------------------- 主窗口
class MainWindow(QMainWindow):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.maps = mapsdata.load_maps()
        self.weapons = core.load_weapons()
        self.gun = None
        self.target = None
        self.sol = None
        self.pend = []             # 已截但还没凑齐的坐标 [(rx, rz, 原文), ...]，最多 PEND_TOTAL 个
        self.last_target = None    # 上一次算好的 (目标, 模式)，供「清除坐标」恢复
        self.tgt_mode = None       # None | circum/centroid（三点外心/共线退回重心）
                                   #      | mid（两点中点）| single2 / single3 | grid（手输/自测）
        self.copied = ""           # 最近一次自动复制到剪贴板的坐标（空 = 没复制成功）
        self.worker = None
        self.drill = None
        self.hotkeys = None
        self.ocr_mode = "quick"        # quick=截 1 次；exact=截 3 次取外心
        self.hk_specs = {}
        self.hk_texts = {}
        for _hid, _ckey, _dflt in HOTKEY_DEFS:
            _spec, _text = parse_hotkey_text(cfg.get(_ckey, _dflt))
            self.hk_specs[_hid] = _spec
            self.hk_texts[_hid] = _text
        self.setWindowTitle("Squad 间瞄火力助手")
        scr = QGuiApplication.primaryScreen().availableGeometry()
        w = min(1260, scr.width() - 40)
        h = min(860, scr.height() - 60)
        self.resize(w, h)
        self.move(scr.left() + (scr.width() - w) // 2, scr.top() + max(8, (scr.height() - h) // 2))

        self.view = MapView()
        self.view.gun_set.connect(self.on_gun_set)
        self._build_panel()

        body = QWidget()
        lay = QHBoxLayout(body)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.panel, 0)
        self.setCentralWidget(body)

        self.overlay = Overlay()
        if cfg.get("overlay"):
            self.overlay.move(*cfg["overlay"])
        else:
            self.overlay.reset_pos()
        self.overlay.moved_by_user.connect(self.on_overlay_moved)
        self.overlay.show()
        self.overlay.set_values("—", "—", "—", "—", "先在地图上点出阵地")

        self._start_hotkeys()

        self.select_map(cfg.get("map", "yehorivka"))
        self.select_weapon(cfg.get("weapon", "Mortar#default"))
        # 初始化时 combo 信号被屏蔽过，这里补一次，确保地图真的加载
        self.on_map_changed(self.cb_map.currentIndex())
        self.on_weapon_changed(self.cb_weap.currentIndex())

    # ---------- 面板 ----------
    def _build_panel(self):
        self.panel = QFrame()
        self.panel.setFixedWidth(404)
        self.panel.setStyleSheet(
            "QFrame{background:#11161d;} QLabel{color:#c7d2de;}"
            "QGroupBox{color:#ffd166;border:1px solid #24303d;border-radius:8px;margin-top:10px;padding:10px 8px 8px 8px;}"
            "QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}"
            "QComboBox,QLineEdit{background:#161d26;color:#e8eef5;border:1px solid #2b3947;border-radius:5px;padding:4px 6px;}"
            "QPushButton{background:#1b2430;color:#dce6f0;border:1px solid #2f3d4c;border-radius:6px;padding:6px 8px;}"
            "QPushButton:hover{background:#22303f;border-color:#3b7f96;}"
            "QPushButton#primary{background:#133240;border-color:#2f7d94;color:#9fe6f5;}"
            "QPushButton#danger{background:#3a1416;border-color:#8a2d2d;color:#ff9a9a;font-weight:600;}"
            "QPushButton#danger:hover{background:#4d1a1d;border-color:#c23c3c;color:#ffc7c7;}"
            # 下拉列表（弹出层是独立窗口，必须单独上色，否则是黑底黑字）
            "QComboBox QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;"
            "outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}"
            "QComboBox QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}"
            "QComboBox QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}"
            "QComboBox QAbstractItemView::item:hover{background:#1a3a49;color:#ffffff;}"
        )
        # 面板内容放进滚动区：小屏幕/低分辨率下也不会把底部的「退出程序」挤没
        self.inner = QWidget()
        self.inner.setStyleSheet("background:transparent;")
        v = QVBoxLayout(self.inner)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(8)

        title = QLabel("间瞄火力计算")
        title.setStyleSheet("font-size:17px;font-weight:600;color:#ffd166;")
        v.addWidget(title)
        self.lb_sub = QLabel(self._sub_text())
        self.lb_sub.setStyleSheet("color:#7f909f;font-size:11px;")
        v.addWidget(self.lb_sub)

        # —— 密钥
        gb_key = QGroupBox("DeepSeek 密钥")
        g = QVBoxLayout(gb_key)
        row = QHBoxLayout()
        self.ed_key = QLineEdit(self.cfg.get("api_key", ""))
        self.ed_key.setEchoMode(QLineEdit.Password)
        self.ed_key.setPlaceholderText("sk-...")
        btn_save_key = QPushButton("保存")
        btn_save_key.clicked.connect(self.save_key)
        row.addWidget(self.ed_key, 1)
        row.addWidget(btn_save_key, 0)
        g.addLayout(row)
        self.lb_key = QLabel("模型 deepseek-flash（V4.1-Flash，读图）")
        self.lb_key.setStyleSheet("color:#7f909f;font-size:11px;")
        g.addWidget(self.lb_key)
        v.addWidget(gb_key)

        # —— 区域
        gb_reg = QGroupBox("识别区域（游戏里的 CURRENT GRID）")
        g = QVBoxLayout(gb_reg)
        self.lb_region = QLabel()
        self.lb_region.setStyleSheet("font-family:Consolas;color:#9fe6f5;")
        g.addWidget(self.lb_region)
        row = QHBoxLayout()
        b1 = QPushButton("框选区域")
        b1.clicked.connect(self.pick_region)
        b2 = QPushButton("预览截图")
        b2.clicked.connect(self.preview_region)
        b3 = QPushButton("预览识别")
        b3.setObjectName("primary")
        b3.clicked.connect(self.test_ocr_region)
        row.addWidget(b1); row.addWidget(b2); row.addWidget(b3)
        g.addLayout(row)
        row2 = QHBoxLayout()
        b4 = QPushButton("演练（假 HUD，端到端自测）")
        b4.clicked.connect(self.open_drill)
        row2.addWidget(b4)
        g.addLayout(row2)

        # —— 区域预设（保存 / 重命名 / 套用）
        rowp = QHBoxLayout()
        rowp.addWidget(QLabel("区域预设"), 0)
        self.cb_region = QComboBox()
        self.cb_region.setToolTip("保存过的框选区域，选中就套用")
        self.cb_region.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_region.setMinimumContentsLength(4)
        self.cb_region.activated.connect(self.on_region_preset)
        self.cb_region.view().setStyleSheet(
            "QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;"
            "outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}"
            "QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}"
            "QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}"
        )
        rowp.addWidget(self.cb_region, 1)
        g.addLayout(rowp)
        rowp2 = QHBoxLayout()
        bp1 = QPushButton("保存当前")
        bp1.setToolTip("把现在的框选区域存成预设，可命名")
        bp1.clicked.connect(self.save_region_preset)
        bp2 = QPushButton("重命名")
        bp2.clicked.connect(self.rename_region_preset)
        bp3 = QPushButton("删除")
        bp3.clicked.connect(self.delete_region_preset)
        rowp2.addWidget(bp1); rowp2.addWidget(bp2); rowp2.addWidget(bp3)
        g.addLayout(rowp2)

        # —— 四个热键（都可自定义，支持组合键）
        self.qk_edits = {}
        for hid, label, tip in (
            (1, "快速定位", "截 1 次，直接把读到的坐标当目标"),
            (2, "准确定位", "截 3 次，取三点外心当目标"),
            (3, "清除坐标", "清掉截错/没凑齐的坐标（上次算好的目标保留）"),
            (4, "浮窗显隐", "显示 / 隐藏半透明浮窗"),
        ):
            row3 = QHBoxLayout()
            lb3 = QLabel(label)
            lb3.setMinimumWidth(58)
            lb3.setToolTip(tip)
            row3.addWidget(lb3, 0)
            ed = QKeySequenceEdit(QKeySequence(self.hk_texts[hid]))
            ed.setMaximumSequenceLength(1)
            ed.setToolTip("点一下这里，然后按下要用的键或组合键（如 F5、Ctrl+Shift+D、Alt+Q）")
            ed.editingFinished.connect(lambda h=hid: self.apply_hotkey(h))
            row3.addWidget(ed, 1)
            g.addLayout(row3)
            self.qk_edits[hid] = ed
        self.lb_hk = QLabel(self._hk_hint())
        self.lb_hk.setWordWrap(True)
        self.lb_hk.setStyleSheet("color:#7f909f;font-size:11px;")
        g.addWidget(self.lb_hk)
        v.addWidget(gb_reg)

        # —— 地图/武器
        gb_alg = QGroupBox("地图与武器")
        g = QGridLayout(gb_alg)
        g.addWidget(QLabel("地图"), 0, 0)
        self.cb_map = QComboBox()
        self.cb_map.currentIndexChanged.connect(self.on_map_changed)
        g.addWidget(self.cb_map, 0, 1, 1, 2)
        g.addWidget(QLabel("武器"), 1, 0)
        self.cb_weap = QComboBox()
        self.cb_weap.currentIndexChanged.connect(self.on_weapon_changed)
        g.addWidget(self.cb_weap, 1, 1, 1, 2)
        self.cb_map.blockSignals(True)
        self.cb_weap.blockSignals(True)
        for i, m in enumerate(self.maps):
            self.cb_map.addItem(m.label, i)
        for w in self.weapons:
            self.cb_weap.addItem(w["name"], w["key"])
        self.cb_map.blockSignals(False)
        self.cb_weap.blockSignals(False)
        for cb in (self.cb_map, self.cb_weap):
            # 下拉框的最小宽度会随最长条目膨胀（26 个武器 → 323 px），把面板撑出滚动区
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(4)
            # 双保险：直接给弹出列表本身设样式，Windows 上才稳定是浅字
            cb.view().setStyleSheet(
                "QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;"
                "outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}"
                "QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}"
                "QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}"
            )
        g.setColumnStretch(1, 1)
        v.addWidget(gb_alg)

        # —— 结果
        gb_res = QGroupBox("诸元")
        g = QGridLayout(gb_res)
        self.vals = {}
        for row, (key, label) in enumerate((("gun", "阵地坐标"), ("tgt", "目标坐标"),
                                            ("brg", "方向角"), ("mil", "密位"))):
            lb = QLabel(label)
            lb.setStyleSheet("color:#8fa1b2;")
            val = QLabel("—")
            val.setStyleSheet("font-family:Consolas;font-size:19px;font-weight:700;color:#fff;")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            g.addWidget(lb, row, 0)
            g.addWidget(val, row, 1)
            self.vals[key] = val
        self.lb_extra = QLabel("")
        self.lb_extra.setStyleSheet("color:#8fa1b2;font-size:11px;")
        self.lb_extra.setWordWrap(True)   # 长串（距离/散布/来源/已复制）自动换行，别把面板撑宽
        g.addWidget(self.lb_extra, 4, 0, 1, 2)
        self.lb_status = QLabel(
            f"先在地图上点一下设阵地，再按 {self.hk_text} 快速定位（或 {self.hk_texts[2]} 准确定位）")
        self.lb_status.setWordWrap(True)
        self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
        g.addWidget(self.lb_status, 5, 0, 1, 2)
        g.setColumnStretch(1, 1)
        v.addWidget(gb_res)

        rowb = QHBoxLayout()
        self.btn_manual = QPushButton(f"快速定位（{self.hk_text}）")
        b5 = self.btn_manual
        b5.setObjectName("primary")
        b5.setToolTip("等同按一次快速定位键：截 1 次直接出结果")
        b5.clicked.connect(lambda: self.start_ocr("quick"))
        self.btn_exact = QPushButton(f"准确定位（{self.hk_texts[2]}）")
        self.btn_exact.setToolTip("等同按准确定位键：截 3 次取三点外心")
        self.btn_exact.clicked.connect(lambda: self.start_ocr("exact"))
        rowb.addWidget(b5); rowb.addWidget(self.btn_exact)
        v.addLayout(rowb)

        rowb2 = QHBoxLayout()
        self.btn_reset_pending = QPushButton("清除已截坐标")
        self.btn_reset_pending.setToolTip(
            "等同按清除坐标键：清掉还没算成目标的坐标，上次算好的目标保留")
        self.btn_reset_pending.setEnabled(False)
        self.btn_reset_pending.clicked.connect(self.clear_pending)
        b6 = QPushButton("浮窗显隐")
        b6.clicked.connect(self.toggle_overlay)
        rowb2.addWidget(self.btn_reset_pending); rowb2.addWidget(b6)
        v.addLayout(rowb2)

        self.lb_tip = QLabel(self._tip_text())
        self.lb_tip.setStyleSheet("color:#6f8090;font-size:11px;")
        self.lb_tip.setWordWrap(True)
        v.addWidget(self.lb_tip)
        self.refresh_hotkey_texts()
        v.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self.inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:0;}")
        outer = QVBoxLayout(self.panel)
        outer.setContentsMargins(4, 4, 4, 6)
        outer.setSpacing(6)
        outer.addWidget(scroll, 1)

        self.btn_quit = QPushButton("退出程序")
        self.btn_quit.setObjectName("danger")
        self.btn_quit.setMinimumHeight(36)
        self.btn_quit.setToolTip("关闭浮窗、注销全局热键并完全退出程序")
        self.btn_quit.clicked.connect(self.quit_app)
        outer.addWidget(self.btn_quit, 0)
        self._refresh_region_combo()
        self.refresh_region_label()

    @property
    def hk_text(self) -> str:
        """旧代码里的 self.hk_text 一律指「快速定位」键。"""
        return self.hk_texts[1]

    # ---------- 选择 ----------
    def select_map(self, slug):
        for i, m in enumerate(self.maps):
            if m.slug == slug:
                self.cb_map.setCurrentIndex(i)
                return
        self.cb_map.setCurrentIndex(0)

    def select_weapon(self, key):
        i = self.cb_weap.findData(key)
        self.cb_weap.setCurrentIndex(max(0, i))

    @property
    def map(self) -> mapsdata.MapData:
        return self.maps[self.cb_map.currentIndex()]

    @property
    def weapon(self) -> dict:
        return self.weapons[self.cb_weap.currentIndex()]

    def on_map_changed(self, _i):
        self.view.set_map(self.map)
        self.cfg["map"] = self.map.slug
        save_config(self.cfg)
        self.gun = None
        self.target = None
        self.sol = None
        self.pend = []
        self.tgt_mode = None
        self.copied = ""        # 换了地图，剪贴板里那个格号就不再对应当前图
        self.recompute()

    def on_weapon_changed(self, _i):
        self.cfg["weapon"] = self.weapon["key"]
        save_config(self.cfg)
        self.recompute()

    # ---------- 阵地 / 计算 ----------
    def on_gun_set(self, rx, rz):
        if rx < 0:
            self.gun = None
            self.recompute()
            return
        self.gun = (rx, rz)
        self.recompute()

    def _commit_target(self, target, mode):
        """确定一个目标坐标：刷新面板/地图，并记住它作为「上一次算好的目标」。"""
        self.target = (max(0.0, min(target[0], self.map.spanx)),
                       max(0.0, min(target[1], self.map.spanz)))
        self.tgt_mode = mode
        self.last_target = (self.target, mode)
        self.view.target = self.target
        self.view.update()
        self._copy_coord()

    def _copy_coord(self):
        """把刚算定的目标坐标自动放进剪贴板（游戏里直接粘贴）。"""
        self.copied = ""
        if not self.target:
            return
        try:
            grid = self.map.grid_at(*self.target)
        except Exception as e:
            print("[clip] 取格号失败:", e)
            return
        if set_clipboard_text(grid):
            self.copied = grid

    def set_target_grid(self, text: str) -> bool:
        try:
            rx, rz = core.parse(text)
        except core.ParseError as e:
            self.lb_status.setText(str(e))
            return False
        if rx > self.map.spanx + 1 or rz > self.map.spanz + 1:
            self.lb_status.setText(f"{text} 不在这张地图上")
            return False
        self.pend = []
        self._commit_target((rx, rz), "grid")
        self.recompute()
        return True

    def recompute(self):
        w = self.weapon
        if hasattr(self, "btn_reset_pending"):
            self.btn_reset_pending.setEnabled(bool(self.pend))
        if not self.gun:
            self.vals["gun"].setText("—")
            self.vals["tgt"].setText("—")
            self.vals["brg"].setText("—")
            self.vals["mil"].setText("—")
            self.lb_extra.setText("")
            self.lb_status.setText("① 在地图上点出阵地（右键可清除）")
            self.overlay.set_values("—", "—", "—", "—", "先在地图上点出阵地")
            self.overlay.set_status("idle", "待命 · 先设阵地")
            return

        hgun = self.map.h_at(*self.gun)
        gun_grid = self.map.grid_at(*self.gun)
        self.vals["gun"].setText(gun_grid)
        gun_txt = f"{gun_grid} · {hgun:.0f} m"

        if not self.target:
            self.vals["tgt"].setText("—")
            self.vals["brg"].setText("—")
            self.vals["mil"].setText("—")
            self.lb_extra.setText("")
            if self.pend:
                got = [self.map.grid_at(q[0], q[1]) for q in self.pend]
                k = len(self.pend)
                marks = "①②③"[:k]
                shown = " + ".join(got)
                self.vals["tgt"].setText(f"{shown} {marks}")
                self.lb_status.setStyleSheet("color:#ffd166;font-size:12px;")
                self.lb_status.setText(
                    f"已记 {k} 个（{shown}）——把准星再挪一点，按 {self.hk_texts[2]} 截第 {k + 1} 个"
                    f"（共 {PEND_TOTAL} 次，取三点外心）"
                )
                self.overlay.set_values(
                    gun_txt, f"{marks[-1]} {got[-1]}（{k}/{PEND_TOTAL}）", "—", "—",
                    f"已记 {shown} · 再按 {self.hk_texts[2]} 截第 {k + 1} 个")
                self.overlay.set_status(
                    "idle", f"{marks} 已记 {got[-1]} · 等第 {k + 1} 个（{k + 1}/{PEND_TOTAL}）")
            else:
                self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
                self.lb_status.setText(
                    f"② 切回游戏：按 {self.hk_text} 快速定位（截 1 次出结果）"
                    f"，或按 {self.hk_texts[2]} 准确定位（截 {PEND_TOTAL} 次取三点外心）")
                self.overlay.set_values(
                    gun_txt, "—", "—", "—",
                    f"{self.hk_text} 快速定位（1 次）· {self.hk_texts[2]} 准确定位（{PEND_TOTAL} 次）")
                self.overlay.set_status(
                    "idle", f"待命 · {self.hk_text} 快速 / {self.hk_texts[2]} 准确")
            return

        htg = self.map.h_at(*self.target)
        res = core.evaluate(w, self.gun, self.target, hgun, htg)
        self.sol = res
        sol = res.get("sol")
        tgt_grid = self.map.grid_at(*self.target)
        tgt_txt = f"{tgt_grid} · {htg:.0f} m"
        self.vals["tgt"].setText(tgt_grid)
        self.vals["brg"].setText(f"{res['brg']:.1f}°")

        if sol:
            unit = core.angle_text(w, sol["deg"])
            self.vals["mil"].setText(f"{sol['mil']:.1f} mil")
            self.lb_extra.setText(
                f"距离 {res['dist']:.0f} m · 高差 {res['dh']:+.0f} m · {res['solName']} · "
                f"飞行 {sol['tof']:.1f} s · 散布 {sol['sprA']:.0f}×{sol['sprB']:.0f} m"
            )
            if w.get("unit") == "mil":
                note = f"飞行 {sol['tof']:.1f} s · 距离 {res['dist']:.0f} m"
            else:
                note = f"射角 {unit} · 飞行 {sol['tof']:.1f} s"
            if res.get("warnMin"):
                note += " · 小于最小射程"
            mode_txt = MODE_TEXT.get(self.tgt_mode, "")
            if mode_txt:
                note += f" · {mode_txt}"
                self.lb_extra.setText(self.lb_extra.text() + f" · {mode_txt}")
            if self.copied:
                self.lb_extra.setText(self.lb_extra.text() + " · 已复制")
            self.overlay.set_values(gun_txt, tgt_txt, f"{res['brg']:.1f}°",
                                    f"{sol['mil']:.1f} mil", note)
            self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
            self.lb_status.setText(
                f"{res['solName']}射角 {unit} · 距离 {res['dist']:.0f} m · 方位 {res['brg']:.1f}°"
            )
            self.overlay.set_status(
                "done", f"计算完成 · {tgt_grid}" + (" · 已复制" if self.copied else ""))
        else:
            why = res.get("err") or "无有效解"
            self.vals["mil"].setText("—")
            self.lb_extra.setText(f"距离 {res['dist']:.0f} m · 高差 {res['dh']:+.0f} m")
            self.overlay.set_values(gun_txt, tgt_txt, f"{res['brg']:.1f}°", "不可达", why[:22], bad=True)
            self.lb_status.setStyleSheet("color:#ff8f8f;font-size:12px;")
            self.lb_status.setText(f"打不到：{why}（有效射程 {self.range_text(w)}）")
            self.overlay.set_status("error", f"计算失败 · {why}"[:28])

    def range_text(self, w) -> str:
        lo, hi = None, None
        d = max(4.0, (w.get("minD") or 0) - 40)
        while d <= 7000:
            s = core.solve(w, d, 0.0)
            good = any(s[k] and s[k]["ok"] for k in ("low", "high"))
            if good:
                lo = d if lo is None else lo
                hi = d
            d += 20
        if lo is None:
            return "—"
        return f"{lo:.0f}–{hi:.0f} m"

    # ---------- 区域预设 ----------
    def _regions(self) -> list:
        rs = self.cfg.get("regions")
        return rs if isinstance(rs, list) else []

    def _region_names(self) -> list:
        return [r.get("name", "") for r in self._regions()]

    def _refresh_region_combo(self, select: str | None = None):
        cur = self.cfg.get("region_name") or ""
        names = self._region_names()
        self.cb_region.blockSignals(True)
        self.cb_region.clear()
        for n in names:
            self.cb_region.addItem(n, n)
        target = select if select is not None else cur
        self.cb_region.setCurrentIndex(names.index(target) if target in names else -1)
        self.cb_region.blockSignals(False)

    def on_region_preset(self, idx):
        if idx < 0:
            return
        name = self.cb_region.itemData(idx)
        for r in self._regions():
            if r.get("name") == name:
                self.cfg["region"] = list(r["rect"])
                self.cfg["region_name"] = name
                save_config(self.cfg)
                self.refresh_region_label()
                self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
                self.lb_status.setText(f"已套用预设「{name}」：{r['rect'][2]} × {r['rect'][3]} px")
                return

    def save_region_preset(self):
        cur = self.cfg.get("region_name") or ""
        default = cur or f"区域 {len(self._regions()) + 1}"
        name, ok = QInputDialog.getText(self, "保存识别区域", "给这个框选区域起个名字（同名会覆盖）：",
                                        QLineEdit.Normal, default)
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.information(self, "保存识别区域", "名字不能为空。")
            return
        rect = list(self.cfg["region"])
        rs = self._regions()
        for r in rs:
            if r.get("name") == name:
                r["rect"] = rect
                break
        else:
            rs.append({"name": name, "rect": rect})
        self.cfg["regions"] = rs
        self.cfg["region_name"] = name
        save_config(self.cfg)
        self._refresh_region_combo(name)
        self.refresh_region_label()
        self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
        self.lb_status.setText(f"已保存预设「{name}」（{rect[2]} × {rect[3]} px）")

    def rename_region_preset(self):
        idx = self.cb_region.currentIndex()
        if idx < 0:
            QMessageBox.information(self, "重命名区域", "先在上面的下拉里选一个已保存的区域。")
            return
        old = self.cb_region.itemData(idx)
        new, ok = QInputDialog.getText(self, "重命名区域", f"把「{old}」改成：", QLineEdit.Normal, old)
        if not ok:
            return
        new = (new or "").strip()
        if not new or new == old:
            return
        if new in self._region_names():
            QMessageBox.information(self, "重命名区域", f"已经有叫「{new}」的区域了，换一个名字。")
            return
        for r in self._regions():
            if r.get("name") == old:
                r["name"] = new
                break
        if (self.cfg.get("region_name") or "") == old:
            self.cfg["region_name"] = new
        save_config(self.cfg)
        self._refresh_region_combo(new)
        self.refresh_region_label()
        self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
        self.lb_status.setText(f"已重命名：「{old}」→「{new}」")

    def delete_region_preset(self):
        idx = self.cb_region.currentIndex()
        if idx < 0:
            QMessageBox.information(self, "删除区域", "先在上面的下拉里选一个已保存的区域。")
            return
        name = self.cb_region.itemData(idx)
        if QMessageBox.question(self, "删除区域", f"删掉预设「{name}」？") != QMessageBox.Yes:
            return
        self.cfg["regions"] = [r for r in self._regions() if r.get("name") != name]
        if (self.cfg.get("region_name") or "") == name:
            self.cfg["region_name"] = ""
        save_config(self.cfg)
        self._refresh_region_combo()
        self.refresh_region_label()
        self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
        self.lb_status.setText(f"已删除预设「{name}」")

    # ---------- 区域 ----------
    def refresh_region_label(self):
        x, y, w, h = self.cfg["region"]
        name = self.cfg.get("region_name") or ""
        tail = f" · 预设「{name}」" if name else " · 未保存"
        self.lb_region.setText(f"{w} × {h} px  @ ({x}, {y}){tail}")

    def pick_region(self):
        self.hide()
        self.sel = RegionSelector()
        self.sel.picked.connect(self.on_region_picked)
        self.sel.show()
        self.sel.raise_()
        self.sel.activateWindow()

    def on_region_picked(self, rect):
        self.cfg["region"] = rect
        save_config(self.cfg)
        self.refresh_region_label()
        self.show()
        self.activateWindow()
        self.preview_region()

    def preview_region(self):
        try:
            img = vision.grab(tuple(self.cfg["region"]))
        except Exception as e:      # noqa: BLE001
            QMessageBox.warning(self, "截图失败", str(e))
            return
        preview = img.copy()
        preview.thumbnail((360, 200))
        pm = QPixmap.fromImage(QImage(preview.tobytes(), preview.width, preview.height, QImage.Format_RGB888))
        dlg = QMessageBox(self)
        dlg.setWindowTitle("识别区域预览")
        dlg.setIconPixmap(pm)
        dlg.setText(f"区域 {self.cfg['region']}")
        dlg.exec()

    def test_ocr_region(self):
        self.start_ocr()

    def start_ocr(self, mode: str = "quick"):
        if self.worker and self.worker.isRunning():
            return
        key = self.ed_key.text().strip()
        if not key:
            self.lb_status.setText("先填 DeepSeek 密钥")
            return
        self.ocr_mode = "exact" if mode == "exact" else "quick"
        self.lb_status.setStyleSheet("color:#ffd166;font-size:12px;")
        if self.ocr_mode == "quick":
            self.lb_status.setText("正在截取坐标（快速定位，1 次）…（截屏 + AI 识图，约 1～2 秒）")
            self.overlay.set_status("busy", "识别中 · 快速定位（1 次）")
            self.overlay.set_values(self.overlay.rows[0][1], "识别中…", "—", "—",
                                    "截 1 次，直接把读到的坐标当目标")
        else:
            n = min(len(self.pend) + 1, PEND_TOTAL)
            self.lb_status.setText(
                f"正在截取第 {n} 个坐标（{n}/{PEND_TOTAL}，准确定位）…（截屏 + AI 识图，约 1～2 秒）")
            self.overlay.set_status("busy", f"截第 {n} 个（{n}/{PEND_TOTAL}）· 正在识别")
            tips = {
                1: f"请把准星对准目标再按热键（共 {PEND_TOTAL} 次）",
                2: f"第 2 次截取：准星再挪一点（2/{PEND_TOTAL}）",
                3: f"第 3 次截取：最后一次，马上出结果（3/{PEND_TOTAL}）",
            }
            got = [self.map.grid_at(q[0], q[1]) for q in self.pend]
            marks = "①②③"[:len(got)]
            prev = (f"{marks[-1]} {got[-1]}（{len(got)}/{PEND_TOTAL}）") if got else "等待第 1 个…"
            self.overlay.set_values(self.overlay.rows[0][1], prev, "—", "—", tips.get(n, ""))
        if not self.overlay.isVisible():
            self.overlay.show()
            self.overlay.raise_()
        self.worker = OcrWorker(self.cfg["region"], key, parent=self)
        self.worker.ok.connect(self.on_ocr_ok)
        self.worker.fail.connect(self.on_ocr_fail)
        self.worker.start()

    def do_capture(self, *_):
        self.start_ocr("quick")

    def clear_pending(self):
        """清除已截但还没算成目标的坐标；上一次算好的目标保持不变。"""
        n = len(self.pend)
        self.pend = []
        if not self.target and self.last_target:
            # 正在凑新目标时旧目标被临时让位，这里把它恢复回来
            self.target, self.tgt_mode = self.last_target
            self.view.target = self.target
            self.view.update()
        self.recompute()
        self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
        if n:
            keep = f"，目标 {self.map.grid_at(*self.target)} 保留" if self.target else ""
            self.lb_status.setText(f"已清除 {n} 个未完成的坐标{keep}")
        else:
            self.lb_status.setText("没有未完成的坐标要清除（目标是上次算好的，保留）")

    def _coord_to_point(self, coord: str):
        try:
            rx, rz = core.parse(coord)
        except core.ParseError as e:
            self.lb_status.setStyleSheet("color:#ff8f8f;font-size:12px;")
            self.lb_status.setText(str(e))
            return None
        if rx > self.map.spanx + 1 or rz > self.map.spanz + 1:
            self.lb_status.setStyleSheet("color:#ff8f8f;font-size:12px;")
            self.lb_status.setText(f"{coord} 不在这张地图上")
            return None
        return (min(rx, self.map.spanx), min(rz, self.map.spanz))

    def on_ocr_ok(self, coord, raw, ms):
        quick = (self.ocr_mode == "quick")
        n = 1 if quick else min(len(self.pend) + 1, PEND_TOTAL)
        if not coord:
            self.lb_status.setStyleSheet("color:#ff8f8f;font-size:12px;")
            if quick:
                self.lb_status.setText(
                    f"没读到坐标（模型说：{raw[:40]}）—— 再按一次 {self.hk_text} 重试")
                self.overlay.set_status("error", "没读到坐标 · 再截一次")
            else:
                extra = (f"（已记的 {len(self.pend)} 个还留着，再按一次重试第 {n} 个）"
                         if self.pend else "")
                self.lb_status.setText(f"没读到坐标（模型说：{raw[:40]}）{extra}")
                self.overlay.set_status("error", f"第 {n} 个没读到 · 再截一次")
            self.overlay.set_values(self.overlay.rows[0][1], "无坐标", "—", "—",
                                    "截图里没有找到网格坐标", bad=True)
            return
        self.last_ocr = coord
        pt = self._coord_to_point(coord)
        if pt is None:
            self.overlay.set_values(self.overlay.rows[0][1], coord, "—", "—", "坐标不在这张地图上", bad=True)
            self.overlay.set_status("error", "计算失败（坐标不在这张地图上）")
            return

        if quick:
            # 快速定位：这一个坐标直接当目标
            self.pend = []
            self._commit_target(pt, "single1")
            self.recompute()
            hint = "（I 易被误读成 L，请核对）" if coord.startswith("I") else ""
            self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
            self.lb_status.setText(f"快速定位：{coord} · {ms} ms{hint}")
            return

        self.pend.append((pt[0], pt[1], coord))
        if len(self.pend) < PEND_TOTAL:
            # 还没凑齐：先记下，等下一个
            self.target = None
            self.tgt_mode = None
            self.view.target = None
            self.view.update()
            self.recompute()
            hint = "（I 易被误读成 L，请核对）" if coord.startswith("I") else ""
            k = len(self.pend)
            self.lb_status.setStyleSheet("color:#ffd166;font-size:12px;")
            self.lb_status.setText(
                f"第 {k} 个坐标 {coord} · {ms} ms{hint} —— 还差 {PEND_TOTAL - k} 个，"
                f"再按 {self.hk_texts[2]} 截 {PEND_TOTAL - k} 次（共 {PEND_TOTAL} 次，取三点外心）")
            return

        # 凑齐了：合并成一个目标点
        pts = list(self.pend)
        self.pend = []
        target, mode, msg = combine_targets(pts)
        self._commit_target(target, mode)
        self.recompute()
        self.lb_status.setStyleSheet("color:#9fe6f5;font-size:12px;")
        self.lb_status.setText(f"{msg} → {self.map.grid_at(*self.target)} · {ms} ms")

    def on_ocr_fail(self, msg):
        self.lb_status.setStyleSheet("color:#ff8f8f;font-size:12px;")
        self.lb_status.setText("识别失败：" + msg)
        self.overlay.set_values(self.overlay.rows[0][1], "—", "—", "—", msg[:40], bad=True)
        self.overlay.set_status("error", "计算失败（接口/网络）")

    # ---------- 演练 ----------
    def open_drill(self):
        samples = self._sample_coords()
        text = random.choice(samples)
        dpr = (QGuiApplication.screenAt(QPoint(self.cfg["region"][0], self.cfg["region"][1]))
               or QGuiApplication.primaryScreen()).devicePixelRatio()
        self.drill = DrillWindow(self.cfg["region"], text, dpr)
        self.drill.show()
        self.drill.raise_()
        self.lb_status.setText(
            f"演练：区域里现在显示 {text}，按 {self.hk_text} 快速定位 / "
            f"{self.hk_texts[2]} 准确定位试试")

    def _sample_coords(self):
        cand = []
        step = 600
        rx = 200
        while rx < self.map.spanx:
            rz = 200
            while rz < self.map.spanz:
                g = self.map.grid_at(rx, rz)
                if "I" not in g and "O" not in g:      # 演练不生成 I/O，免得字形歧义干扰自测
                    cand.append(g)
                rz += step
            rx += step
        return cand or ["D4-5-7"]

    # ---------- 杂项 ----------
    def save_key(self):
        self.cfg["api_key"] = self.ed_key.text().strip()
        save_config(self.cfg)
        self.lb_key.setText("已保存到 config.json")

    def _sub_text(self) -> str:
        return (f"{self.hk_text} 快速定位 · {self.hk_texts[2]} 准确定位 · "
                f"{self.hk_texts[3]} 清除坐标 · {self.hk_texts[4]} 浮窗")

    def _hk_hint(self) -> str:
        return (f"当前：{self.hk_text} 快速 · {self.hk_texts[2]} 准确（3 次）· "
                f"{self.hk_texts[3]} 清除 · {self.hk_texts[4]} 浮窗"
                "（点输入框按下新键即改，支持组合键）")

    def _tip_text(self) -> str:
        return (
            "玩法：① 在游戏里站稳，记下自己的格子（也可在这张地图上点出来）\n"
            "② 把准星附近那一小块 CURRENT GRID 框成识别区域\n"
            f"③ 切回游戏：{self.hk_text} 快速定位（截 1 次直接出结果）；"
            f"{self.hk_texts[2]} 准确定位（截 3 次取外心）；"
            f"{self.hk_texts[3]} 清掉截错/没凑齐的坐标（上次算好的目标保留）\n"
            "④ 算完目标格号会自动复制到剪贴板，游戏聊天里直接粘贴"
        )

    def refresh_hotkey_texts(self):
        if not hasattr(self, "btn_manual"):
            return
        self.lb_sub.setText(self._sub_text())
        self.btn_manual.setText(f"快速定位（{self.hk_text}）")
        self.btn_exact.setText(f"准确定位（{self.hk_texts[2]}）")
        self.btn_reset_pending.setToolTip(
            f"等同按清除坐标键（{self.hk_texts[3]}）：清掉还没算成目标的坐标，上次算好的目标保留")
        self.lb_tip.setText(self._tip_text())
        for hid, ed in getattr(self, "qk_edits", {}).items():
            ed.setKeySequence(QKeySequence(self.hk_texts[hid]))

    def _start_hotkeys(self):
        old = getattr(self, "hotkeys", None)
        if old is not None:
            try:
                old.stop()
                old.wait(1000)
            except Exception:
                pass
        bindings = [(hid, self.hk_specs[hid][0], self.hk_specs[hid][1])
                    for hid, _ck, _d in HOTKEY_DEFS]
        self.hotkeys = Hotkeys(bindings, self)
        self.hotkeys.pressed.connect(self.on_hotkey)
        self.hotkeys.failed.connect(self.on_hotkey_failed)
        self.hotkeys.start()

    def apply_hotkey(self, hid: int = 1):
        ed = getattr(self, "qk_edits", {}).get(hid)
        if ed is None:
            return
        seq = ed.keySequence()
        spec = seq_to_hotkey(seq)
        old_text = self.hk_texts[hid]
        if spec is None:
            self.lb_hk.setStyleSheet("color:#ff8f8f;font-size:11px;")
            self.lb_hk.setText("这个键用不了：请用 F1–F24、字母/数字（可加 Ctrl/Shift/Alt）")
            ed.setKeySequence(QKeySequence(old_text))
            return
        text = seq.toString(QKeySequence.NativeText)
        if text == old_text:
            return
        for other, other_text in self.hk_texts.items():
            if other != hid and other_text == text:
                self.lb_hk.setStyleSheet("color:#ff8f8f;font-size:11px;")
                self.lb_hk.setText(f"「{text}」已经给别的功能用了，换一个")
                ed.setKeySequence(QKeySequence(old_text))
                return
        mods, vk = spec
        self.hk_specs[hid] = (mods, vk)
        self.hk_texts[hid] = text
        for ckey, chid in (("hotkey", 1), ("hotkey_exact", 2),
                           ("hotkey_clear", 3), ("hotkey_overlay", 4)):
            if chid == hid:
                self.cfg[ckey] = text
        save_config(self.cfg)
        self._start_hotkeys()
        self.refresh_hotkey_texts()
        self.recompute()
        plain = mods == 0 and not (0x70 <= vk <= 0x87)
        msg = f"当前：{text} · 已保存"
        if plain:
            msg += "（单键会全局占用，游戏里打字也会触发，建议加 Ctrl/Shift）"
        self.lb_hk.setStyleSheet("color:#9fe6f5;font-size:11px;")
        self.lb_hk.setText(msg)

    def on_hotkey_failed(self, hid, err, mods):
        name = self.hk_texts.get(hid, "?")
        self.lb_hk.setStyleSheet("color:#ff8f8f;font-size:11px;")
        self.lb_hk.setText(f"「{name}」注册失败（错误码 {err}，通常是被别的程序占用），换一个组合试试")
        self.lb_status.setStyleSheet("color:#ff8f8f;font-size:12px;")
        self.lb_status.setText(f"热键「{name}」被占用：请点面板上的按钮，或改一个热键")

    def on_hotkey(self, hid):
        if hid == 1:
            self.start_ocr("quick")
        elif hid == 2:
            self.start_ocr("exact")
        elif hid == 3:
            self.clear_pending()
        elif hid == 4:
            self.toggle_overlay()

    def toggle_overlay(self):
        if self.overlay.isVisible():
            self.overlay.hide()
        else:
            self.overlay.show()
            self.overlay.raise_()

    def on_overlay_moved(self, x, y):
        self.cfg["overlay"] = [x, y]
        save_config(self.cfg)

    def quit_app(self):
        """红色「退出程序」：注销热键、关浮窗，然后彻底结束进程。"""
        self._shutdown("退出")
        QApplication.quit()

    def _shutdown(self, reason=""):
        if getattr(self, "_shutting", False):
            return
        self._shutting = True
        print(f"[app] {reason}", flush=True)
        try:
            if self.hotkeys is not None:
                self.hotkeys.stop()
                self.hotkeys.wait(800)
        except Exception:
            pass
        try:
            if self.worker and self.worker.isRunning():
                self.worker.wait(2000)
        except Exception:
            pass
        try:
            if self.drill is not None:
                self.drill.close()
                self.drill = None
        except Exception:
            pass
        try:
            self.overlay.hide()
            self.overlay.close()
        except Exception:
            pass
        try:
            save_config(self.cfg)
        except Exception:
            pass

    def closeEvent(self, ev):
        self._shutdown("关闭窗口")
        super().closeEvent(ev)


def _boot_logging():
    """打包成单文件（无控制台）时：stdout/stderr 与崩溃堆栈都落盘，
    这样双击运行出问题时还能在 run.log 里看到原因，而不是「点了没反应」。"""
    if not paths.frozen():
        return
    try:
        log = open(paths.log_path(), "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 · "
              f"exe={sys.executable} · 配置={paths.config_path()} =====\n")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log

    def hook(etype, value, tb):
        import traceback
        try:
            log.write("".join(traceback.format_exception(etype, value, tb)))
            log.flush()
        except Exception:
            pass
        try:
            QMessageBox.critical(
                None, "出错了",
                f"程序遇到问题：\n{str(value)[:600]}\n\n详细信息已写入：\n{paths.log_path()}")
        except Exception:
            pass

    sys.excepthook = hook


def _single_instance(app, args) -> bool:
    """已经开着一个就不要再开第二个：两个实例会互相抢全局热键（RegisterHotKey 1409），
    第二个实例看起来"热键失灵"，很容易被误判成程序坏了。自测模式不受限制。"""
    if {"--e2e", "--drilltest", "--hotkeytest", "--shotdesk"} & set(args):
        return True
    try:
        from PySide6.QtCore import QSharedMemory
        lock = QSharedMemory("SquadArtyCalc-single-instance")
        if not lock.create(1):
            QMessageBox.information(
                None, "已经在运行",
                "Squad 间瞄火力助手已经开着了，请用任务栏/Alt+Tab 切到那个窗口。\n"
                "（两个同时开的话，全局热键会被其中一个独占，另一个会提示注册失败。）")
            return False
        app._single_lock = lock      # 保住引用，别被 GC 掉
        return True
    except Exception:
        return True


def main():
    _boot_logging()
    app = QApplication(sys.argv)
    args = sys.argv[1:]
    if not _single_instance(app, args):
        return 0
    cfg = load_config()
    win = MainWindow(cfg)
    win.show()

    if {"--e2e", "--drilltest", "--hotkeytest", "--shotdesk"} & set(args):
        win.select_map("yehorivka")      # 自测坐标是按 Yehorivka 定的，别被上次选的地图带偏
    if "--e2e" in args:
        win.gun = (3083.3, 3183.3)
        win.view.gun = win.gun
        tgt_grid = win.map.grid_at(3083.3 + 520, 3183.3 - 260)
        win.set_target_grid(tgt_grid)
        win.recompute()
        print("e2e target grid", tgt_grid)

        def shot():
            win.grab().save(str(HERE / "shot_main.png"))
            win.overlay.grab().save(str(HERE / "shot_overlay.png"))
            print("E2E gun", win.vals["gun"].text(), "tgt", win.vals["tgt"].text(),
                  "brg", win.vals["brg"].text(), "mil", win.vals["mil"].text())
            print("overlay note:", win.overlay.note)
            app.quit()

        QTimer.singleShot(1500, shot)

    if "--drilltest" in args:
        gun = (3083.3, 3183.3)
        win.gun = gun
        win.view.gun = gun
        win.view.update()
        coord = win.map.grid_at(gun[0] + 480, gun[1] + 120)
        dpr = QGuiApplication.primaryScreen().devicePixelRatio()
        win.drill = DrillWindow(win.cfg["region"], coord, dpr)
        win.drill.show()
        win.drill.raise_()

        def go():
            try:
                img = vision.grab(tuple(win.cfg["region"]))
                img.save(str(HERE / "shot_region.png"))
                got, raw, ms = vision.ask(img, win.ed_key.text().strip())
                print("drill expect", coord, "got", repr(got), ms, "ms raw", raw[:60])
                win.on_ocr_ok(got or "", raw, ms)
            except Exception as e:      # noqa: BLE001
                print("drill error", e)
            win.grab().save(str(HERE / "shot_main.png"))
            win.overlay.grab().save(str(HERE / "shot_overlay.png"))
            print("drill values", win.vals["gun"].text(), win.vals["tgt"].text(),
                  win.vals["brg"].text(), win.vals["mil"].text())
            app.quit()

        QTimer.singleShot(1200, go)

    if "--hotkeytest" in args:
        gun = (3083.3, 3183.3)
        win.gun = gun
        win.view.gun = gun
        win.recompute()
        coord = win.map.grid_at(gun[0] - 420, gun[1] + 300)
        dpr = QGuiApplication.primaryScreen().devicePixelRatio()
        win.drill = DrillWindow(win.cfg["region"], coord, dpr)
        win.drill.show()
        win.drill.raise_()

        def press():
            user32 = ctypes.windll.user32
            mods, vk = win.hk_specs[1]
            hold = []
            if mods & 0x0002:
                hold.append(0x11)      # Ctrl
            if mods & 0x0004:
                hold.append(0x10)      # Shift
            if mods & 0x0001:
                hold.append(0x12)      # Alt
            for m in hold:
                user32.keybd_event(m, 0, 0, 0)
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 2, 0)
            for m in reversed(hold):
                user32.keybd_event(m, 0, 2, 0)
            print(f"sent {win.hk_text} (mods={mods:#x} vk={vk:#x}), drill shows", coord)

        def report():
            print("hotkey OCR:", getattr(win, "last_ocr", None), "| status:", win.lb_status.text(),
                  "| mil:", win.vals["mil"].text())
            app.quit()

        QTimer.singleShot(1800, press)
        QTimer.singleShot(8000, report)

    if "--shotdesk" in args:
        gun = (3083.3, 3183.3)
        win.gun = gun
        win.view.gun = gun
        tgt = win.map.grid_at(gun[0] + 430, gun[1] + 210)
        win.set_target_grid(tgt)
        win.recompute()
        win.overlay.move(760, 300)

        def desk():
            vision.ImageGrab.grab(all_screens=True).save(str(HERE / "shot_desktop.png"))
            print("desktop shot", tgt)
            app.quit()

        QTimer.singleShot(1600, desk)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
