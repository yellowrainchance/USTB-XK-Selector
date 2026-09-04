#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check.py — 抢课程序 CLI 自检（只读，绝不发写请求）

用法：
  python check.py                 # 会话 + 学期 + 规则 + 已选
  python check.py --kcdm 1019003  # 追加目标课精查（也可先填进 config.json）

每次请求自动带 ≥3 秒同接口间隔；共 2~3 次请求，无提交行为。
"""
import argparse
import sys

from xk_core import (APIError, SessionExpired, XKClient, extract_courses,
                     extract_rules, load_config, match_course, remaining_of,
                     enrolled_of)


def main() -> int:
    ap = argparse.ArgumentParser(description="抢课程序只读自检")
    ap.add_argument("--kcdm", help="目标课程代码（覆盖 config.json 的 target_kcdm）")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("session"):
        print("config.json 里还没有填 session（浏览器登录后复制 SESSION Cookie 值）")
        return 1
    kcdm = (args.kcdm or cfg.get("target_kcdm") or "").strip()

    c = XKClient(cfg)

    print("== [1/3] 会话 + 学期发现  POST Xsxk/queryXkdqXnxq")
    try:
        disc = c.discover()
        print(f"  当前选课学期：{disc.get('p_dqxn')} 学期{disc.get('p_dqxq')}  "
              f"p_dqxnxq={disc.get('p_dqxnxq')}  cxsfmt={c.form['cxsfmt']}")
    except SessionExpired as e:
        print(f"  ✗ {e}")
        return 2

    print("== [2/3] 课程列表 + 规则  POST Xsxk/queryKxrw (第1页)")
    try:
        res = c.list_courses(1)
        r = extract_rules(res, cfg.get("xkfsdm"))
        rule = r["rule"]
        total = (res.get("kxrwList") or {}).get("total")
        print(f"  可选 {total} 门 / 全量已选 {len(r['enrolled_codes'])} 门 / "
              f"本方式已选 {len(r['enrolled_way'])} 门")
        print(f"  模式={rule.get('xkms')} 阶段={rule.get('lcmc')} 限选{rule.get('xkzys')}门")
        print(f"  窗口 {rule.get('ksrq')} ~ {rule.get('jsrq')}")
        dqsj = c.sync_server_time(rule.get("dqsj"))
        if dqsj:
            print(f"  服务器时间 {dqsj}  本机偏差 {c.server_offset:+.1f}s")
    except (APIError, SessionExpired) as e:
        print(f"  ✗ {e}")
        return 2

    if kcdm:
        print(f"== [3/3] 目标课精查  POST Xsxk/queryKxrw (p_gjz={kcdm})")
        try:
            r2 = c.query_by_kcdm(kcdm)
            courses = extract_courses(r2)
            t = match_course(courses, kcdm, cfg.get("target_kxh") or "")
            if t:
                print(f"  ✅ {t.get('kcdm')} {t.get('kcmc')} 课序号{t.get('kxh')}  "
                      f"容量{t.get('zrl')}/已选{enrolled_of(t)}  余量{remaining_of(t)}")
                print(f"     教师 {t.get('dgjsmc')}  学分{t.get('xf')}  校区{t.get('xiaoqumc')}")
            else:
                print(f"  ⚠️ 结果 {len(courses)} 条，未匹配到 {kcdm}（可看原始响应排查）")
                for cc in courses[:3]:
                    print(f"     {cc.get('kcdm')} {cc.get('kcmc')} {cc.get('kxh')}")
        except (APIError, SessionExpired) as e:
            print(f"  ✗ {e}")
            return 2
    else:
        print("== [3/3] 跳过目标课精查（用 --kcdm 或填 config.json 的 target_kcdm）")

    print("\n自检完成（只读，未发任何写请求）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
