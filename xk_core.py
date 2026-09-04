#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xk_core.py — 北科大教务抢课程序 · 核心库
==========================================
UI（app.py）与 CLI（check.py）都只依赖这一层，别的文件不要绕过它直接发请求。

事实来源：../SYSTEM_NOTES.md —— 改行为前先读；实测与笔记不符时，先改笔记再改代码。

职责：
  1. Config   —— config.json 读写（SESSION 属敏感信息，不落代码、不入库、不上传）
  2. XKClient —— 封装全部接口。内置：按接口限流、掉线检测、服务器时间同步
  3. Monitor  —— 后台轮询线程。状态机：同步 → 等开抢(慢) → 快轮询 → 提交 → 复核
                 事件通过回调抛给 UI（Qt 信号转接）或 CLI

设计原则（防明年改版）：
  - 学期 / 选课规则 / 限选门数 全部运行时发现，零硬编码
  - 三个未确认项（p_xktjz 取值、submit_mode、错误文案）全部参数化 + 错误分类
  - 请求间隔有下限；触发限流自动指数退避，不硬怼
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE = "https://byyt.ustb.edu.cn/"
TIMEOUT = 20

# 实测：同接口 2 秒间隔连发 4 次全成功；1 秒内连发必被限流（见 SYSTEM_NOTES §13.3）
MIN_INTERVAL = 2.0     # 硬下限，任何间隔不允许低于它
SAFE_INTERVAL = 3.0    # 稳妥下限（UI 滑块从这起步）

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://byyt.ustb.edu.cn",
    "Referer": "https://byyt.ustb.edu.cn/Xsxk/query/1",
}


def _app_dir() -> Path:
    """数据目录：PyInstaller 打包后=exe 所在目录（便携，config/logs 跟着 exe 走）；
    开发运行时=本源码目录。打包后 __file__ 指向临时解压目录，绝不能拿来存东西。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()

CONFIG_PATH = APP_DIR / "config.json"

# 筛选枚举本地缓存（按"选课方式|学期"分桶，避免切换方式后翻页重拉全量）
ENUM_CACHE_PATH = APP_DIR / "enum_cache.json"
ENUM_CACHE_TTL = 6 * 3600      # 有效期 6 小时；课程池随开抢窗口变化，够新也不至于天天重拉


def load_enum_cache() -> dict:
    """读枚举缓存文件；缺失/损坏返回 {}。"""
    try:
        if not ENUM_CACHE_PATH.exists():
            return {}
        raw = json.loads(ENUM_CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def save_enum_cache(cache: dict) -> None:
    """写枚举缓存文件（失败静默，缓存只是加速不是必需）。"""
    try:
        ENUM_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    except Exception:
        pass


def enum_cache_get(key: str) -> dict | None:
    """取未过期的缓存枚举；没有/已过期返回 None。key 形如 'sztzk-b-b|2026-20271'。"""
    if not key:
        return None
    entry = load_enum_cache().get(key)
    if not isinstance(entry, dict):
        return None
    try:
        ts = float(entry.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if time.time() - ts > ENUM_CACHE_TTL:
        return None
    enums = entry.get("enums")
    return enums if isinstance(enums, dict) else None


def enum_cache_put(key: str, enums: dict) -> None:
    if not key or not isinstance(enums, dict):
        return
    cache = load_enum_cache()
    cache[key] = {"ts": time.time(), "enums": enums}
    save_enum_cache(cache)


def enum_cache_invalidate(key: str | None = None) -> None:
    """删一个 key；key=None 清空整个枚举缓存（手动刷新/学期更替时用）。"""
    cache = load_enum_cache()
    if key is None:
        cache.clear()
    else:
        cache.pop(key, None)
    save_enum_cache(cache)

DEFAULT_CONFIG = {
    "session": "",            # SESSION Cookie（浏览器登录后复制）
    "xkfsdm": "sztzk-b-b",    # 选课方式代码（素质拓展课）
    "run_mode": "grab",       # grab=定时抢课（等窗口+轮询+自动提交） | direct=直接选课（立即提交一次）
    "targets": [],            # 抢课目标列表 [{kcdm, kxh, kcmc, xkfsdm}]，可多门
    "target_kcdm": "",        # 兼容旧配置/CLI：= targets[0]（保存时自动同步）
    "target_kxh": "",         # 目标课序号（可空：只按课程代码匹配）
    "start_mode": "auto",     # 开始时间来源：auto=跟随选课方式规则 | custom=自定义 | now=立即
    "start_time": "",         # 自定义开始时间 "YYYY-MM-DD HH:MM:SS"
    "end_mode": "auto",       # 结束：auto=跟随规则 jsrq | custom=自定义 | none=不限
    "end_time": "",           # 自定义结束时间 "YYYY-MM-DD HH:MM:SS"
    "poll_interval": 5,       # 开抢后的快轮询间隔（秒）
    "pre_poll_interval": 30,  # 开抢前的慢等待刷新间隔（秒）
    "direct_retries": 3,      # 直接选课失败自动重试次数（满员/限流/未找到会重试）
    "direct_retry_interval": 0,  # 直接选课重试间隔（秒），0=跟随 poll_interval
    "min_interval": 3,        # 同接口最小请求间隔（秒）
    "auto_submit": True,      # 余量出现是否全自动提交
    "submit_mode": "direct",  # direct=只调 addGouwuche | cart=addGouwuche+addXuanke
    "xktjz_direct": "rwtjzyx",  # 未确认项1：先到先得直接选课的 p_xktjz
    "xktjz_gwc": "rwtjzgwc",    # 任务 → 购物车
    "xktjz_yx": "gwctjzyx",     # 购物车 → 已选（已确认字面量）
    "log_keep_local": True,     # 运行日志是否自动保留到本地（logs/ 按天分文件，见 app.py LogPage）
}


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class XKError(Exception):
    """基类。"""


class SessionExpired(XKError):
    """SESSION 失效：被重定向到登录页或返回非 JSON。"""


class APIError(XKError):
    """服务端返回 jg != '1' 的业务错误。"""

    def __init__(self, path: str, jg, message: str):
        self.path = path
        self.jg = jg
        self.message = message or ""
        super().__init__(f"{path} 返回 jg={jg} message={self.message}")


# ---------------------------------------------------------------------------
# 错误分类（未确认项3：满员/限选/冲突/时间窗 的具体文案，等 9/4 实弹后补进表）
# ---------------------------------------------------------------------------
ERROR_PATTERNS: list[tuple[str, list[str]]] = [
    ("not_open",       ["不在", "时间范围", "未开始", "未到", "未开放", "开放时间"]),
    ("rate_limited",   ["频率过高", "频繁", "稍后重试", "请求过多"]),
    ("already",        ["重复", "已选过", "已经选过", "已修过", "不能重复"]),
    ("full",           ["容量", "满员", "已满", "余量", "选满", "人数已满"]),
    ("limit",          ["限选", "超过", "超出", "上限", "门数"]),
    ("conflict",       ["冲突", "时间冲突"]),
    ("cart",           ["购物车"]),
]


def classify_error(message: str) -> str:
    """把服务端错误文案归类，返回类别名；认不出返回 'unknown'。"""
    if not message:
        return "unknown"
    for cat, keys in ERROR_PATTERNS:
        for k in keys:
            if k in message:
                return cat
    return "unknown"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise XKError(f"config.json 不是合法 JSON，请检查: {CONFIG_PATH}")
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    return cfg


def save_config(cfg: dict) -> None:
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 基础表单
# ---------------------------------------------------------------------------
def base_queryform() -> dict:
    """与前端 xsxk JS 里 queryform 初始定义一一对应（null 序列化为空串）。

    注意：$.post 会把整个对象发过去，服务端有 SQL 拼接逻辑，
    所以这里保持全字段发送、不要裁剪（SYSTEM_NOTES §4）。
    """
    return {
        "cxsfmt": "0", "p_pylx": "1", "mxpylx": "1", "p_sfgldjr": "0",
        "p_sfredis": "0", "p_sfsyxkgwc": "0", "p_xktjz": "",
        "p_chaxunxh": "", "p_gjz": "", "p_skjs": "",
        "p_xn": "", "p_xq": "", "p_xnxq": "",
        "p_dqxn": "", "p_dqxq": "", "p_dqxnxq": "",
        "p_xkfsdm": "", "p_xiaoqu": "", "p_kkyx": "", "p_kclb": "", "p_xkxs": "",
        "p_dyc": "", "p_kkxnxq": "", "p_id": "", "p_ids[]": "",
        "p_sfhlctkc": "0", "p_sfhllrlkc": "0",
        "p_kxsj_xqj": "", "p_kxsj_ksjc": "", "p_kxsj_jsjc": "",
        "p_kcdm_js": "", "p_kcdm_cxrw": "", "p_kcdm_cxrw_zckc": "",
        "p_kc_gjz": "",
        "p_xzcxtjz_nj": "", "p_xzcxtjz_yx": "", "p_xzcxtjz_zy": "",
        "p_xzcxtjz_zyfx": "", "p_xzcxtjz_bj": "",
        "p_sfxsgwckb": "1", "p_skyy": "", "p_sfmxzj": "0",
    }


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class XKClient:
    """选课模块接口封装。按接口限流（同接口间隔 ≥ min_interval）。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.form = base_queryform()
        self.form["p_xkfsdm"] = (cfg.get("xkfsdm") or "sztzk-b-b").strip()
        self.min_interval = max(MIN_INTERVAL, float(cfg.get("min_interval") or SAFE_INTERVAL))
        self._http = requests.Session()
        self._http.headers.update(HEADERS)
        sid = (cfg.get("session") or "").strip()
        if sid:
            self._http.cookies.set("SESSION", sid, domain="byyt.ustb.edu.cn", path="/")
        self._lock = threading.Lock()          # 全局串行，避免多线程并发打崩
        self._last_call: dict[str, float] = {}
        self._server_offset = 0.0              # 本机 - 服务器（秒）
        self.language_enums: list[tuple[str, str]] = []  # (DM, MC) 授课语言
        self.enroll_year = ""                  # 入学年份（yxkcList.njmc），拼 20xx-20xx-x 用

    # ---- 低层 ----
    def _post(self, path: str, form: dict) -> dict:
        with self._lock:
            t = time.monotonic()
            wait = self.min_interval - (t - self._last_call.get(path, 0.0))
            if wait > 0:
                time.sleep(wait)
            try:
                r = self._http.post(BASE + path, data=form, timeout=TIMEOUT,
                                    allow_redirects=False)
            finally:
                self._last_call[path] = time.monotonic()
        if r.status_code in (301, 302):
            raise SessionExpired(f"{path} 被重定向到 {r.headers.get('Location')} —— SESSION 已失效")
        ctype = r.headers.get("Content-Type", "")
        if "json" not in ctype.lower():
            raise SessionExpired(f"{path} 返回非 JSON（{ctype}）—— 会话大概率已失效")
        return r.json()

    # ---- 学期发现 ----
    def discover(self) -> dict:
        """queryXkdqXnxq：完整表单必须整包发送（只发 3 字段时 p_xn/p_xq/p_xnxq 返回 null）。
        返回响应并回填 self.form 的学期值 + cxsfmt。"""
        res = self._post("Xsxk/queryXkdqXnxq", self.form)
        if not (res.get("p_dqxnxq") or ""):
            raise SessionExpired("响应里没有 p_dqxnxq —— 会话可能已失效")
        self.form["p_dqxn"] = res.get("p_dqxn") or ""
        self.form["p_dqxq"] = res.get("p_dqxq") or ""
        self.form["p_dqxnxq"] = res.get("p_dqxnxq") or ""
        # 与前端 mounted 钩子一致：p_xn/p_xq/p_xnxq 照 res 填（可能是空）
        for k in ("p_xn", "p_xq", "p_xnxq"):
            self.form[k] = res.get(k) or ""
        self.form["cxsfmt"] = res.get("cxsfmt") or "0"
        return res

    # ---- 查询 ----
    # 筛选参数 → queryform 字段（与原版选课系统一致，全部服务端过滤）
    FILTER_KEYS = {
        "gjz": "p_gjz",            # 关键字（课程代码/名称）
        "skjs": "p_skjs",          # 授课教师（模糊）
        "kkyx": "p_kkyx",          # 开课学院（代码）
        "kclb": "p_kclb",          # 课程类别（代码）
        "kcxz": "p_kcxz",          # 课程性质（代码）
        "xiaoqu": "p_xiaoqu",      # 校区（代码）
        "skyy": "p_skyy",          # 授课语言（代码）
        "sfmxzj": "p_sfmxzj",      # 是否面向自己 '1'=面向 '-1'=不面向 '0'=全部
        "hlctkc": "p_sfhlctkc",    # 忽略冲突课程 '1'
        "hllrlkc": "p_sfhllrlkc",  # 忽略零容量课程 '1'
    }

    def list_courses(self, page: int = 1, page_size: int = 18,
                     filters: dict | None = None) -> dict:
        """queryKxrw：可选课程列表（含规则 xkgzszOne、已选 yxkcList、allKcidNotIn）。

        filters：与前端一致的筛选条件（键见 FILTER_KEYS），只传非空的，其余保持空串。
        """
        f = dict(self.form)
        for k, field in self.FILTER_KEYS.items():
            v = (filters or {}).get(k)
            if v is None:
                continue  # 未筛选的键保持 self.form 原值，最小侵入
            f[field] = str(v)
        f.update(pageNum=page, pageSize=page_size)
        res = self._post("Xsxk/queryKxrw", f)
        if str(res.get("jg")) != "1":
            raise APIError("Xsxk/queryKxrw", res.get("jg"), res.get("message"))
        return res

    def query_by_kcdm(self, kcdm: str) -> dict:
        """按课程代码精确查（抢课用它，避免翻页）。

        ⚠️ 实测（2026-09-01）：Xsxk/queryKxrwByKcdm_js 只返回 {jg, message}
        （"该课程有可选任务"），**不带课程数据**——原版 JS 里它只是"按课程代码
        检索"弹窗的提示条接口。真正能拿到课程行的是 queryKxrw + p_gjz 关键词
        过滤，返回完整响应（含 kxrwList / xsxkPage / yxkcList）。
        """
        return self.list_courses(1, 50, {"gjz": kcdm})

    def enum_cache_key(self) -> str:
        """缓存桶键 = 选课方式|学期（学期取 p_dqxnxq，没有则退回 p_xnxq）。"""
        dm = str(self.form.get("p_xkfsdm") or "")
        xq = str(self.form.get("p_dqxnxq") or self.form.get("p_xnxq") or "")
        return f"{dm}|{xq}"

    def fetch_course_enums(self, max_pages: int = 5, use_cache: bool = True) -> dict:
        """拉全量课程（pageSize=100 翻页）聚合筛选下拉枚举。

        只读操作；每页间隔受全局限流保护（同接口 ≥ min_interval）。
        返回 extract_enums() 的结果。若 SESSION 失效抛 SessionExpired。

        use_cache=True（默认）：先查本地缓存（键=选课方式|学期），命中且未过期
        直接返回不翻页；未命中才拉取并写缓存。手动刷新传 use_cache=False。
        """
        key = self.enum_cache_key()
        if use_cache:
            hit = enum_cache_get(key)
            if hit is not None:
                return hit
        enums = {"kkyx": [], "kclb": [], "kcxz": [], "xiaoqu": [], "skyy": [], "dgjs": []}
        page = 1
        while page <= max_pages:
            res = self.list_courses(page, 100)
            courses = extract_courses(res)
            part = extract_enums(courses)
            for k in enums:
                # 合并保序去重
                for item in part[k]:
                    if isinstance(item, tuple):
                        if (item[0], item[1]) not in {(a, b) for a, b in enums[k]}:
                            enums[k].append(item)
                    elif item not in enums[k]:
                        enums[k].append(item)
            info = page_info(res)
            if page >= info["pages"] or info["pages"] == 0 or not courses:
                break
            page += 1
        if use_cache:
            enum_cache_put(key, enums)
        return enums

    def fetch_enroll_ways(self) -> list[dict]:
        """queryYxkc → xkgzszList：全部选课方式（Tab）及各自规则。

        每个元素是完整规则对象：xkfsdm/xkfsmc/xkms/xkzys/ksrq/jsrq/lcmc/xksfzsjn/
        sfywxrw/dqsj …（实测 8 个：必修/体育I/体育III/素质拓展/专业拓展/MOOC/
        跨专业/校际互选）。顺带把响应里的 skyyList（授课语言枚举）缓存到
        self.language_enums 供 UI 用。
        """
        res = self._post("Xsxk/queryYxkc", dict(self.form))
        ways = res.get("xkgzszList") or []
        sk = res.get("skyyList") or []
        self.language_enums = [(str(x.get("DM")), x.get("MC") or "") for x in sk]
        # 入学年份（yxkcList.njmc，如 '2024'）→ 学年学期下拉显示 20xx-20xx-x 用
        for it in _extract_list(res, "yxkcList"):
            v = str(it.get("njmc") or "").strip()
            if v.isdigit():
                self.enroll_year = v
                break
        else:
            self.enroll_year = ""
        return ways if isinstance(ways, list) else []

    def fetch_kkxnxq_list(self) -> dict:
        """queryKkxqList：开课学年学期枚举（DM=1/2/3 → Autumn/Spring/Summer）
        + 学制 xzdm。与原版一致组合成"学年×学期"选项，值=学年DM+学期DM
        （如 '31'=第3学年Autumn，对应 2026-2027-1），映射 queryform.p_kkxnxq。
        只读。

        显示格式：有入学年份（fetch_enroll_ways 从 yxkcList.njmc 取）时用
        原版同款 '20xx-20xx-x'（如 2026-2027-1）；取不到才退回 '第X学年 · Autumn'。
        """
        res = self._post("Xsxk/queryKkxqList", dict(self.form))
        sems = res.get("list") or []
        xzdm = str(res.get("xzdm") or "")
        # 原版 dataList1：按学制生成"第X学年"（其他学制按 3 年兜底）
        n = {"4": 4, "5": 5, "8": 8}.get(xzdm, 3)
        base = int(self.enroll_year) if str(self.enroll_year).isdigit() else 0
        options = []
        for i in range(1, n + 1):
            for s in sems:
                y_dm = str(i)
                t_dm = str(s.get("DM") or "")
                if base:
                    start = base + i - 1          # 入学年 + 学年偏移 → 学年起始年
                    mc = f"{start}-{start + 1}-{t_dm}"
                else:
                    mc = f"第{i}学年 · {s.get('MC_EN') or s.get('MC') or ''}"
                options.append({"DM": y_dm + t_dm, "MC": mc})
        return {"xzdm": xzdm, "semesters": sems, "options": options}

    def switch_way(self, dm: str) -> None:
        """切换选课方式。对齐原版 qhxxk：改 p_xkfsdm 并重置全部筛选/查询条件
        （不重置学期，学期由 UI 单独控制）。"""
        self.form["p_xkfsdm"] = dm
        for k in ("p_gjz", "p_skjs", "p_kkyx", "p_kclb", "p_kcxz", "p_xiaoqu",
                  "p_skyy", "p_kxsj_xqj", "p_kxsj_ksjc", "p_kxsj_jsjc",
                  "p_kkxnxq", "p_dyc", "p_id", "p_kcdm_js", "p_kcdm_cxrw",
                  "p_kcdm_cxrw_zckc", "p_kc_gjz", "p_xzcxtjz_nj",
                  "p_xzcxtjz_yx", "p_xzcxtjz_zy", "p_xzcxtjz_zyfx",
                  "p_xzcxtjz_bj"):
            self.form[k] = ""
        for k in ("p_sfhlctkc", "p_sfhllrlkc", "p_sfmxzj"):
            self.form[k] = "0"

    def enrolled(self) -> list[dict]:
        """queryYxkc：已选课程列表。解析防御式：几种常见结构都认。"""
        res = self._post("Xsxk/queryYxkc", dict(self.form))
        return _extract_list(res, "yxkcList")

    # ---- 写操作 ----
    def add_gouwuche(self, p_id: str, xktjz: str) -> dict:
        f = dict(self.form, p_id=p_id, p_xkxs="", p_xktjz=xktjz)
        return self._post("Xsxk/addGouwuche", f)

    def add_xuanke(self, p_id: str, xktjz: str | None = None) -> dict:
        f = dict(self.form, p_id=p_id, p_xktjz=xktjz or self.cfg.get("xktjz_yx", "gwctjzyx"))
        return self._post("Xsxk/addXuanke", f)

    def tuike(self, p_id: str) -> dict:
        """退课。注意 p_id 是已选列表的 id（与可选列表的 id 是两套，混用失败）。"""
        return self._post("Xsxk/tuike", dict(self.form, p_id=p_id))

    # ---- 时间 ----
    def sync_server_time(self, dqsj: str | None) -> datetime | None:
        """用服务端回显时间对时。返回解析出的服务器时间（本地时区），解析失败返回 None。"""
        if not dqsj:
            return None
        try:
            server = datetime.strptime(dqsj, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        self._server_offset = (datetime.now() - server).total_seconds()
        return server

    @property
    def server_now(self) -> datetime:
        """按最近一次对时推算的服务器当前时间。"""
        return datetime.now() - timedelta(seconds=self._server_offset)

    @property
    def server_offset(self) -> float:
        return self._server_offset


# ---------------------------------------------------------------------------
# 数据工具
# ---------------------------------------------------------------------------
def enrolled_of(course: dict) -> str | None:
    """已选人数（字符串）。queryKxrw 里 `yxzrs` 实测恒为 None，真实值在 `yxzrlrs`。

    聚合字段缺失时回退：yxzrs → bksyxrlrs + yjsyxrlrs 之和；都拿不到返回 None。
    """
    for k in ("yxzrlrs", "yxzrs"):
        v = course.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    s = 0
    hit = False
    for k in ("bksyxrlrs", "yjsyxrlrs"):
        v = course.get(k)
        if v is not None and str(v).strip() != "":
            try:
                s += int(v)
                hit = True
            except (TypeError, ValueError):
                pass
    return str(s) if hit else None


def remaining_of(course: dict) -> int:
    """余量 = zrl − 已选人数（已选人数字段见 enrolled_of）。"""
    try:
        return int(course.get("zrl") or 0) - int(enrolled_of(course) or 0)
    except (TypeError, ValueError):
        return 0


def match_course(courses: list[dict], kcdm: str, kxh: str = "") -> dict | None:
    """在结果里定位目标课。给了课序号(kxh)就精确匹配；否则同代码的多个教学班
    里取**余量最大**的一行（实测同一 kcdm 会返回多个 id 不同的教学班）。
    """
    if kxh:
        for c in courses:
            if c.get("kcdm") == kcdm and str(c.get("kxh") or "") == str(kxh):
                return c
    best = None
    for c in courses:
        if c.get("kcdm") == kcdm:
            if best is None or remaining_of(c) > remaining_of(best):
                best = c
    return best


def _extract_list(res: dict, primary_key: str) -> list[dict]:
    """防御式取列表：几种响应结构都认。"""
    v = res.get(primary_key)
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    if isinstance(v, dict):
        inner = v.get("list")
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
    v = res.get("list")
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    # 顶层直接是数组？后端未出现，但防御一下
    if isinstance(res, list):
        return [x for x in res if isinstance(x, dict)]
    return []


def extract_courses(res: dict) -> list[dict]:
    """从 queryKxrw / queryKxrwByKcdm_js 响应里取可选课程列表。"""
    return _extract_list(res, "kxrwList")


def page_info(res: dict) -> dict:
    """从 queryKxrw 响应里取分页信息（防御式）。"""
    k = res.get("kxrwList")
    if isinstance(k, dict):
        return {
            "total": int(k.get("total") or 0),
            "page_num": int(k.get("pageNum") or 1),
            "page_size": int(k.get("pageSize") or 0),
            "pages": int(k.get("pages") or 0),
        }
    return {"total": 0, "page_num": 1, "page_size": 0, "pages": 0}


_P_TAG_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

def parse_skxx(html: str | None) -> str:
    """解析 pkjgmx/kcxx 上课信息 HTML → 可读文本。

    原版格式：<div class="ivu-tag">...<p>1-16周,星期五第11-12节 逸夫楼102</p></div>
    返回：多条用 " ｜ " 连接，如 "1-16周,星期五第11-12节 逸夫楼102 ｜ 1-16周,星期三第3-4节 逸夫楼202"
    """
    if not html:
        return ""
    parts = []
    for m in _P_TAG_RE.findall(html):
        txt = _TAG_RE.sub("", m).strip()
        if txt:
            parts.append(txt)
    if not parts:  # 没有 <p> 就整体去标签
        txt = _TAG_RE.sub("", html).strip()
        if txt:
            parts.append(txt)
    return " ｜ ".join(parts)


def _dedup_keep_order(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for code, name in items:
        key = (str(code), str(name))
        if key in seen:
            continue
        seen.add(key)
        out.append((str(code), str(name)))
    return out


def extract_enums(courses: list[dict]) -> dict:
    """从课程列表聚合筛选下拉枚举（代码→名称，保序去重）。

    返回: {"kkyx": [(code,name)...], "kclb": [...], "kcxz": [...],
           "xiaoqu": [...], "skyy": [...], "dgjs": [name...]}
    """
    groups = {
        "kkyx": ("kkyx", "kkyxmc"),
        "kclb": ("kclb", "kclbmc"),
        "kcxz": ("kcxz", "kcxzmc"),
        "xiaoqu": ("xiaoqu", "xiaoqumc"),
        "skyy": ("skyydm", "skyymc"),
    }
    out: dict[str, list] = {k: [] for k in groups}
    out["dgjs"] = []
    for c in courses:
        for key, (code_f, name_f) in groups.items():
            code = c.get(code_f)
            name = c.get(name_f)
            if code is not None and str(code) != "":
                out[key].append((str(code), name or str(code)))
        t = (c.get("dgjsmc") or "").strip()
        if t and t not in out["dgjs"]:
            out["dgjs"].append(t)
    for key in groups:
        out[key] = _dedup_keep_order(out[key])
    return out


def extract_rules(res: dict, xkfsdm: str = "") -> dict:
    """从查询响应里取选课规则 + 已选代码。

    - enrolled_codes：allKcidNotIn（全部已选课程代码，跨方式）→ 判断"目标课是否已选"
    - enrolled_way：yxkcList 里 xkfsdm == 当前方式的代码 → 限选门数按方式统计
      ⚠️ allKcidNotIn 不能用于限选判断（实测它含全部 11 门已选，含必修/专业拓展/MOOC）
    """
    xsxk = res.get("xsxkPage") or {}
    rule = xsxk.get("xkgzszOne") or {}
    enrolled_all = _to_code_set(xsxk.get("allKcidNotIn") or [])
    way_codes: set[str] = set()
    for it in _extract_list(res, "yxkcList"):
        if xkfsdm and it.get("xkfsdm") != xkfsdm:
            continue  # 只统计本选课方式下的已选（限选按方式）
        code = it.get("kcdm") or ""
        if code:
            way_codes.add(code.strip())
    return {"rule": rule, "enrolled_codes": enrolled_all, "enrolled_way": way_codes}


def _to_code_set(items) -> set[str]:
    out = set()
    for it in items:
        if isinstance(it, dict):
            code = it.get("kcdm") or it.get("kch") or ""
        else:
            code = str(it)
        if code:
            out.add(code.strip())
    return out


# ---------------------------------------------------------------------------
# 监控器（后台线程）
# ---------------------------------------------------------------------------
def normalize_targets(cfg: dict) -> list[dict]:
    """把配置里的抢课目标统一成 [{kcdm, kxh, kcmc, xkfsdm}]。

    兼容旧配置：targets 为空时，用老的 target_kcdm/target_kxh 当作唯一目标。
    过滤掉没有课程代码的条目，(kcdm, kxh) 相同的只保留第一条。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for it in (cfg.get("targets") or []):
        if not isinstance(it, dict):
            continue
        kcdm = str(it.get("kcdm") or "").strip()
        if not kcdm or kcdm in ("None", "none"):
            continue
        kxh = str(it.get("kxh") or "").strip()
        kxh = "" if kxh in ("None", "none") else kxh
        if (kcdm, kxh) in seen:
            continue
        seen.add((kcdm, kxh))
        out.append({"kcdm": kcdm, "kxh": kxh,
                    "kcmc": str(it.get("kcmc") or "").strip(),
                    "xkfsdm": str(it.get("xkfsdm") or cfg.get("xkfsdm") or "").strip()})
    if not out:
        kcdm = str(cfg.get("target_kcdm") or "").strip()
        if kcdm and kcdm not in ("None", "none"):
            kxh = str(cfg.get("target_kxh") or "").strip()
            out.append({"kcdm": kcdm, "kxh": "" if kxh in ("None", "none") else kxh,
                        "kcmc": "", "xkfsdm": str(cfg.get("xkfsdm") or "").strip()})
    return out


class Monitor(threading.Thread):
    """多目标抢课 / 选课状态机。回调全部从本线程触发，UI 侧负责转成 Qt 信号。

    目标：cfg["targets"] 里可有多门课，允许跨选课方式；每个目标独立推进状态：
      pending 待处理 → waiting 窗口未开/余量 0 → submitting 提交中
      → done 已选成功 / failed 失败（不再重试）

    事件回调（通过 callbacks 传入）：
      on_log(level, msg)     level ∈ info/warn/success/error
      on_state(state: dict)  状态快照，含 targets（每个目标带运行时 status/remaining/message）
      on_found(course)       余量>0（人工确认模式下通知）
      on_result(outcome)     某个目标抢课成功
      on_done(state)         线程终态（成功/停止/出错都会触发）
    """

    def __init__(self, client: XKClient, cfg: dict, callbacks: dict | None = None):
        super().__init__(daemon=True, name="xk-monitor")
        self.client = client
        self.cfg = cfg
        self.cb = callbacks or {}
        self._stop_evt = threading.Event()
        self._confirm_evt = threading.Event()   # 人工确认模式：等待用户拍板
        self._confirm_res = False
        self.rules_by_way: dict[str, dict] = {}      # 选课方式 → 规则对象
        self.enrolled: set[str] = set()              # 全量已选课程代码
        self.enrolled_by_way: dict[str, set[str]] = {}  # 方式 → 该方式已选代码
        self.state: dict = {}
        self.targets: list[dict] = []                # 目标（运行时带 status/remaining 等）
        self.target = None                           # 最近一次命中的课程对象
        self._interval = max(MIN_INTERVAL, float(cfg.get("poll_interval") or SAFE_INTERVAL))
        self._backoff = self._interval

    # ---- 对外控制 ----
    def stop(self) -> None:
        self._stop_evt.set()

    def confirm_submit(self, yes: bool = True) -> None:
        """人工确认模式下，用户点了 确认/取消。"""
        self._confirm_res = yes
        self._confirm_evt.set()

    def _wait_confirm(self) -> None:
        self._confirm_evt.clear()
        while not self._confirm_evt.wait(0.2):
            if self._stop_evt.is_set():
                self._confirm_res = False
                return

    # ---- 内部事件 ----
    def _log(self, level: str, msg: str) -> None:
        cb = self.cb.get("on_log")
        if cb:
            try:
                cb(level, msg)
            except Exception:
                pass

    # ---- 目标分组 ----
    def _ways(self) -> list[str]:
        """目标涉及的选课方式（保序去重）。"""
        out: list[str] = []
        for t in self.targets:
            dm = t.get("xkfsdm") or ""
            if dm and dm not in out:
                out.append(dm)
        return out

    def _targets_of(self, dm: str) -> list[dict]:
        return [t for t in self.targets if (t.get("xkfsdm") or "") == dm]

    def _finished(self, t: dict) -> bool:
        return t.get("status") in ("done", "failed")

    def _snapshot(self) -> list[dict]:
        return [dict(t) for t in self.targets]

    # ---- 时间窗 ----
    def _group_start(self, dm: str):
        """该选课方式的开始时间（受 start_mode 控制）。"""
        sm = (self.cfg.get("start_mode") or "auto").strip()
        if sm == "now":
            return None
        if sm == "custom":
            return _parse_dt(self.cfg.get("start_time"))
        rule = self.rules_by_way.get(dm) or {}
        return _parse_dt(rule.get("ksrq"))

    def _group_end(self, dm: str):
        """该选课方式的结束时间（受 end_mode 控制）。"""
        em = (self.cfg.get("end_mode") or "auto").strip()
        if em == "none":
            return None
        if em == "custom":
            return _parse_dt(self.cfg.get("end_time"))
        rule = self.rules_by_way.get(dm) or {}
        return _parse_dt(rule.get("jsrq"))

    def _emit_state(self, status: str, message: str = "", **extra) -> None:
        self.state = {
            "status": status, "message": message,
            "poll_interval": self.cfg.get("poll_interval", 5),
            "run_mode": (self.cfg.get("run_mode") or "grab"),
            "targets": self._snapshot(),
            "total": len(self.targets),
            "finished": sum(1 for t in self.targets if self._finished(t)),
            "done": sum(1 for t in self.targets if t.get("status") == "done"),
            "remaining": None, "countdown": None,
        }
        self.state.update(extra)
        cb = self.cb.get("on_state")
        if cb:
            try:
                cb(dict(self.state))
            except Exception:
                pass

    # ---- 主循环 ----
    def run(self) -> None:
        try:
            self._run_inner()
        except SessionExpired as e:
            self._log("error", f"会话失效：{e}")
            self._emit_state("expired", str(e))
        except Exception as e:  # 兜底，绝不让线程静默死掉
            self._log("error", f"监控异常终止：{e!r}")
            self._emit_state("error", str(e))
        finally:
            cb = self.cb.get("on_done")
            if cb:
                try:
                    cb(dict(self.state))
                except Exception:
                    pass

    def _run_inner(self) -> None:
        cfg = self.cfg
        mode = (cfg.get("run_mode") or "grab").strip()
        self.targets = normalize_targets(cfg)
        for t in self.targets:
            t.setdefault("status", "pending")
            t.setdefault("kcmc", "")
            t["remaining"] = None
            t["message"] = ""
        if not self.targets:
            self._log("error", "尚未添加任何抢课目标（去「课程搜索」页点「添加目标」）")
            self._emit_state("error", "未设置目标课程")
            return

        # 1) 会话 + 学期
        self._emit_state("syncing", "同步会话与学期…")
        self._log("info", "会话校验 + 学期发现…")
        disc = self.client.discover()
        self._log("info", f"当前选课学期：{disc.get('p_dqxn')} 学期{disc.get('p_dqxq')} "
                          f"(p_dqxnxq={disc.get('p_dqxnxq')})")

        # 2) 按选课方式分组加载规则 + 已选
        ways = self._ways()
        self._log("info", f"读取选课规则（{len(ways)} 个选课方式）与已选列表…")
        for dm in ways:
            self._ensure_way(dm, silent=False)
        self._log("info", f"目标 {len(self.targets)} 门 / 全量已选 {len(self.enrolled)} 门 "
                          f"/ 服务器偏差 {self.client.server_offset:+.1f}s")

        # 3) 本地预检：已选过的直接标记完成；超限选的直接判失败
        for t in self.targets:
            dm = t.get("xkfsdm") or ""
            rule = self.rules_by_way.get(dm) or {}
            limit = _to_int(rule.get("xkzys"))
            if t["kcdm"] in self.enrolled:
                t["status"] = "done"
                t["message"] = "已在已选列表"
                self._log("success", f"{t['kcdm']} 已在已选列表，跳过")
            elif limit and len(self.enrolled_by_way.get(dm) or set()) >= limit:
                t["status"] = "failed"
                t["message"] = f"该方式已达限选 {limit} 门"
                self._log("error", f"{t['kcdm']}：选课方式 {dm} 已达限选上限 {limit} 门")
        if all(self._finished(t) for t in self.targets):
            self._emit_state("done", "全部目标已处理完毕")
            return

        # 4) 直接选课模式：跳过等待与轮询，每个目标立即提交一次
        if mode == "direct":
            self._run_direct()
            return

        # 5) 等待开抢（开抢前不做任何 HTTP 轮询，只慢速刷新倒计时）
        starts = [s for s in (self._group_start(dm) for dm in ways) if s]
        ksrq = min(starts) if starts else None
        if ksrq:
            if self.client.server_now > ksrq:
                self._log("info", f"开始时间 {ksrq:%Y-%m-%d %H:%M:%S} 已过，直接进入轮询")
            else:
                self._log("info", f"等待开始时间 {ksrq:%Y-%m-%d %H:%M:%S}")
                last_stamp = None
                while not self._stop_evt.is_set():
                    now = self.client.server_now
                    sec = (ksrq - now).total_seconds()
                    if sec <= 0:
                        break
                    stamp = int(sec) // 30  # 30 秒粒度，避免日志刷屏
                    if stamp != last_stamp:
                        last_stamp = stamp
                        if sec > 60:
                            self._log("info", f"距开始还有 {_fmt_duration(sec)}（服务器时间 {now:%H:%M:%S}）")
                        else:
                            self._log("info", f"距开始还有 {sec:.1f} 秒，准备就绪")
                    self._emit_state("waiting", f"距开始 {_fmt_duration(sec)}",
                                     countdown=_fmt_duration(sec))
                    self._wait(min(pre_poll_interval(cfg), sec + 0.5, 60))
                if self._stop_evt.is_set():
                    self._emit_state("stopped", "已手动停止")
                    return

        # 6) 快轮询（多目标）
        self._log("success", f"开始轮询（{len(self.targets)} 个目标，每轮间隔 {self._interval:.0f}s）")
        self._poll_loop()

    # ---- 直接选课（不等待、不轮询、不重试） ----
    def _run_direct(self) -> None:
        """逐个目标：查 → 提交 → 复核，失败按分类**自动重试**（direct_retries 次）。

        重试策略：
          - 会重试：满员（有人退课能捡）、限流/网络抖动、未找到（可能稍后才上线）
          - 立即放弃：未到开抢时间、超限选、时间冲突（重试无意义）
          - 已在已选 → 视为成功
        想无限期盯余量，用 grab（定时抢课）模式。
        """
        cfg = self.cfg
        retries = max(0, int(cfg.get("direct_retries", 3)))
        interval = float(cfg.get("direct_retry_interval") or cfg.get("poll_interval") or SAFE_INTERVAL)
        interval = max(MIN_INTERVAL, interval)
        total = len([t for t in self.targets if not self._finished(t)])
        self._log("info", f"直接选课模式：{total} 个目标，失败自动重试最多 {retries} 次"
                          f"（间隔 {interval:.0f}s；未开抢/超限选/时间冲突不重试）")
        cur = 0
        for dm in self._ways():
            for t in self._targets_of(dm):
                if self._stop_evt.is_set():
                    self._emit_state("stopped", "已手动停止")
                    return
                if self._finished(t):
                    continue
                cur += 1
                self._ensure_way(dm)
                t["status"] = "submitting"
                t["message"] = f"查询中…（{cur}/{total}）"
                self._emit_state("submitting", f"正在处理 {t['kcdm']}（{cur}/{total}）")
                for attempt in range(retries + 1):
                    if self._stop_evt.is_set():
                        self._emit_state("stopped", "已手动停止")
                        return
                    if attempt > 0:
                        self._log("info", f"{t['kcdm']} 第 {attempt}/{retries} 次重试…")
                        t["message"] = f"重试 {attempt}/{retries}…"
                        self._emit_state("submitting", f"正在重试 {t['kcdm']}（{cur}/{total}）")
                    t.pop("_fail_cat", None)
                    try:
                        res = self.client.query_by_kcdm(t["kcdm"])
                        courses = extract_courses(res)
                        c = match_course(courses, t["kcdm"], t.get("kxh") or "")
                        if c is None:
                            cat = "not_found"
                            self._log("warn", f"{t['kcdm']} 未找到（结果 {len(courses)} 条）—— 检查课程代码或选课方式")
                        else:
                            rem = remaining_of(c)
                            t["kcmc"] = c.get("kcmc") or t.get("kcmc") or ""
                            t["remaining"] = rem
                            self.target = c
                            self._log("info", f"{t['kcdm']} {t['kcmc']} 课序号{c.get('kxh')} "
                                              f"容量{c.get('zrl')}/已选{enrolled_of(c)} 余量{rem}")
                            if rem <= 0:
                                self._log("warn", f"{t['kcdm']} 余量为 0，仍按要求提交（大概率被拒满员，会重试）")
                            if self._do_submit(c, cfg, t):
                                break                       # 复核成功
                            cat = t.get("_fail_cat") or "unknown"
                    except SessionExpired:
                        raise
                    except APIError as e:
                        cat = classify_error(e.message)
                        t["message"] = f"{cat}：{e.message}"
                        self._log("warn", f"{t['kcdm']} 查询失败（{cat}）：{e.message}")
                    except Exception as e:
                        cat = "exception"
                        t["message"] = f"{e!r}"
                        self._log("warn", f"{t['kcdm']} 异常：{e!r}")

                    # 已在已选 → 算成功
                    if cat == "already":
                        t["status"] = "done"
                        t["message"] = "已在已选列表"
                        self._log("success", f"{t['kcdm']} 已在已选列表，视为成功")
                        break
                    # 重试无意义的失败：立即放弃
                    if cat in ("not_open", "limit", "conflict"):
                        t["status"] = "failed"
                        t["message"] = f"{cat}（重试无意义）"
                        self._log("error", f"{t['kcdm']} 失败（{cat}），重试无意义，放弃")
                        break
                    # 可重试失败：没到次数上限就等间隔再来
                    if attempt < retries:
                        self._log("info", f"{t['kcdm']} 失败（{cat}），{interval:.0f}s 后重试")
                        self._wait(interval)
                    else:
                        t["status"] = "failed"
                        t["message"] = f"重试 {retries} 次仍失败（{cat}）"
                        self._log("error", f"{t['kcdm']} 重试 {retries} 次后仍失败（{cat}），放弃")

        ok = sum(1 for t in self.targets if t.get("status") == "done")
        fail = sum(1 for t in self.targets if t.get("status") == "failed")
        if ok and not fail:
            self._emit_state("done", f"直接选课完成：{ok} 门已选")
        elif ok:
            self._emit_state("done", f"直接选课结束：成功 {ok} 门，失败 {fail} 门（详见日志）")
        else:
            self._log("info", "没有目标成功。想无限期盯余量，请切回「定时抢课」模式")
            self._emit_state("error", f"直接选课未成功（{fail} 门失败，详见日志）")

    # ---- 快轮询（多目标） ----
    def _poll_loop(self) -> None:
        interval = self._interval
        if interval < SAFE_INTERVAL:
            self._log("warn", f"轮询间隔 {interval:.0f}s 低于稳妥值 {SAFE_INTERVAL:.0f}s，注意限流风险")
        self._backoff = interval
        quiet_cycles = 0
        found_notified = False

        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            for dm in self._ways():
                if self._stop_evt.is_set():
                    break
                # 该方式下的目标都结束了就跳过
                if all(self._finished(t) for t in self._targets_of(dm)):
                    continue
                now = self.client.server_now
                st = self._group_start(dm)
                en = self._group_end(dm)
                if st and now < st:
                    for t in self._targets_of(dm):
                        if not self._finished(t):
                            t["status"] = "waiting"
                            t["message"] = f"未到开始时间（{st:%m-%d %H:%M:%S}）"
                    continue
                if en and now > en:
                    for t in self._targets_of(dm):
                        if not self._finished(t):
                            t["status"] = "failed"
                            t["message"] = "已过选课截止时间"
                    continue
                self._ensure_way(dm)
                for t in self._targets_of(dm):
                    if self._stop_evt.is_set():
                        break
                    if self._finished(t):
                        continue
                    ok = self._check_one(t, dm)
                    if ok == "awaiting":
                        # 人工确认模式：等用户拍板（期间不轮询，防误抢 + 省请求）
                        found_notified = True
                        self._emit_state("awaiting", f"{t['kcdm']} 发现余量，等待人工确认")
                        cb = self.cb.get("on_found")
                        if cb:
                            try:
                                cb(self.target if self.target else t)
                            except Exception:
                                pass
                        self._log("info", f"人工确认模式：{t['kcdm']} 请在界面点击「确认提交」")
                        self._wait_confirm()
                        found_notified = False
                        if self._confirm_res:
                            self._log("info", f"用户确认，提交 {t['kcdm']}")
                            self._do_submit(self.target, self.cfg, t) if self.target else None
                        else:
                            t["message"] = "用户取消了一次提交，继续监控"
                            self._log("info", f"{t['kcdm']} 用户取消提交，继续监控")
                    elif ok is True:
                        quiet_cycles = 0

            if all(self._finished(t) for t in self.targets):
                done_n = sum(1 for t in self.targets if t.get("status") == "done")
                self._log("success", f"全部目标处理完毕（成功 {done_n}/{len(self.targets)}）")
                self._emit_state("done", f"完成：成功 {done_n} / 共 {len(self.targets)}")
                return

            self._emit_state("polling", f"轮询中（{self.state.get('finished', 0)}/{len(self.targets)} 已结束）")
            if not found_notified:
                quiet_cycles += 1
            elapsed = time.monotonic() - t0
            self._wait(max(0.5, self._backoff - elapsed))

        self._emit_state("stopped", "已手动停止")

    # ---- 单个目标：查余量 → 有量就提交 ----
    def _check_one(self, t: dict, dm: str) -> bool | str:
        """返回 True=本轮有进展（有量/已处理），False=无进展，'awaiting'=等人工确认。"""
        cfg = self.cfg
        try:
            res = self.client.query_by_kcdm(t["kcdm"])
            courses = extract_courses(res)
            # 顺带刷新规则/已选/服务器时间（防御接口改版导致的信息缺失）
            if res.get("xsxkPage") or res.get("yxkcList"):
                nr = extract_rules(res, dm)
                if nr["rule"]:
                    self.rules_by_way[dm] = nr["rule"]
                    self.client.sync_server_time(nr["rule"].get("dqsj"))
                if nr["enrolled_codes"]:
                    self.enrolled = nr["enrolled_codes"]
                    self.enrolled_by_way[dm] = nr["enrolled_way"]
            if t["kcdm"] in self.enrolled:
                t["status"] = "done"
                t["message"] = "已在已选列表"
                self._log("success", f"{t['kcdm']} 已在已选列表 —— 抢课成功")
                return True
            c = match_course(courses, t["kcdm"], t.get("kxh") or "")
            if c is None:
                t["status"] = "waiting"
                t["message"] = f"结果里未找到（{len(courses)} 条）"
                self._log("warn", f"{t['kcdm']} 未在结果里 —— 检查课程代码或选课方式")
                self._backoff = min(self._backoff * 2, 60.0)
                return False
            t["kcmc"] = c.get("kcmc") or t.get("kcmc") or ""
            rem = remaining_of(c)
            t["remaining"] = rem
            t["status"] = "waiting" if rem <= 0 else "polling"
            t["message"] = f"余量 {rem}"
            self._backoff = self._interval
            if rem <= 0:
                return False
            self.target = c
            self._log("success", f"🎯 {t['kcdm']} {t['kcmc']} 余量 {rem}（容量{c.get('zrl')}/已选{enrolled_of(c)}）")
            if cfg.get("auto_submit", True):
                self._do_submit(c, cfg, t)
                return True
            return "awaiting"
        except SessionExpired:
            raise
        except APIError as e:
            cat = classify_error(e.message)
            self._backoff = min(self._backoff * 2, 60.0)
            t["message"] = f"查询失败({cat})"
            self._log("warn", f"{t['kcdm']} 查询失败（{cat}）：{e.message}，退避至 {self._backoff:.0f}s")
            return False
        except Exception as e:
            self._backoff = min(self._backoff * 2, 30.0)
            t["message"] = f"异常 {e!r}"
            self._log("warn", f"{t['kcdm']} 轮询异常：{e!r}，退避至 {self._backoff:.0f}s")
            return False

    # ---- 切换选课方式并刷新该方式规则 ----
    def _ensure_way(self, dm: str, silent: bool = True) -> None:
        if self.client.form.get("p_xkfsdm") == dm and dm in self.rules_by_way:
            return
        try:
            if self.client.form.get("p_xkfsdm") != dm:
                self.client.switch_way(dm)
            res = self.client.list_courses(1)
            r = extract_rules(res, dm)
            if r["rule"]:
                self.rules_by_way[dm] = r["rule"]
                self.client.sync_server_time(r["rule"].get("dqsj"))
            if r["enrolled_codes"]:
                self.enrolled = r["enrolled_codes"]
            self.enrolled_by_way[dm] = r["enrolled_way"]
            rule = self.rules_by_way.get(dm) or {}
            if not silent:
                self._log("info", f"[{dm}] 阶段={rule.get('lcmc') or '?'} "
                                  f"模式={'先到先得' if rule.get('xkms') == '1' else rule.get('xkms')} "
                                  f"限选{rule.get('xkzys') or '?'}门 窗口 {rule.get('ksrq')} ~ {rule.get('jsrq')}")
            if not rule:
                self._log("warn", f"[{dm}] 未读到规则对象（xkgzszOne），跳过窗口/限选判断")
        except SessionExpired:
            raise                     # 会话失效必须上抛，由 run() 统一转 expired 状态
        except Exception as e:
            # 切方式/读规则失败（限流、网络抖动…）不能终止监控：
            # 记日志、退避，下一轮轮询会再试；期间该方式目标查询可能拿到旧方式结果，
            # 由 _check_one 的"未找到→退避"兜底，不会误提交
            self._backoff = min(self._backoff * 2, 60.0)
            self._log("warn", f"[{dm}] 切方式/读规则失败：{e!r}，退避至 {self._backoff:.0f}s 后重试")

    # ---- 提交 ----
    def _do_submit(self, target: dict, cfg: dict, t: dict | None = None) -> bool:
        kcdm = target.get("kcdm")
        pid = target.get("id")
        mode = cfg.get("submit_mode", "direct")
        msgs: list[str] = []
        self.target = target
        if t is not None:
            t["status"] = "submitting"
            t["message"] = "提交中…"

        self._emit_state("submitting", f"{kcdm} 发现余量，正在提交（{mode}）…")
        self._log("success", f"开始提交：{kcdm} p_id={pid}  mode={mode}")

        try:
            if mode == "cart":
                r1 = self.client.add_gouwuche(pid, cfg.get("xktjz_gwc", "rwtjzgwc"))
                msgs.append(f"addGouwuche: jg={r1.get('jg')} {r1.get('message')}")
                self._log("info", msgs[-1])
                if str(r1.get("jg")) != "1":
                    self._after_submit_fail(r1, kcdm, cfg, t)
                    return False
                r2 = self.client.add_xuanke(pid, cfg.get("xktjz_yx", "gwctjzyx"))
                msgs.append(f"addXuanke: jg={r2.get('jg')} {r2.get('message')}")
                self._log("info", msgs[-1])
                ok = str(r2.get("jg")) == "1"
            else:  # direct
                r = self.client.add_gouwuche(pid, cfg.get("xktjz_direct", "rwtjzyx"))
                msgs.append(f"addGouwuche: jg={r.get('jg')} {r.get('message')}")
                self._log("info", msgs[-1])
                if str(r.get("jg")) != "1":
                    self._after_submit_fail(r, kcdm, cfg, t)
                    return False
                ok = True
        except SessionExpired:
            raise
        except Exception as e:
            self._log("error", f"提交请求异常：{e!r}")
            return False

        if not ok:
            self._log("warn", "提交返回失败，继续监控")
            return False

        # 复核：queryYxkc 看目标课是否真的在已选里（不要只信 jg）
        self._log("info", "提交返回成功，开始复核已选列表…")
        self._wait(SAFE_INTERVAL)
        try:
            yx = self.client.enrolled()
            in_list = any(str(c.get("kcdm")) == str(kcdm) for c in yx)
        except Exception as e:
            self._log("warn", f"复核失败：{e!r}，稍后再查")
            in_list = False
        if in_list:
            self._log("success", f"✅ 复核确认：{kcdm} {target.get('kcmc')} 已在已选列表")
            if t is not None:
                t["status"] = "done"
                t["message"] = "抢课成功"
            outcome = {"ok": True, "kcdm": kcdm, "kcmc": target.get("kcmc"),
                       "mode": mode, "msgs": msgs}
            cb = self.cb.get("on_result")
            if cb:
                try:
                    cb(outcome)
                except Exception:
                    pass
            # 还有别的目标没处理完 → 回轮询；否则 done
            if t is not None and all(self._finished(x) for x in self.targets):
                self._emit_state("done", f"抢课成功：{kcdm} {target.get('kcmc')}")
            elif t is None:
                self._emit_state("done", f"抢课成功：{kcdm} {target.get('kcmc')}")
            return True
        self._log("warn", "返回成功但已选列表里暂时没有 —— 可能走的是购物车，建议切 submit_mode=cart 或稍等复核")
        if t is not None:
            t["status"] = "polling"
            t["message"] = "提交成功但未确认，继续观察"
        self._emit_state("polling", f"{kcdm} 提交成功但未确认，继续观察")
        return False

    def _after_submit_fail(self, r: dict, kcdm: str, cfg: dict, t: dict | None = None) -> None:
        msg = r.get("message") or ""
        cat = classify_error(msg)
        if t is not None:
            t["_fail_cat"] = cat           # 供 direct 模式判断该不该重试
            if t.get("status") not in ("done", "failed"):
                t["status"] = "polling"
                t["message"] = f"提交被拒：{msg or cat}"
        if cat == "not_open":
            self._log("warn", f"提交被拒：不在选课时间范围（{msg}）—— 继续等")
        elif cat == "full":
            self._log("warn", f"{kcdm} 提交被拒：满员（{msg}）—— 继续盯，有人退就能捡")
        elif cat == "limit":
            self._log("error", f"提交被拒：超过限选（{msg}）—— 建议停掉或换课")
        elif cat == "already":
            self._log("success", f"{kcdm} 已在已选（{msg}）")
        elif cat == "conflict":
            self._log("error", f"提交被拒：时间冲突（{msg}）—— 建议停掉或换课")
        else:
            self._log("warn", f"提交失败（{cat}）：{msg}")

    # ---- 工具 ----
    def _wait(self, seconds: float) -> None:
        self._stop_evt.wait(max(0.0, seconds))


# ---------------------------------------------------------------------------
# 扫码登录（微认证 · 微信扫一扫 → 自动换 byyt SESSION）
# ---------------------------------------------------------------------------
# 链路（已实测验证，2026-09-01，详见 SYSTEM_NOTES §15.3）：
#   GET  /idp/authCenter/authenticate?client_id=YW2025006&... → 302 带 lck/entityId + REQID cookie
#   POST /idp/authn/queryAuthMethods {lck, entityId}          → 认证方式（byyt 配置的是 microQr 微认证）
#   POST /idp/authn/getMicroQr {entityId, lck, authChainCode} → appId/returnUrl/randomToken/url(qrpage)
#   GET  sis.ustb.edu.cn/connect/qrpage?appid&return_url&rand_token → HTML 里服务端渲染 sid
#   GET  /connect/qrimg?sid=...                               → 二维码 PNG
#   轮询 GET /connect/state?sid=...                           → code=1 得 auth_code（扫码+确认后）
#   GET  returnUrl + appid + auth_code + rand_token          → sso 认证 → 302 回 byyt → SESSION cookie
class QrLogin(threading.Thread):
    """后台线程：生成微认证二维码 → 轮询扫码状态 → 成功自动换 SESSION。

    事件回调（全部从本线程触发，UI 侧转 Qt 信号）：
      on_qr(bytes)       二维码 PNG 图片（可显示）
      on_status(str)     状态文字（等待扫码/已扫码请在手机确认/…）
      on_cookie(str)     成功拿到 byyt 的 SESSION 值
      on_expired(str)    二维码失效（可调 start 重新生成）
      on_error(str)      出错
    """

    POLL_INTERVAL = 1.0      # 状态轮询间隔（微认证前端用 500ms，稳妥取 1s）
    DEFAULT_TIMEOUT = 180    # 二维码最大等待秒数

    SSO_BASE = "https://sso.ustb.edu.cn/idp/"
    SIS_BASE = "https://sis.ustb.edu.cn"
    CLIENT_ID = "YW2025006"
    REDIRECT_URI = "https://byyt.ustb.edu.cn/oauth/login/code"

    def __init__(self, callbacks: dict | None = None, session=None,
                 timeout: int = DEFAULT_TIMEOUT):
        super().__init__(daemon=True, name="xk-qrlogin")
        self.cb = callbacks or {}
        self.timeout = timeout
        self._stop_evt = threading.Event()
        self.session = session if session is not None else requests.Session()
        if session is None:
            self.session.headers.update({
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://sso.ustb.edu.cn/ac/",
                "Origin": "https://sso.ustb.edu.cn",
                "Content-Type": "application/json",
            })
        self.sid: str = ""
        self.return_url: str = ""
        self.appid: str = ""
        self.random_token: str = ""

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        try:
            if not self._prepare_qr():
                return
            self._poll_state()
        except Exception as e:  # 兜底
            self._cb("on_error", f"扫码登录异常：{e!r}")

    def _cb(self, name: str, *args) -> None:
        fn = self.cb.get(name)
        if fn:
            try:
                fn(*args)
            except Exception:
                pass

    # ---- 阶段一：生成二维码 ----
    def _prepare_qr(self) -> bool:
        import urllib.parse as up
        # 1) authenticate → lck/entityId + REQID cookie
        r = self.session.get(self.SSO_BASE + "authCenter/authenticate",
                             params={"response_type": "code", "client_id": self.CLIENT_ID,
                                     "redirect_uri": self.REDIRECT_URI, "scope": "all",
                                     "state": "null"}, allow_redirects=False, timeout=20)
        loc = r.headers.get("Location") or ""
        if "#" not in loc or "?" not in loc:
            self._cb("on_error", f"无法获取登录上下文（authenticate 未正常跳转）：{loc[:100]}")
            return False
        fr = loc.split("#", 1)[1].split("?", 1)[1]
        qs = up.parse_qs(fr)
        lck = (qs.get("lck") or [""])[0]
        entity_id = (qs.get("entityId") or [""])[0]
        if not lck or not entity_id:
            self._cb("on_error", "登录上下文不完整（缺 lck/entityId）")
            return False

        # 2) queryAuthMethods → 微认证认证链
        r2 = self.session.post(self.SSO_BASE + "authn/queryAuthMethods",
                               json={"lck": lck, "entityId": entity_id}, timeout=15)
        data = (r2.json().get("data") or []) if r2.status_code == 200 else []
        chain = next((x for x in data if x.get("moduleCode") == "microQr"), data[0] if data else None)
        if not chain:
            self._cb("on_error", "该应用未配置微认证扫码（moduleCode=microQr）")
            return False

        # 3) getMicroQr → 微认证上下文
        r3 = self.session.post(self.SSO_BASE + "authn/getMicroQr",
                               json={"entityId": entity_id, "lck": lck,
                                     "authChainCode": chain.get("authChainCode") or ""},
                               timeout=15)
        d3 = r3.json().get("data") or {}
        self.appid = d3.get("appId") or ""
        self.random_token = d3.get("randomToken") or ""
        self.return_url = d3.get("returnUrl") or ""
        qrpage = d3.get("url") or ""
        if not (self.appid and self.random_token and self.return_url and qrpage):
            self._cb("on_error", "微认证上下文不完整")
            return False

        # 4) qrpage → 解析服务端渲染的 sid（params 交给 requests 自动编码，勿再手动 quote）
        r4 = self.session.get(qrpage, params={"appid": self.appid,
                                              "return_url": self.return_url,
                                              "rand_token": self.random_token},
                              timeout=20)
        m = re.search(r"/connect/qrimg\?sid=([0-9a-fA-F]+)", r4.text)
        if not m:
            self._cb("on_error", "微认证页面解析失败（未找到二维码 sid）")
            return False
        self.sid = m.group(1)

        # 5) 二维码图片
        r5 = self.session.get(self.SIS_BASE + "/connect/qrimg",
                              params={"sid": self.sid}, timeout=20)
        if r5.status_code != 200 or not r5.content:
            self._cb("on_error", "二维码图片获取失败")
            return False
        self._cb("on_qr", r5.content)
        self._cb("on_status", "请用微信扫一扫")
        return True

    # ---- 阶段二：轮询扫码状态 ----
    def _poll_state(self) -> None:
        import urllib.parse as up
        t0 = time.time()
        while not self._stop_evt.is_set():
            if time.time() - t0 > self.timeout:
                self._cb("on_expired", "二维码已过期，请刷新重试")
                return
            try:
                r = self.session.get(self.SIS_BASE + "/connect/state",
                                     params={"sid": self.sid}, timeout=15)
                d = r.json() if r.status_code == 200 else {}
                code = d.get("code")
                if code == 1:                      # 扫码+确认成功，拿 auth_code
                    self._finish_login(str(d.get("data") or ""))
                    return
                if code == 2:
                    self._cb("on_status", "已扫码，请在手机上确认")
                elif code in (0, 4):
                    self._cb("on_status", "等待微信扫码…")
                elif code == 3 or (isinstance(code, int) and code > 200):
                    self._cb("on_expired", "二维码已失效，请刷新")
                    return
                else:
                    self._cb("on_status", f"状态异常({code})：{d.get('message') or ''}")
            except Exception as e:
                self._cb("on_error", f"轮询扫码状态失败：{e!r}")
                return
            self._stop_evt.wait(self.POLL_INTERVAL)
        self._cb("on_error", "已取消")

    # ---- 阶段三：拿 auth_code 换 SESSION ----
    def _finish_login(self, auth_code: str) -> None:
        """回跳 authenticateByLck → sso 颁发 session（Set-Cookie k=session_xxx）
        → thirdPartyAuthEngine 用 session 颁发 OAuth code → 302 跳 byyt 换 SESSION。

        手动跟随每一跳（最多 12 跳）：
        1) 遇 301/302/307/308 → 直接跳 Location
        2) 遇 200 + Set-Cookie → 重试同 URL（最多 3 次），让 sso 同步会话
        3) 遇 200 且 HTML 含 location.href / meta refresh 跳转 → 提取 URL 继续跟
        requests 不执行 JS，从 body 里正则挖跳转地址可绕过 SPA 渲染依赖。
        失败时把**最后一次响应完整 body** 写到 xk/logs/qrlogin_fail_<ts>.html，
        弹窗只显示路径，避免日志爆炸式刷屏；用户分享文件即可逆向 JS 调什么。
        """
        if not auth_code:
            self._cb("on_error", "扫码成功但未返回 auth_code")
            return
        sep = "&" if "?" in self.return_url else "?"
        url = (f"{self.return_url}{sep}appid={self.appid}&auth_code={auth_code}"
               f"&rand_token={self.random_token}")
        from urllib.parse import urljoin
        trail: list[str] = []
        last_resp = None                  # (url, body) 最后一次响应
        js_candidate = None               # (url, body) 首次 200+Set-Cookie（含 JS 的页面）
        _probed = False
        try:
            cur = url
            retries_left = 3
            for _ in range(12):
                r = self.session.get(cur, timeout=25, allow_redirects=False)
                loc = r.headers.get("Location") or ""
                setck = r.headers.get("Set-Cookie") or ""
                body_snip = (r.text[:1500].replace("\n", " ").strip() if r.text else "")
                trail.append(f"{r.status_code} {r.url[:110]}"
                             + (f"  Set-Cookie: {setck[:60]}" if setck else "")
                             + (f"  Body: {body_snip[:180]}" if body_snip and r.status_code == 200 else ""))
                last_resp = (r.url, r.text or "")
                # 首次 200 + Set-Cookie：sso 创建会话的页面（含 JS 绑定认证状态）
                if r.status_code == 200 and setck and js_candidate is None and r.text:
                    js_candidate = (r.url, r.text)

                # 标准跳转
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    cur = urljoin(r.url, loc)
                    retries_left = 3
                    continue

                # 200：优先从 body 挖 JS/meta 跳转 URL（竹云 thirdPartyAuthEngine
                # 就是"var locationValue = <byyt URL>; location = locationValue"，
                # OAuth code 已在 URL 里，直接跳即可，无需 JS 执行）
                if r.status_code == 200 and r.text:
                    m = re.search(r'locationValue\s*=\s*[\'"]([^\'"]+)[\'"]', r.text)
                    if not m:
                        m = re.search(r'(?:location\.href|location\.replace)\s*=\s*[\'"]([^\'"]+)[\'"]', r.text)
                    if not m:
                        m = re.search(r'http-equiv\s*=\s*["\']refresh["\'][^>]*url=([^\'">\s]+)', r.text, re.I)
                    if m:
                        cur = urljoin(r.url, m.group(1).replace("&amp;", "&"))
                        retries_left = 3
                        continue

                # 200 + Set-Cookie：sso 同步会话，重试同 URL（最多 3 次）
                if r.status_code == 200 and setck and retries_left > 0:
                    retries_left -= 1
                    continue

                # 200 + Set-Cookie 但 JS 不在 body（外部 js 文件）→ 探测竹云常见内部接口。
                # 这些接口在带 k 会话 cookie 时返回 302 跳 byyt（OAuth code 颁发）
                if r.status_code == 200 and setck and not _probed:
                    _probed = True
                    from urllib.parse import urlencode, parse_qs as _pqs, urlparse as _up
                    lck_v = (_pqs(_up(r.url).query).get("lck") or [""])[0]
                    for cand in ("authCenter/getAuthCode",
                                 "authCenter/authenticateThirdParty",
                                 "authn/thirdAuth/getAuthCode"):
                        cu = urljoin(r.url, f"{cand}?{urlencode({'lck': lck_v})}")
                        try:
                            pr = self.session.get(cu, timeout=15, allow_redirects=False)
                            p_loc = pr.headers.get("Location") or ""
                            trail.append(f"PROBE {pr.status_code} {cu[:100]}"
                                         + (f"  → {p_loc[:80]}" if p_loc else ""))
                            if pr.status_code in (301, 302, 303, 307, 308) and p_loc:
                                cur = urljoin(pr.url, p_loc)
                                retries_left = 3
                                break
                        except Exception:
                            continue
                    else:
                        break               # 三个候选都未给跳转 → 放弃
                    continue

                break                       # 终点
        except Exception as e:
            self._cb("on_error", f"登录回跳失败：{e!r}")
            return

        for c in self.session.cookies:
            if "session" in c.name.lower() and c.value:
                self._cb("on_status", "登录成功！")
                self._cb("on_cookie", c.value)
                return
        names = sorted({c.name for c in self.session.cookies})
        html_path = ""
        dump = js_candidate or last_resp
        if dump and dump[1]:
            try:
                log_dir = APP_DIR / "logs"
                log_dir.mkdir(exist_ok=True)
                html_path = log_dir / f"qrlogin_fail_{datetime.now():%Y%m%d_%H%M%S}.html"
                html_path.write_text(
                    f"<!-- URL: {dump[0]} -->\n" + dump[1], encoding="utf-8")
            except Exception:
                pass
        self._cb("on_error",
                 "认证完成但未找到 SESSION cookie。跳转轨迹：\n" + "\n".join(trail)
                 + f"\n\n当前 cookie：{names or '（无）'}"
                 + (f"\n\n已导出完整页面（含 JS 跳转逻辑）到：{html_path}\n"
                    "请打开该文件查看 JS（特别找 location.href / $.post / ajax 的接口路径），"
                    "发我后我直接调 sso 内部接口跳过 JS。\n如需重新生成二维码，点弹窗的「刷新二维码」。" if html_path else "")
                 + "\n若停在 thirdPartyAuthEngine 200：那是 sso 用 JS 调内部接口"
                 "颁 OAuth code 的同步页——requests 不执行 JS。")


def _to_int(v, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def _fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分{sec}秒"
    if m:
        return f"{m}分{sec}秒"
    return f"{sec}秒"


def pre_poll_interval(cfg: dict) -> float:
    try:
        return max(10.0, float(cfg.get("pre_poll_interval") or 30))
    except (TypeError, ValueError):
        return 30.0


# ---------------------------------------------------------------------------
# 自检：python xk_core.py（只读，不发写请求）
# ---------------------------------------------------------------------------
def selftest() -> int:
    cfg = load_config()
    if not cfg.get("session"):
        print("config.json 里还没有填 session（浏览器登录后复制 SESSION Cookie 值）")
        return 1
    c = XKClient(cfg)
    print("== 自检：会话 + 学期 ==")
    disc = c.discover()
    print(f"  学期 {disc.get('p_dqxn')} 学期{disc.get('p_dqxq')}  p_dqxnxq={disc.get('p_dqxnxq')}  cxsfmt={c.form['cxsfmt']}")
    print("== 自检：课程列表 + 规则 ==")
    res = c.list_courses(1)
    r = extract_rules(res, cfg.get("xkfsdm"))
    rule = r["rule"]
    print(f"  可选总数 {((res.get('kxrwList') or {}).get('total'))}  "
          f"全量已选 {len(r['enrolled_codes'])} 门 / 本方式已选 {len(r['enrolled_way'])} 门")
    print(f"  模式 {rule.get('xkms')} 限选 {rule.get('xkzys')} 窗口 {rule.get('ksrq')} ~ {rule.get('jsrq')}")
    dqsj = c.sync_server_time(rule.get("dqsj"))
    if dqsj:
        print(f"  服务器时间 {dqsj}  本机偏差 {c.server_offset:+.1f}s")
    kcdm = cfg.get("target_kcdm") or ""
    if kcdm:
        print(f"== 自检：目标课 {kcdm} 精查 ==")
        r2 = c.query_by_kcdm(kcdm)
        courses = extract_courses(r2)
        t = match_course(courses, kcdm, cfg.get("target_kxh") or "")
        if t:
            print(f"  ✅ {t.get('kcdm')} {t.get('kcmc')} 课序号{t.get('kxh')} "
                  f"容量{t.get('zrl')}/已选{enrolled_of(t)} 余量{remaining_of(t)}")
        else:
            print(f"  ⚠️ 结果 {len(courses)} 条，未匹配到 {kcdm}")
    print("自检完成（只读，未发任何写请求）")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
