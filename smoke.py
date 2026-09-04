#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke.py — UI 冒烟测试（offscreen 渲染 + 事件循环跑 2 秒，不联网）。
用法: QT_QPA_PLATFORM=offscreen python smoke.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app  # noqa: E402


def main() -> int:
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    w = app.MainWindow()
    w.show()
    # 模拟一次信号流：日志 + 状态 + 搜索结果填充 + 枚举
    w.bridge.log.emit("info", "冒烟测试：日志通道")
    w.bridge.state.emit({"status": "polling", "message": "测试状态", "remaining": 5,
                         "countdown": None, "poll_interval": 5,
                         "target_kcdm": "TEST", "target_kxh": "001"})
    w.bridge.search_done.emit(
        {"rows": [["1019003", "深海矿产资源开发与利用", "001", "2.0", "32.0",
                   "素质拓展-科学素养(素质拓展)", "任选", "校本部",
                   "1-16周,星期五第11-12节 逸夫楼102", "冯雅丽", "150", "64", "86"]],
         "total": 267, "page": 1, "pages": 15, "page_size": 20}, "")
    w.bridge.enums_ready.emit(
        {"kkyx": [("31", "资源与安全工程学院"), ("28", "人文素质教育中心")],
         "kclb": [("2307", "素质拓展-科学素养(素质拓展)")],
         "kcxz": [("4", "任选")],
         "xiaoqu": [("01", "校本部")],
         "skyy": [("1", "中文")],
         "dgjs": ["冯雅丽", "晋世翔"]})
    w.bridge.test_done.emit({"xnxq": "2026-2027 学期1", "total": 267,
                             "enrolled_all": 11, "enrolled_way": 0, "xkms": "1",
                             "limit": 3, "window": "2026-09-04 15:00:00 ~ 2026-12-31 23:59:00",
                             "offset": 60.9}, "")
    w.tabs.setCurrentWidget(w.monitor_page)
    w.tabs.setCurrentWidget(w.log_page)
    QTimer.singleShot(2000, qapp.quit)
    rc = qapp.exec()
    print("冒烟测试通过：窗口构造 + 信号流 + 渲染无异常")
    return rc


if __name__ == "__main__":
    sys.exit(main())
