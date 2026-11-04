# -*- coding: utf-8 -*-
"""半透明浮窗：底半透明、文字不透明，显示状态 + 4 个值。"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QWidget

BG = QColor(9, 13, 19, 165)
BORDER = QColor(255, 178, 90, 200)
LABEL = QColor(160, 178, 196, 255)
VALUE = QColor(255, 255, 255, 255)
ACCENT = QColor(255, 209, 102, 255)
ERROR = QColor(255, 130, 130, 255)
NOTE = QColor(150, 166, 182, 255)

STATUS_COLOR = {
    "idle": QColor(150, 166, 182, 255),
    "busy": QColor(255, 209, 102, 255),
    "done": QColor(126, 227, 139, 255),
    "error": QColor(255, 130, 130, 255),
}
STATUS_TEXT = {
    "idle": "待命",
    "busy": "参数计算中",
    "done": "计算完成",
    "error": "计算失败",
}


class Overlay(QWidget):
    moved_by_user = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("间瞄诸元")
        # 顺序按需求：密位在方位上面，方位是最底下一行
        self.rows = [
            ("阵地", "—"),
            ("目标", "—"),
            ("密位", "—"),
            ("方位", "—"),
        ]
        self.note = "按 F1 识别目标坐标"
        self.bad = False
        self.status_state = "idle"
        self.status_text = STATUS_TEXT["idle"]
        self._dots = 0
        self._drag = None
        self._w = 318
        self._pulse = QTimer(self)
        self._pulse.setInterval(320)
        self._pulse.timeout.connect(self._tick)
        self.resize(self._w, self.height_for())

    # ---------- 内容 ----------
    def set_values(self, gun: str, target: str, bearing: str, mil: str, note: str = "", bad: bool = False):
        # 注意参数顺序未变（gun, target, bearing, mil），只是摆出来的行序改成
        # 阵地 / 目标 / 密位 / 方位——方位固定在最下面一行
        self.rows = [("阵地", gun), ("目标", target), ("密位", mil), ("方位", bearing)]
        self.note = note
        self.bad = bad
        self.resize(self._w, self.height_for())
        self.update()

    def set_status(self, state: str = "idle", text: str = ""):
        """state: idle / busy / done / error。文字留空则用默认文案。"""
        self.status_state = state or "idle"
        self.status_text = text or STATUS_TEXT.get(self.status_state, STATUS_TEXT["idle"])
        if self.status_state == "busy":
            self._dots = 1
            self._pulse.start()
        else:
            self._pulse.stop()
            self._dots = 0
        self.resize(self._w, self.height_for())
        self.update()

    def _tick(self):
        self._dots = (self._dots + 1) % 4
        self.update()

    def height_for(self) -> int:
        return 12 + 26 + 4 * 34 + (24 if self.note else 8)

    # ---------- 绘制 ----------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, 13, 13)

        p.fillPath(path, BG)
        pen = QPen(BORDER, 2)
        p.setPen(pen)
        p.drawPath(path)

        # 左侧橙色装饰条
        p.setPen(Qt.NoPen)
        p.setBrush(ACCENT)
        p.drawRoundedRect(QRectF(1, 14, 4, self.height() - 28), 2, 2)

        label_font = QFont("Microsoft YaHei UI", 10)
        value_font = QFont("Consolas", 15)
        value_font.setBold(True)

        # —— 状态行
        y = 10
        color = STATUS_COLOR.get(self.status_state, STATUS_COLOR["idle"])
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPoint(25, y + 13), 4, 4)
        p.setBrush(Qt.NoBrush)
        text = self.status_text
        if self.status_state == "busy":
            text = self.status_text + "." * self._dots
        st_font = QFont("Microsoft YaHei UI", 10)
        st_font.setBold(True)
        p.setFont(st_font)
        p.setPen(color)
        text = QFontMetrics(st_font).elidedText(text, Qt.ElideRight, int(self.width() - 54))
        p.drawText(QRectF(36, y, self.width() - 52, 26), Qt.AlignLeft | Qt.AlignVCenter, text)
        y += 26

        for label, value in self.rows:
            p.setFont(label_font)
            p.setPen(LABEL)
            p.drawText(QRectF(18, y, 52, 30), Qt.AlignLeft | Qt.AlignVCenter, label)
            p.setFont(value_font)
            p.setPen(ERROR if (self.bad and value in ("—", "无坐标")) else VALUE)
            value = QFontMetrics(value_font).elidedText(value, Qt.ElideRight, int(self.width() - 92))
            p.drawText(QRectF(70, y, self.width() - 88, 30), Qt.AlignLeft | Qt.AlignVCenter, value)
            y += 34

        if self.note:
            nt_font = QFont("Microsoft YaHei UI", 9)
            p.setFont(nt_font)
            p.setPen(ERROR if self.bad else NOTE)
            note = QFontMetrics(nt_font).elidedText(self.note, Qt.ElideRight, int(self.width() - 36))
            p.drawText(QRectF(18, y - 4, self.width() - 34, 20), Qt.AlignLeft | Qt.AlignVCenter, note)

    # ---------- 交互 ----------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            self._drag = None
            self.moved_by_user.emit(self.x(), self.y())

    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        act_hide = QAction("隐藏（F2）", menu)
        act_reset = QAction("回到默认位置", menu)
        act_hide.triggered.connect(self.hide)
        act_reset.triggered.connect(self.reset_pos)
        menu.addAction(act_hide)
        menu.addAction(act_reset)
        menu.exec(ev.globalPos())

    def reset_pos(self):
        from PySide6.QtGui import QGuiApplication

        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 40, scr.top() + 60)
        self.moved_by_user.emit(self.x(), self.y())

    def showEvent(self, ev):
        super().showEvent(ev)
        self.raise_()
