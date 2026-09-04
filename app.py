#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — 北科大教务抢课程序 · PySide6 桌面端
==============================================
五页：会话管理 / 课程搜索 / 监控中心 / 运行日志 / 设置
+ 系统托盘（最小化到托盘、桌面通知）

后端全部走 xk_core.py（XKClient / Monitor），本文件不做任何业务逻辑。
事实来源：../SYSTEM_NOTES.md

运行：python app.py
"""
import math
import os
import sys
import threading
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import (QObject, QPoint, QPointF, QPropertyAnimation, QRectF,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QFontMetrics, QIcon,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateTimeEdit,
                               QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QSpinBox, QStackedWidget,
                               QSystemTrayIcon, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from xk_core import (APIError, APP_DIR, CONFIG_PATH, SAFE_INTERVAL,
                     SessionExpired, XKClient, Monitor, QrLogin,
                     enum_cache_get, enum_cache_invalidate, extract_courses,
                     extract_rules, load_config, match_course,
                     normalize_targets, page_info, parse_skxx, remaining_of,
                     save_config, enrolled_of)

RUN_LOG_PATH = APP_DIR / "run.log"
ASSET_DIR = APP_DIR / "_ui_assets"


# ---------------------------------------------------------------------------
# 设计令牌（Design Tokens）— 全 UI 共享的语义常量
# ---------------------------------------------------------------------------
class T:
    FONT_FAMILY = '"Microsoft YaHei UI","Microsoft YaHei","Segoe UI","PingFang SC",sans-serif'
    FONT_MONO = '"Cascadia Code","JetBrains Mono","Consolas",monospace'

    HDR_BG_TOP = "#0B0D12"
    HDR_BG_MID = "#131720"
    HDR_BG_BOT = "#1A1F2C"
    HDR_ACCENT = "#A5B4FC"
    HDR_TEXT = "#F4F4F5"
    HDR_DIM = "#71717A"

    APP_BG = "#F4F4F5"
    CARD_BG = "#FFFFFF"
    SUBTLE_BG = "#FAFAF9"
    NAV_BG = "#FAFAF9"       # 导航 / 状态栏底色（比内容区浅一档，做层次）
    NAV_HOVER = "#F1F0EE"

    TEXT_1 = "#18181B"
    TEXT_2 = "#52525B"
    TEXT_3 = "#A1A1AA"
    TEXT_INV = "#FAFAFA"

    BORDER = "#E7E5E4"
    BORDER_STRONG = "#D6D3D1"
    BORDER_FOCUS = "#6366F1"

    BRAND = "#6366F1"
    BRAND_HOVER = "#4F46E5"
    BRAND_ACTIVE = "#4338CA"
    BRAND_SOFT = "#EEF2FF"
    BRAND_SOFT_2 = "#E0E7FF"

    OK = "#10B981"
    OK_SOFT = "#ECFDF5"
    OK_BORDER = "#A7F3D0"
    OK_DEEP = "#047857"
    WARN = "#F59E0B"
    WARN_SOFT = "#FFFBEB"
    WARN_BORDER = "#FDE68A"
    WARN_DEEP = "#B45309"
    ERR = "#EF4444"
    ERR_SOFT = "#FEF2F2"
    ERR_BORDER = "#FECACA"
    ERR_DEEP = "#B91C1C"
    INFO = "#3B82F6"
    INFO_SOFT = "#EFF6FF"
    INFO_BORDER = "#BFDBFE"
    MUTED = "#71717A"
    MUTED_SOFT = "#F4F4F5"
    MUTED_BORDER = "#E4E4E7"


LOG_COLORS = {
    "info":    "#CBD5E1",
    "warn":    "#FBBF24",
    "success": "#4ADE80",
    "error":   "#F87171",
    "debug":   "#64748B",
    "found":   "#A78BFA",
}
LEVEL_TAG = {
    "info": "INFO", "warn": "WARN", "success": " OK ",
    "error": " ERR ", "found": "FOUND", "debug": "DBG ",
}

# 运行日志本地存档：logs/ 目录下每次启动建一个独立文件（文件名带启动时刻），
# 同一秒内再次启动自动加序号，绝不共用/覆盖；整段会话实时追加
LOG_DIR = APP_DIR / "logs"


def new_session_log(start: datetime) -> Path | None:
    """为一次启动分配并创建独立存档文件，写入启动头；失败返回 None（存档降级，不打断启动）。"""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        base = f"xk_log_{start:%Y%m%d_%H%M%S}"
        p = LOG_DIR / f"{base}.log"
        i = 1
        while p.exists():
            p = LOG_DIR / f"{base}_{i}.log"
            i += 1
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"======== 程序启动 {start:%Y-%m-%d %H:%M:%S} · 本次会话独立存档 ========\n")
        return p
    except OSError:
        return None


def persist_log_line(path: Path, now: datetime, level: str, msg: str) -> None:
    """把一条日志实时追加到本会话存档文件；失败静默（存档是增强，不能拖垮主流程）。"""
    if not path or not level or not str(msg).strip():
        return
    try:
        LOG_DIR.mkdir(exist_ok=True)
        new_file = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            if new_file:
                f.write(f"======== 程序启动 {now:%Y-%m-%d %H:%M:%S} · 本次会话独立存档 ========\n")
            f.write(f"[{now:%Y-%m-%d %H:%M:%S}] [{LEVEL_TAG.get(level, 'INFO')}] {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 资源生成：所有图标运行时用 QPainter 绘制（无外部依赖）
# ---------------------------------------------------------------------------
def ensure_ui_assets() -> dict:
    ASSET_DIR.mkdir(exist_ok=True)
    out: dict = {}

    def _save(name, w, h, draw):
        p = ASSET_DIR / name
        pm = QPixmap(w, h)
        pm.fill(Qt.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing)
        q.setRenderHint(QPainter.SmoothPixmapTransform)
        draw(q, w, h)
        q.end()
        pm.save(str(p), "PNG")
        out[name] = str(p).replace("\\", "/")

    def _brand(q, w, h):
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, QColor("#A5B4FC"))
        g.setColorAt(1, QColor("#4F46E5"))
        q.setBrush(g)
        q.setPen(Qt.NoPen)
        q.drawEllipse(1, 1, w - 2, h - 2)
        q.setBrush(QColor(255, 255, 255, 40))
        q.drawEllipse(3, 2, w - 6, int(h * 0.45))
        q.setPen(QColor("white"))
        f = QFont("Microsoft YaHei UI", int(h * 0.50), QFont.Bold)
        f.setStyleStrategy(QFont.PreferAntialias)
        q.setFont(f)
        q.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "Q")
    _save("brand_sm.png", 36, 36, _brand)
    _save("brand.png", 64, 64, _brand)

    def _stroke(name, w, h, path_fn, color="#52525B", width=1.7):
        def _draw(q, w, h):
            q.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            q.setBrush(Qt.NoBrush)
            path_fn(q, w, h)
        _save(name + ".png", w, h, _draw)

    def ic_search(q, w, h):
        q.drawEllipse(QPointF(w * 0.42, h * 0.42), w * 0.16, w * 0.16)
        q.drawLine(QPointF(w * 0.54, h * 0.54), QPointF(w * 0.82, h * 0.82))
    _stroke("ic_search", 18, 18, ic_search)

    def ic_lock(q, w, h):
        q.drawRoundedRect(QRectF(w * 0.22, h * 0.45, w * 0.56, h * 0.38), 2.5, 2.5)
        path = QPainterPath()
        path.moveTo(w * 0.30, h * 0.45)
        path.lineTo(w * 0.30, h * 0.32)
        path.arcTo(QRectF(w * 0.30, h * 0.18, w * 0.40, h * 0.30), 180, -180)
        path.lineTo(w * 0.70, h * 0.45)
        q.drawPath(path)
        q.setBrush(QColor("#52525B"))
        q.drawEllipse(QPointF(w * 0.50, h * 0.62), 1.8, 1.8)
    _stroke("ic_lock", 18, 18, ic_lock)

    def ic_qr(q, w, h):
        for (x, y) in [(0.12, 0.12), (0.66, 0.12), (0.12, 0.66)]:
            q.drawRoundedRect(QRectF(w * x, h * y, w * 0.22, h * 0.22), 1.5, 1.5)
        for (x, y) in [(0.20, 0.20), (0.74, 0.20), (0.20, 0.74)]:
            q.drawEllipse(QPointF(w * x, h * y), 1.6, 1.6)
        for (x, y) in [(0.42, 0.42), (0.50, 0.42), (0.58, 0.42),
                       (0.42, 0.50), (0.50, 0.58), (0.58, 0.50),
                       (0.42, 0.58), (0.50, 0.50), (0.58, 0.58),
                       (0.66, 0.42), (0.66, 0.50), (0.66, 0.58),
                       (0.42, 0.66), (0.50, 0.66), (0.58, 0.66),
                       (0.42, 0.74), (0.50, 0.74), (0.58, 0.74)]:
            q.fillRect(QRectF(w * x - 0.8, h * y - 0.8, 1.6, 1.6), QColor("#52525B"))
    _stroke("ic_qr", 18, 18, ic_qr, width=1.6)

    def ic_target(q, w, h):
        q.drawEllipse(QRectF(w * 0.15, h * 0.15, w * 0.70, h * 0.70))
        q.drawEllipse(QRectF(w * 0.35, h * 0.35, w * 0.30, h * 0.30))
        q.setBrush(QColor("#52525B"))
        q.drawEllipse(QPointF(w * 0.50, h * 0.50), 1.6, 1.6)
    _stroke("ic_target", 18, 18, ic_target, width=1.6)

    def ic_play(q, w, h):
        path = QPainterPath()
        path.moveTo(w * 0.30, h * 0.18)
        path.lineTo(w * 0.82, h * 0.50)
        path.lineTo(w * 0.30, h * 0.82)
        path.closeSubpath()
        q.setBrush(QColor("#FAFAFA"))
        q.setPen(Qt.NoPen)
        q.drawPath(path)
    _save("ic_play.png", 18, 18, ic_play)

    def ic_stop(q, w, h):
        q.setBrush(QColor("#FAFAFA"))
        q.setPen(Qt.NoPen)
        q.drawRoundedRect(QRectF(w * 0.25, h * 0.25, w * 0.50, h * 0.50), 2, 2)
    _save("ic_stop.png", 18, 18, ic_stop)

    def ic_refresh(q, w, h):
        path = QPainterPath()
        path.arcMoveTo(QRectF(w * 0.18, h * 0.18, w * 0.64, w * 0.64), 30)
        path.arcTo(QRectF(w * 0.18, h * 0.18, w * 0.64, w * 0.64), 30, 280)
        q.drawPath(path)
        path2 = QPainterPath()
        path2.moveTo(w * 0.74, h * 0.22)
        path2.lineTo(w * 0.82, h * 0.36)
        path2.lineTo(w * 0.64, h * 0.34)
        q.drawPath(path2)
    _stroke("ic_refresh", 18, 18, ic_refresh)

    def ic_gear(q, w, h):
        q.drawEllipse(QRectF(w * 0.30, h * 0.30, w * 0.40, h * 0.40))
        for i in range(8):
            ang = i * 45
            rad = math.radians(ang)
            x1 = w * 0.50 + math.cos(rad) * w * 0.42
            y1 = h * 0.50 + math.sin(rad) * h * 0.42
            x2 = w * 0.50 + math.cos(rad) * w * 0.62
            y2 = h * 0.50 + math.sin(rad) * h * 0.62
            q.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    _stroke("ic_gear", 18, 18, ic_gear, width=1.6)

    def ic_log(q, w, h):
        for i, frac in enumerate([0.30, 0.50, 0.70]):
            w_ = 0.55 if i % 2 == 0 else 0.38
            q.drawLine(QPointF(w * 0.18, h * frac), QPointF(w * (0.18 + w_), h * frac))
    _stroke("ic_log", 18, 18, ic_log)

    def ic_plus(q, w, h):
        q.drawLine(QPointF(w * 0.20, h * 0.50), QPointF(w * 0.80, h * 0.50))
        q.drawLine(QPointF(w * 0.50, h * 0.20), QPointF(w * 0.50, h * 0.80))
    _stroke("ic_plus", 14, 14, ic_plus, width=1.8)

    def ic_x(q, w, h):
        q.drawLine(QPointF(w * 0.25, h * 0.25), QPointF(w * 0.75, h * 0.75))
        q.drawLine(QPointF(w * 0.75, h * 0.25), QPointF(w * 0.25, h * 0.75))
    _stroke("ic_x", 14, 14, ic_x, width=1.8)

    def ic_check(q, w, h):
        pen = QPen(QColor("white"), 2.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        q.setPen(pen)
        q.drawLine(QPointF(w * 0.20, h * 0.52), QPointF(w * 0.43, h * 0.74))
        q.drawLine(QPointF(w * 0.43, h * 0.74), QPointF(w * 0.82, h * 0.26))
    _save("ic_check.png", 16, 16, ic_check)

    def ic_dot(q, w, h):
        q.setPen(Qt.NoPen)
        q.setBrush(QColor("white"))
        q.drawEllipse(QRectF(w * 0.22, h * 0.22, w * 0.56, h * 0.56))
    _save("ic_dot.png", 12, 12, ic_dot)

    def ic_chevron_down(q, w, h):
        q.drawLine(QPointF(w * 0.22, h * 0.40), QPointF(w * 0.50, h * 0.62))
        q.drawLine(QPointF(w * 0.50, h * 0.62), QPointF(w * 0.78, h * 0.40))
    _stroke("ic_chevron_down", 12, 12, ic_chevron_down, color="#71717A", width=1.8)

    def ic_chevron_up(q, w, h):
        q.drawLine(QPointF(w * 0.22, h * 0.60), QPointF(w * 0.50, h * 0.38))
        q.drawLine(QPointF(w * 0.50, h * 0.38), QPointF(w * 0.78, h * 0.60))
    _stroke("ic_chevron_up", 12, 12, ic_chevron_up, color="#71717A", width=1.8)

    def ic_pulse(q, w, h):
        g = QRadialGradient(w * 0.5, h * 0.5, w * 0.5)
        g.setColorAt(0, QColor("#34D399"))
        g.setColorAt(0.5, QColor("#10B981"))
        g.setColorAt(1, QColor("#10B98100"))
        q.setBrush(g)
        q.setPen(Qt.NoPen)
        q.drawEllipse(QRectF(0, 0, w, h))
        q.setBrush(QColor("#10B981"))
        q.drawEllipse(QPointF(w * 0.5, h * 0.5), w * 0.14, h * 0.14)
    _save("ic_pulse.png", 18, 18, ic_pulse)

    def ic_idle_dot(q, w, h):
        g = QRadialGradient(w * 0.5, h * 0.5, w * 0.5)
        g.setColorAt(0, QColor("#A1A1AA"))
        g.setColorAt(0.5, QColor("#71717A"))
        g.setColorAt(1, QColor("#71717A00"))
        q.setBrush(g)
        q.setPen(Qt.NoPen)
        q.drawEllipse(QRectF(0, 0, w, h))
        q.setBrush(QColor("#71717A"))
        q.drawEllipse(QPointF(w * 0.5, h * 0.5), w * 0.14, h * 0.14)
    _save("ic_idle.png", 18, 18, ic_idle_dot)

    def ic_bolt(q, w, h):
        path = QPainterPath()
        path.moveTo(w * 0.55, h * 0.10)
        path.lineTo(w * 0.20, h * 0.55)
        path.lineTo(w * 0.45, h * 0.55)
        path.lineTo(w * 0.40, h * 0.90)
        path.lineTo(w * 0.80, h * 0.42)
        path.lineTo(w * 0.55, h * 0.42)
        path.closeSubpath()
        q.setBrush(QColor("#F59E0B"))
        q.setPen(Qt.NoPen)
        q.drawPath(path)
    _save("ic_bolt.png", 18, 18, ic_bolt)

    def ic_clock(q, w, h):
        q.setBrush(QColor("#FFFBEB"))
        q.setPen(QPen(QColor("#F59E0B"), 1.4))
        q.drawEllipse(QRectF(w * 0.12, h * 0.12, w * 0.76, h * 0.76))
        q.setPen(QPen(QColor("#B45309"), 1.8, Qt.SolidLine, Qt.RoundCap))
        q.drawLine(QPointF(w * 0.50, h * 0.50), QPointF(w * 0.50, h * 0.28))
        q.drawLine(QPointF(w * 0.50, h * 0.50), QPointF(w * 0.68, h * 0.50))
    _save("ic_clock.png", 18, 18, ic_clock)

    def ic_check_circle(q, w, h):
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, QColor("#34D399"))
        g.setColorAt(1, QColor("#059669"))
        q.setBrush(g)
        q.setPen(Qt.NoPen)
        q.drawEllipse(QRectF(0, 0, w, h))
        pen = QPen(QColor("white"), 2.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        q.setPen(pen)
        q.drawLine(QPointF(w * 0.28, h * 0.52), QPointF(w * 0.45, h * 0.68))
        q.drawLine(QPointF(w * 0.45, h * 0.68), QPointF(w * 0.74, h * 0.34))
    _save("ic_ok_badge.png", 18, 18, ic_check_circle)

    def ic_menu(q, w, h):
        pen = QPen(QColor("#52525B"), 2.0)
        pen.setCapStyle(Qt.RoundCap)
        q.setPen(pen)
        q.setBrush(Qt.NoBrush)
        for i, frac in enumerate((0.26, 0.50, 0.74)):
            q.drawLine(QPointF(w * 0.16, h * frac), QPointF(w * 0.84, h * frac))
    _save("ic_menu.png", 18, 18, ic_menu)

    def ic_arrow_up(q, w, h):
        pen = QPen(QColor("#52525B"), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        q.setPen(pen)
        q.setBrush(Qt.NoBrush)
        q.drawLine(QPointF(w * 0.50, h * 0.78), QPointF(w * 0.50, h * 0.24))
        q.drawLine(QPointF(w * 0.26, h * 0.48), QPointF(w * 0.50, h * 0.24))
        q.drawLine(QPointF(w * 0.74, h * 0.48), QPointF(w * 0.50, h * 0.24))
    _save("ic_arrow_up.png", 16, 16, ic_arrow_up)

    def ic_arrow_down(q, w, h):
        pen = QPen(QColor("#52525B"), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        q.setPen(pen)
        q.setBrush(Qt.NoBrush)
        q.drawLine(QPointF(w * 0.50, h * 0.22), QPointF(w * 0.50, h * 0.76))
        q.drawLine(QPointF(w * 0.26, h * 0.52), QPointF(w * 0.50, h * 0.76))
        q.drawLine(QPointF(w * 0.74, h * 0.52), QPointF(w * 0.50, h * 0.76))
    _save("ic_arrow_down.png", 16, 16, ic_arrow_down)

    def ic_export(q, w, h):
        pen = QPen(QColor("#52525B"), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        q.setPen(pen)
        q.setBrush(Qt.NoBrush)
        q.drawLine(QPointF(w * 0.50, h * 0.18), QPointF(w * 0.50, h * 0.62))
        q.drawLine(QPointF(w * 0.32, h * 0.44), QPointF(w * 0.50, h * 0.62))
        q.drawLine(QPointF(w * 0.68, h * 0.44), QPointF(w * 0.50, h * 0.62))
        q.drawLine(QPointF(w * 0.22, h * 0.78), QPointF(w * 0.78, h * 0.78))
    _save("ic_export.png", 16, 16, ic_export)

    def ic_info(q, w, h):
        q.setBrush(QColor("#EEF2FF"))
        q.setPen(QPen(QColor("#6366F1"), 1.5))
        q.drawEllipse(QRectF(w * 0.08, h * 0.08, w * 0.84, h * 0.84))
        pen = QPen(QColor("#4338CA"), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        q.setPen(pen)
        q.setBrush(Qt.NoBrush)
        q.drawPoint(QPointF(w * 0.50, h * 0.30))
        q.drawLine(QPointF(w * 0.50, h * 0.42), QPointF(w * 0.50, h * 0.72))
    _save("ic_info.png", 18, 18, ic_info)

    def ic_filter(q, w, h):
        pen = QPen(QColor("#52525B"), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        q.setPen(pen)
        q.setBrush(Qt.NoBrush)
        q.drawLine(QPointF(w * 0.14, h * 0.26), QPointF(w * 0.86, h * 0.26))
        q.drawLine(QPointF(w * 0.28, h * 0.50), QPointF(w * 0.72, h * 0.50))
        q.drawLine(QPointF(w * 0.40, h * 0.74), QPointF(w * 0.60, h * 0.74))
    _save("ic_filter.png", 16, 16, ic_filter)

    def ic_trash(q, w, h):
        pen = QPen(QColor("#DC2626"), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        q.setPen(pen)
        q.setBrush(Qt.NoBrush)
        q.drawLine(QPointF(w * 0.20, h * 0.28), QPointF(w * 0.80, h * 0.28))
        q.drawLine(QPointF(w * 0.32, h * 0.28), QPointF(w * 0.32, h * 0.20))
        q.drawLine(QPointF(w * 0.68, h * 0.28), QPointF(w * 0.68, h * 0.20))
        q.drawLine(QPointF(w * 0.28, h * 0.36), QPointF(w * 0.34, h * 0.82))
        q.drawLine(QPointF(w * 0.72, h * 0.36), QPointF(w * 0.66, h * 0.82))
        q.drawLine(QPointF(w * 0.34, h * 0.82), QPointF(w * 0.66, h * 0.82))
    _save("ic_trash.png", 16, 16, ic_trash)

    return out


def make_brand_icon(size: int = 32) -> QIcon:
    """托盘图标 / 品牌图（不同尺寸）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, 0, size)
    g.setColorAt(0, QColor("#A5B4FC"))
    g.setColorAt(1, QColor("#4F46E5"))
    p.setBrush(g)
    p.setPen(Qt.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.setBrush(QColor(255, 255, 255, 40))
    p.drawEllipse(3, 2, size - 6, int(size * 0.45))
    p.setPen(QColor("white"))
    f = QFont("Microsoft YaHei UI", int(size * 0.46), QFont.Bold)
    f.setStyleStrategy(QFont.PreferAntialias)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "Q")
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------------------
# 主题 QSS — 设计令牌的视觉投影
# ---------------------------------------------------------------------------
_QSS_TMPL = r"""
/* ==================== 全局基底 ==================== */
QMainWindow, QDialog, QWidget {
    color: __TEXT_1__;
    font-family: __FONT__;
    font-size: 13.5px;
}
QMainWindow, QDialog {
    background: __APP_BG__;
}
QWidget#pageRoot {
    background: transparent;
}
QWidget#appRoot {
    background: __APP_BG__;
}

/* ==================== 滚动条 ==================== */
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 4px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #D6D3D1; border-radius: 5px; min-height: 30px;
    border: 2px solid transparent;
}
QScrollBar::handle:vertical:hover { background: #A8A29E; }
QScrollBar:horizontal {
    background: transparent; height: 10px; margin: 4px;
    border-radius: 5px;
}
QScrollBar::handle:horizontal {
    background: #D6D3D1; border-radius: 5px; min-width: 30px;
    border: 2px solid transparent;
}
QScrollBar::handle:horizontal:hover { background: #A8A29E; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ==================== 品牌顶栏 ==================== */
QWidget#appHeader {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 __HDR_TOP__, stop:0.5 __HDR_MID__, stop:1 __HDR_BOT__);
    border-bottom: 1px solid rgba(255,255,255,0.04);
}
QLabel#appTitle {
    color: __HDR_TEXT__; font-size: 16.5px; font-weight: 700;
    letter-spacing: 0.4px;
}
QLabel#appSub {
    color: #8B92A3; font-size: 11.5px; letter-spacing: 0.2px;
}
QLabel#appVerPill {
    color: __HDR_ACCENT__; background: rgba(99,102,241,0.12);
    border: 1px solid rgba(99,102,241,0.22);
    border-radius: 10px; padding: 3px 12px;
    font-size: 11.5px; font-weight: 600;
}
QLabel#appLive {
    color: __HDR_TEXT__; font-size: 12.5px; font-weight: 500;
    padding: 4px 12px 4px 8px; border-radius: 12px;
    background: rgba(255,255,255,0.04);
}

/* ==================== 标签页 ==================== */
QTabWidget::pane { border: none; background: transparent; }
QTabWidget > QWidget > QWidget { background: __APP_BG__; }
QTabBar {
    background: __CARD_BG__;
    border-bottom: 1px solid __BORDER__;
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background: transparent; color: __TEXT_2__;
    padding: 12px 24px; margin: 0 1px;
    border: none; border-bottom: 2px solid transparent;
    font-size: 13.5px; font-weight: 500;
}
QTabBar::tab:hover {
    color: __TEXT_1__; background: rgba(99,102,241,0.04);
}
QTabBar::tab:selected {
    color: __BRAND__; font-weight: 700;
    border-bottom: 2px solid __BRAND__;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #FFFFFF, stop:1 #FAFAFA);
}

/* ==================== 卡片 ==================== */
QFrame#card {
    background: __CARD_BG__;
    border: 1px solid __BORDER__;
    border-radius: 14px;
}
QFrame#cardElevated {
    background: __CARD_BG__;
    border: 1px solid __BORDER__;
    border-radius: 14px;
}
QFrame#cardFlat {
    background: __SUBTLE_BG__;
    border: 1px solid __BORDER__;
    border-radius: 10px;
}
QLabel#cardTitle {
    color: __TEXT_1__; font-size: 14.5px; font-weight: 700;
    padding: 0;
}
QLabel#cardSub {
    color: __TEXT_2__; font-size: 12px; padding: 0;
}
QLabel#cardEyebrow {
    color: __BRAND__; font-size: 11px; font-weight: 700;
    letter-spacing: 0.6px; text-transform: uppercase;
    padding: 0;
}
QLabel#sectionLabel {
    color: __TEXT_2__; font-size: 12px; font-weight: 600;
    padding: 0;
}
QLabel#statLabel {
    color: __TEXT_2__; font-size: 11.5px; font-weight: 500;
    letter-spacing: 0.3px;
}
QLabel#statValue {
    color: __TEXT_1__; font-size: 18px; font-weight: 700;
}
QLabel#hintBox {
    background: __BRAND_SOFT__; color: __BRAND_ACTIVE__;
    border: 1px solid #C7D2FE; border-radius: 10px;
    padding: 10px 14px; font-size: 12.5px; line-height: 1.55;
}
QLabel#tipBox {
    background: __SUBTLE_BG__; color: __TEXT_2__;
    border: 1px solid __BORDER__; border-radius: 10px;
    padding: 10px 14px; font-size: 12.5px; line-height: 1.55;
}
QLabel#okBox {
    background: __OK_SOFT__; color: __OK_DEEP__;
    border: 1px solid __OK_BORDER__; border-radius: 10px;
    padding: 12px 14px; font-size: 12.5px; line-height: 1.55;
}
QLabel#errBox {
    background: __ERR_SOFT__; color: __ERR_DEEP__;
    border: 1px solid __ERR_BORDER__; border-radius: 10px;
    padding: 12px 14px; font-size: 12.5px; line-height: 1.55;
}
QLabel#warnBox {
    background: __WARN_SOFT__; color: __WARN_DEEP__;
    border: 1px solid __WARN_BORDER__; border-radius: 10px;
    padding: 10px 14px; font-size: 12.5px; line-height: 1.55;
}

/* ==================== 状态徽章（彩色 chip） ==================== */
QLabel#badgeInfo {
    background: __INFO_SOFT__; color: __INFO__;
    border-radius: 8px; padding: 3px 11px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid __INFO_BORDER__;
}
QLabel#badgeSuccess {
    background: __OK_SOFT__; color: __OK_DEEP__;
    border-radius: 8px; padding: 3px 11px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid __OK_BORDER__;
}
QLabel#badgeWarn {
    background: __WARN_SOFT__; color: __WARN_DEEP__;
    border-radius: 8px; padding: 3px 11px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid __WARN_BORDER__;
}
QLabel#badgeError {
    background: __ERR_SOFT__; color: __ERR_DEEP__;
    border-radius: 8px; padding: 3px 11px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid __ERR_BORDER__;
}
QLabel#badgeMuted {
    background: __MUTED_SOFT__; color: __TEXT_2__;
    border-radius: 8px; padding: 3px 11px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid __MUTED_BORDER__;
}
QLabel#badgeBrand {
    background: __BRAND_SOFT__; color: __BRAND_ACTIVE__;
    border-radius: 8px; padding: 3px 11px;
    font-size: 11.5px; font-weight: 600;
    border: 1px solid #C7D2FE;
}

/* ==================== 按钮 ==================== */
QPushButton {
    background: __CARD_BG__; color: __TEXT_1__;
    border: 1px solid __BORDER_STRONG__; border-radius: 8px;
    padding: 7px 16px; font-weight: 500;
    min-height: 20px;
}
QPushButton:hover {
    background: #FAFAFA; border-color: #A8A29E;
}
QPushButton:pressed { background: #F5F5F4; }
QPushButton:disabled {
    background: #FAFAFA; color: __TEXT_3__;
    border-color: __BORDER__;
}
QPushButton#btnPrimary {
    background: __BRAND__; color: #FFFFFF;
    border: none; border-radius: 8px;
    padding: 8px 18px; font-weight: 600;
}
QPushButton#btnPrimary:hover { background: __BRAND_HOVER__; }
QPushButton#btnPrimary:pressed { background: __BRAND_ACTIVE__; }
QPushButton#btnPrimary:disabled { background: #C7D2FE; color: #FFFFFF; }
QPushButton#btnSuccess {
    background: __OK__; color: #FFFFFF;
    border: none; border-radius: 8px;
    padding: 8px 18px; font-weight: 600;
}
QPushButton#btnSuccess:hover { background: #059669; }
QPushButton#btnSuccess:pressed { background: #047857; }
QPushButton#btnSuccess:disabled { background: #A7F3D0; color: #FFFFFF; }
QPushButton#btnDanger {
    background: __ERR__; color: #FFFFFF;
    border: none; border-radius: 8px;
    padding: 8px 18px; font-weight: 600;
}
QPushButton#btnDanger:hover { background: #DC2626; }
QPushButton#btnDanger:pressed { background: __ERR_DEEP__; }
QPushButton#btnDanger:disabled { background: #FECACA; color: #FFFFFF; }
QPushButton#btnGhost {
    background: transparent; color: __TEXT_2__;
    border: 1px solid __BORDER__; border-radius: 8px;
    padding: 7px 16px; font-weight: 500;
}
QPushButton#btnGhost:hover {
    background: __SUBTLE_BG__; color: __TEXT_1__;
    border-color: __BORDER_STRONG__;
}
QPushButton#btnGhost:pressed { background: #F5F5F4; }
QPushButton#btnGhost:disabled {
    color: __TEXT_3__; border-color: __BORDER__;
    background: transparent;
}
QPushButton#btnLink {
    background: transparent; color: __BRAND__;
    border: none; padding: 4px 8px; font-weight: 500;
    text-align: left;
}
QPushButton#btnLink:hover { color: __BRAND_HOVER__; }
QPushButton#btnMini {
    background: __SUBTLE_BG__; color: __TEXT_2__;
    border: 1px solid __BORDER__; border-radius: 6px;
    padding: 3px 10px; font-size: 12px; font-weight: 500;
}
QPushButton#btnMini:hover {
    background: __BRAND_SOFT__; border-color: #C7D2FE;
    color: __BRAND_ACTIVE__;
}
QPushButton#btnMini:pressed { background: __BRAND_SOFT_2__; }
QPushButton#btnMiniDanger {
    background: __SUBTLE_BG__; color: __TEXT_2__;
    border: 1px solid __BORDER__; border-radius: 6px;
    padding: 3px 10px; font-size: 12px; font-weight: 500;
}
QPushButton#btnMiniDanger:hover {
    background: __ERR_SOFT__; border-color: __ERR_BORDER__;
    color: __ERR_DEEP__;
}

/* ==================== 输入控件 ==================== */
QLineEdit, QComboBox, QDateTimeEdit, QSpinBox, QDoubleSpinBox {
    background: __CARD_BG__; color: __TEXT_1__;
    border: 1px solid __BORDER_STRONG__; border-radius: 8px;
    padding: 6px 10px; selection-background-color: __BRAND_SOFT_2__;
    selection-color: __BRAND_ACTIVE__; min-height: 20px;
}
QLineEdit:focus, QComboBox:focus, QDateTimeEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid __BORDER_FOCUS__;
    background: __CARD_BG__;
}
QLineEdit:disabled, QComboBox:disabled, QDateTimeEdit:disabled,
QSpinBox:disabled, QDoubleSpinBox:disabled {
    background: __SUBTLE_BG__; color: __TEXT_3__;
    border-color: __BORDER__;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow { image: url("__CHEV_DOWN__"); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: __CARD_BG__; border: 1px solid __BORDER_STRONG__;
    border-radius: 10px; selection-background-color: __BRAND_SOFT__;
    selection-color: __BRAND_ACTIVE__;
    outline: 0; padding: 6px;
    color: __TEXT_1__;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 22px; border: none; background: transparent;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 22px; border: none; background: transparent;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("__CHEV_UP__"); width: 10px; height: 10px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("__CHEV_DOWN__"); width: 10px; height: 10px; }
QSpinBox, QDoubleSpinBox { padding-right: 28px; }
QDateTimeEdit::drop-down {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 24px; border: none; background: transparent;
}
QDateTimeEdit::down-arrow { image: url("__CHEV_DOWN__"); width: 12px; height: 12px; }
QDateTimeEdit { padding-right: 28px; }
QCalendarWidget QWidget { background: __CARD_BG__; }
QCalendarWidget QToolButton {
    color: __TEXT_1__; background: transparent; border: none;
    border-radius: 6px; padding: 4px 10px;
}
QCalendarWidget QToolButton:hover { background: __BRAND_SOFT__; }
QCalendarWidget QAbstractItemView:enabled {
    background: __CARD_BG__; color: __TEXT_1__;
    selection-background-color: __BRAND__; selection-color: #FFFFFF;
}

/* ==================== 复选 / 单选 ==================== */
QCheckBox, QRadioButton { spacing: 8px; color: __TEXT_1__; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 18px; height: 18px;
    border: 1.5px solid __BORDER_STRONG__; background: __CARD_BG__;
    border-radius: 5px;
}
QRadioButton::indicator { border-radius: 10px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: __BRAND__;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: __BRAND__; border-color: __BRAND__;
}
QCheckBox::indicator:checked { image: url("__CHECK__"); }
QRadioButton::indicator:checked { image: url("__DOT__"); }
QCheckBox:disabled, QRadioButton:disabled { color: __TEXT_3__; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {
    background: __SUBTLE_BG__; border-color: __BORDER__;
}

/* ==================== 表格 ==================== */
QTableWidget {
    background: __CARD_BG__; border: 1px solid __BORDER__;
    border-radius: 12px; gridline-color: transparent;
    alternate-background-color: #FAFAFA;
    selection-background-color: __BRAND_SOFT__;
    selection-color: __BRAND_ACTIVE__;
    outline: 0; padding: 0;
}
QTableWidget::item {
    padding: 8px 10px; border: none;
    color: __TEXT_1__;
}
QTableWidget::item:hover { background: #FAFAFA; }
QTableWidget::item:selected {
    background: __BRAND_SOFT__; color: __BRAND_ACTIVE__;
}
QHeaderView::section {
    background: #FAFAFA; color: __TEXT_2__;
    padding: 10px 10px; border: none;
    border-bottom: 1px solid __BORDER__;
    font-weight: 600; font-size: 12px;
    letter-spacing: 0.2px;
}
QHeaderView::section:first { border-top-left-radius: 12px; }
QHeaderView::section:last { border-top-right-radius: 12px; }
QTableCornerButton::section { background: #FAFAFA; border: none; }
QTableWidget QPushButton { padding: 4px 10px; }

/* ==================== 日志（深色面板） ==================== */
QPlainTextEdit {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #0F172A, stop:1 #1E293B);
    color: #E2E8F0; border: 1px solid #1E293B; border-radius: 12px;
    padding: 14px; font-family: __FONT_MONO__;
    font-size: 12.5px; selection-background-color: #334155;
    selection-color: #FFFFFF;
}

/* ==================== 托盘菜单 / 提示 ==================== */
QMenu {
    background: __CARD_BG__; border: 1px solid __BORDER__;
    border-radius: 10px; padding: 6px;
    color: __TEXT_1__;
}
QMenu::item { padding: 8px 24px; border-radius: 6px; }
QMenu::item:selected { background: __BRAND_SOFT__; color: __BRAND_ACTIVE__; }
QMenu::item:disabled { color: __TEXT_3__; }
QMenu::separator { height: 1px; background: __BORDER__; margin: 5px 10px; }
QToolTip {
    background: #18181B; color: #F4F4F5; border: none;
    border-radius: 6px; padding: 6px 10px; font-size: 12px;
}

/* ==================== 消息框 ==================== */
QMessageBox { background: __CARD_BG__; }
QMessageBox QLabel { color: __TEXT_1__; }
QMessageBox QPushButton { min-width: 76px; }

/* ==================== 进度条 ==================== */
QProgressBar {
    background: #F5F5F4; border: none; border-radius: 6px;
    text-align: center; color: __TEXT_2__;
    font-size: 11.5px; font-weight: 600; height: 8px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 __BRAND__, stop:1 #A78BFA);
    border-radius: 6px;
}

/* ==================== 分组框（已基本不用，保留兜底） ==================== */
QGroupBox { border: none; margin-top: 0; padding: 0; }
"""


def build_qss() -> str:
    a = ensure_ui_assets()
    s = _QSS_TMPL
    repls = {
        "__TEXT_1__": T.TEXT_1, "__TEXT_2__": T.TEXT_2, "__TEXT_3__": T.TEXT_3,
        "__TEXT_INV__": T.TEXT_INV, "__APP_BG__": T.APP_BG, "__CARD_BG__": T.CARD_BG,
        "__SUBTLE_BG__": T.SUBTLE_BG, "__BORDER__": T.BORDER,
        "__BORDER_STRONG__": T.BORDER_STRONG, "__BORDER_FOCUS__": T.BORDER_FOCUS,
        "__BRAND__": T.BRAND, "__BRAND_HOVER__": T.BRAND_HOVER,
        "__BRAND_ACTIVE__": T.BRAND_ACTIVE, "__BRAND_SOFT__": T.BRAND_SOFT,
        "__BRAND_SOFT_2__": T.BRAND_SOFT_2,
        "__OK__": T.OK, "__OK_SOFT__": T.OK_SOFT, "__OK_BORDER__": T.OK_BORDER,
        "__OK_DEEP__": T.OK_DEEP,
        "__WARN__": T.WARN, "__WARN_SOFT__": T.WARN_SOFT,
        "__WARN_BORDER__": T.WARN_BORDER, "__WARN_DEEP__": T.WARN_DEEP,
        "__ERR__": T.ERR, "__ERR_SOFT__": T.ERR_SOFT,
        "__ERR_BORDER__": T.ERR_BORDER, "__ERR_DEEP__": T.ERR_DEEP,
        "__INFO__": T.INFO, "__INFO_SOFT__": T.INFO_SOFT,
        "__INFO_BORDER__": T.INFO_BORDER, "__MUTED__": T.MUTED,
        "__MUTED_SOFT__": T.MUTED_SOFT, "__MUTED_BORDER__": T.MUTED_BORDER,
        "__HDR_TOP__": T.HDR_BG_TOP, "__HDR_MID__": T.HDR_BG_MID,
        "__HDR_BOT__": T.HDR_BG_BOT, "__HDR_ACCENT__": T.HDR_ACCENT,
        "__HDR_TEXT__": T.HDR_TEXT,
        "__FONT__": T.FONT_FAMILY, "__FONT_MONO__": T.FONT_MONO,
        "__CHEV_DOWN__": a["ic_chevron_down.png"],
        "__CHEV_UP__": a["ic_chevron_up.png"],
        "__CHECK__": a["ic_check.png"],
        "__DOT__": a["ic_dot.png"],
    }
    for k, v in repls.items():
        s = s.replace(k, v)
    return s


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def run_in_thread(fn, *args, **kwargs):
    """在后台线程跑一个函数，异常不外泄。"""
    def wrapper():
        try:
            fn(*args, **kwargs)
        except Exception as e:  # noqa
            print(f"[app] 后台任务异常: {e!r}", file=sys.stderr)
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# 跨线程信号桥（Monitor 回调 → Qt 信号）
# ---------------------------------------------------------------------------
class Bridge(QObject):
    log = Signal(str, str)
    state = Signal(dict)
    found = Signal(dict)
    result = Signal(dict)
    search_done = Signal(dict, str)
    meta_ready = Signal(dict)
    enums_ready = Signal(dict)
    test_done = Signal(dict, str)
    notify = Signal(str, str, str)
    live_changed = Signal(bool)   # True=监控运行中


# ---------------------------------------------------------------------------
# UI 组件工厂
# ---------------------------------------------------------------------------
def make_badge(text: str, level: str = "muted") -> QLabel:
    """状态徽章。level: info/success/warn/error/muted/brand"""
    lbl = QLabel(text)
    lbl.setObjectName({
        "info": "badgeInfo", "success": "badgeSuccess",
        "warn": "badgeWarn", "error": "badgeError",
        "muted": "badgeMuted", "brand": "badgeBrand",
    }.get(level, "badgeMuted"))
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


def make_card_title(eyebrow: str, title: str, sub: str = "") -> QWidget:
    """卡片头部：eyebrow（小字大写） + 标题 + 副标题。"""
    w = QWidget()
    w.setObjectName("cardHead")
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    if eyebrow:
        eb = QLabel(eyebrow)
        eb.setObjectName("cardEyebrow")
        v.addWidget(eb)
    t = QLabel(title)
    t.setObjectName("cardTitle")
    v.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setObjectName("cardSub")
        s.setWordWrap(True)
        v.addWidget(s)
    return w


def make_card(title: str, body_widget: QWidget, eyebrow: str = "",
              sub: str = "") -> QFrame:
    """带标题的标准卡片。"""
    card = QFrame()
    card.setObjectName("card")
    v = QVBoxLayout(card)
    v.setContentsMargins(20, 18, 20, 18)
    v.setSpacing(14)
    v.addWidget(make_card_title(eyebrow, title, sub))
    v.addWidget(body_widget)
    return card


# ---------------------------------------------------------------------------
# 页面脚手架：页头（固定，不滚动） + 内容区（独立滚动）
# ---------------------------------------------------------------------------
class PageScaffold(QWidget):
    """统一的页面骨架。

    结构（从上到下）：
      ┌ 页头：标题 + 副标题（左）…… 主操作按钮（右，固定不动）
      ├ 内容区：QScrollArea，纵向滚动；低于内容最小高度时自动出滚动条
      └ 底栏（可选）：贴底的操作条，同样不随内容滚动

    这样窗口再小也不会把按钮和表格挤扁——要么内容滚，要么表格保持最小高度。
    """

    def __init__(self, title: str, subtitle: str = "", parent=None,
                 scrollable: bool = True):
        super().__init__(parent)
        self.setObjectName("pageRoot")
        self._scrollable = scrollable
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- 页头（固定） ----
        header = QWidget()
        header.setObjectName("pageHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 18, 28, 16)
        hl.setSpacing(16)
        tbox = QVBoxLayout()
        tbox.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("pageTitle")
        tbox.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("pageSub")
            s.setWordWrap(True)
            tbox.addWidget(s)
        hl.addLayout(tbox, 1)
        self.actions_row = QHBoxLayout()
        self.actions_row.setSpacing(8)
        hl.addLayout(self.actions_row, 0)
        outer.addWidget(header)

        # ---- 内容区（默认滚动；日志这类自带滚动的控件可关掉） ----
        self.content = QWidget()
        self.content.setObjectName("pageContent")
        self.cv = QVBoxLayout(self.content)
        self.cv.setContentsMargins(28, 18, 28, 22)
        self.cv.setSpacing(16)
        if scrollable:
            self.scroll = QScrollArea()
            self.scroll.setObjectName("pageScroll")
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.NoFrame)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.scroll.setWidget(self.content)
            outer.addWidget(self.scroll, 1)
        else:
            self.scroll = None
            outer.addWidget(self.content, 1)

        # ---- 底栏（可选） ----
        self.footer = QWidget()
        self.footer.setObjectName("pageFooter")
        self.footer_l = QHBoxLayout(self.footer)
        self.footer_l.setContentsMargins(28, 12, 28, 12)
        self.footer_l.setSpacing(10)
        self.footer.setVisible(False)
        outer.addWidget(self.footer)

    def add_action(self, widget):
        self.actions_row.addWidget(widget)

    def add_footer_widget(self, widget, stretch: int = 0):
        self.footer.setVisible(True)
        if widget is None:
            self.footer_l.addStretch(stretch or 1)
        else:
            self.footer_l.addWidget(widget, stretch)

    def add_footer_stretch(self, stretch: int = 1):
        self.footer.setVisible(True)
        self.footer_l.addStretch(stretch)

    def content_layout(self) -> QVBoxLayout:
        return self.cv


class NavButton(QPushButton):
    """左侧导航项：图标 + 文字 + 可选计数徽章。"""

    def __init__(self, icon_key: str, text: str):
        super().__init__()
        self.setObjectName("navBtn")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(38)
        self._icon_key = icon_key
        self._text = text
        self._badge_text = ""
        self._collapsed = False
        self._icon = QIcon(ensure_ui_assets()[icon_key])
        self._refresh()

    def set_badge(self, text: str):
        if text == self._badge_text:
            return
        self._badge_text = text
        self._refresh()

    def set_collapsed(self, collapsed: bool):
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._refresh()

    def _refresh(self):
        if self._collapsed:
            self.setIcon(self._icon)
            self.setIconSize(QSize(18, 18))
            self.setText("")
            tip = self._text
            if self._badge_text:
                tip = f"{self._text}（{self._badge_text}）"
            self.setToolTip(tip)
        else:
            self.setIcon(self._icon)
            self.setIconSize(QSize(16, 16))
            self.setText(("  " + self._text) if not self._badge_text
                         else f"  {self._text}    {self._badge_text}")
            self.setToolTip("")


class NavPane(QWidget):
    """左侧导航栏。窗口变窄时自动折叠为纯图标栏（并允许手动切换）。"""

    COLLAPSE_WIDTH = 1040
    WIDTH_EXPANDED = 216
    WIDTH_COLLAPSED = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("navPane")
        self._collapsed = False
        self._items: list[NavButton] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 14, 10, 14)
        v.setSpacing(4)

        a = ensure_ui_assets()
        self.toggle_btn = QPushButton()
        self.toggle_btn.setObjectName("navToggle")
        self.toggle_btn.setIcon(QIcon(a["ic_menu.png"]))
        self.toggle_btn.setIconSize(QSize(18, 18))
        self.toggle_btn.setFixedHeight(36)
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.setToolTip("收起 / 展开导航栏")
        v.addWidget(self.toggle_btn)

        self.main_box = QVBoxLayout()
        self.main_box.setSpacing(4)
        v.addLayout(self.main_box)
        v.addSpacing(6)

        line = QFrame()
        line.setObjectName("navSep")
        line.setFrameShape(QFrame.HLine)
        v.addWidget(line)
        v.addSpacing(6)

        self.bottom_box = QVBoxLayout()
        self.bottom_box.setSpacing(4)
        v.addLayout(self.bottom_box)
        v.addStretch(1)

    def add_item(self, btn: NavButton, bottom: bool = False):
        btn.set_collapsed(self._collapsed)
        (self.bottom_box if bottom else self.main_box).addWidget(btn)
        self._items.append(btn)
        return btn

    def set_collapsed(self, collapsed: bool):
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.setFixedWidth(self.WIDTH_COLLAPSED if collapsed else self.WIDTH_EXPANDED)
        for b in self._items:
            b.set_collapsed(collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed


# ---------------------------------------------------------------------------
# 底部状态栏（Windows 桌面软件的常驻信息条）
# ---------------------------------------------------------------------------
class AppStatusBar(QWidget):
    """底部状态栏：任何时候都想瞄一眼的信息放这里，不占页面空间。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("appStatusBar")
        self.setFixedHeight(34)
        h = QHBoxLayout(self)
        h.setContentsMargins(18, 0, 18, 0)
        h.setSpacing(0)

        self.run_dot = QLabel("")
        self.run_dot.setObjectName("statusDot")
        self.run_dot.setFixedSize(9, 9)
        h.addWidget(self.run_dot)
        h.addSpacing(8)
        self.run_label = QLabel("待命")
        self.run_label.setObjectName("statusText")
        h.addWidget(self.run_label)
        h.addSpacing(6)
        h.addWidget(self._sep())

        self.session_label = QLabel("会话未验证")
        self.session_label.setObjectName("statusText")
        h.addWidget(self.session_label)
        h.addWidget(self._sep())

        self.term_label = QLabel("学期 —")
        self.term_label.setObjectName("statusText")
        h.addWidget(self.term_label)
        h.addWidget(self._sep())

        self.target_label = QLabel("目标 0")
        self.target_label.setObjectName("statusText")
        h.addWidget(self.target_label)
        h.addWidget(self._sep())

        self.interval_label = QLabel("轮询 —")
        self.interval_label.setObjectName("statusText")
        h.addWidget(self.interval_label)
        h.addWidget(self._sep())

        self.req_label = QLabel("请求 0")
        self.req_label.setObjectName("statusText")
        h.addWidget(self.req_label)

        h.addStretch(1)
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("statusHint")
        h.addWidget(self.hint_label)
        self._req_count = 0

    @staticmethod
    def _sep() -> QFrame:
        f = QFrame()
        f.setObjectName("statusSep")
        f.setFrameShape(QFrame.VLine)
        f.setFixedHeight(16)
        return f

    def set_running(self, active: bool):
        self.run_dot.setObjectName("statusDotActive" if active else "statusDot")
        self.run_dot.style().unpolish(self.run_dot)
        self.run_dot.style().polish(self.run_dot)
        self.run_label.setText("监控运行中" if active else "待命")

    def set_session(self, ok: bool):
        self.session_label.setText("会话有效" if ok else "会话未验证")

    def set_term(self, text: str):
        self.term_label.setText(text or "学期 —")

    def set_targets(self, n: int):
        self.target_label.setText(f"目标 {n}")

    def set_interval(self, seconds):
        self.interval_label.setText(f"轮询 {seconds}s" if seconds else "轮询 —")

    def set_hint(self, text: str):
        self.hint_label.setText(text or "")

    def bump_requests(self, n: int = 1):
        self._req_count += n
        self.req_label.setText(f"请求 {self._req_count}")


# ---------------------------------------------------------------------------
# 二级界面：课程详情
# ---------------------------------------------------------------------------
class CourseDetailDialog(QDialog):
    """课程详情。

    表格一行塞不下完整信息（上课时间往往很长），详情做成二级界面单独看。
    主按钮「加入抢课目标」—— accept() 即视为确认加入。
    """

    def __init__(self, info: dict, way_name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("detailDialog")
        self.setWindowTitle("课程详情")
        self.setMinimumWidth(520)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(16)

        hero = QFrame()
        hero.setObjectName("detailHero")
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(20, 18, 20, 18)
        hv.setSpacing(6)
        t = QLabel(info.get("课程名称") or "—")
        t.setObjectName("detailTitle")
        t.setWordWrap(True)
        hv.addWidget(t)
        c = QLabel(f"{info.get('课程代码') or '—'}　课序号 {info.get('课序号') or '—'}"
                   + (f"　·　{way_name}" if way_name else ""))
        c.setObjectName("detailCode")
        hv.addWidget(c)
        root.addWidget(hero)

        # 余量一眼看到
        rem_txt = info.get("余量") or "—"
        try:
            rem_n = int(rem_txt)
            rem_badge = make_badge(f"余量 {rem_n}", "success" if rem_n > 0 else "error")
        except (TypeError, ValueError):
            rem_badge = make_badge(f"余量 {rem_txt}", "muted")
        rem_badge.setMinimumHeight(30)
        root.addWidget(rem_badge)

        grid = QGridLayout()
        grid.setContentsMargins(2, 0, 2, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(0)
        row = 0
        for key in ("课程类别", "课程性质", "学分", "总学时", "校区",
                    "任课教师", "容量", "已选"):
            k = QLabel(key)
            k.setObjectName("detailKey")
            v = QLabel(info.get(key) or "—")
            v.setObjectName("detailVal")
            v.setWordWrap(True)
            grid.addWidget(k, row, 0)
            grid.addWidget(v, row, 1)
            row += 1
        k = QLabel("上课时间")
        k.setObjectName("detailKey")
        v = QLabel(info.get("上课时间") or "—")
        v.setObjectName("detailVal")
        v.setWordWrap(True)
        grid.addWidget(k, row, 0)
        grid.addWidget(v, row, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        root.addStretch(1)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        btns.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("btnGhost")
        close.setMinimumHeight(38)
        close.clicked.connect(self.reject)
        btns.addWidget(close)
        add = QPushButton(" 加入抢课目标")
        add.setObjectName("btnPrimary")
        add.setIcon(QIcon(ensure_ui_assets()["ic_plus.png"]))
        add.setIconSize(QSize(13, 13))
        add.setMinimumHeight(38)
        add.clicked.connect(self.accept)
        btns.addWidget(add)
        root.addLayout(btns)


# ---------------------------------------------------------------------------
# 二级界面：关于
# ---------------------------------------------------------------------------
class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("detailDialog")
        self.setWindowTitle("关于")
        self.setFixedWidth(440)
        self.setModal(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 22)
        root.setSpacing(14)

        hero = QHBoxLayout()
        brand = QLabel()
        brand.setFixedSize(44, 44)
        brand.setPixmap(QPixmap(ensure_ui_assets()["brand.png"]).scaled(
            44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        hero.addWidget(brand)
        tb = QVBoxLayout()
        tb.setSpacing(2)
        t = QLabel("USTB Selector")
        t.setObjectName("detailTitle")
        tb.addWidget(t)
        s = QLabel("北京科技大学 · 选课抢课助手")
        s.setObjectName("detailCode")
        tb.addWidget(s)
        hero.addLayout(tb)
        hero.addStretch(1)
        root.addLayout(hero)

        body = QLabel(
            "单账号、本人自用的选课辅助工具。所有请求均发往教务系统官方接口，"
            "仅用于缩短人工重复操作的时间。\n\n"
            "· SESSION Cookie 只保存在本机 config.json，不会上传任何服务器\n"
            "· 默认轮询间隔不低于 3 秒，避免对教务系统造成压力\n"
            "· 关闭窗口不做退出，程序缩到系统托盘继续监控")
        body.setObjectName("detailVal")
        body.setWordWrap(True)
        root.addWidget(body)

        ver = make_badge("v1.0 · 个人版", "brand")
        ver.setMinimumHeight(28)
        root.addWidget(ver)
        root.addStretch(1)

        ok = QPushButton("好")
        ok.setObjectName("btnPrimary")
        ok.setMinimumHeight(38)
        ok.clicked.connect(self.accept)
        br = QHBoxLayout()
        br.addStretch(1)
        br.addWidget(ok)
        root.addLayout(br)


# ---------------------------------------------------------------------------
# 实时状态指示器（脉冲动画）
# ---------------------------------------------------------------------------
class LiveIndicator(QLabel):
    """右上角实时状态指示：脉冲绿点 + 文字。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("appLive")
        self._active = False
        self._anim_phase = 0
        self.setText("  待命")
        self.set_active(False)

    def set_active(self, active: bool):
        self._active = active
        if not hasattr(self, "_timer"):
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
        if active:
            self.setText("  监控运行中")
            self._timer.start(50)
        else:
            self._timer.stop()
            self.set_pixmap_from_state(0)

    def set_pixmap_from_state(self, phase: int):
        a = ensure_ui_assets()
        if self._active:
            pm = QPixmap(a["ic_pulse.png"])
        else:
            pm = QPixmap(a["ic_idle.png"])
        # 用 phase 做透明度变化（通过 painter 重绘）
        if phase > 0 and self._active:
            overlay = QPixmap(pm.size())
            overlay.fill(Qt.transparent)
            p = QPainter(overlay)
            p.setRenderHint(QPainter.Antialiasing)
            p.setOpacity(0.3 + 0.7 * (1 - abs(phase - 0.5) * 2))
            p.drawPixmap(0, 0, pm)
            p.end()
            pm = overlay
        self.setPixmap(pm.scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        # 图标在文字左侧需要 prepend
        txt = self.text().lstrip()
        # 重新组合：图标 + 空格 + 文字
        self.setText("  " + txt)

    def _tick(self):
        self._anim_phase = (self._anim_phase + 0.05) % 1.0
        self.set_pixmap_from_state(self._anim_phase)


# ---------------------------------------------------------------------------
# 微信扫码登录弹窗
# ---------------------------------------------------------------------------
class QrLoginDialog(QDialog):
    qr_ready = Signal(bytes)
    status = Signal(str)
    login_ok = Signal(str)
    login_err = Signal(str)
    expired = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("微信扫码登录 · 校园微认证")
        self.setModal(True)
        self.setMinimumSize(380, 520)
        self.setObjectName("qrDialog")
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 26, 28, 22)
        v.setSpacing(16)

        # 头部
        head_row = QHBoxLayout()
        head_row.setSpacing(12)
        head_row.setContentsMargins(0, 0, 0, 0)
        qr_icon = QLabel()
        a = ensure_ui_assets()
        qr_icon.setPixmap(QPixmap(a["ic_qr.png"]).scaled(
            22, 22, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head_row.addWidget(qr_icon)
        head = QLabel("微信扫码登录")
        head.setObjectName("qrTitle")
        head_row.addWidget(head, 1)
        v.addLayout(head_row)

        sub = QLabel("用微信扫描下方二维码，在手机上点确认即可获取 SESSION。")
        sub.setObjectName("qrSub")
        sub.setWordWrap(True)
        v.addWidget(sub)

        # 二维码卡片
        qr_wrap = QFrame()
        qr_wrap.setObjectName("qrCard")
        qv = QVBoxLayout(qr_wrap)
        qv.setContentsMargins(18, 18, 18, 18)
        qv.setAlignment(Qt.AlignCenter)
        self.qr_label = QLabel("正在生成二维码…")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setMinimumSize(280, 280)
        qv.addWidget(self.qr_label)
        v.addWidget(qr_wrap, 1)

        # 状态
        self.status_label = QLabel("正在生成二维码…")
        self.status_label.setObjectName("qrStatus")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        v.addWidget(self.status_label)

        # 按钮
        row = QHBoxLayout()
        row.setSpacing(10)
        self.refresh_btn = QPushButton(" 刷新二维码")
        self.refresh_btn.setObjectName("btnGhost")
        self.refresh_btn.setIcon(QIcon(a["ic_refresh.png"]))
        self.refresh_btn.setIconSize(QSize(14, 14))
        self.refresh_btn.setMinimumHeight(36)
        self.refresh_btn.clicked.connect(self.on_refresh)
        self.close_btn = QPushButton("关闭")
        self.close_btn.setObjectName("btnGhost")
        self.close_btn.setMinimumHeight(36)
        self.close_btn.clicked.connect(self.close)
        row.addStretch(1)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.close_btn)
        v.addLayout(row)

        self.qr_ready.connect(self._on_qr)
        self.status.connect(self._on_status)
        self.login_ok.connect(self._on_ok)
        self.login_err.connect(self._on_err)
        self.expired.connect(self._on_expired)
        self._login: QrLogin | None = None
        self._ok_session = ""
        self._start()

    def _start(self):
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("正在生成二维码…")
        self.qr_label.setText("正在生成二维码…")
        self._login = QrLogin({
            "on_qr": self.qr_ready.emit,
            "on_status": self.status.emit,
            "on_cookie": self.login_ok.emit,
            "on_expired": self.expired.emit,
            "on_error": self.login_err.emit,
        })
        self._login.start()

    def on_refresh(self):
        if self._login and self._login.is_alive():
            self._login.stop()
        self._start()

    def closeEvent(self, e):
        if self._login and self._login.is_alive():
            self._login.stop()
        super().closeEvent(e)

    def _on_qr(self, data: bytes):
        pm = QPixmap()
        pm.loadFromData(data)
        if pm.isNull():
            self.qr_label.setText("二维码图片加载失败")
            return
        self.qr_label.setPixmap(pm.scaled(260, 260, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))

    def _on_status(self, text: str):
        self.status_label.setText(text)

    def _on_expired(self, text: str):
        self.status_label.setText(text)
        self.refresh_btn.setEnabled(True)

    def _on_err(self, text: str):
        self.status_label.setText(text)
        self.refresh_btn.setEnabled(True)

    def _on_ok(self, session_value: str):
        self._ok_session = session_value
        self.status_label.setText(f"登录成功！SESSION 已获取（{session_value[:10]}…）")
        self.refresh_btn.setEnabled(True)
        self.accept()


# ---------------------------------------------------------------------------
# 页面 1：会话管理
# ---------------------------------------------------------------------------
class SessionPage(QWidget):
    def __init__(self, bridge: Bridge, get_cfg, log):
        super().__init__()
        self.setObjectName("pageRoot")
        self.bridge = bridge
        self.get_cfg = get_cfg
        self.log = log

        self.scaffold = PageScaffold(
            "登录会话", "获取 SESSION 是使用本工具的第一步 — 推荐扫码登录，3 秒搞定。")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scaffold)
        layout = self.scaffold.content_layout()

        # ---- 提示卡 ----
        hint = QLabel(
            "  两种方式获取 SESSION：\n"
            "    ① 推荐 — 点下方「扫码登录（微信）」，微信扫一下即可；\n"
            "    ② 备选 — 浏览器登录 byyt.ustb.edu.cn → F12 → Application → Cookie → 复制 SESSION 值，粘贴到下方。")
        hint.setObjectName("hintBox")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ---- SESSION 输入卡 ----
        input_card = QFrame()
        input_card.setObjectName("card")
        icv = QVBoxLayout(input_card)
        icv.setContentsMargins(20, 18, 20, 18)
        icv.setSpacing(14)
        icv.addWidget(make_card_title("01  SESSION", "输入或扫码获取 Cookie",
                                       "SESSION 是教务系统识别你身份的令牌，请妥善保管。"))

        # 输入行：锁图标 + 输入框
        edit_row = QHBoxLayout()
        edit_row.setSpacing(8)
        self.session_edit = QLineEdit()
        self.session_edit.setPlaceholderText("SESSION Cookie 值（例如 OGY2NTNkOTAt...）")
        self.session_edit.setEchoMode(QLineEdit.Password)
        self.session_edit.setMinimumHeight(38)
        edit_row.addWidget(self.session_edit)
        icv.addLayout(edit_row)
        layout.addWidget(input_card)

        # ---- 会话信息卡 ----
        info_card = QFrame()
        info_card.setObjectName("card")
        icv2 = QVBoxLayout(info_card)
        icv2.setContentsMargins(20, 18, 20, 18)
        icv2.setSpacing(14)
        icv2.addWidget(make_card_title("02  会话信息", "测试成功后会显示学期、规则、窗口等",
                                        "数据由后端从教务系统实时拉取，可作为后续抢课参数的依据。"))
        self.info_label = QLabel("尚未测试 — 请先粘贴 SESSION 并点「保存并测试」。")
        self.info_label.setObjectName("tipBox")
        self.info_label.setWordWrap(True)
        icv2.addWidget(self.info_label)
        layout.addWidget(info_card)

        layout.addStretch(1)

        # ---- 主操作提到页头右侧（固定，不随内容滚动） ----
        a = ensure_ui_assets()
        self.qr_btn = QPushButton(" 扫码登录（微信）")
        self.qr_btn.setObjectName("btnPrimary")
        self.qr_btn.setIcon(QIcon(a["ic_qr.png"]))
        self.qr_btn.setIconSize(QSize(16, 16))
        self.qr_btn.setMinimumHeight(38)
        self.qr_btn.clicked.connect(self.on_qr_login)
        self.scaffold.add_action(self.qr_btn)
        self.save_btn = QPushButton("保存并测试")
        self.save_btn.setObjectName("btnSuccess")
        self.save_btn.setMinimumHeight(38)
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self.on_save_test)
        self.scaffold.add_action(self.save_btn)

        self.session_edit.textChanged.connect(
            lambda s: self.save_btn.setEnabled(bool(s.strip())))

    def refresh(self):
        cfg = self.get_cfg()
        self.session_edit.setText(cfg.get("session", ""))

    def on_qr_login(self):
        dlg = QrLoginDialog(self)
        dlg.exec()
        if dlg._ok_session:
            self.session_edit.setText(dlg._ok_session)
            self.log("success", "扫码登录成功，SESSION 已自动填入")
            self.on_save_test()

    def on_save_test(self):
        sid = self.session_edit.text().strip()
        if not sid:
            self.log("warn", "SESSION 为空，未保存")
            return
        cfg = self.get_cfg()
        cfg["session"] = sid
        save_config(cfg)
        self.log("info", "SESSION 已写入 config.json，开始测试连接…")
        self.save_btn.setEnabled(False)
        self.save_btn.setText("测试中…")
        self.info_label.setText("测试中…")
        self.info_label.setObjectName("tipBox")
        self.info_label.style().unpolish(self.info_label)
        self.info_label.style().polish(self.info_label)
        run_in_thread(self._do_test, cfg)

    def _do_test(self, cfg):
        try:
            c = XKClient(cfg)
            disc = c.discover()
            res = c.list_courses(1)
            r = extract_rules(res, cfg.get("xkfsdm"))
            rule = r["rule"]
            c.sync_server_time(rule.get("dqsj"))
            info = {
                "xnxq": f"{disc.get('p_dqxn')} 学期{disc.get('p_dqxq')}",
                "total": (res.get("kxrwList") or {}).get("total"),
                "enrolled_all": len(r["enrolled_codes"]),
                "enrolled_way": len(r["enrolled_way"]),
                "xkms": rule.get("xkms"), "limit": rule.get("xkzys"),
                "window": f"{rule.get('ksrq')} ~ {rule.get('jsrq')}",
                "offset": c.server_offset,
            }
            self.bridge.test_done.emit(info, "")
        except SessionExpired as e:
            self.bridge.test_done.emit({}, f"SESSION 失效：{e}")
        except Exception as e:  # noqa
            self.bridge.test_done.emit({}, f"测试失败：{e!r}")


# ---------------------------------------------------------------------------
# 页面 2：课程搜索
# ---------------------------------------------------------------------------
class SearchPage(QWidget):
    """课程搜索页 — 全量课程 + 服务端筛选 + 分页。"""
    target_set = Signal()

    COLUMNS = ["课程代码", "课程名称", "课序号", "学分", "总学时", "课程类别",
               "课程性质", "校区", "上课时间", "任课教师", "容量", "已选", "余量", "操作"]
    PAGE_SIZES = [10, 20, 30, 50, 100]
    SFMXZJ = [("全部", ""), ("面向", "1"), ("不面向", "-1")]

    def __init__(self, bridge: Bridge, get_cfg, log):
        super().__init__()
        self.setObjectName("pageRoot")
        self.bridge = bridge
        self.get_cfg = get_cfg
        self.log = log
        self._enums: dict | None = None
        self._loading_enums = False
        self._ways: list[dict] = []
        self._way_loaded = False
        self._page = 1
        self._page_size = 20
        self._pages = 1
        self._total = 0
        self._busy = False
        self._filters_used: dict = {}
        self._client = None

        self.scaffold = PageScaffold(
            "课程搜索", "像教务系统那样浏览与筛选全部可选课程，把心仪的课加入抢课目标。")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scaffold)
        layout = self.scaffold.content_layout()
        a = ensure_ui_assets()

        # ---- 01 选课范围卡 ----
        range_card = QFrame()
        range_card.setObjectName("card")
        rcv = QVBoxLayout(range_card)
        rcv.setContentsMargins(20, 18, 20, 18)
        rcv.setSpacing(12)
        rcv.addWidget(make_card_title("01  选课范围", "选择选课方式和学期",
                                       "不同选课方式的可选课、限选门数、开抢时间各不相同。"))
        r1 = QHBoxLayout()
        r1.setSpacing(12)
        r1.setContentsMargins(0, 4, 0, 0)
        self.f_way = self._combo_min()
        r1.addWidget(self._wrap_labeled("选课方式", self.f_way), 2)
        self.f_way.currentIndexChanged.connect(self._on_way_changed)
        self.f_kkxnxq = self._combo_min()
        r1.addWidget(self._wrap_labeled("学年学期", self.f_kkxnxq), 2)
        self.f_kkxnxq.currentIndexChanged.connect(self._on_kkxnxq_changed)
        r1.addStretch(1)
        self.way_rule_label = QLabel("")
        self.way_rule_label.setObjectName("badgeBrand")
        self.way_rule_label.setAlignment(Qt.AlignCenter)
        self.way_rule_label.setVisible(False)
        r1.addWidget(self.way_rule_label)
        rcv.addLayout(r1)
        layout.addWidget(range_card)

        # ---- 02 筛选条件卡（可折叠：小窗口收起后把空间全留给表格） ----
        filter_card = QFrame()
        filter_card.setObjectName("card")
        fcv = QVBoxLayout(filter_card)
        fcv.setContentsMargins(20, 16, 20, 18)
        fcv.setSpacing(12)

        head_row = QHBoxLayout()
        head_row.setSpacing(10)
        self.filters_toggle = QPushButton("  筛选条件")
        self.filters_toggle.setObjectName("sectionToggle")
        self.filters_toggle.setCheckable(True)
        self.filters_toggle.setChecked(True)
        self.filters_toggle.setIcon(QIcon(a["ic_filter.png"]))
        self.filters_toggle.setIconSize(QSize(15, 15))
        self.filters_toggle.setCursor(Qt.PointingHandCursor)
        self.filters_toggle.setToolTip("收起筛选条件，把纵向空间全部留给课程列表")
        head_row.addWidget(self.filters_toggle)
        sub = QLabel("服务端过滤（与教务系统一致）· 填得越少命中越多")
        sub.setObjectName("cardSub")
        head_row.addWidget(sub)
        head_row.addStretch(1)
        fcv.addLayout(head_row)

        self.filters_body = QWidget()
        fbv = QVBoxLayout(self.filters_body)
        fbv.setContentsMargins(0, 0, 0, 0)
        fbv.setSpacing(10)

        f1 = QHBoxLayout()
        f1.setSpacing(10)
        self.f_gjz = QLineEdit()
        self.f_gjz.setPlaceholderText("课程代码 / 名称")
        self.f_gjz.setMinimumHeight(34)
        f1.addWidget(self._wrap_labeled("关键字", self.f_gjz), 1)
        self.f_skjs = QLineEdit()
        self.f_skjs.setPlaceholderText("姓名（模糊）")
        self.f_skjs.setMinimumHeight(34)
        f1.addWidget(self._wrap_labeled("授课教师", self.f_skjs), 1)
        self.f_xiaoqu = self._combo_min()
        f1.addWidget(self._wrap_labeled("校区", self.f_xiaoqu), 1)
        self.f_skyy = self._combo_min()
        f1.addWidget(self._wrap_labeled("授课语言", self.f_skyy), 1)
        self.f_kcxz = self._combo_min()
        f1.addWidget(self._wrap_labeled("课程性质", self.f_kcxz), 1)
        fbv.addLayout(f1)

        f2 = QHBoxLayout()
        f2.setSpacing(10)
        self.f_kkyx = self._combo_min()
        f2.addWidget(self._wrap_labeled("开课学院", self.f_kkyx), 2)
        self.f_kclb = self._combo_min()
        f2.addWidget(self._wrap_labeled("课程类别", self.f_kclb), 2)
        self.f_sfmxzj = self._combo_min()
        f2.addWidget(self._wrap_labeled("是否面向自己", self.f_sfmxzj), 1)
        self.cb_hlct = QCheckBox("忽略冲突课程")
        self.cb_hlct.setStyleSheet("QCheckBox { color: " + T.TEXT_2 + "; }")
        self.cb_hllr = QCheckBox("忽略零容量课程")
        self.cb_hllr.setStyleSheet("QCheckBox { color: " + T.TEXT_2 + "; }")
        cb_row = QHBoxLayout()
        cb_row.setSpacing(16)
        cb_row.addWidget(self.cb_hlct)
        cb_row.addWidget(self.cb_hllr)
        cb_row.addStretch(1)
        f2.addLayout(cb_row, 1)
        fbv.addLayout(f2)

        fcv.addWidget(self.filters_body)
        self.filters_toggle.toggled.connect(self.filters_body.setVisible)
        layout.addWidget(filter_card)

        # ---- 03 课程表（最小高度：小窗口时整页滚动，而不是被压扁） ----
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setMinimumHeight(360)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for col, w in ((0, 80), (2, 60), (3, 50), (4, 60), (5, 180), (6, 70),
                       (7, 75), (8, 240), (9, 90), (10, 55), (11, 55), (12, 55), (13, 90)):
            self.table.setColumnWidth(col, w)
        layout.addWidget(self.table, 1)
        self.table.doubleClicked.connect(self._on_row_detail)

        # ---- 页头右侧：主操作（固定，不随内容滚动） ----
        self.search_btn = QPushButton(" 查询")
        self.search_btn.setObjectName("btnPrimary")
        self.search_btn.setIcon(QIcon(a["ic_search.png"]))
        self.search_btn.setIconSize(QSize(14, 14))
        self.search_btn.setMinimumWidth(110)
        self.search_btn.setMinimumHeight(38)
        self.search_btn.clicked.connect(self.on_search)
        self.scaffold.add_action(self.search_btn)
        self.reset_btn = QPushButton("重置")
        self.reset_btn.setObjectName("btnGhost")
        self.reset_btn.setMinimumHeight(38)
        self.reset_btn.clicked.connect(self.on_reset)
        self.scaffold.add_action(self.reset_btn)
        self.enum_label = QLabel("")
        self.enum_label.setObjectName("sectionLabel")
        self.scaffold.add_action(self.enum_label)
        self.refresh_enum_btn = QPushButton("刷新枚举")
        self.refresh_enum_btn.setObjectName("btnMini")
        self.refresh_enum_btn.setIcon(QIcon(a["ic_refresh.png"]))
        self.refresh_enum_btn.setIconSize(QSize(12, 12))
        self.refresh_enum_btn.setMinimumHeight(38)
        self.refresh_enum_btn.setToolTip("重新从服务器拉取当前选课方式的筛选枚举（清除本地缓存）")
        self.refresh_enum_btn.clicked.connect(self.on_refresh_enums)
        self.scaffold.add_action(self.refresh_enum_btn)

        # ---- 贴底栏：分页常驻可见 ----
        self.page_label = QLabel("共 0 条")
        self.page_label.setObjectName("sectionLabel")
        self.scaffold.add_footer_widget(self.page_label)
        self.size_combo = QComboBox()
        for s in self.PAGE_SIZES:
            self.size_combo.addItem(f"{s} 条/页", s)
        self.size_combo.setCurrentIndex(1)
        self.size_combo.setMinimumHeight(30)
        self.size_combo.currentIndexChanged.connect(self.on_size_changed)
        self.scaffold.add_footer_widget(self.size_combo)
        self.scaffold.add_footer_widget(None, 1)
        self.btn_first = QPushButton("首页")
        self.btn_prev = QPushButton("上一页")
        self.page_cur = QLabel("第 1 / 1 页")
        self.page_cur.setStyleSheet(f"color:{T.TEXT_2}; font-weight:600; padding:0 8px;")
        self.btn_next = QPushButton("下一页")
        self.btn_last = QPushButton("末页")
        for b in (self.btn_first, self.btn_prev, self.btn_next, self.btn_last):
            b.setObjectName("btnMini")
            b.setMinimumHeight(30)
            b.setEnabled(False)
            self.scaffold.add_footer_widget(b)
        self.btn_first.clicked.connect(lambda: self.on_page(1))
        self.btn_prev.clicked.connect(lambda: self.on_page(self._page - 1))
        self.btn_next.clicked.connect(lambda: self.on_page(self._page + 1))
        self.btn_last.clicked.connect(lambda: self.on_page(self._pages))
        self.scaffold.add_footer_widget(self.page_cur)

        self.target_label = QLabel("目标：未设置")
        self.target_label.setObjectName("badgeBrand")
        self.scaffold.add_footer_widget(self.target_label)

        self._init_combos()
        self.f_gjz.returnPressed.connect(self.on_search)
        self.f_skjs.returnPressed.connect(self.on_search)

    def _combo_min(self):
        c = QComboBox()
        c.setMinimumHeight(34)
        return c

    def _wrap_labeled(self, label_text, widget):
        """带文字标签的输入包装：左标签竖排在上、控件在下。"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        l = QLabel(label_text)
        l.setObjectName("sectionLabel")
        v.addWidget(l)
        v.addWidget(widget)
        return w

    def visibleEvent(self, e):
        super().visibleEvent(e)
        self.load_meta()
        self.load_enums()

    # ---- 元数据 ----
    def load_meta(self):
        if self._way_loaded or self._busy:
            return
        self._way_loaded = True
        self.f_way.addItem("加载中…", "")
        self.f_kkxnxq.addItem("加载中…", "")
        run_in_thread(self._do_meta)

    def _do_meta(self):
        try:
            c = self._get_client()
            if not c.form.get("p_dqxnxq"):
                c.discover()
            ways = c.fetch_enroll_ways()
            kk = c.fetch_kkxnxq_list()
            self.bridge.meta_ready.emit({"ways": ways, "kk": kk})
        except SessionExpired as e:
            self.log("error", f"选课方式/学期加载失败（SESSION 失效）：{e}")
            self._way_loaded = False
        except Exception as e:
            self.log("error", f"选课方式/学期加载失败：{e!r}")
            self._way_loaded = False

    def fill_meta(self, data: dict):
        ways = data.get("ways") or []
        self._ways = ways
        cur = (self.get_cfg().get("xkfsdm") or "sztzk-b-b").strip()
        self.f_way.blockSignals(True)
        self.f_way.clear()
        idx = 0
        for i, w in enumerate(ways):
            dm = w.get("xkfsdm") or ""
            mc = w.get("xkfsmc") or dm
            self.f_way.addItem(mc, dm)
            if dm == cur:
                idx = i
        if not ways:
            self.f_way.addItem("素质拓展选课", "sztzk-b-b")
        self.f_way.setCurrentIndex(idx)
        self.f_way.blockSignals(False)

        kk = data.get("kk") or {}
        self.f_kkxnxq.blockSignals(True)
        self.f_kkxnxq.clear()
        self.f_kkxnxq.addItem("当前开课学期", "")
        for opt in kk.get("options") or []:
            self.f_kkxnxq.addItem(opt.get("MC") or "", opt.get("DM") or "")
        self.f_kkxnxq.setCurrentIndex(0)
        self.f_kkxnxq.blockSignals(False)

        self._update_way_rule(cur)
        self.log("info", f"选课方式 {len(ways)} 个、学年学期 {len(kk.get('options') or [])} 项已加载")

    def _on_way_changed(self, idx: int):
        dm = self.f_way.itemData(idx) or ""
        if not dm or self._busy:
            if not dm:
                return
            self.log("warn", "查询进行中，切换已忽略（完成后可再切）")
            return
        c = self._get_client()
        c.switch_way(dm)
        cfg = self.get_cfg()
        cfg["xkfsdm"] = dm
        save_config(cfg)
        self._enums = None
        self._init_combos()
        self.f_gjz.clear()
        self.f_skjs.clear()
        self.cb_hlct.setChecked(False)
        self.cb_hllr.setChecked(False)
        self.f_kkxnxq.blockSignals(True)
        self.f_kkxnxq.setCurrentIndex(0)
        self.f_kkxnxq.blockSignals(False)
        self._update_way_rule(dm)
        self.log("info", f"已切换选课方式：{self.f_way.currentText()}（{dm}）")
        self._reload_enums_for_way()
        self.on_search()

    def _on_kkxnxq_changed(self, idx: int):
        v = self.f_kkxnxq.itemData(idx) or ""
        if self._busy:
            self.log("warn", "查询进行中，切换已忽略")
            return
        c = self._get_client()
        c.form["p_kkxnxq"] = v
        self.log("info", f"开课学期已切换：{self.f_kkxnxq.currentText()}（p_kkxnxq={v or '当前学期'}）")
        self.on_search()

    def _update_way_rule(self, dm: str):
        for w in self._ways:
            if w.get("xkfsdm") == dm:
                ksrq = (w.get("ksrq") or "").strip()
                jsrq = (w.get("jsrq") or "").strip()
                self.way_rule_label.setText(
                    f"  限选 {w.get('xkzys') or '?'} 门 · {ksrq or '?'} ~ {jsrq or '?'}  ")
                self.way_rule_label.setVisible(True)
                return
        self.way_rule_label.setText("")
        self.way_rule_label.setVisible(False)

    # ---- 枚举 ----
    def _reload_enums_for_way(self):
        c = self._get_client()
        cached = enum_cache_get(c.enum_cache_key())
        if cached is not None:
            self._enums = cached
            self._loading_enums = False
            self._init_combos()
            self.fill_enums(cached)
            self.enum_label.setText(
                f"枚举（缓存）：{len(cached.get('kkyx') or [])} 学院 / "
                f"{len(cached.get('kclb') or [])} 类别 / "
                f"{len(cached.get('dgjs') or [])} 教师")
            self.log("info", "枚举命中本地缓存，无需重新加载")
        else:
            self._enums = None
            self._init_combos()
            self.load_enums()

    def on_refresh_enums(self):
        if self._busy:
            self.log("warn", "查询进行中，稍后再刷新枚举")
            return
        if self._loading_enums:
            self.log("warn", "枚举聚合进行中，稍后再刷新")
            return
        c = self._get_client()
        enum_cache_invalidate(c.enum_cache_key())
        self._enums = None
        self._init_combos()
        self.log("info", "已清除当前方式枚举缓存，重新聚合…")
        self.load_enums()

    def _init_combos(self):
        for cb in (self.f_kkyx, self.f_kclb, self.f_kcxz, self.f_xiaoqu, self.f_skyy):
            cb.clear()
            cb.addItem("全部", "")
        self.f_sfmxzj.clear()
        for mc, dm in self.SFMXZJ:
            self.f_sfmxzj.addItem(mc, dm)

    def load_enums(self):
        if self._enums is not None or self._loading_enums:
            return
        self._loading_enums = True
        self.enum_label.setText("枚举加载中…")
        run_in_thread(self._do_enums)

    def _do_enums(self):
        try:
            c = self._get_client()
            dm = c.form.get("p_xkfsdm")
            if not c.form.get("p_dqxnxq"):
                c.discover()
            enums = c.fetch_course_enums()
            if dm != c.form.get("p_xkfsdm"):
                self._enums = None
                self._loading_enums = False
                self.load_enums()
                return
            self.bridge.enums_ready.emit(enums)
        except SessionExpired as e:
            self.log("error", f"枚举聚合失败（SESSION 失效）：{e}")
            self.bridge.enums_ready.emit({})
        except Exception as e:
            self.log("error", f"枚举聚合失败：{e!r}")
            self.bridge.enums_ready.emit({})

    def fill_enums(self, enums: dict):
        self._loading_enums = False
        self._enums = enums if enums else None
        lang = self._get_client().language_enums
        for cb, key in ((self.f_kkyx, "kkyx"), (self.f_kclb, "kclb"),
                        (self.f_kcxz, "kcxz"), (self.f_xiaoqu, "xiaoqu"),
                        (self.f_skyy, "skyy")):
            cb.clear()
            cb.addItem("全部", "")
            items = lang if key == "skyy" and lang else (self._enums or {}).get(key) or []
            for code, name in items:
                cb.addItem(f"{name}（{code}）", code)
        n = len((self._enums or {}).get("dgjs") or [])
        if self._enums:
            self.enum_label.setText(f"枚举已加载：{len(self._enums.get('kkyx') or [])} 学院 / "
                                    f"{len(self._enums.get('kclb') or [])} 类别 / {n} 教师")
        else:
            self.enum_label.setText("")

    def _get_client(self) -> XKClient:
        cfg = self.get_cfg()
        sid = (cfg.get("session") or "").strip()
        if self._client is None or (self._client.cfg.get("session") or "").strip() != sid:
            self._client = XKClient(cfg)
        return self._client

    def collect_filters(self) -> dict:
        f: dict = {}
        gjz = self.f_gjz.text().strip()
        if gjz:
            f["gjz"] = gjz
        skjs = self.f_skjs.text().strip()
        if skjs:
            f["skjs"] = skjs
        for cb, key in ((self.f_kkyx, "kkyx"), (self.f_kclb, "kclb"), (self.f_kcxz, "kcxz"),
                        (self.f_xiaoqu, "xiaoqu"), (self.f_skyy, "skyy")):
            code = cb.currentData()
            if code:
                f[key] = code
        sfmxzj = self.f_sfmxzj.currentData()
        if sfmxzj:
            f["sfmxzj"] = sfmxzj
        if self.cb_hlct.isChecked():
            f["hlctkc"] = "1"
        if self.cb_hllr.isChecked():
            f["hllrlkc"] = "1"
        return f

    def on_search(self):
        if self._busy:
            return
        self._page = 1
        self._filters_used = self.collect_filters()
        self._go()

    def on_reset(self):
        if self._busy:
            return
        self.f_gjz.clear()
        self.f_skjs.clear()
        self._init_combos()
        self.cb_hlct.setChecked(False)
        self.cb_hllr.setChecked(False)
        self.on_search()

    def on_size_changed(self):
        if self._busy:
            return
        self._page_size = int(self.size_combo.currentData() or 20)
        self.on_search()

    def on_page(self, target: int):
        if self._busy:
            return
        target = max(1, min(self._pages, target))
        if target == self._page:
            return
        self._page = target
        self._go()

    def _go(self):
        self._busy = True
        self.search_btn.setEnabled(False)
        self.search_btn.setText("查询中…")
        run_in_thread(self._do_query, self._page, self._page_size, dict(self._filters_used))

    def _do_query(self, page: int, page_size: int, filters: dict):
        try:
            c = self._get_client()
            if not c.form.get("p_dqxnxq"):
                c.discover()
            res = c.list_courses(page, page_size, filters)
            rows = []
            for cc in extract_courses(res):
                rows.append([
                    cc.get("kcdm", ""), cc.get("kcmc", ""), cc.get("kxh", ""),
                    str(cc.get("xf", "") or ""), str(cc.get("zxs", "") or ""),
                    cc.get("kclbmc", ""), cc.get("kcxzmc", ""),
                    cc.get("xiaoqumc", ""), parse_skxx(cc.get("pkjgmx") or cc.get("kcxx")),
                    cc.get("dgjsmc", ""), str(cc.get("zrl", "")), str(enrolled_of(cc) or ""),
                    str(remaining_of(cc)),
                ])
            info = page_info(res)
            self.bridge.search_done.emit(
                {"rows": rows, "total": info["total"], "page": info["page_num"],
                 "pages": info["pages"], "page_size": page_size}, "")
        except SessionExpired as e:
            self.bridge.search_done.emit({}, f"SESSION 失效：{e}")
        except Exception as e:
            self.bridge.search_done.emit({}, f"查询失败：{e!r}")

    def _on_search_result(self, data: dict, err: str):
        self._busy = False
        self.search_btn.setEnabled(True)
        self.search_btn.setText(" 查询")
        if err:
            self.log("error", err)
            return
        rows = data.get("rows") or []
        self._fill_table(rows)
        self._pages = max(1, int(data.get("pages") or 1))
        self._total = int(data.get("total") or 0)
        self.page_label.setText(f"共 {self._total} 条")
        self.page_cur.setText(f"第 {self._page} / {self._pages} 页")
        first_enabled = self._pages > 1 and self._page > 1
        last_enabled = self._pages > 1 and self._page < self._pages
        self.btn_first.setEnabled(first_enabled)
        self.btn_prev.setEnabled(first_enabled)
        self.btn_next.setEnabled(last_enabled)
        self.btn_last.setEnabled(last_enabled)
        self.log("success", f"查询完成：共 {self._total} 门，第 {self._page}/{self._pages} 页")

    def _fill_table(self, rows: list):
        t = self.table
        self._rows = list(rows)
        t.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, val in enumerate(row):
                item = QTableWidgetItem(str(val) if val is not None else "")
                if j == 12:
                    try:
                        rem = int(val)
                    except (TypeError, ValueError):
                        rem = -1
                    if rem > 0:
                        f = QFont()
                        f.setBold(True)
                        item.setFont(f)
                        item.setForeground(QColor(T.OK_DEEP))
                    elif rem == 0:
                        item.setForeground(QColor(T.TEXT_3))
                elif j == 8:
                    item.setToolTip(str(val))
                elif j in (3, 4, 10, 11, 12):
                    item.setTextAlignment(Qt.AlignCenter)
                t.setItem(i, j, item)
            a = ensure_ui_assets()
            btn = QPushButton("＋目标")
            btn.setObjectName("btnMini")
            btn.setIcon(QIcon(a["ic_plus.png"]))
            btn.setIconSize(QSize(12, 12))
            btn.setFixedHeight(28)
            kcdm, kxh = str(row[0]), str(row[2]) if row[2] not in (None, "") else ""
            btn.clicked.connect(partial(self._add_target_from_row, kcdm, kxh, row[1]))
            t.setCellWidget(i, 13, btn)

    def _on_row_detail(self, index):
        """双击行 → 二级界面：课程详情（列表放不下完整信息，详情单独看）。"""
        row = index.row()
        rows = getattr(self, "_rows", None)
        if not rows or row >= len(rows):
            return
        data = rows[row]
        keys = ["课程代码", "课程名称", "课序号", "学分", "总学时", "课程类别",
                "课程性质", "校区", "上课时间", "任课教师", "容量", "已选", "余量"]
        info = {k: ("" if i >= len(data) or data[i] is None else str(data[i]))
                for i, k in enumerate(keys)}
        dm = self.f_way.itemData(self.f_way.currentIndex()) or ""
        dlg = CourseDetailDialog(info, self.f_way.currentText(), self)
        if dlg.exec():
            self._add_target_from_row(info["课程代码"], info["课序号"], info["课程名称"])
        elif dm:
            pass

    def _add_target_from_row(self, kcdm: str, kxh: str, kcmc: str):
        cfg = self.get_cfg()
        dm = self.f_way.itemData(self.f_way.currentIndex()) or cfg.get("xkfsdm") or ""
        mc = self.f_way.currentText()
        targets = cfg.get("targets") or []
        for it in targets:
            if str(it.get("kcdm")) == kcdm and str(it.get("kxh") or "") == (kxh or ""):
                self.log("warn", f"{kcdm}（课序号 {kxh or '任意'}）已在目标列表，不重复添加")
                return
        targets.append({"kcdm": kcdm, "kxh": kxh, "kcmc": kcmc,
                        "xkfsdm": dm, "xkfsmc": mc})
        cfg["targets"] = targets
        cfg["target_kcdm"] = targets[0]["kcdm"]
        cfg["target_kxh"] = targets[0].get("kxh") or ""
        save_config(cfg)
        self.log("success", f"已添加目标：{kcdm} {kcmc}（课序号 {kxh or '任意'}，方式 {mc}）"
                            f" → 目标共 {len(targets)} 门")
        self._update_target_label()
        self.target_set.emit()

    def _update_target_label(self):
        cfg = self.get_cfg()
        targets = cfg.get("targets") or []
        if not targets:
            kcdm = cfg.get("target_kcdm") or ""
            if kcdm:
                self.target_label.setText(f"  目标：{kcdm}（旧配置，建议重新添加）  ")
            else:
                self.target_label.setText("  目标：未设置（查询后在列表中点「＋目标」）  ")
        else:
            self.target_label.setText(
                f"  目标：共 {len(targets)} 门（最新：{targets[-1]['kcdm']}）  ")

    def refresh(self):
        self._update_target_label()


# ---------------------------------------------------------------------------
# 页面 3：抢课目标（从监控页独立出来 — 开抢前先在这里备好清单）
# ---------------------------------------------------------------------------
class TargetsPage(QWidget):
    """抢课目标清单：排序、删除、限选核算。

    独立成页的理由：目标管理是「开抢之前」反复做的事，和运行监控是两种完全不同的
    心智模式；混在监控页里既把页面撑得很满，又让监控页在小窗口下必然被压扁。
    """

    targets_changed = Signal()
    TCOLS = ["优先级", "课程代码", "课程名称", "课序号", "选课方式", "状态", "余量", "操作"]
    STATUS_TEXT = {
        "pending": "待处理", "waiting": "等待开抢", "polling": "监控中",
        "submitting": "提交中", "done": "已选成功", "failed": "失败",
    }
    STATUS_BADGE = {
        "done": "success", "failed": "error", "submitting": "brand",
        "polling": "brand", "pending": "muted", "waiting": "warn",
    }

    def __init__(self, bridge: Bridge, get_cfg, log):
        super().__init__()
        self.setObjectName("pageRoot")
        self.bridge = bridge
        self.get_cfg = get_cfg
        self.log = log
        self._way_names: dict[str, str] = {}
        self._way_rules: dict[str, dict] = {}
        self._running = False
        self._snap: list = []

        self.scaffold = PageScaffold(
            "抢课目标", "开抢前在这里备好清单。顺序即优先级，程序按从上到下的顺序处理。")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scaffold)
        layout = self.scaffold.content_layout()
        a = ensure_ui_assets()

        # ---- 概览条 ----
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("tipBox")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        # ---- 目标表 ----
        self.table = QTableWidget(0, len(self.TCOLS))
        self.table.setHorizontalHeaderLabels(self.TCOLS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setMinimumHeight(300)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for col, w in ((0, 62), (1, 92), (3, 62), (4, 150), (5, 100), (6, 60), (7, 150)):
            self.table.setColumnWidth(col, w)
        layout.addWidget(self.table, 1)

        # ---- 手动添加 ----
        add_card = QFrame()
        add_card.setObjectName("card")
        av = QVBoxLayout(add_card)
        av.setContentsMargins(20, 16, 20, 16)
        av.setSpacing(12)
        av.addWidget(make_card_title("手动添加", "不知道课程代码？去「课程搜索」页查"))
        add_row = QHBoxLayout()
        add_row.setSpacing(10)
        self.add_kcdm = QLineEdit()
        self.add_kcdm.setPlaceholderText("课程代码，如 1099074")
        self.add_kcdm.setMinimumHeight(34)
        add_row.addWidget(self._labeled("课程代码"), 0)
        add_row.addWidget(self.add_kcdm, 2)
        self.add_kxh = QLineEdit()
        self.add_kxh.setPlaceholderText("可空 = 任意序号")
        self.add_kxh.setMinimumHeight(34)
        add_row.addWidget(self._labeled("课序号"), 0)
        add_row.addWidget(self.add_kxh, 2)
        self.add_btn = QPushButton(" 添加")
        self.add_btn.setObjectName("btnPrimary")
        self.add_btn.setIcon(QIcon(a["ic_plus.png"]))
        self.add_btn.setIconSize(QSize(12, 12))
        self.add_btn.setMinimumHeight(34)
        self.add_btn.clicked.connect(self._on_add_target)
        add_row.addWidget(self.add_btn)
        av.addLayout(add_row)
        layout.addWidget(add_card)

        # ---- 页头右侧：清空 ----
        self.clear_btn = QPushButton(" 清空全部")
        self.clear_btn.setObjectName("btnGhost")
        self.clear_btn.setIcon(QIcon(a["ic_trash.png"]))
        self.clear_btn.setIconSize(QSize(13, 13))
        self.clear_btn.setMinimumHeight(38)
        self.clear_btn.clicked.connect(self._on_clear_all)
        self.scaffold.add_action(self.clear_btn)

        self.refresh()

    @staticmethod
    def _labeled(text):
        l = QLabel(text)
        l.setObjectName("sectionLabel")
        return l

    def fill_ways(self, data: dict):
        ways = data.get("ways") or []
        self._way_names = {w.get("xkfsdm") or "": w.get("xkfsmc") or "" for w in ways}
        self._way_rules = {w.get("xkfsdm") or "": w for w in ways}
        self.refresh()

    def _way_name(self, dm: str) -> str:
        return self._way_names.get(dm) or dm

    def set_running(self, running: bool):
        self._running = running
        self.add_btn.setEnabled(not running)
        self.clear_btn.setEnabled(not running)
        self.refresh()

    def refresh(self, snap: list | None = None):
        if snap is not None:
            self._snap = snap
        cfg = self.get_cfg()
        targets = normalize_targets(cfg)
        runtime = {(t.get("kcdm"), t.get("kxh") or ""): t for t in self._snap}
        self.table.setRowCount(len(targets))
        for i, tg in enumerate(targets):
            key = (tg["kcdm"], tg.get("kxh") or "")
            rt = runtime.get(key) or {}
            status = rt.get("status") or ("pending" if self._running else "")
            self._set_item(i, 0, str(i + 1), center=True)
            self._set_item(i, 1, tg["kcdm"])
            self._set_item(i, 2, tg.get("kcmc") or "")
            self._set_item(i, 3, tg.get("kxh") or "", center=True)
            self._set_item(i, 4, self._way_name(tg.get("xkfsdm") or ""))

            b = make_badge(self.STATUS_TEXT.get(status, status or "—"),
                           self.STATUS_BADGE.get(status, "muted"))
            self.table.setCellWidget(i, 5, b)

            rem = rt.get("remaining")
            item = QTableWidgetItem(str(rem) if rem is not None else "—")
            item.setTextAlignment(Qt.AlignCenter)
            if rem is not None and rem > 0:
                item.setForeground(QColor(T.OK_DEEP))
                f = QFont()
                f.setBold(True)
                item.setFont(f)
            self.table.setItem(i, 6, item)

            self.table.setCellWidget(i, 7, self._ops_widget(i, len(targets),
                                                            tg["kcdm"], tg.get("kxh") or ""))
        self._update_summary(targets)

    def _set_item(self, row: int, col: int, text: str, center: bool = False):
        item = QTableWidgetItem(text)
        if center:
            item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, col, item)

    def _ops_widget(self, idx: int, total: int, kcdm: str, kxh: str) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(4)
        a = ensure_ui_assets()
        up = QPushButton()
        up.setObjectName("btnIcon")
        up.setIcon(QIcon(a["ic_arrow_up.png"]))
        up.setIconSize(QSize(12, 12))
        up.setFixedSize(28, 28)
        up.setToolTip("上移（提高优先级）")
        up.setEnabled(not self._running and idx > 0)
        up.clicked.connect(partial(self._move, idx, -1))
        h.addWidget(up)
        down = QPushButton()
        down.setObjectName("btnIcon")
        down.setIcon(QIcon(a["ic_arrow_down.png"]))
        down.setIconSize(QSize(12, 12))
        down.setFixedSize(28, 28)
        down.setToolTip("下移（降低优先级）")
        down.setEnabled(not self._running and idx < total - 1)
        down.clicked.connect(partial(self._move, idx, 1))
        h.addWidget(down)
        rm = QPushButton()
        rm.setObjectName("btnIconDanger")
        rm.setIcon(QIcon(a["ic_x.png"]))
        rm.setIconSize(QSize(11, 11))
        rm.setFixedSize(28, 28)
        rm.setToolTip("移除这个目标")
        rm.setEnabled(not self._running)
        rm.clicked.connect(partial(self._on_remove_target, kcdm, kxh))
        h.addWidget(rm)
        h.addStretch(1)
        return w

    def _save_targets(self, targets: list):
        cfg = self.get_cfg()
        cfg["targets"] = targets
        if targets:
            cfg["target_kcdm"] = targets[0]["kcdm"]
            cfg["target_kxh"] = targets[0].get("kxh") or ""
        else:
            cfg["target_kcdm"] = ""
            cfg["target_kxh"] = ""
        save_config(cfg)
        self.refresh()
        self.targets_changed.emit()

    def _move(self, idx: int, delta: int):
        cfg = self.get_cfg()
        targets = list(cfg.get("targets") or [])
        new_idx = idx + delta
        if not (0 <= new_idx < len(targets)):
            return
        targets[idx], targets[new_idx] = targets[new_idx], targets[idx]
        self._save_targets(targets)
        self.log("info", f"已调整优先级：{targets[new_idx].get('kcdm')} → 第 {new_idx + 1} 位")

    def _on_remove_target(self, kcdm: str, kxh: str):
        if self._running:
            self.log("warn", "监控运行中不能移除目标，请先停止")
            return
        cfg = self.get_cfg()
        targets = [t for t in (cfg.get("targets") or [])
                   if not (str(t.get("kcdm")) == kcdm and str(t.get("kxh") or "") == kxh)]
        self._save_targets(targets)
        self.log("info", f"已移除目标 {kcdm}（课序号 {kxh or '任意'}），剩余 {len(targets)} 门")

    def _on_add_target(self):
        kcdm = self.add_kcdm.text().strip()
        kxh = self.add_kxh.text().strip()
        if not kcdm:
            QMessageBox.warning(self, "缺少代码", "请填写课程代码（可在「课程搜索」页查到）")
            return
        if kxh in ("None", "none"):
            kxh = ""
        cfg = self.get_cfg()
        targets = list(cfg.get("targets") or [])
        for it in targets:
            if str(it.get("kcdm")) == kcdm and str(it.get("kxh") or "") == kxh:
                self.log("warn", f"{kcdm}（课序号 {kxh or '任意'}）已在目标列表")
                return
        dm = (cfg.get("xkfsdm") or "sztzk-b-b").strip()
        targets.append({"kcdm": kcdm, "kxh": kxh, "kcmc": "",
                        "xkfsdm": dm, "xkfsmc": self._way_name(dm)})
        self._save_targets(targets)
        self.add_kcdm.clear()
        self.add_kxh.clear()
        self.log("success", f"已添加目标 {kcdm}（课序号 {kxh or '任意'}，方式 "
                            f"{self._way_name(dm)}）→ 目标共 {len(targets)} 门")

    def _on_clear_all(self):
        cfg = self.get_cfg()
        n = len(cfg.get("targets") or [])
        if not n:
            return
        if QMessageBox.question(self, "清空目标", f"确定移除全部 {n} 个抢课目标？",
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        self._save_targets([])
        self.log("warn", f"已清空全部 {n} 个抢课目标")

    def _update_summary(self, targets: list):
        if not targets:
            self.summary_label.setObjectName("warnBox")
            self.summary_label.setText(
                "还没有添加任何目标。去「课程搜索」页查询后点每行的「＋目标」，或在下方手动添加。")
            self._restyle_summary()
            return
        # 按选课方式分组，逐个核算限选余量
        groups: dict[str, list] = {}
        for t in targets:
            groups.setdefault(t.get("xkfsdm") or "", []).append(t)
        parts = []
        warn = False
        for dm, items in groups.items():
            name = self._way_name(dm)
            rule = self._way_rules.get(dm) or {}
            limit = rule.get("xkzys")
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None
            if limit is not None:
                left = limit - len(items)
                parts.append(f"{name}：{len(items)}/{limit} 门（还可加 {max(0, left)}）")
                if left < 0:
                    warn = True
            else:
                parts.append(f"{name}：{len(items)} 门")
        txt = f"共 {len(targets)} 门，跨 {len(groups)} 个选课方式 —— " + "；".join(parts)
        if warn:
            txt += "　⚠ 有方式超出限选门数，超出的必然选不上"
            self.summary_label.setObjectName("errBox")
        else:
            self.summary_label.setObjectName("okBox")
        self.summary_label.setText(txt)
        self._restyle_summary()

    def _restyle_summary(self):
        s = self.summary_label.style()
        s.unpolish(self.summary_label)
        s.polish(self.summary_label)

    def _on_state(self, st: dict):
        self._running = st.get("status") not in ("done", "stopped", "error", "expired")
        self.refresh(st.get("targets"))


# ---------------------------------------------------------------------------
# 页面 4：监控中心（只管执行与实时状态，目标管理已拆到上一页）
# ---------------------------------------------------------------------------
class MonitorPage(QWidget):
    """监控中心 — 双运行模式 + 自定义时间安排 + 实时状态。"""

    STATUS_TEXT = {
        "pending": "待处理", "waiting": "等待开抢", "polling": "监控中",
        "submitting": "提交中", "done": "已选成功", "failed": "失败",
    }
    STATUS_COLOR = {
        "done": T.OK_DEEP, "failed": T.ERR_DEEP,
        "submitting": T.BRAND_ACTIVE, "polling": T.BRAND,
        "pending": T.TEXT_3, "waiting": T.WARN_DEEP,
    }
    STATUS_BADGE = {
        "done": "badgeSuccess", "failed": "badgeError",
        "submitting": "badgeBrand", "polling": "badgeBrand",
        "pending": "badgeMuted", "waiting": "badgeWarn",
    }
    TCOLS = ["课程代码", "课程名称", "课序号", "选课方式", "状态", "余量", "说明", "操作"]

    def __init__(self, bridge: Bridge, get_cfg, log, targets_page=None):
        super().__init__()
        self.setObjectName("pageRoot")
        self.bridge = bridge
        self.get_cfg = get_cfg
        self.log = log
        self.targets_page = targets_page
        self.monitor: Monitor | None = None
        self._way_names: dict[str, str] = {}
        self._way_rules: dict[str, dict] = {}
        self._running = False
        self._loading = False

        self.scaffold = PageScaffold(
            "监控中心", "定好策略后交给程序：它会盯到开抢时间，发现余量立刻提交。")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scaffold)
        layout = self.scaffold.content_layout()

        # ---- 模式 + 时间卡（左右布局） ----
        top_row = QHBoxLayout()
        top_row.setSpacing(14)

        # 模式卡
        mode_card = QFrame()
        mode_card.setObjectName("card")
        mv = QVBoxLayout(mode_card)
        mv.setContentsMargins(20, 18, 20, 18)
        mv.setSpacing(12)
        mv.addWidget(make_card_title("01  模式", "选择运行模式"))
        self.rb_grab = QRadioButton("定时抢课 — 等开抢时间，自动轮询余量并提交")
        self.rb_direct = QRadioButton("直接选课 — 立即对每个目标各提交一次，失败自动重试")
        self.rb_grab.setChecked(True)
        self.rb_grab.setStyleSheet(f"font-size:13px; padding:4px 0;")
        self.rb_direct.setStyleSheet(f"font-size:13px; padding:4px 0;")
        self.rb_grab.toggled.connect(self._on_mode_changed)
        self.rb_direct.toggled.connect(self._on_mode_changed)
        mv.addWidget(self.rb_grab)
        mv.addWidget(self.rb_direct)
        top_row.addWidget(mode_card, 1)

        # 时间卡
        self.time_box = QFrame()
        self.time_box.setObjectName("card")
        tv = QVBoxLayout(self.time_box)
        tv.setContentsMargins(20, 18, 20, 18)
        tv.setSpacing(12)
        tv.addWidget(make_card_title("02  时间安排", "仅「定时抢课」生效",
                                       "开始时间前不发任何请求；结束时间到则自动停止。"))
        sr = QHBoxLayout()
        sr.setSpacing(8)
        self.start_mode = QComboBox()
        for txt, dm in [("跟随选课规则（推荐）", "auto"),
                        ("自定义时间", "custom"),
                        ("立即开始（不等开抢）", "now")]:
            self.start_mode.addItem(txt, dm)
        self.start_mode.setMinimumHeight(34)
        self.start_mode.currentIndexChanged.connect(self._on_time_changed)
        sr.addWidget(self.start_mode, 2)
        self.start_dt = QDateTimeEdit()
        self.start_dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.start_dt.setCalendarPopup(True)
        self.start_dt.setMinimumHeight(34)
        self.start_dt.setEnabled(False)
        sr.addWidget(self.start_dt, 2)
        tv.addLayout(sr)

        er = QHBoxLayout()
        er.setSpacing(8)
        self.end_mode = QComboBox()
        for txt, dm in [("跟随选课规则（推荐）", "auto"),
                        ("自定义时间", "custom"),
                        ("不限制", "none")]:
            self.end_mode.addItem(txt, dm)
        self.end_mode.setMinimumHeight(34)
        self.end_mode.currentIndexChanged.connect(self._on_time_changed)
        er.addWidget(self.end_mode, 2)
        self.end_dt = QDateTimeEdit()
        self.end_dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.end_dt.setCalendarPopup(True)
        self.end_dt.setMinimumHeight(34)
        self.end_dt.setEnabled(False)
        er.addWidget(self.end_dt, 2)
        tv.addLayout(er)

        self.time_hint = QLabel("")
        self.time_hint.setObjectName("tipBox")
        self.time_hint.setWordWrap(True)
        tv.addWidget(self.time_hint)
        top_row.addWidget(self.time_box, 1)

        layout.addLayout(top_row)

        # ---- 03 目标摘要（只是只读概览，增删排序都去「抢课目标」页） ----
        tgt_card = QFrame()
        tgt_card.setObjectName("card")
        tvv = QVBoxLayout(tgt_card)
        tvv.setContentsMargins(20, 18, 20, 18)
        tvv.setSpacing(12)
        tvv.addWidget(make_card_title("03  目标概览", "增删与排序在「抢课目标」页",
                                       "运行中锁定增删，避免中途变化造成状态混乱。"))
        self.tgt_summary = QLabel("—")
        self.tgt_summary.setObjectName("tipBox")
        self.tgt_summary.setWordWrap(True)
        tvv.addWidget(self.tgt_summary)
        tgt_row = QHBoxLayout()
        tgt_row.setSpacing(10)
        self.goto_targets_btn = QPushButton("管理目标")
        self.goto_targets_btn.setObjectName("btnGhost")
        self.goto_targets_btn.setIcon(QIcon(ensure_ui_assets()["ic_target.png"]))
        self.goto_targets_btn.setIconSize(QSize(13, 13))
        self.goto_targets_btn.setMinimumHeight(34)
        tgt_row.addWidget(self.goto_targets_btn)
        tgt_row.addStretch(1)
        tvv.addLayout(tgt_row)
        layout.addWidget(tgt_card)

        # ---- 04 实时状态 ----
        bottom = QFrame()
        bottom.setObjectName("card")
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(20, 18, 20, 18)
        bv.setSpacing(12)
        bv.addWidget(make_card_title("04  实时状态", "各目标推进情况"))

        row1 = QHBoxLayout()
        row1.setSpacing(14)
        self.status_badge = make_badge("● 未启动", "muted")
        self.status_badge.setMinimumHeight(28)
        row1.addWidget(self.status_badge)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("就绪")
        row1.addWidget(self.progress_bar, 1)
        bv.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(20)
        rem_lbl = QLabel("最近余量")
        rem_lbl.setObjectName("sectionLabel")
        row2.addWidget(rem_lbl)
        self.remaining_label = QLabel("—")
        self.remaining_label.setStyleSheet(
            f"font-size:20px; font-weight:700; color:{T.OK_DEEP};")
        row2.addWidget(self.remaining_label)
        self.countdown_label = QLabel("")
        self.countdown_label.setObjectName("warnBox")
        self.countdown_label.setVisible(False)
        row2.addWidget(self.countdown_label)
        row2.addStretch(1)
        bv.addLayout(row2)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("tipBox")
        self.detail_label.setWordWrap(True)
        bv.addWidget(self.detail_label)
        layout.addWidget(bottom)

        # ---- 人工确认按钮（仅「手动确认」模式下出现） ----
        self.confirm_btn = QPushButton("确认提交")
        self.confirm_btn.setObjectName("btnSuccess")
        self.confirm_btn.setMinimumHeight(38)
        self.confirm_btn.setEnabled(False)
        self.confirm_btn.clicked.connect(lambda: self._confirm(True))
        self.scaffold.add_action(self.confirm_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("btnGhost")
        self.cancel_btn.setMinimumHeight(38)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(lambda: self._confirm(False))
        self.scaffold.add_action(self.cancel_btn)

        # ---- 主操作：开始 / 停止，提到页头右侧固定不动 ----
        a = ensure_ui_assets()
        self.start_btn = QPushButton(" 开始抢课")
        self.start_btn.setObjectName("btnPrimary")
        self.start_btn.setIcon(QIcon(a["ic_play.png"]))
        self.start_btn.setIconSize(QSize(14, 14))
        self.start_btn.setMinimumWidth(140)
        self.start_btn.setMinimumHeight(38)
        self.start_btn.clicked.connect(self.on_start)
        self.scaffold.add_action(self.start_btn)
        self.stop_btn = QPushButton(" 停止")
        self.stop_btn.setObjectName("btnDanger")
        self.stop_btn.setIcon(QIcon(a["ic_stop.png"]))
        self.stop_btn.setIconSize(QSize(14, 14))
        self.stop_btn.setMinimumWidth(104)
        self.stop_btn.setMinimumHeight(38)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        self.scaffold.add_action(self.stop_btn)

        self.bridge.found.connect(self._on_found)
        self.bridge.result.connect(self._on_result)
        self._fill_targets()

    def _labeled_inline(self, text):
        l = QLabel(text)
        l.setObjectName("sectionLabel")
        return l

    def fill_ways(self, data: dict):
        ways = data.get("ways") or []
        self._way_names = {w.get("xkfsdm") or "": w.get("xkfsmc") or "" for w in ways}
        self._way_rules = {w.get("xkfsdm") or "": w for w in ways}
        self._update_time_hint()
        self._fill_targets()

    def _way_name(self, dm: str) -> str:
        return self._way_names.get(dm) or dm

    def _update_time_hint(self):
        cfg = self.get_cfg()
        parts = []
        for tg in normalize_targets(cfg):
            dm = tg.get("xkfsdm") or ""
            if dm in parts:
                continue
            w = self._way_rules.get(dm) or {}
            if w:
                parts.append(f"{dm}: {self._way_name(dm)}（{w.get('ksrq') or '?'} ~ "
                             f"{w.get('jsrq') or '?'}，限选 {w.get('xkzys') or '?'} 门）")
        if parts:
            self.time_hint.setText("各目标所属选课方式的规则窗口：" + "；".join(parts))
        elif self._way_rules:
            self.time_hint.setText("")
        else:
            self.time_hint.setText("（选课方式信息加载后这里会显示各方式的规则窗口）")

    def _fill_targets(self, snap: list | None = None):
        """目标表已迁到 TargetsPage；这里只同步概览文字。"""
        if self.targets_page is not None:
            self.targets_page.refresh(snap if snap is not None else None)
        cfg = self.get_cfg()
        targets = normalize_targets(cfg)
        if not targets:
            self.tgt_summary.setObjectName("warnBox")
            self.tgt_summary.setText(
                "尚未添加任何抢课目标 —— 点右侧「管理目标」去添加，否则启动会直接被拦下。")
        else:
            names = "、".join(
                f"{(t.get('kcmc') or t['kcdm'])}"
                for t in targets[:4])
            more = f" 等 {len(targets)} 门" if len(targets) > 4 else ""
            self.tgt_summary.setObjectName("okBox")
            self.tgt_summary.setText(f"本次将处理 {len(targets)} 门：{names}{more}")
        s = self.tgt_summary.style()
        s.unpolish(self.tgt_summary)
        s.polish(self.tgt_summary)

    def refresh(self):
        cfg = self.get_cfg()
        self._loading = True
        self._apply_mode((cfg.get("run_mode") or "grab") == "direct", save=False)
        sm = cfg.get("start_mode") or "auto"
        i = self.start_mode.findData(sm)
        self.start_mode.setCurrentIndex(max(0, i))
        self.start_dt.setDateTime(datetime.now())
        if cfg.get("start_time"):
            self.start_dt.setDateTime(datetime.strptime(
                cfg["start_time"], "%Y-%m-%d %H:%M:%S"))
        self.start_dt.setEnabled(sm == "custom")
        em = cfg.get("end_mode") or "auto"
        i = self.end_mode.findData(em)
        self.end_mode.setCurrentIndex(max(0, i))
        self.end_dt.setDateTime(datetime.now())
        if cfg.get("end_time"):
            self.end_dt.setDateTime(datetime.strptime(
                cfg["end_time"], "%Y-%m-%d %H:%M:%S"))
        self.end_dt.setEnabled(em == "custom")
        self._loading = False
        self._fill_targets()
        self._update_time_hint()
        self._apply_confirm_visibility()

    def _apply_confirm_visibility(self):
        manual = not bool(self.get_cfg().get("auto_submit", True))
        self.confirm_btn.setVisible(manual)
        self.cancel_btn.setVisible(manual)

    def _on_mode_changed(self, checked: bool):
        if not checked:
            return
        self._apply_mode(self.rb_direct.isChecked(), save=not self._loading)

    def _apply_mode(self, direct: bool, save: bool = True):
        self.rb_grab.blockSignals(True)
        self.rb_direct.blockSignals(True)
        self.rb_direct.setChecked(direct)
        self.rb_grab.setChecked(not direct)
        self.rb_grab.blockSignals(False)
        self.rb_direct.blockSignals(False)
        self.time_box.setEnabled(not direct)
        a = ensure_ui_assets()
        if direct:
            self.start_btn.setText(" 立即选课")
            self.start_btn.setIcon(QIcon(a["ic_bolt.png"]))
        else:
            self.start_btn.setText(" 开始抢课")
            self.start_btn.setIcon(QIcon(a["ic_play.png"]))
        if save:
            cfg = self.get_cfg()
            cfg["run_mode"] = "direct" if direct else "grab"
            save_config(cfg)
            self.log("info", f"运行模式已切换为：{'直接选课' if direct else '定时抢课'}")

    def _on_time_changed(self):
        if self._loading:
            return
        sm = self.start_mode.currentData()
        em = self.end_mode.currentData()
        self.start_dt.setEnabled(sm == "custom")
        self.end_dt.setEnabled(em == "custom")
        cfg = self.get_cfg()
        cfg["start_mode"] = sm
        cfg["start_time"] = (self.start_dt.dateTime().toString("yyyy-MM-dd HH:mm:ss")
                             if sm == "custom" else "")
        cfg["end_mode"] = em
        cfg["end_time"] = (self.end_dt.dateTime().toString("yyyy-MM-dd HH:mm:ss")
                           if em == "custom" else "")
        save_config(cfg)
        names = {"auto": "跟随选课规则", "custom": "自定义", "now": "立即开始", "none": "不限"}
        self.log("info", f"时间安排：开始={names.get(sm)} 结束={names.get(em)}")

    def on_start(self):
        if self.monitor and self.monitor.is_alive():
            self.log("warn", "监控已在运行")
            return
        self._apply_confirm_visibility()
        cfg = self.get_cfg()
        if not cfg.get("session"):
            QMessageBox.warning(self, "缺少会话", "请先在「会话管理」页粘贴并测试 SESSION")
            return
        targets = normalize_targets(cfg)
        if not targets:
            QMessageBox.warning(self, "缺少目标",
                                "尚未添加任何抢课目标：去「课程搜索」页点「＋目标」，"
                                "或在上方手动添加课程代码")
            return
        cfg["run_mode"] = "direct" if self.rb_direct.isChecked() else "grab"
        if cfg["run_mode"] == "grab":
            cfg["start_mode"] = self.start_mode.currentData()
            cfg["start_time"] = (self.start_dt.dateTime().toString("yyyy-MM-dd HH:mm:ss")
                                 if cfg["start_mode"] == "custom" else "")
            cfg["end_mode"] = self.end_mode.currentData()
            cfg["end_time"] = (self.end_dt.dateTime().toString("yyyy-MM-dd HH:mm:ss")
                               if cfg["end_mode"] == "custom" else "")
        else:
            cfg["start_mode"] = "now"
            cfg["start_time"] = ""
            cfg["end_mode"] = "none"
            cfg["end_time"] = ""
        save_config(cfg)
        self._running = True
        self.confirm_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        if self.targets_page is not None:
            self.targets_page.set_running(True)
        self._fill_targets()
        mode_txt = "直接选课" if cfg["run_mode"] == "direct" else "定时抢课"
        self.log("info", f"启动监控线程（{mode_txt}，目标 {len(targets)} 门）…")
        client = XKClient(cfg)
        cb = {
            "on_log": lambda lv, msg: self.bridge.log.emit(lv, msg),
            "on_state": lambda st: self.bridge.state.emit(st),
            "on_found": lambda c: self.bridge.found.emit(c),
            "on_result": lambda o: self.bridge.result.emit(o),
            "on_done": lambda st: None,
        }
        self.monitor = Monitor(client, cfg, cb)
        self.monitor.start()
        self.bridge.live_changed.emit(True)

    def on_stop(self):
        if self.monitor and self.monitor.is_alive():
            self.monitor.stop()
            self.log("warn", "正在停止监控…")
        self._running = False
        if self.targets_page is not None:
            self.targets_page.set_running(False)
        self.bridge.live_changed.emit(False)

    def _confirm(self, yes: bool):
        if self.monitor and self.monitor.is_alive():
            self.monitor.confirm_submit(yes)
            self.confirm_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)

    def _on_state(self, st: dict):
        status_map = {
            "syncing": ("同步中…", "info", "muted"),
            "waiting": ("等待开始时间", "warn", "warn"),
            "polling": ("轮询中", "info", "brand"),
            "submitting": ("提交中", "info", "brand"),
            "awaiting": ("等待人工确认", "warn", "warn"),
            "done": ("已完成", "success", "success"),
            "stopped": ("已停止", "muted", "muted"),
            "error": ("出错", "error", "error"),
            "expired": ("会话失效", "error", "error"),
        }
        raw_status = st.get("status")
        txt, _level, badge_level = status_map.get(raw_status, (raw_status or "未知", "muted", "muted"))
        # 移除旧 badge，新建
        new_badge = make_badge(f"● {txt}", badge_level)
        new_badge.setMinimumHeight(28)
        # 替换布局里的旧 badge
        old = self.status_badge
        parent = old.parent()
        # 简单做法：直接修改 objectName 和文本（结构兼容），保留原 widget
        old.setText(f"● {txt}")
        old.setObjectName({
            "info": "badgeInfo", "success": "badgeSuccess",
            "warn": "badgeWarn", "error": "badgeError",
            "muted": "badgeMuted", "brand": "badgeBrand",
        }.get(badge_level, "badgeMuted"))
        old.style().unpolish(old)
        old.style().polish(old)
        old.update()

        total = st.get("total") or 0
        finished = st.get("finished") or 0
        done = st.get("done") or 0
        if total:
            pct = int(finished / total * 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"进度 {finished}/{total}（成功 {done}）")
        else:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat(txt)

        if st.get("countdown") is not None:
            self.countdown_label.setText(f"  倒计时：{st['countdown']}  ")
            self.countdown_label.setVisible(True)
        else:
            self.countdown_label.setVisible(False)
        self.detail_label.setText(st.get("message") or "")
        self._fill_targets(st.get("targets"))
        if st.get("status") in ("done", "stopped", "error", "expired"):
            self._running = False
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            if self.targets_page is not None:
                self.targets_page.set_running(False)
            self._fill_targets(st.get("targets"))
            self.bridge.live_changed.emit(False)
            if st.get("status") == "done":
                self.bridge.notify.emit("抢课成功", f"{st.get('message')}", "success")
        if st.get("status") == "expired":
            self.bridge.notify.emit("会话失效", "请重新粘贴 Cookie", "error")

    def _on_found(self, course: dict):
        self.confirm_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.remaining_label.setText(str(remaining_of(course)))
        self.bridge.notify.emit("发现余量",
                                f"{course.get('kcdm')} {course.get('kcmc')} "
                                f"余量 {remaining_of(course)}", "found")

    def _on_result(self, outcome: dict):
        self.confirm_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.remaining_label.setText("已选 ✓")
        self.bridge.notify.emit("抢课成功",
                                f"{outcome.get('kcdm')} {outcome.get('kcmc')} "
                                f"已进入已选列表", "success")


# ---------------------------------------------------------------------------
# 页面 4：运行日志
# ---------------------------------------------------------------------------
class LogPage(QWidget):
    """运行日志 — 分级过滤 + 关键字搜索 + 本地自动存档 + 导出。

    日志区自带滚动条，所以这一页不走外层滚动（scrollable=False），
    让它直接吃满剩余空间。

    本地存档：每次程序启动建一个独立文件 logs/xk_log_启动时刻.log（同秒再启动自动加
    序号），整段会话实时追加，同一天多次打开互不混淆。开关状态来自 config.json 的
    log_keep_local（MainWindow 构造时传入 get_cfg 读取）。
    """

    LEVELS = [("全部", ""), ("信息", "info"), ("成功", "success"),
              ("警告", "warn"), ("错误", "error")]

    def __init__(self, get_cfg=None):
        super().__init__()
        self.setObjectName("pageRoot")
        self._level = ""
        self._keyword = ""
        self._raw: list[tuple[str, str, str]] = []
        self.get_cfg = get_cfg
        self._start = datetime.now()      # 本次启动时刻：固定本次会话的存档文件名
        self._log_file: Path | None = None
        self._keep = True          # 是否把每条日志自动追加到本地 logs/
        if get_cfg is not None:
            try:
                self._keep = bool((get_cfg() or {}).get("log_keep_local", True))
            except Exception:
                self._keep = True
        if self._keep:
            self._log_file = new_session_log(self._start)

        self.scaffold = PageScaffold(
            "运行日志",
            "实时滚动所有后台动作；每次启动独立存档到 logs/（文件名带启动时刻），可过滤、搜索或导出。",
            scrollable=False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scaffold)

        # ---- 页头右侧：过滤 / 搜索 / 操作 ----
        a = ensure_ui_assets()
        self.count_label = QLabel("  0 条  ")
        self.count_label.setObjectName("badgeMuted")
        self.scaffold.add_action(self.count_label)

        self.level_combo = QComboBox()
        for txt, lv in self.LEVELS:
            self.level_combo.addItem(txt, lv)
        self.level_combo.setMinimumHeight(38)
        self.level_combo.setFixedWidth(96)
        self.level_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.scaffold.add_action(self.level_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索日志…")
        self.search_edit.setMinimumHeight(38)
        self.search_edit.setFixedWidth(180)
        self.search_edit.textChanged.connect(self._on_filter_changed)
        self.scaffold.add_action(self.search_edit)

        self.auto_scroll_btn = QPushButton("自动滚动")
        self.auto_scroll_btn.setObjectName("btnGhost")
        self.auto_scroll_btn.setCheckable(True)
        self.auto_scroll_btn.setChecked(True)
        self.auto_scroll_btn.setMinimumHeight(38)
        self.scaffold.add_action(self.auto_scroll_btn)

        self.keep_btn = QPushButton(" 本地存档")
        self.keep_btn.setObjectName("btnGhost")
        self.keep_btn.setCheckable(True)
        self.keep_btn.setChecked(self._keep)
        self.keep_btn.setToolTip(
            "本次启动的日志实时写入 logs/ 目录的独立文件（文件名带启动时刻）。"
            "关闭后本次运行不再追加，历史存档原样保留。")
        self.keep_btn.setMinimumHeight(38)
        self.keep_btn.toggled.connect(self._on_keep_toggled)
        self.scaffold.add_action(self.keep_btn)

        self.export_btn = QPushButton(" 导出")
        self.export_btn.setObjectName("btnGhost")
        self.export_btn.setIcon(QIcon(a["ic_export.png"]))
        self.export_btn.setIconSize(QSize(13, 13))
        self.export_btn.setMinimumHeight(38)
        self.export_btn.clicked.connect(self._on_export)
        self.scaffold.add_action(self.export_btn)

        self.clear_btn = QPushButton(" 清空")
        self.clear_btn.setObjectName("btnGhost")
        self.clear_btn.setIcon(QIcon(a["ic_x.png"]))
        self.clear_btn.setIconSize(QSize(12, 12))
        self.clear_btn.setMinimumHeight(38)
        self.clear_btn.clicked.connect(self.clear)
        self.scaffold.add_action(self.clear_btn)

        # ---- 日志区（自带滚动，吃满剩余空间） ----
        layout = self.scaffold.content_layout()
        layout.setContentsMargins(28, 16, 28, 20)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        layout.addWidget(self.view, 1)

        # ---- 底部：存档路径提示 + 打开目录 ----
        footer = QHBoxLayout()
        footer.setContentsMargins(2, 8, 2, 0)
        self.file_label = QLabel()
        self.file_label.setObjectName("logFileLabel")
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        footer.addWidget(self.file_label, 1)
        open_btn = QPushButton(" 打开日志目录")
        open_btn.setObjectName("btnGhost")
        open_btn.setMinimumHeight(32)
        open_btn.setToolTip("在文件管理器中打开 logs/ 目录")
        open_btn.clicked.connect(self._open_log_dir)
        footer.addWidget(open_btn)
        layout.addLayout(footer)
        self._count = 0
        self._update_file_label()

    def append(self, level: str, msg: str):
        now = datetime.now()
        if self._keep and self._log_file:
            persist_log_line(self._log_file, now, level, msg)
        ts = now.strftime("%H:%M:%S")
        self._raw.append((ts, level, msg))
        if len(self._raw) > 5000:
            del self._raw[:-5000]
        self._count += 1
        self.count_label.setText(f"  {self._count} 条  ")
        if self._match(level, msg):
            self._render(ts, level, msg)

    # ---- 本地存档 ----
    def _on_keep_toggled(self, on: bool):
        self._keep = on
        if on and self._log_file is None:
            self._log_file = new_session_log(self._start)
        self._update_file_label()
        if self.get_cfg is not None:
            try:
                cfg = self.get_cfg()
                cfg["log_keep_local"] = bool(on)
                save_config(cfg)
            except Exception:
                pass
        self.append("info" if on else "warn",
                    f"本地存档已{'开启' if on else '关闭'}"
                    + (f"，写入 {self._log_file.name if self._log_file else '（创建失败）'}"
                       if on else "，本次运行内容不再落盘（历史文件原样保留）"))

    def _update_file_label(self):
        if self._keep and self._log_file:
            self.file_label.setText(
                f"本地日志（本次启动）：{self._log_file}（每条实时写入，无需手动保存）")
        elif self._keep:
            self.file_label.setText("本地日志：存档创建失败（logs/ 目录不可写？）")
        else:
            self.file_label.setText("本地日志：已关闭，本次运行内容不会写入文件")

    def _open_log_dir(self):
        try:
            LOG_DIR.mkdir(exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(LOG_DIR))  # noqa: S606
            else:
                self.append("info", f"日志目录：{LOG_DIR}")
        except OSError:
            self.append("error", f"无法打开日志目录 {LOG_DIR}")

    def _match(self, level: str, msg: str) -> bool:
        if self._level and level != self._level:
            return False
        if self._keyword and self._keyword not in msg.lower():
            return False
        return True

    def _render(self, ts: str, level: str, msg: str):
        tag = LEVEL_TAG.get(level, "INFO")
        color = LOG_COLORS.get(level, "#CBD5E1")
        safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.view.appendHtml(
            f'<span style="color:#64748B">{ts}</span> '
            f'<span style="color:{color}; font-weight:bold">[{tag}]</span> '
            f'<span style="color:{color}">{safe}</span>')
        if self.auto_scroll_btn.isChecked():
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_filter_changed(self):
        self._level = self.level_combo.currentData() or ""
        self._keyword = self.search_edit.text().strip().lower()
        self.view.clear()
        for ts, level, msg in self._raw:
            if self._match(level, msg):
                self._render(ts, level, msg)

    def _on_export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", f"xk_log_{datetime.now():%Y%m%d_%H%M%S}.txt",
            "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for ts, level, msg in self._raw:
                    f.write(f"{ts} [{LEVEL_TAG.get(level, 'INFO')}] {msg}\n")
            self.append("success", f"日志已导出到 {path}")
        except OSError as e:
            self.append("error", f"导出失败：{e!r}")

    def clear(self):
        self.view.clear()
        self._raw.clear()
        self._count = 0
        self.count_label.setText("  0 条  ")
        if self._keep:
            self.append("info", "已清空界面显示；本地存档文件保留全部历史")


# ---------------------------------------------------------------------------
# 页面 5：设置
# ---------------------------------------------------------------------------
class SettingsPage(QWidget):
    """设置页：基本设置优先，实验性参数折叠到「高级设置」里。"""

    def __init__(self, get_cfg, log):
        super().__init__()
        self.setObjectName("pageRoot")
        self.get_cfg = get_cfg
        self.log = log

        self.scaffold = PageScaffold(
            "设置", "基本设置对绝大多数场景已经够用；高级设置仅排查问题时再展开。")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scaffold)
        layout = self.scaffold.content_layout()

        # 选课方式
        way_card = QFrame()
        way_card.setObjectName("card")
        wf = QVBoxLayout(way_card)
        wf.setContentsMargins(20, 18, 20, 18)
        wf.setSpacing(12)
        wf.addWidget(make_card_title("01  选课方式", "默认选课方式代码"))
        r = QHBoxLayout()
        r.setSpacing(10)
        r.addWidget(QLabel("选课方式代码："))
        self.xkfsdm = QLineEdit()
        self.xkfsdm.setPlaceholderText("例如 sztzk-b-b")
        self.xkfsdm.setMinimumHeight(34)
        r.addWidget(self.xkfsdm, 1)
        wf.addLayout(r)
        wf_tip = QLabel("去「课程搜索」页选好选课方式，记住左上角显示的代码填到这里。默认 sztzk-b-b（素质拓展课）。")
        wf_tip.setObjectName("tipBox")
        wf_tip.setWordWrap(True)
        wf.addWidget(wf_tip)
        layout.addWidget(way_card)

        # 提交方式
        submit_card = QFrame()
        submit_card.setObjectName("card")
        sf = QVBoxLayout(submit_card)
        sf.setContentsMargins(20, 18, 20, 18)
        sf.setSpacing(12)
        sf.addWidget(make_card_title("02  提交方式", "怎么把课抢到手"))
        self.auto_submit = QCheckBox("发现余量立即自动提交（推荐）")
        self.auto_submit.setStyleSheet("font-size:13.5px; padding:4px 0;")
        sf.addWidget(self.auto_submit)
        r2 = QHBoxLayout()
        r2.setSpacing(10)
        r2.addWidget(QLabel("提交链路："))
        self.submit_mode = QComboBox()
        self.submit_mode.addItem("只调 addGouwuche（推荐）", "direct")
        self.submit_mode.addItem("addGouwuche + addXuanke 两步", "cart")
        self.submit_mode.setMinimumHeight(34)
        r2.addWidget(self.submit_mode, 1)
        sf.addLayout(r2)
        sm_tip = QLabel("大多数情况下用「只调 addGouwuche」就行；如果服务端要求确认步骤才计入已选，再切到「两步」。")
        sm_tip.setObjectName("tipBox")
        sm_tip.setWordWrap(True)
        sf.addWidget(sm_tip)
        layout.addWidget(submit_card)

        # 失败重试
        retry_card = QFrame()
        retry_card.setObjectName("card")
        rf = QVBoxLayout(retry_card)
        rf.setContentsMargins(20, 18, 20, 18)
        rf.setSpacing(12)
        rf.addWidget(make_card_title("03  失败重试", "仅对「直接选课」模式生效"))
        rs = QHBoxLayout()
        rs.setSpacing(12)
        rs.addWidget(QLabel("失败重试次数（0 = 不重试）："))
        self.direct_retries = QSpinBox()
        self.direct_retries.setRange(0, 20)
        self.direct_retries.setSuffix(" 次")
        self.direct_retries.setMinimumHeight(34)
        rs.addWidget(self.direct_retries)
        rs.addSpacing(20)
        rs.addWidget(QLabel("重试间隔："))
        self.direct_retry_interval = QDoubleSpinBox()
        self.direct_retry_interval.setRange(3, 300)
        self.direct_retry_interval.setDecimals(1)
        self.direct_retry_interval.setSuffix(" 秒")
        self.direct_retry_interval.setMinimumHeight(34)
        rs.addWidget(self.direct_retry_interval)
        rs.addStretch(1)
        rf.addLayout(rs)
        dr_tip = QLabel("满员 / 限流 / 未找到课程 会自动重试；未开抢 / 超限选 / 时间冲突 重试无意义，会直接放弃。")
        dr_tip.setObjectName("tipBox")
        dr_tip.setWordWrap(True)
        rf.addWidget(dr_tip)
        layout.addWidget(retry_card)

        # 高级
        self.advanced_toggle = QCheckBox("显示高级设置（仅排查问题时展开）")
        self.advanced_toggle.setStyleSheet("font-size:13px; font-weight:600; padding:4px 0;")
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        layout.addWidget(self.advanced_toggle)

        self.advanced_widget = QFrame()
        self.advanced_widget.setObjectName("card")
        al = QVBoxLayout(self.advanced_widget)
        al.setContentsMargins(20, 18, 20, 18)
        al.setSpacing(12)
        al.addWidget(make_card_title("04  高级", "轮询节流与提交参数"))

        timing = QFrame()
        timing.setObjectName("cardFlat")
        tf = QVBoxLayout(timing)
        tf.setSpacing(10)
        tf.setContentsMargins(16, 14, 16, 14)
        tf.addWidget(QLabel("轮询与限流").__class__("轮询与限流"))
        ti1 = QHBoxLayout()
        ti1.addWidget(QLabel("开抢后轮询间隔（≥3 稳妥）："))
        self.poll_interval = QDoubleSpinBox()
        self.poll_interval.setRange(2, 300)
        self.poll_interval.setDecimals(1)
        self.poll_interval.setSuffix(" 秒")
        self.poll_interval.setMinimumHeight(32)
        ti1.addWidget(self.poll_interval)
        ti1.addStretch(1)
        tf.addLayout(ti1)
        ti2 = QHBoxLayout()
        ti2.addWidget(QLabel("开抢前倒计时刷新："))
        self.pre_poll_interval = QSpinBox()
        self.pre_poll_interval.setRange(10, 600)
        self.pre_poll_interval.setSuffix(" 秒")
        self.pre_poll_interval.setMinimumHeight(32)
        ti2.addWidget(self.pre_poll_interval)
        ti2.addSpacing(20)
        ti2.addWidget(QLabel("同接口最小间隔："))
        self.min_interval = QDoubleSpinBox()
        self.min_interval.setRange(2, 60)
        self.min_interval.setDecimals(1)
        self.min_interval.setSuffix(" 秒")
        self.min_interval.setMinimumHeight(32)
        ti2.addWidget(self.min_interval)
        ti2.addStretch(1)
        tf.addLayout(ti2)
        ti_tip = QLabel("触发限流会自动退避；这些值决定稳态轮询节奏，正常不需要改。")
        ti_tip.setObjectName("tipBox")
        ti_tip.setWordWrap(True)
        tf.addWidget(ti_tip)
        al.addWidget(timing)

        xktjz = QFrame()
        xktjz.setObjectName("cardFlat")
        xf = QVBoxLayout(xktjz)
        xf.setSpacing(10)
        xf.setContentsMargins(16, 14, 16, 14)
        for label, attr in [
            ("direct 链路：", "xktjz_direct"),
            ("进购物车：", "xktjz_gwc"),
            ("购物车 → 已选：", "xktjz_yx"),
        ]:
            r = QHBoxLayout()
            r.addWidget(QLabel(label))
            le = QLineEdit()
            le.setMinimumHeight(32)
            setattr(self, attr, le)
            r.addWidget(le, 1)
            xf.addLayout(r)
        xk_tip = QLabel("服务端「提交」接口的细分动作代号（默认值即可，9/4 实弹后按需调整）。")
        xk_tip.setObjectName("tipBox")
        xk_tip.setWordWrap(True)
        xf.addWidget(xk_tip)
        al.addWidget(xktjz)

        self.advanced_widget.setVisible(False)
        layout.addWidget(self.advanced_widget)

        layout.addStretch(1)

        # 保存按钮放页头右侧（固定，不用滚到底才能保存）
        a = ensure_ui_assets()
        self.save_btn = QPushButton(" 保存设置")
        self.save_btn.setObjectName("btnPrimary")
        self.save_btn.setIcon(QIcon(a["ic_check.png"]))
        self.save_btn.setIconSize(QSize(14, 14))
        self.save_btn.setMinimumWidth(140)
        self.save_btn.setMinimumHeight(38)
        self.save_btn.clicked.connect(self.on_save)
        self.scaffold.add_action(self.save_btn)

    def _toggle_advanced(self, checked: bool):
        self.advanced_widget.setVisible(checked)

    def refresh(self):
        cfg = self.get_cfg()
        self.xkfsdm.setText(cfg.get("xkfsdm", "sztzk-b-b"))
        self.auto_submit.setChecked(bool(cfg.get("auto_submit", True)))
        self.submit_mode.setCurrentIndex(
            max(0, self.submit_mode.findData(cfg.get("submit_mode", "direct"))))
        self.direct_retries.setValue(int(cfg.get("direct_retries", 3)))
        ri = cfg.get("direct_retry_interval")
        self.direct_retry_interval.setValue(
            float(ri) if ri else float(cfg.get("poll_interval", 5)))
        self.poll_interval.setValue(float(cfg.get("poll_interval", 5)))
        self.pre_poll_interval.setValue(int(cfg.get("pre_poll_interval", 30)))
        self.min_interval.setValue(float(cfg.get("min_interval", 3)))
        self.xktjz_direct.setText(cfg.get("xktjz_direct", "rwtjzyx"))
        self.xktjz_gwc.setText(cfg.get("xktjz_gwc", "rwtjzgwc"))
        self.xktjz_yx.setText(cfg.get("xktjz_yx", "gwctjzyx"))

    def on_save(self):
        cfg = self.get_cfg()
        cfg["xkfsdm"] = self.xkfsdm.text().strip() or "sztzk-b-b"
        cfg["auto_submit"] = self.auto_submit.isChecked()
        cfg["submit_mode"] = self.submit_mode.currentData()
        cfg["direct_retries"] = self.direct_retries.value()
        cfg["direct_retry_interval"] = self.direct_retry_interval.value()
        cfg["poll_interval"] = self.poll_interval.value()
        cfg["pre_poll_interval"] = self.pre_poll_interval.value()
        cfg["min_interval"] = self.min_interval.value()
        cfg["xktjz_direct"] = self.xktjz_direct.text().strip() or "rwtjzyx"
        cfg["xktjz_gwc"] = self.xktjz_gwc.text().strip() or "rwtjzgwc"
        cfg["xktjz_yx"] = self.xktjz_yx.text().strip() or "gwctjzyx"
        save_config(cfg)
        self.log("success", "设置已保存到 config.json")
        if cfg["poll_interval"] < SAFE_INTERVAL:
            self.log("warn", f"轮询间隔 {cfg['poll_interval']:.0f}s 低于稳妥值 {SAFE_INTERVAL:.0f}s，注意限流风险")


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("USTB Selector · 选课抢课助手")
        self.resize(1180, 800)
        self.setMinimumSize(720, 560)
        self._apply_theme()

        self.bridge = Bridge()
        self.cfg = load_config()
        self._auto_collapsed = False

        # ---- 页面 ----
        self.log_page = LogPage(self._get_cfg)
        self.session_page = SessionPage(self.bridge, self._get_cfg, self.log_page.append)
        self.search_page = SearchPage(self.bridge, self._get_cfg, self.log_page.append)
        self.targets_page = TargetsPage(self.bridge, self._get_cfg, self.log_page.append)
        self.monitor_page = MonitorPage(self.bridge, self._get_cfg, self.log_page.append,
                                        self.targets_page)
        self.settings_page = SettingsPage(self._get_cfg, self.log_page.append)

        # tabs 保持这个名字（smoke.py / 预览脚本依赖），内部换成 QStackedWidget
        self.tabs = QStackedWidget()
        for page in (self.session_page, self.search_page, self.targets_page,
                     self.monitor_page, self.log_page, self.settings_page):
            self.tabs.addWidget(page)

        # ---- 左侧导航 ----
        self.nav = NavPane()
        self.nav_session = self.nav.add_item(NavButton("ic_lock.png", "会话管理"))
        self.nav_search = self.nav.add_item(NavButton("ic_search.png", "课程搜索"))
        self.nav_targets = self.nav.add_item(NavButton("ic_target.png", "抢课目标"))
        self.nav_monitor = self.nav.add_item(NavButton("ic_bolt.png", "监控运行"))
        self.nav_log = self.nav.add_item(NavButton("ic_log.png", "运行日志"))
        self.nav_settings = self.nav.add_item(NavButton("ic_gear.png", "设置"), bottom=True)
        self.nav_about = self.nav.add_item(NavButton("ic_info.png", "关于"), bottom=True)
        self.nav.toggle_btn.clicked.connect(self._toggle_nav)
        self._nav_map = {
            self.nav_session: self.session_page,
            self.nav_search: self.search_page,
            self.nav_targets: self.targets_page,
            self.nav_monitor: self.monitor_page,
            self.nav_log: self.log_page,
            self.nav_settings: self.settings_page,
        }
        for btn, page in self._nav_map.items():
            btn.clicked.connect(partial(self._goto, btn, page))
        self.nav_about.clicked.connect(self._on_about)

        # ---- 组装窗口 ----
        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)
        bl.addWidget(self.nav)
        bl.addWidget(self.tabs, 1)

        root = QWidget()
        root.setObjectName("appRoot")
        rv = QVBoxLayout(root)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        rv.addWidget(self._build_header())
        rv.addWidget(body, 1)
        self.status_bar = AppStatusBar()
        rv.addWidget(self.status_bar)
        self.setCentralWidget(root)

        # ---- 信号接线 ----
        self.bridge.log.connect(self.log_page.append)
        self.bridge.state.connect(self.monitor_page._on_state)
        self.bridge.state.connect(self.targets_page._on_state)
        self.bridge.state.connect(self._on_state_statusbar)
        self.bridge.search_done.connect(self._on_search_done)
        self.bridge.meta_ready.connect(self.search_page.fill_meta)
        self.bridge.meta_ready.connect(self.monitor_page.fill_ways)
        self.bridge.meta_ready.connect(self.targets_page.fill_ways)
        self.bridge.enums_ready.connect(self.search_page.fill_enums)
        self.bridge.test_done.connect(self._on_test_done)
        self.bridge.notify.connect(self._on_notify)
        self.bridge.live_changed.connect(self._on_live_changed)
        self.search_page.target_set.connect(self.targets_page.refresh)
        self.search_page.target_set.connect(self._sync_target_badge)
        self.targets_page.targets_changed.connect(self.monitor_page._fill_targets)
        self.targets_page.targets_changed.connect(self._sync_target_badge)
        self.targets_page.targets_changed.connect(self.search_page._update_target_label)
        self.monitor_page.goto_targets_btn.clicked.connect(
            lambda: self._goto(self.nav_targets, self.targets_page))

        self.nav_session.setChecked(True)
        self._setup_tray()
        self._refresh_all()
        self._sync_target_badge()
        self._apply_responsive()

    # ---------------- 导航 / 响应式 ----------------
    def _goto(self, btn: NavButton, page: QWidget):
        for b in self._nav_map:
            b.setChecked(b is btn)
        self.nav_about.setChecked(False)
        self.tabs.setCurrentWidget(page)
        self._on_page_changed(page)

    def _on_about(self):
        self.nav_about.setChecked(False)
        AboutDialog(self).exec()

    def _toggle_nav(self):
        self._auto_collapsed = self.nav.is_collapsed()
        self.nav.set_collapsed(not self.nav.is_collapsed())

    def _apply_responsive(self):
        """窗口变窄时导航自动折叠成图标栏；手动展开后不再自动收。"""
        narrow = self.width() < NavPane.COLLAPSE_WIDTH
        want = True if narrow else self._auto_collapsed
        if want != self.nav.is_collapsed():
            self.nav.set_collapsed(want)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_responsive()

    def _sync_target_badge(self):
        n = len(self.get_cfg().get("targets") or [])
        self.nav_targets.set_badge(str(n) if n else "")
        self.status_bar.set_targets(n)

    def get_cfg(self) -> dict:
        return self._get_cfg()

    def _on_state_statusbar(self, st: dict):
        self.status_bar.bump_requests()
        pi = st.get("poll_interval")
        if pi:
            self.status_bar.set_interval(pi)

    def _on_page_changed(self, page: QWidget):
        if page is self.search_page:
            self.search_page.load_meta()
            self.search_page.load_enums()
        elif page is self.monitor_page:
            self.monitor_page.refresh()
        elif page is self.targets_page:
            self.targets_page.refresh()

    def _apply_theme(self):
        qapp = QApplication.instance()
        if qapp is None:
            return
        f = QFont("Microsoft YaHei UI", 9)
        f.setStyleStrategy(QFont.PreferAntialias)
        qapp.setFont(f)
        # 注入 page 标题/副标题的 QSS（不在 _QSS_TMPL 里以免与 QSS 占位符冲突）
        qapp.setStyleSheet(build_qss() + EXTRA_QSS)

    def _build_header(self) -> QWidget:
        """品牌顶栏：深色 + 渐变 + 品牌 mark + 标题 + 实时状态 + 版本 pill。"""
        header = QWidget()
        header.setObjectName("appHeader")
        header.setFixedHeight(64)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(24, 10, 24, 10)
        hl.setSpacing(14)

        # 品牌 mark
        a = ensure_ui_assets()
        brand = QLabel()
        brand.setFixedSize(40, 40)
        brand.setPixmap(QPixmap(a["brand_sm.png"]).scaled(
            40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        hl.addWidget(brand)

        # 标题区
        tb = QVBoxLayout()
        tb.setSpacing(1)
        title = QLabel("USTB Selector")
        title.setObjectName("appTitle")
        sub = QLabel("北京科技大学 · 选课抢课助手")
        sub.setObjectName("appSub")
        tb.addWidget(title)
        tb.addWidget(sub)
        hl.addLayout(tb)

        hl.addStretch(1)

        # 实时状态
        self.live_indicator = LiveIndicator(header)
        hl.addWidget(self.live_indicator)

        # 版本 pill
        ver = QLabel("v1.0 · 个人版")
        ver.setObjectName("appVerPill")
        hl.addWidget(ver)
        return header

    def _get_cfg(self) -> dict:
        self.cfg = load_config()
        return self.cfg

    def _refresh_all(self):
        for page in (self.session_page, self.search_page, self.monitor_page,
                     self.targets_page, self.settings_page):
            page.refresh()
        cfg = self._get_cfg()
        self.status_bar.set_interval(cfg.get("poll_interval", 5))
        self.status_bar.set_session(bool(cfg.get("session")))
        self.status_bar.set_hint("先在「会话管理」验证 SESSION"
                                 if not cfg.get("session") else "")

    def _on_live_changed(self, active: bool):
        self.live_indicator.set_active(active)
        self.status_bar.set_running(active)

    def _setup_tray(self):
        from PySide6.QtWidgets import QMenu
        self.tray = QSystemTrayIcon(make_brand_icon(32), self)
        self.tray.setToolTip("USTB Selector")
        menu = QMenu()
        act_show = QAction("显示窗口", self)
        act_show.triggered.connect(self._show_window)
        act_start = QAction("开始监控", self)
        act_start.triggered.connect(lambda: (self._goto(self.nav_monitor, self.monitor_page),
                                             self.monitor_page.on_start()))
        act_stop = QAction("停止监控", self)
        act_stop.triggered.connect(self.monitor_page.on_stop)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_show)
        menu.addAction(act_start)
        menu.addAction(act_stop)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.show()
        self.tray.activated.connect(
            lambda reason: self._show_window() if reason == QSystemTrayIcon.DoubleClick else None)

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self):
        if self.monitor_page.monitor and self.monitor_page.monitor.is_alive():
            self.monitor_page.monitor.stop()
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, e):
        e.ignore()
        self.hide()
        self.tray.showMessage("抢课程序", "已最小化到托盘，监控不受影响",
                              QSystemTrayIcon.Information, 2000)

    def _on_search_done(self, data: dict, err: str):
        self.search_page._on_search_result(data, err)

    def _on_test_done(self, info: dict, err: str):
        self.session_page.save_btn.setEnabled(True)
        self.session_page.save_btn.setText("保存并测试")
        if err:
            self.session_page.info_label.setObjectName("errBox")
            self.session_page.info_label.setText("✗ " + err)
            self.session_page.info_label.style().unpolish(self.session_page.info_label)
            self.session_page.info_label.style().polish(self.session_page.info_label)
            self.log_page.append("error", err)
            self.status_bar.set_session(False)
            self.status_bar.set_hint("会话验证失败，请重新获取 SESSION")
            return
        self.status_bar.set_session(True)
        self.status_bar.set_term(str(info.get("xnxq") or ""))
        self.status_bar.set_hint("")
        xkms = str(info.get("xkms"))
        mode_txt = "先到先得" if xkms == "1" else info.get("xkms")
        txt = (f"会话有效\n"
               f"选课学期：{info.get('xnxq')}\n"
               f"可选课程：{info.get('total')} 门\n"
               f"已选：全量 {info.get('enrolled_all')} 门 / 本方式 {info.get('enrolled_way')} 门\n"
               f"模式：{mode_txt}    限选 {info.get('limit')} 门\n"
               f"选课窗口：{info.get('window')}\n"
               f"服务器时间偏差：{info.get('offset', 0):+.1f}s")
        self.session_page.info_label.setObjectName("okBox")
        self.session_page.info_label.setText(txt)
        self.session_page.info_label.style().unpolish(self.session_page.info_label)
        self.session_page.info_label.style().polish(self.session_page.info_label)
        self.log_page.append("success", "会话测试通过：学期、规则、窗口已读取")
        self._refresh_all()

    def _on_notify(self, title: str, msg: str, level: str = "info"):
        icon = QSystemTrayIcon.Information
        if level == "error":
            icon = QSystemTrayIcon.Critical
        elif level == "found":
            icon = QSystemTrayIcon.Warning
        self.tray.showMessage(title, msg, icon, 6000)


# ---------------------------------------------------------------------------
# 页面级（不在 _QSS_TMPL 里的）样式补充 — 标题/副标题/标签/特殊卡片
# ---------------------------------------------------------------------------
EXTRA_QSS = f"""
QLabel#pageTitle {{
    color: {T.TEXT_1}; font-size: 22px; font-weight: 700;
    letter-spacing: -0.3px;
}}
QLabel#pageSub {{
    color: {T.TEXT_2}; font-size: 12.5px; line-height: 1.5;
}}
QLabel#qrTitle {{
    color: {T.TEXT_1}; font-size: 18px; font-weight: 700;
}}
QLabel#qrSub {{
    color: {T.TEXT_2}; font-size: 12.5px; line-height: 1.5;
}}
QLabel#qrStatus {{
    color: {T.TEXT_2}; font-size: 12.5px; padding: 8px 12px;
    background: {T.SUBTLE_BG}; border: 1px solid {T.BORDER};
    border-radius: 8px;
}}
QFrame#qrCard {{
    background: #FFFFFF; border: 1px solid {T.BORDER};
    border-radius: 14px;
}}
QDialog#qrDialog {{ background: {T.APP_BG}; }}

/* ---------- 页面骨架：固定页头 + 滚动内容 ---------- */
QWidget#pageHeader {{
    background: {T.CARD_BG};
    border-bottom: 1px solid {T.BORDER};
}}
QScrollArea#pageScroll {{
    background: {T.APP_BG}; border: none;
}}
QScrollArea#pageScroll QWidget#pageContent {{
    background: transparent;
}}
QWidget#pageFooter {{
    background: {T.CARD_BG};
    border-top: 1px solid {T.BORDER};
}}

/* ---------- 左侧导航 ---------- */
QWidget#navPane {{
    background: {T.NAV_BG};
    border-right: 1px solid {T.BORDER};
}}
QPushButton#navBtn {{
    background: transparent; border: none; border-radius: 8px;
    color: {T.TEXT_2}; font-size: 13.5px; text-align: left;
    padding: 0 10px; min-height: 38px;
}}
QPushButton#navBtn:hover {{ background: {T.NAV_HOVER}; color: {T.TEXT_1}; }}
QPushButton#navBtn:checked {{
    background: {T.BRAND_SOFT}; color: {T.BRAND_ACTIVE}; font-weight: 600;
}}
QPushButton#navBtn:focus {{ outline: none; border: 1px solid {T.BORDER_FOCUS}; }}
QPushButton#navToggle {{
    background: transparent; border: none; border-radius: 8px;
    color: {T.TEXT_3}; padding: 0;
}}
QPushButton#navToggle:hover {{ background: {T.NAV_HOVER}; color: {T.TEXT_1}; }}
QFrame#navSep {{ color: {T.BORDER}; background: {T.BORDER}; max-height: 1px; border: none; }}

/* ---------- 底部状态栏 ---------- */
QWidget#appStatusBar {{
    background: {T.NAV_BG};
    border-top: 1px solid {T.BORDER};
}}
QLabel#statusText {{
    color: {T.TEXT_2}; font-size: 12px; padding: 0 12px;
}}
QLabel#statusHint {{
    color: {T.TEXT_3}; font-size: 12px; padding: 0 4px;
}}
QLabel#statusDot {{
    background: {T.TEXT_3}; border-radius: 5px; min-width: 9px; max-width: 9px;
    min-height: 9px; max-height: 9px;
}}
QLabel#statusDotActive {{
    background: {T.OK}; border-radius: 5px; min-width: 9px; max-width: 9px;
    min-height: 9px; max-height: 9px;
}}
QFrame#statusSep {{
    color: {T.BORDER}; background: {T.BORDER}; max-width: 1px; border: none;
}}

/* ---------- 可折叠分区 ---------- */
QPushButton#sectionToggle {{
    background: transparent; border: none; color: {T.TEXT_1};
    font-size: 14px; font-weight: 600; text-align: left; padding: 2px 0;
}}
QPushButton#sectionToggle:hover {{ color: {T.BRAND_ACTIVE}; }}

/* ---------- 二级界面：课程详情 / 关于 ---------- */
QDialog#detailDialog {{ background: {T.APP_BG}; }}
QFrame#detailHero {{
    background: {T.CARD_BG}; border: 1px solid {T.BORDER};
    border-radius: 14px;
}}
QLabel#detailTitle {{
    color: {T.TEXT_1}; font-size: 20px; font-weight: 700;
}}
QLabel#detailCode {{
    color: {T.TEXT_3}; font-size: 12.5px;
}}
QLabel#detailKey {{
    color: {T.TEXT_3}; font-size: 12px; padding: 8px 0;
}}
QLabel#detailVal {{
    color: {T.TEXT_1}; font-size: 13px; padding: 8px 0;
}}
QLabel#detailValStrong {{
    color: {T.OK_DEEP}; font-size: 15px; font-weight: 700; padding: 8px 0;
}}
QLabel#detailValZero {{
    color: {T.ERR_DEEP}; font-size: 15px; font-weight: 700; padding: 8px 0;
}}
QLabel#logFileLabel {{
    color: {T.TEXT_3}; font-size: 12px; padding: 0 2px;
}}
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("系统托盘不可用，退出程序")
        return 1
    w = MainWindow()
    w.show()
    if os.environ.get("USTB_SELFTEST") == "1":
        # 打包后冒烟自测：显示 2.5 秒后自动退出，验证启动到 LogPage 落盘无报错
        QTimer.singleShot(2500, app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
