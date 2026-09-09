# -*- coding: utf-8 -*-
"""
probe_buckets.py — 多桶聚合控制台 · 参考「状态生产端」
================================================================================
控制台只做「看 + 点」，状态数据由生产端写入 JSON。本脚本就是一份**可直接用**的
参考实现：按配置逐个探测每条桶出口，把结果写进状态文件，供控制台读取。

判定语义（与控制台状态词一一对应）：
  ok      3 次探活里 ≥2 次拿到 204 → 该出口真能上网
  portal  未拿到 204 且响应体命中门户标记 → 流量被劫持到认证页（需先认证/补绑定）
  loss    既不是 204 也不是门户页 → 丢包 / 出口不通
  （探测未做、跳过 → 保留上一次状态，不覆盖）

用法：
  python probe_buckets.py --config config.private.json
  python probe_buckets.py --config config.private.json --only A        # 只探一个桶
  python probe_buckets.py --config config.private.json --json          # 输出机器可读
  python probe_buckets.py --config config.private.json --list          # 列出桶 id
  python probe_buckets.py --config config.private.json --print-only    # 只打印不写文件

接进控制台：把 `actions.refresh_state` 配成本脚本即可（控制台「重新巡检」按钮会调它），
单桶探测则由控制台直接复用本模块的 probe_bucket()。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from console_config import ConfigError, load_config, default_config_path, is_unset  # noqa: E402

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DEVNULL = os.devnull


def run(argv, timeout=20, shell=False):
    """执行命令并返回 (rc, stdout)。rc=None 表示超时/异常。"""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, shell=shell,
                           creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return None, "ERR: 超时"
    except Exception as e:  # noqa: BLE001
        return None, "ERR: %s" % e


def ssh_argv(ssh_cfg, remote_cmd):
    """组装 ssh 命令（BatchMode，不交互）。"""
    args = ["ssh", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=%d" % int(ssh_cfg.get("timeout") or 10)]
    if ssh_cfg.get("key"):
        args += ["-i", ssh_cfg["key"]]
    if ssh_cfg.get("port") and int(ssh_cfg["port"]) != 22:
        args += ["-p", str(ssh_cfg["port"])]
    for opt in ssh_cfg.get("opts") or []:
        args += ["-o", str(opt)]
    args.append("%s@%s" % (ssh_cfg.get("user") or "root", ssh_cfg.get("host") or ""))
    args.append(remote_cmd)
    return args


def _curl_base(cfg, timeout):
    curl = cfg.probe.get("curl") or "curl"
    return [curl, "-s", "--max-time", str(int(timeout))]


def _ping_host(cfg, bucket):
    """ICMP 目标：probe.ping_host > 探活地址的主机名。"""
    tgt = bucket.get("target") or cfg.probe.get("target") or ""
    if is_unset(tgt):
        return ""
    host = urllib.parse.urlparse(str(tgt)).hostname or ""
    return cfg.probe.get("ping_host") or host


def icmp_loss_pct(cfg, bucket, count=None):
    """量该桶出口的 ICMP 丢包率。返回 (loss_pct | None, 说明)。

    · local / ssh 出口可以直接 ping；SOCKS 出口无法 ICMP（返回 None）；
    · 解析中英文 ping 汇总行的「X%」（Windows 中文=「丢失 = 2 (40% 丢失)」）。"""
    pr = bucket.get("probe") or {}
    mode = pr.get("mode") or "local"
    if mode in ("socks", "none"):
        return None, "该出口不支持 ICMP（probe.mode=%s）" % mode
    if not cfg.probe.get("icmp", True):
        return None, "已在配置里关闭 ICMP 探测（probe.icmp=false）"
    host = _ping_host(cfg, bucket)
    if not host:
        return None, "未配置探活地址，取不到 ping 目标"
    count = int(count or cfg.probe.get("icmp_count") or 5)
    to = count * 3 + 12
    if mode == "ssh":
        ssh_cfg = pr.get("ssh") or {}
        if is_unset(ssh_cfg.get("host")):
            return None, "ssh 探测缺少 probe.ssh.host"
        iface = pr.get("iface") or ""
        cmd = "ping -c %d -W 2 %s%s" % (count, ("-I %s " % iface) if iface else "", host)
        rc, out = run(ssh_argv(ssh_cfg, cmd), timeout=to)
    else:
        if os.name == "nt":
            args = ["ping", "-n", str(count), "-w", "2000", host]
        else:
            args = ["ping", "-c", str(count), "-W", "2", host]
        rc, out = run(args, timeout=to)
    if rc is None:
        return None, out
    m = re.search(r"(\d+)\s*%", out or "")
    if not m:
        tail = (out or "").strip().splitlines()[-1:] or [""]
        return None, "未识别 ping 汇总行：%s" % tail[0][:80]
    last = (out or "").strip().splitlines()[-1:] or [""]
    return int(m.group(1)), last[0][:120]


def probe_bucket(cfg, bucket, tries=None, timeout=None, target=None):
    """探测单个桶出口。返回 dict(codes, state, portal, body, detail, http_loss, icmp_loss)。"""
    tgt = target or bucket.get("target") or cfg.probe.get("target")
    if is_unset(tgt):
        return {"codes": [], "state": "n/a", "portal": False, "body": "",
                "detail": "未配置探活地址（probe.target）", "http_loss": None, "icmp_loss": None}
    pr = bucket.get("probe") or {}
    tries = int(tries or pr.get("tries") or 3)
    timeout = int(timeout or pr.get("timeout") or 6)
    mode = pr.get("mode") or "local"

    codes, body, detail = [], "", ""

    if mode == "none":
        return {"codes": [], "state": "n/a", "portal": False, "body": "",
                "detail": "该桶未配置探测方式（probe.mode=none）",
                "http_loss": None, "icmp_loss": None}

    if mode == "ssh":
        ssh_cfg = pr.get("ssh") or {}
        if is_unset(ssh_cfg.get("host")):
            return {"codes": [], "state": "n/a", "portal": False, "body": "",
                    "detail": "ssh 探测缺少 probe.ssh.host",
                    "http_loss": None, "icmp_loss": None}
        iface = pr.get("iface") or ""
        iface_arg = ("--interface %s " % iface) if iface else ""
        for _ in range(tries):
            rc, out = run(ssh_argv(ssh_cfg,
                                   "curl -s -m %d -o /dev/null -w '%%{http_code}' %s%s"
                                   % (timeout, iface_arg, tgt)),
                          timeout=timeout + 12)
            codes.append(out if (out and out.isdigit()) else "000")
        if any(c != "204" for c in codes):
            rc, body = run(ssh_argv(ssh_cfg, "curl -s -m %d %s%s" % (timeout, iface_arg, tgt)),
                           timeout=timeout + 12)
            body = body if rc == 0 else ""
        if iface:
            rc, ip = run(ssh_argv(ssh_cfg,
                                  "ip -4 addr show %s 2>/dev/null | grep -o 'inet [0-9.]*' | head -1"
                                  % iface), timeout=timeout + 12)
            detail = "远端 %s %s" % (iface, (ip or "(无IP)") if rc == 0 else "(查询失败)")
    else:
        base = _curl_base(cfg, timeout)
        socks = pr.get("socks") if mode == "socks" else None
        if mode == "socks" and is_unset(socks):
            return {"codes": [], "state": "n/a", "portal": False, "body": "",
                    "detail": "socks 探测缺少 probe.socks（不猜本机直连，避免误判为在线）",
                    "http_loss": None, "icmp_loss": None}
        for _ in range(tries):
            args = base + ["-o", DEVNULL, "-w", "%{http_code}"]
            if socks and not is_unset(socks):
                args += ["-x", socks]
            args.append(tgt)
            rc, out = run(args, timeout=timeout + 6)
            codes.append(out if (out and out.isdigit()) else "000")
        if any(c != "204" for c in codes):
            args = base
            if socks and not is_unset(socks):
                args += ["-x", socks]
            args.append(tgt)
            rc, body = run(args, timeout=timeout + 6)
            body = body if rc == 0 else ""

    markers = cfg.probe.get("portal_markers") or []
    portal = bool(body) and any(m in body for m in markers)
    ok = codes.count("204")
    if ok >= max(1, tries - 1):
        state = "ok"
    elif portal:
        state = "portal"
    else:
        state = "loss"
    # 丢包率：HTTP 层 = 没拿到 204 的比例；ICMP 层 = ping 汇总行里的百分比
    http_loss = int(round(100.0 * (tries - ok) / tries)) if tries else None
    icmp_loss, icmp_note = (None, "")
    if state != "n/a":
        icmp_loss, icmp_note = icmp_loss_pct(cfg, bucket)
    return {"codes": codes, "state": state, "portal": portal,
            "body": body[:400], "detail": detail,
            "http_loss": http_loss, "icmp_loss": icmp_loss, "icmp_note": icmp_note}


def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_state(cfg, updates, keep=True):
    """原子写入状态文件（默认与旧状态合并，避免单桶探测抹掉其它桶）。"""
    os.makedirs(os.path.dirname(cfg.state_file) or ".", exist_ok=True)
    st = _load_state(cfg.state_file) if keep else {}
    st.update(updates)
    st["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = cfg.state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg.state_file)
    return st


def probe_all(cfg, only=None):
    only = set(only or [])
    results, updates = {}, {}
    for b in cfg.buckets:
        if only and b["id"] not in only:
            continue
        r = probe_bucket(cfg, b)
        results[b["id"]] = r
        if r["state"] != "n/a":
            updates[b["state_key"]] = r["state"]
            if r.get("http_loss") is not None:
                updates["loss_" + b["state_key"]] = r["http_loss"]
            if r.get("icmp_loss") is not None:
                updates["icmp_" + b["state_key"]] = r["icmp_loss"]
    return results, updates


def main(argv=None):
    ap = argparse.ArgumentParser(description="多桶聚合控制台 · 参考状态生产端")
    ap.add_argument("--config", "-c", default=None, help="配置文件路径（默认自动查找）")
    ap.add_argument("--only", action="append", default=[], help="只探测指定桶 id（可重复）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--print-only", action="store_true", help="只打印，不写状态文件")
    ap.add_argument("--list", action="store_true", help="列出配置中的桶")
    ap.add_argument("--check", action="store_true", help="只做配置自检")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(default_config_path(args.config))
    except ConfigError as e:
        print("配置错误: %s" % e, file=sys.stderr)
        return 2

    if args.check:
        from console_config import config_summary
        print(config_summary(cfg))
        return 0

    if args.list:
        for b in cfg.buckets:
            print("%-10s %-6s %-6s %s" % (b["id"], b["kind"], b["probe"]["mode"],
                                          b["label"]))
        return 0

    only = []
    for chunk in args.only:
        only += [x.strip() for x in chunk.split(",") if x.strip()]
    unknown = [x for x in only if x not in cfg.bucket_by_id]
    if unknown:
        print("未知桶 id: %s" % ", ".join(unknown), file=sys.stderr)
        return 2

    results, updates = probe_all(cfg, only)

    if not args.print_only and updates:
        write_state(cfg, updates)

    if args.json:
        print(json.dumps({"results": results, "written": updates,
                          "state_file": cfg.state_file,
                          "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                         ensure_ascii=False, indent=2))
        return 0

    print("== 桶探测 @ %s ==" % time.strftime("%Y-%m-%d %H:%M:%S"))
    for bid, r in results.items():
        b = cfg.bucket_by_id[bid]
        line = "  %-10s %-8s %s" % (bid, r["state"], b["label"])
        if r["codes"]:
            line += "  探活=%s" % ",".join(r["codes"])
        if r.get("detail"):
            line += "  %s" % r["detail"]
        print(line)
        if r["portal"]:
            print("      ↳ 流量被门户劫持：需先完成认证 / 补齐免认证绑定")
    if not args.print_only:
        print("  已写入: %s" % cfg.state_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
