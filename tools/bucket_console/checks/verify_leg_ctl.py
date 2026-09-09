# -*- coding: utf-8 -*-
"""回归校验：leg_ctl.py（分流熔断的「摘腿 / 放腿」参考实现）。

覆盖：
  · 文本级解析：块状节点定义、引号名、注释与缩进原样保留、流式写法明确拒绝
  · off / on：生效文件按剔除集合重渲染，母本永不被改写
  · 幂等：重复摘/放同一条腿不重复写、不报错
  · keep_min 保护 / 未知腿 / dry-run 不落盘
  · 热更新：PUT <api>/providers/proxies/<名> 的 URL 与请求体（本地假控制面）
  · 热更新失败：文件照样写好、状态照样记录、退出码 5
  · 母本缺失播种 + 告警落进状态；漂移检测与 sync 修复；sync --from-breaker

用法: python checks/verify_leg_ctl.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import leg_ctl as lc  # noqa: E402
import console_config as cc  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print("   [%s] %s %s" % ("OK" if cond else "FAIL", name, detail))
    if not cond:
        fails.append(name)


# ---------------- 假控制面：记录 PUT ----------------
PUTS = []


class _H(BaseHTTPRequestHandler):
    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        PUTS.append({"path": self.path, "body": self.rfile.read(n).decode("utf-8")})
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_a):
        pass


srv = HTTPServer(("127.0.0.1", 0), _H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
API = "http://127.0.0.1:%d" % srv.server_address[1]

FULL_YAML = """# 腿列表（母本）
proxies:
  - name: "A-direct"
    type: socks5
    server: 10.0.0.1
    port: 1080
  - name: B-socks
    type: socks5
    server: 10.0.0.2
    port: 1081
  - name: C-eth3
    type: socks5
    server: 10.0.0.3
    port: 1082
  - name: D-eth4
    type: socks5
    server: 10.0.0.4
    port: 1083
"""


def build_env(tmp, keep_min=1, api=API):
    """造一份最小配置 + 母本 + 生效文件，返回 (cfg 路径, provider 路径, full 路径)。"""
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8-sig") as f:
        raw = json.load(f)
    pf = os.path.join(tmp, "legs.yaml")
    full = os.path.join(tmp, "legs.full.yaml")
    with open(pf, "w", encoding="utf-8", newline="\n") as f:
        f.write(FULL_YAML)
    raw["app"]["data_dir"] = os.path.join(tmp, "data")
    raw["aggregation"]["legs"] = {
        "provider_file": pf, "full_file": full, "provider_name": "legs",
        "api": api, "keep_min": keep_min,
    }
    cfgp = os.path.join(tmp, "cfg.json")
    with open(cfgp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    return cfgp, pf, full


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def run(cfgp, *args):
    return lc.main(["--config", cfgp] + list(args))


try:
    print("== 解析与渲染 ==")
    blocks = lc.parse_legs(FULL_YAML)
    check("解析出 4 条腿", [b["name"] for b in blocks] ==
          ["A-direct", "B-socks", "C-eth3", "D-eth4"], [b["name"] for b in blocks])
    check("引号名被去引号", blocks[0]["name"] == "A-direct", blocks[0]["name"])
    check("server/port 解析正确",
          (blocks[2]["server"], blocks[2]["port"]) == ("10.0.0.3", 1082),
          (blocks[2]["server"], blocks[2]["port"]))
    r = lc.render_legs(FULL_YAML, ["B-socks"])
    check("渲染剔除 B 且保留其余 3 条",
          "B-socks" not in r and all(x in r for x in ("A-direct", "C-eth3", "D-eth4")))
    check("渲染保留前缀注释与缩进",
          r.startswith("# 腿列表（母本）\nproxies:\n  - name:"), repr(r[:40]))
    check("渲染后顺序与母本一致",
          [b["name"] for b in lc.parse_legs(r)] == ["A-direct", "C-eth3", "D-eth4"])
    check("全部剔除时不留空行尾巴", lc.render_legs(FULL_YAML, ["A-direct", "B-socks",
                                                             "C-eth3", "D-eth4"]).strip()
          .endswith("proxies:"))
    try:
        lc.parse_legs("proxies:\n  - {name: X, type: socks5}\n")
        check("流式写法明确拒绝", False)
    except ValueError as e:
        check("流式写法明确拒绝", "流式" in str(e), str(e))

    tmp = tempfile.mkdtemp(prefix="bc_legs_")
    cfgp, pf, full = build_env(tmp)
    cfg = cc.load_config(cfgp)
    lg = cfg.aggregation["legs"]
    check("配置归一：full_file 自动派生", lg["full_file"] == full, lg["full_file"])
    check("配置归一：api 取自 legs.api", lg["api"] == API, lg["api"])

    print("\n== off / on ==")
    PUTS.clear()
    rc = run(cfgp, "off", "--leg", "B-socks", "--reason", "loss=80")
    check("off 退出码 0", rc == 0, rc)
    body = read(pf)
    check("生效文件已剔除该腿", "B-socks" not in body and "C-eth3" in body)
    check("母本未被改写（仍是 4 条）", len(lc.parse_legs(read(full))) == 4)
    st = json.load(open(os.path.join(tmp, "data", "legs_excluded.json"), encoding="utf-8"))
    check("剔除集合已落盘", st["excluded"] == ["B-socks"], st["excluded"])
    check("记录了动作与原因", "off B-socks" in st.get("last_action", "") and "loss=80"
          in st.get("last_action", ""), st.get("last_action"))
    check("热更新 PUT 到 provider 路径", PUTS and PUTS[-1]["path"] == "/providers/proxies/legs",
          PUTS[-1]["path"] if PUTS else None)
    check("热更新请求体 = 渲染结果", PUTS and PUTS[-1]["body"] == body)
    check("写了备份文件",
          any(x.startswith("legs.yaml.bak-") for x in os.listdir(tmp)), os.listdir(tmp))
    check("无 .tmp 残留", not os.path.exists(pf + ".tmp"))

    n_puts = len(PUTS)
    rc = run(cfgp, "off", "--leg", "B-socks")
    check("重复摘同一条腿幂等（不再写/不再热更新）",
          rc == 0 and len(PUTS) == n_puts, (rc, len(PUTS)))

    rc = run(cfgp, "on", "--leg", "B-socks")
    check("on 退出码 0", rc == 0, rc)
    check("放回后 4 条腿、顺序不变",
          [b["name"] for b in lc.parse_legs(read(pf))] ==
          ["A-direct", "B-socks", "C-eth3", "D-eth4"])
    check("放回后剔除集合为空",
          json.load(open(os.path.join(tmp, "data", "legs_excluded.json"),
                         encoding="utf-8"))["excluded"] == [])

    print("\n== 保护与边界 ==")
    tmp2 = tempfile.mkdtemp(prefix="bc_legs2_")
    cfgp2, pf2, full2 = build_env(tmp2, keep_min=3)
    rc = run(cfgp2, "off", "--leg", "A-direct")
    check("keep_min=3 时摘第 1 条允许", rc == 0, rc)
    rc = run(cfgp2, "off", "--leg", "B-socks")
    check("keep_min=3 时摘到剩 2 条被拒（退出码 4）", rc == 4, rc)
    check("被拒后生效文件仍是 3 条", len(lc.parse_legs(read(pf2))) == 3)
    rc = run(cfgp2, "off", "--leg", "NoSuchLeg")
    check("未知腿退出码 3", rc == 3, rc)
    before = read(pf2)
    rc = lc.main(["--config", cfgp2, "on", "--leg", "A-direct", "--dry-run"])
    check("dry-run 写在子命令后同样生效", rc == 0, rc)
    rc = lc.main(["--config", cfgp2, "--dry-run", "on", "--leg", "A-direct"])
    check("dry-run 退出码 0", rc == 0, rc)
    check("dry-run 不改文件", read(pf2) == before)
    check("dry-run 不改状态",
          json.load(open(os.path.join(tmp2, "data", "legs_excluded.json"),
                         encoding="utf-8"))["excluded"] == ["A-direct"])
    shutil.rmtree(tmp2, ignore_errors=True)

    print("\n== 播种 / 漂移 / 熔断同步 ==")
    os.remove(full)
    PUTS.clear()
    rc = run(cfgp, "off", "--leg", "D-eth4")
    check("母本缺失时自动播种并继续", rc == 0 and os.path.exists(full), rc)
    st = json.load(open(os.path.join(tmp, "data", "legs_excluded.json"), encoding="utf-8"))
    check("播种告警落进状态", "播种" in st.get("seeded_from_warning", ""),
          st.get("seeded_from_warning"))

    # 漂移：手工把 provider 文件改回全量（与剔除集合不符）
    with open(pf, "w", encoding="utf-8", newline="\n") as f:
        f.write(read(full))
    rc = run(cfgp, "sync")
    check("sync 修漂移（按剔除集合重建）",
          rc == 0 and "D-eth4" not in read(pf) and len(lc.parse_legs(read(pf))) == 3, rc)

    bp = os.path.join(tmp, "data", "breaker_state.json")
    with open(bp, "w", encoding="utf-8") as f:
        json.dump({"tripped": ["B", "C"]}, f)
    rc = run(cfgp, "sync", "--from-breaker")
    names = [b["name"] for b in lc.parse_legs(read(pf))]
    check("sync --from-breaker 按桶的 speed_leg 同步",
          rc == 0 and names == ["A-direct", "D-eth4"], (rc, names))
    check("熔断同步也写进了剔除集合",
          json.load(open(os.path.join(tmp, "data", "legs_excluded.json"),
                         encoding="utf-8"))["excluded"] == ["B-socks", "C-eth3"])

    print("\n== 热更新失败不丢改动 ==")
    tmp3 = tempfile.mkdtemp(prefix="bc_legs3_")
    cfgp3, pf3, full3 = build_env(tmp3, api="http://127.0.0.1:1")
    rc = run(cfgp3, "off", "--leg", "C-eth3", "--no-reload")
    check("--no-reload 只改文件（退出码 0）", rc == 0, rc)
    rc = run(cfgp3, "off", "--leg", "B-socks")
    check("控制面不可达 → 退出码 5", rc == 5, rc)
    check("文件仍然写好（下次刷新即生效）", "B-socks" not in read(pf3))
    check("状态仍然记录",
          json.load(open(os.path.join(tmp3, "data", "legs_excluded.json"),
                         encoding="utf-8"))["excluded"] == ["B-socks", "C-eth3"])
    shutil.rmtree(tmp3, ignore_errors=True)

    print("\n== CLI 冒烟 ==")
    import subprocess
    p = subprocess.run([sys.executable, os.path.join(ROOT, "leg_ctl.py"),
                        "--config", cfgp, "list"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    check("list 子命令退出码 0 且列出腿",
          p.returncode == 0 and "A-direct" in (p.stdout or ""), (p.returncode, p.stdout[-120:]))
    shutil.rmtree(tmp, ignore_errors=True)
finally:
    try:
        srv.shutdown()
    except Exception:
        pass

print("\n=== 结果 ===")
if fails:
    for f in fails:
        print("  [FAIL] %s" % f)
    raise SystemExit(1)
print("  [OK] leg_ctl 摘腿/放腿全部通过")
