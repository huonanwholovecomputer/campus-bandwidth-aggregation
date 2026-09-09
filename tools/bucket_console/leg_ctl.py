#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
leg_ctl.py — 聚合腿列表维护（分流熔断的「摘腿 / 放腿」参考实现）
================================================================================
控制台只负责判定「这条腿丢包太高，该摘了」，怎么摘取决于你的聚合出口。
本脚本给出一种通用做法，适用于**节点列表放在独立文件、由聚合出口读取**的形态
（mihomo / clash 系的 proxy-provider、各类自建聚合器的节点清单等）：

  · full_file     母本全量定义 —— 所有腿的权威清单，本脚本**只从这里渲染**
  · provider_file 实际生效文件 —— 聚合出口真正读取的列表（按剔除集合渲染出来）
  · state_file    剔除集合     —— 被摘掉的腿名（默认 app.data_dir/legs_excluded.json）
  · 热更新        PUT <api>/providers/proxies/<provider_name>（mihomo external-controller）

命令：
  list                        列出全量腿与当前状态（含漂移检测）
  off  --leg <名>             摘腿：加入剔除集合 → 重渲染 → 热更新
  on   --leg <名>             放腿：移出剔除集合 → 重渲染 → 热更新
  sync                        按剔除集合重建 provider_file（修漂移）
  sync --from-breaker         用控制台熔断态（breaker_state.json 的 tripped）当剔除集合
  status                      打印配置与状态摘要

安全设计：
  · 原子写（tmp + os.replace）+ 自动备份 provider_file.bak-<时间戳>（保留 backup_keep 份）
  · keep_min 保护：不允许把腿摘到少于 N 条（默认 1）
  · 幂等：重复摘同一条腿不报错、不重复写
  · 母本缺失时从 provider_file 播种，但会**显著告警**（若 provider_file 已是剔除后的
    结果，播种会丢腿 —— 脚本会把播种来源记进状态文件，`status` 可复查）
  · --dry-run 只打印将要发生的变化，不落盘、不热更新

退出码：0 成功 / 2 配置错误 / 3 腿列表文件问题 / 4 触发 keep_min 保护 / 5 热更新失败
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from console_config import ConfigError, load_config, default_config_path  # noqa: E402

RC_OK, RC_CFG, RC_LEGS, RC_KEEP, RC_RELOAD = 0, 2, 3, 4, 5
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_NAME_RE = re.compile(r"^\s*-\s*name:\s*(.+?)\s*$")
_SERVER_RE = re.compile(r"^\s*server:\s*(.+?)\s*$")
_PORT_RE = re.compile(r"^\s*port:\s*(\d+)\s*$")


def _unquote(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


# --------------------------------------------------------------------------
# 腿列表解析 / 渲染（文本级，保留注释与缩进，不依赖 PyYAML）
# --------------------------------------------------------------------------
def parse_legs(text: str) -> list[dict]:
    """把节点列表拆成 [{name, server, port, block}]。

    只识别「块状写法」（每项以 `- name: xxx` 开头）；遇到 `- {name: ...}` 这种
    流式写法会抛 ValueError —— 宁可不做，也不能猜错把腿弄丢。
    """
    blocks, cur = [], None
    for line in (text or "").splitlines():
        if re.match(r"^\s*-\s*\{", line):
            raise ValueError("检测到流式写法 `- {name: ...}`，本脚本只支持块状节点定义")
        m = _NAME_RE.match(line)
        if m:
            if cur:
                blocks.append(cur)
            cur = {"name": _unquote(m.group(1)), "server": "", "port": 0,
                   "lines": [line]}
        elif cur is not None:
            cur["lines"].append(line)
            ms = _SERVER_RE.match(line)
            if ms:
                cur["server"] = _unquote(ms.group(1))
            mp = _PORT_RE.match(line)
            if mp:
                cur["port"] = int(mp.group(1))
    if cur:
        blocks.append(cur)
    for b in blocks:
        b["block"] = "\n".join(b.pop("lines")).rstrip("\n")
    return blocks


def render_legs(full_text: str, excluded) -> str:
    """按剔除集合重拼 provider 文件内容（母本顺序保持不变）。"""
    blocks = parse_legs(full_text)
    ex = set(excluded or ())
    keep = [b for b in blocks if b["name"] not in ex]
    idx = (full_text or "").find("- name:")
    prefix = full_text[:idx] if idx > 0 else ""
    if not prefix.strip():
        prefix = "proxies:\n"
    body = "\n".join(b["block"] for b in keep)
    # rstrip 去掉 `- name:` 那一行残留的缩进（否则会多出一行只有空格的空行）
    return prefix.rstrip() + "\n" + (body + "\n" if body else "")


# --------------------------------------------------------------------------
# 状态与文件
# --------------------------------------------------------------------------
def state_path(cfg) -> str:
    return cfg.aggregation["legs"]["state_file"]


def load_state(cfg) -> dict:
    try:
        with open(state_path(cfg), "r", encoding="utf-8-sig") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(cfg, d: dict) -> bool:
    path = state_path(cfg)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        d = dict(d)
        d["_note"] = ("腿剔除集合（leg_ctl.py 维护）：excluded 里的腿不会出现在 provider_file 里；"
                      "删除本文件等于「全部放回」，再跑一次 sync 即可")
        d["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001
        print("!! 状态文件写失败：%s" % e, file=sys.stderr)
        return False


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def write_provider(cfg, text: str, dry: bool = False) -> tuple[bool, str]:
    """原子写 provider_file（先备份）。返回 (ok, 说明)。"""
    pf = cfg.aggregation["legs"]["provider_file"]
    if dry:
        return True, "（dry-run 未写）"
    try:
        os.makedirs(os.path.dirname(pf) or ".", exist_ok=True)
        if os.path.exists(pf):
            bak = "%s.bak-%s" % (pf, time.strftime("%Y%m%d_%H%M%S"))
            with open(pf, "rb") as src, open(bak, "wb") as dst:
                dst.write(src.read())
            _prune_backups(pf, cfg.aggregation["legs"]["backup_keep"])
        tmp = pf + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, pf)
        return True, "已写 %s" % pf
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(pf + ".tmp"):
                os.remove(pf + ".tmp")
        except Exception:
            pass
        return False, "写失败：%s" % e


def _prune_backups(pf: str, keep: int):
    """只保留最近 keep 份 .bak-*（keep<=0 表示不清理）。"""
    if keep <= 0:
        return
    d, base = os.path.dirname(pf) or ".", os.path.basename(pf)
    try:
        baks = sorted(x for x in os.listdir(d) if x.startswith(base + ".bak-"))
    except Exception:
        return
    for x in baks[:-keep]:
        try:
            os.remove(os.path.join(d, x))
        except Exception:
            pass


def read_full(cfg) -> tuple[str, str]:
    """读母本全量定义。返回 (文本, 播种告警)。

    母本缺失时从 provider_file 播种：若 provider_file 已经是「剔除后」的结果，
    被摘的腿就永久丢了 —— 因此这里必须大声告警，并把来源记进状态文件。
    """
    lg = cfg.aggregation["legs"]
    full, pf = lg["full_file"], lg["provider_file"]
    if full and os.path.exists(full):
        try:
            txt = read_text(full)
        except Exception as e:  # noqa: BLE001
            raise OSError("读不了 %s：%s" % (full, e))
        if txt.strip():
            return txt, ""
    if not pf or not os.path.exists(pf):
        raise OSError("读不到腿列表：full_file=%s / provider_file=%s 都不存在"
                      % (full or "(空)", pf or "(空)"))
    try:
        txt = read_text(pf)
    except Exception as e:  # noqa: BLE001
        raise OSError("读不了 %s：%s" % (pf, e))
    if not txt.strip():
        raise OSError("%s 是空文件，不能作为腿列表" % pf)
    ok = False
    if full:
        try:
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(txt)
                f.flush()
                os.fsync(f.fileno())
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
    warn = ("母本 %s 缺失，已从 provider_file 播种（%s）——"
            "若 provider_file 已是剔除后的结果，被摘的腿不在母本里，请人工核对"
            % (full, "成功" if ok else "失败"))
    print("!! " + warn, file=sys.stderr)
    return txt, warn


# --------------------------------------------------------------------------
# 热更新
# --------------------------------------------------------------------------
def provider_put(cfg, text: str, dry: bool = False) -> tuple[bool | None, str]:
    """把腿列表热更新到聚合出口（mihomo：PUT /providers/proxies/<名>）。

    返回 (状态, 说明)：True 成功 / False 失败 / None 跳过（未配置或 dry-run）。
    跳过不算失败 —— 只维护文件、靠重启或下次刷新生效也是合法用法。
    """
    lg = cfg.aggregation["legs"]
    api, name = lg["api"], lg["provider_name"]
    if dry:
        return None, "（dry-run 未热更新）"
    if not api or not name:
        return None, "未配置 aggregation.legs.api / provider_name —— 跳过热更新（文件已更新）"
    url = "%s/providers/proxies/%s" % (api.rstrip("/"), name)
    try:
        req = urllib.request.Request(url, data=text.encode("utf-8"), method="PUT",
                                     headers={"Content-Type": "text/plain"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as r:
            code = getattr(r, "status", 0) or r.getcode()
        return 200 <= int(code) < 300, "HTTP %s" % code
    except Exception as e:  # noqa: BLE001
        return False, "%s（%s）" % (e, url)


# --------------------------------------------------------------------------
# 核心动作
# --------------------------------------------------------------------------
def _need_legs(cfg) -> str:
    lg = cfg.aggregation["legs"]
    if not lg["provider_file"]:
        raise ConfigError("未配置 aggregation.legs.provider_file（聚合出口实际读取的节点列表文件）")
    return lg["provider_file"]


def apply_exclusions(cfg, excluded, dry=False, reload=True, quiet=False,
                     last_action="") -> int:
    """按剔除集合重渲染 provider_file 并热更新（成功后自动记状态）。返回退出码。"""
    _need_legs(cfg)
    full_text, warn = read_full(cfg)
    try:
        blocks = parse_legs(full_text)
    except ValueError as e:
        print("!! %s" % e, file=sys.stderr)
        return RC_LEGS
    names = [b["name"] for b in blocks]
    if not names:
        print("!! 母本里没有解析出任何腿（检查 %s 的格式）"
              % cfg.aggregation["legs"]["full_file"], file=sys.stderr)
        return RC_LEGS
    unknown = sorted(set(excluded) - set(names))
    if unknown:
        print("!! 母本里没有这些腿：%s\n   现有腿：%s"
              % (", ".join(unknown), ", ".join(names)), file=sys.stderr)
        return RC_LEGS
    keep_min = cfg.aggregation["legs"]["keep_min"]
    if keep_min and len(names) - len(set(excluded)) < keep_min:
        print("!! keep_min 保护：现有 %d 条腿、剔除 %d 条，将少于下限 %d 条，已放弃"
              % (len(names), len(set(excluded)), keep_min), file=sys.stderr)
        return RC_KEEP
    text = render_legs(full_text, excluded)
    kept = [n for n in names if n not in set(excluded)]
    if not quiet:
        print("腿总数 %d → 生效 %d%s" % (len(names), len(kept),
                                        ("（剔除：%s）" % ", ".join(sorted(excluded)))
                                        if excluded else "（全部生效）"))
    ok, msg = write_provider(cfg, text, dry)
    print("  写 provider：%s" % msg)
    if not ok:
        return RC_LEGS
    if not dry:
        # 文件已改成功 → 立刻记状态（即使随后热更新失败，状态也与文件一致）
        st = load_state(cfg)
        st["excluded"] = sorted(set(excluded))
        if last_action:
            st["last_action"] = last_action
        if warn:
            st["seeded_from_warning"] = warn
        save_state(cfg, st)
    if reload:
        ok2, msg2 = provider_put(cfg, text, dry)
        print("  热更新：%s" % msg2)
        if ok2 is False:
            print("  ↳ 文件已写好，但热更新没成功：重启聚合出口（或下次刷新 provider）后即生效",
                  file=sys.stderr)
            return RC_RELOAD
    return RC_OK


def cmd_list(cfg, dry=False) -> int:
    _need_legs(cfg)
    full_text, _ = read_full(cfg)
    blocks = parse_legs(full_text)
    st = load_state(cfg)
    ex = set(st.get("excluded") or [])
    pf = cfg.aggregation["legs"]["provider_file"]
    try:
        cur = set(b["name"] for b in parse_legs(read_text(pf)))
    except Exception:
        cur = None
    print("== 腿列表 ==")
    print("  母本 : %s" % cfg.aggregation["legs"]["full_file"])
    print("  生效 : %s" % pf)
    print("  状态 : %s" % state_path(cfg))
    for b in blocks:
        flags = []
        if b["name"] in ex:
            flags.append("已摘除")
        if cur is not None and (b["name"] in cur) != (b["name"] not in ex):
            flags.append("⚠与生效文件不一致")
        print("  %-24s %-18s %s" % (b["name"],
                                    ("%s:%s" % (b["server"], b["port"])) if b["server"]
                                    else "", " ".join(flags)))
    if cur is None:
        print("  （读不到生效文件，跳过漂移检测）")
    return RC_OK


def cmd_off(cfg, leg, reason="", dry=False, reload=True) -> int:
    ex = set(load_state(cfg).get("excluded") or [])
    if leg in ex:
        print("腿 %s 已在剔除集合里（幂等：不重复写）" % leg)
        return RC_OK
    ex.add(leg)
    return apply_exclusions(cfg, ex, dry=dry, reload=reload,
                            last_action="off %s%s" % (leg, ("（%s）" % reason) if reason else ""))


def cmd_on(cfg, leg, dry=False, reload=True) -> int:
    ex = set(load_state(cfg).get("excluded") or [])
    if leg not in ex:
        print("腿 %s 不在剔除集合里（幂等：不重复写）" % leg)
        return RC_OK
    ex.discard(leg)
    return apply_exclusions(cfg, ex, dry=dry, reload=reload, last_action="on %s" % leg)


def cmd_sync(cfg, from_breaker=False, dry=False, reload=True) -> int:
    st = load_state(cfg)
    if from_breaker:
        bp = os.path.join(cfg.data_dir, "breaker_state.json")
        try:
            with open(bp, "r", encoding="utf-8-sig") as f:
                tripped = set((json.load(f) or {}).get("tripped") or [])
        except Exception as e:  # noqa: BLE001
            print("!! 读不到控制台熔断态 %s：%s" % (bp, e), file=sys.stderr)
            return RC_CFG
        by_leg = {}
        for b in cfg.buckets:
            by_leg[b["id"]] = b.get("speed_leg") or b["id"]
        ex = set(by_leg.get(x, x) for x in tripped)
        print("从控制台熔断态同步：tripped=%s → 剔除 %s" % (sorted(tripped), sorted(ex)))
    else:
        ex = set(st.get("excluded") or [])
    return apply_exclusions(cfg, ex, dry=dry, reload=reload,
                            last_action="sync%s" % (" --from-breaker" if from_breaker else ""))


def cmd_status(cfg) -> int:
    lg = cfg.aggregation["legs"]
    print("== leg_ctl 配置 ==")
    print("  provider_file : %s%s" % (lg["provider_file"],
                                      "" if os.path.exists(lg["provider_file"]) else "  (不存在)"))
    print("  full_file     : %s%s" % (lg["full_file"],
                                      "" if os.path.exists(lg["full_file"]) else "  (不存在，首次运行会播种)"))
    print("  provider_name : %s" % (lg["provider_name"] or "（未配置 → 跳过热更新）"))
    print("  api           : %s" % (lg["api"] or "（未配置 → 跳过热更新）"))
    print("  state_file    : %s" % lg["state_file"])
    print("  keep_min      : %s   backup_keep: %s" % (lg["keep_min"], lg["backup_keep"]))
    st = load_state(cfg)
    print("  剔除集合      : %s" % (st.get("excluded") or []))
    if st.get("seeded_from_warning"):
        print("  ⚠ 播种告警    : %s" % st["seeded_from_warning"])
    if st.get("last_action"):
        print("  上次动作      : %s @ %s" % (st["last_action"], st.get("time") or ""))
    return RC_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="聚合腿列表维护（分流熔断的摘腿/放腿实现）")
    ap.add_argument("--config", "-c", default=None, help="配置文件路径（默认自动查找）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发生的变化，不落盘")
    ap.add_argument("--no-reload", action="store_true", help="只改文件，不调热更新接口")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果（供脚本调用）")
    # 子命令也接受 --dry-run / --no-reload（写在子命令后面同样生效）；
    # SUPPRESS 保证「子命令没写」时不会把顶层已解析的值覆盖成 False。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="只打印将要发生的变化，不落盘")
    common.add_argument("--no-reload", action="store_true", default=argparse.SUPPRESS,
                        help="只改文件，不调热更新接口")
    sub = ap.add_subparsers(dest="cmd")

    p_off = sub.add_parser("off", parents=[common], help="摘腿")
    p_off.add_argument("--leg", required=True, help="腿名（与控制台 {leg} 一致）")
    p_off.add_argument("--reason", default="", help="写进状态的原因，如 loss=80")
    p_on = sub.add_parser("on", parents=[common], help="放腿")
    p_on.add_argument("--leg", required=True)
    p_sync = sub.add_parser("sync", parents=[common], help="按剔除集合重建 provider_file")
    p_sync.add_argument("--from-breaker", action="store_true",
                        help="用控制台 breaker_state.json 的 tripped 当剔除集合")
    sub.add_parser("list", parents=[common], help="列出腿与状态")
    sub.add_parser("status", parents=[common], help="打印配置摘要")

    args = ap.parse_args(argv)
    try:
        cfg = load_config(default_config_path(args.config))
    except ConfigError as e:
        print("配置错误：%s" % e, file=sys.stderr)
        return RC_CFG

    try:
        if args.cmd == "off":
            rc = cmd_off(cfg, args.leg, args.reason, args.dry_run, not args.no_reload)
        elif args.cmd == "on":
            rc = cmd_on(cfg, args.leg, args.dry_run, not args.no_reload)
        elif args.cmd == "sync":
            rc = cmd_sync(cfg, args.from_breaker, args.dry_run, not args.no_reload)
        elif args.cmd == "list":
            rc = cmd_list(cfg, args.dry_run)
        elif args.cmd == "status":
            rc = cmd_status(cfg)
        else:
            ap.print_help()
            return RC_CFG
    except (ConfigError, OSError, ValueError) as e:
        print("!! %s" % e, file=sys.stderr)
        return RC_CFG if isinstance(e, ConfigError) else RC_LEGS

    if args.json:
        st = load_state(cfg)
        print(json.dumps({"rc": rc, "excluded": st.get("excluded") or [],
                          "state_file": state_path(cfg),
                          "provider_file": cfg.aggregation["legs"]["provider_file"],
                          "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                         ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
