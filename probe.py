#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
USTB 本研一体化教务系统 · 选课探针（单次诊断运行）

用法：
  1. 在同目录 config.json 的 "session" 里填入浏览器登录后的 SESSION Cookie 值
  2. 运行：python probe.py

做什么：
  [1] 会话校验        POST Xsxk/queryXkdqXnxq（未登录会被重定向到登录页，据此判断）
  [2] 学期自动发现    p_xn / p_xq / p_xnxq（明年换学期无需改代码）
  [3] 选课规则读取    xkgzszOne：先到先得/抽签、起止时间、限选门数、服务器时间
  [4] 课程列表拉取    POST Xsxk/queryKxrw（第 1 页），校验余量公式 zrl - yxzrs
  [5] 目标课精查      config.json 里填了 target_kcdm 时，用 queryKxrwByKcdm_js 精确定位
  [6] 提交链路测试    test_submit=true 时，对 target_kcdm 发一次 addGouwuche
                     （当前不在选课时段，预期返回"不在时间范围"类错误——
                       这恰好能验证端点、参数结构、错误文案三件事，不会真的选上课）

不做什么：
  - 不写死任何学期/规则/课程代码，全部运行时发现
  - 不做高频轮询（这是探针，不是抢课器）

⚠️ 字段与接口的唯一事实来源：../SYSTEM_NOTES.md
   改本文件前先读它；实测发现与笔记不符时，先改笔记再改代码。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先: pip install requests")
    sys.exit(1)

BASE = "https://byyt.ustb.edu.cn/"
TIMEOUT = 20

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://byyt.ustb.edu.cn",
    "Referer": "https://byyt.ustb.edu.cn/Xsxk/query/1",
}


def load_config():
    p = Path(__file__).with_name("config.json")
    if not p.exists():
        print("找不到 config.json")
        sys.exit(1)
    cfg = json.loads(p.read_text(encoding="utf-8"))
    if not cfg.get("session"):
        print("=" * 56)
        print("config.json 里还没有填 session。")
        print("获取方法：")
        print("  1. 浏览器登录 https://byyt.ustb.edu.cn/")
        print("  2. F12 -> 应用(Application) -> Cookie -> byyt.ustb.edu.cn")
        print("  3. 复制 SESSION 那一行的值，粘到 config.json 的 session 字段")
        print("=" * 56)
        sys.exit(1)
    return cfg


def make_http(session_value):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("SESSION", session_value, domain="byyt.ustb.edu.cn", path="/")
    return s


def post_api(http, path, form):
    """POST 表单并解析 JSON。被踢出登录时服务端会 302 到登录页(HTML)，据此报错。"""
    r = http.post(BASE + path, data=form, timeout=TIMEOUT, allow_redirects=False)
    if r.status_code in (301, 302):
        raise PermissionError(f"{path} 被重定向到 {r.headers.get('Location')} —— SESSION 已失效")
    ctype = r.headers.get("Content-Type", "")
    if "json" not in ctype.lower():
        raise PermissionError(f"{path} 返回非 JSON（{ctype}）—— 会话大概率已失效")
    return r.json()


def base_queryform():
    """与前端 xsxk JS 里 queryform 初始定义一一对应（null 序列化为空串）。"""
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


def fmt_row(c):
    zrl = int(c.get("zrl") or 0)
    yx = int(c.get("yxzrs") or 0)
    return (f"  {c.get('kcdm',''):<10} {(c.get('kcmc') or '')[:20]:<22}"
            f" 容量{zrl:>4} 已选{yx:>4} 余量{zrl-yx:>4}  id={c.get('id','')[:8]}…")


def main():
    cfg = load_config()
    http = make_http(cfg["session"])
    form = base_queryform()
    ok, fail = 0, 0

    # [1][2] 会话校验 + 学期发现
    print("\n[1/5] 会话校验 & 学期发现  POST Xsxk/queryXkdqXnxq")
    try:
        res = post_api(http, "Xsxk/queryXkdqXnxq", form)
        # 注意：此接口响应没有 jg/message 字段，直接返回学期对象
        # 关键字段是 p_dqxn/p_dqxq/p_dqxnxq（当前学年学期）；
        # p_xn/p_xq/p_xnxq 可能为 null，服务端在查询时自行解析，照前端原样填回即可
        dq = res.get("p_dqxnxq") or ""
        if not dq:
            raise PermissionError("响应里没有 p_dqxnxq —— 会话可能已失效")
        form["p_dqxn"] = res.get("p_dqxn") or ""
        form["p_dqxq"] = res.get("p_dqxq") or ""
        form["p_dqxnxq"] = dq
        # 与前端 mounted 钩子行为一致：p_xn/p_xq/p_xnxq 照 res 填（可能是空）
        for k in ("p_xn", "p_xq", "p_xnxq"):
            form[k] = res.get(k) or ""
        form["cxsfmt"] = res.get("cxsfmt") or "0"
        # p_xkfsdm（选课方式代码）服务端不会在学期发现里给，必须从配置带
        # 实弹验证：queryKxrw 不带它返回 jg=-1 操作失败
        form["p_xkfsdm"] = (cfg.get("xkfsdm") or "sztzk-b-b").strip()
        print(f"  会话有效。当前选课学期: {form['p_dqxn']} 学期{form['p_dqxq']} "
              f"(p_dqxnxq={dq})  cxsfmt={form['cxsfmt']}  p_xkfsdm={form['p_xkfsdm']}")
        ok += 1
    except Exception as e:
        print(f"  失败: {e}")
        return 1

    # [3][4] 课程列表 + 规则
    print("\n[2/5] 课程列表 & 选课规则  POST Xsxk/queryKxrw (第1页)")
    page = dict(form, pageNum=1, pageSize=18)
    try:
        res = post_api(http, "Xsxk/queryKxrw", page)
        if str(res.get("jg")) != "1":
            raise RuntimeError(f"jg={res.get('jg')}, message={res.get('message')}")
        page_info = res.get("kxrwList") or {}
        courses = page_info.get("list") or []
        rule = (res.get("xsxkPage") or {}).get("xkgzszOne") or {}
        yx = res.get("yxkcList") or []
        print(f"  可选课程共 {page_info.get('total')} 门（本页 {len(courses)} 门），已选 {len(yx)} 门")
        if rule:
            print(f"  规则: 模式={rule.get('xkms')}({'先到先得' if rule.get('xkms')=='1' else '抽签'}) "
                  f"阶段={rule.get('lcmc')} 限选{rule.get('xkzys')}门")
            print(f"  窗口: {rule.get('ksrq')} ~ {rule.get('jsrq')}")
            dqsj = rule.get("dqsj")
            if dqsj:
                try:
                    server = datetime.strptime(dqsj, "%Y-%m-%d %H:%M:%S")
                    offset = (datetime.now() - server).total_seconds()
                    print(f"  服务器时间: {dqsj}  本机偏差约 {offset:+.1f}s")
                except ValueError:
                    print(f"  服务器时间: {dqsj}")
        for c in courses[:8]:
            print(fmt_row(c))
        if len(courses) > 8:
            print(f"  …（其余 {len(courses)-8} 门略）")
        ok += 1
    except Exception as e:
        print(f"  失败: {e}")
        fail += 1

    # [5] 目标课精查
    kcdm = (cfg.get("target_kcdm") or "").strip()
    if kcdm:
        print(f"\n[3/5] 目标课精查  POST Xsxk/queryKxrwByKcdm_js (kcdm={kcdm})")
        try:
            f2 = dict(form, p_kcdm_cxrw=kcdm, p_kcdm_js=kcdm)
            res = post_api(http, "Xsxk/queryKxrwByKcdm_js", f2)
            print(f"  jg={res.get('jg')}  message={res.get('message')}")
            ok += 1
        except Exception as e:
            print(f"  失败: {e}")
            fail += 1
    else:
        print("\n[3/5] 跳过（config.json 未填 target_kcdm）")

    # [6] 提交链路测试
    if cfg.get("test_submit") and kcdm:
        print(f"\n[4/5] 提交链路测试  POST Xsxk/addGouwuche (p_xktjz={cfg.get('test_xktjz')})")
        print("  当前不在选课时段，预期返回时间范围类错误 —— 目的是验证链路而非选课")
        try:
            target = next((c for c in courses if c.get("kcdm") == kcdm), None)
            if not target:
                print(f"  第 1 页里没有 kcdm={kcdm}，跳过提交测试（可先确认课程代码）")
            else:
                f3 = dict(form, p_id=target["id"], p_xkxs="",
                          p_xktjz=cfg.get("test_xktjz") or "rwtjzyx")
                res = post_api(http, "Xsxk/addGouwuche", f3)
                print(f"  jg={res.get('jg')}  message={res.get('message')}")
                print("  （任何结构化响应都说明端点与参数被服务端正常处理了）")
                ok += 1
        except Exception as e:
            print(f"  失败: {e}")
            fail += 1
    else:
        print("\n[4/5] 跳过提交测试（test_submit 未开启或未指定目标课）")

    print(f"\n[5/5] 完成：成功 {ok} 项，失败 {fail} 项")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
