# -*- coding: utf-8 -*-
"""回归校验：从内部测试版回灌到开源版的修复 / 通用化能力。

覆盖：
  P1 task_state 3 列解析 + 本地化状态归一
  P2 真机闸新鲜度（fail-closed）
  P3 表格重建保持选中
  P5 按钮忙碌态
  P6 模式文件原子写
  P7 命令占位符只替换已知键（curl -w '%{http_code}' 不再 KeyError）
  P8 配置结构性自检（重复 id / state_key）
  P9 配置编辑器（热更新 / 非法值不写盘 / .bak 备份）
  P10 分流熔断（连续超阈值隔离 / 连续达标恢复 / keep_min 保护 / 状态落盘）
  P11 桶停用-启用（enabled=false 不探测不动作）
  P12 门户劫持不计入丢包（portal 态不报丢包率 → 熔断不会把好腿当坏腿摘掉）
  P13 配置类型写错归一到 ConfigError（不抛裸 AttributeError / ValueError）
  P14 占位符判定（裸 `<name>` 与复合模板算未配置；shell 重定向不算）
  P15 写盘锁（含残留死锁回收）/ 备份名同秒不撞车 / tail_log 只读尾部
  P16 字符串命令的替换值转义（argv 为字符串时走 shell=True）

用法: python checks/verify_ports.py [计划任务名]
      （可选参数用于 P1：指定一个本机已注册的计划任务当夹具；不传则自动枚举取第一个，
        取不到就跳过该项。测试夹具不硬编码任何环境私有任务名。）
"""
import csv
import os
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bucket_console as bc  # noqa: E402
import console_config as cc  # noqa: E402

fails = []
try:
    root = tk.Tk()
    root.withdraw()
except Exception as _tkerr:  # noqa: BLE001
    # 无显示环境（CI / 纯终端）下 tk.Tk() 会直接抛，原来在 import 时就构造，
    # 整个脚本连非界面用例都跑不了。这里降级为跳过界面用例。
    root = None
    print("   [SKIP] 无法创建 Tk 根窗口（%s），跳过依赖界面的用例" % _tkerr)


def check(name, cond, detail=""):
    print("   [%s] %s %s" % ("OK" if cond else "FAIL", name, detail))
    if not cond:
        fails.append(name)


try:
    print("== P1 task_state ==")
    # 任务名属环境私有（见 config.private.json 的 tasks[]），这里**不硬编码任何具体任务名**：
    # 可用 argv[1] 显式指定一个本机已注册任务，否则枚举任务库取第一个当夹具；
    # 两者都拿不到（无任务 / 平台不支持 / 枚举超时）则跳过该项，不算失败。
    real = sys.argv[1] if len(sys.argv) > 1 else ""
    if not real:
        try:
            probe = subprocess.run(["schtasks.exe", "/Query", "/FO", "CSV", "/NH"],
                                   capture_output=True, timeout=20)
            for row in csv.reader(bc._decode_console(probe.stdout or b"").splitlines()):
                if len(row) >= 3 and row[0].strip().strip("\\"):
                    real = row[0].strip().strip("\\").split("\\")[-1]
                    break
        except Exception:  # noqa: BLE001
            real = ""
    if real:
        st = bc.task_state(real)
        check("已注册任务返回规范英文状态", st in ("Ready", "Running", "Disabled"), "-> %r" % st)
    else:
        print("   [SKIP] 未取得已注册任务名（可传参指定：verify_ports.py <任务名>），跳过该项")
    check("不存在任务返回「不存在」", bc.task_state("NoSuchTaskXYZ") == "不存在")
    check("本地化映射表存在", "准备就绪" in bc._TASK_STATE_MAP)
    check("_decode_console 可解 GBK", bc._decode_console("就绪".encode("cp936")) == "就绪")

    print("\n== P7 占位符只替换已知键 ==")
    cfg = cc.load_config(os.path.join(os.path.dirname(HERE), "config.example.json"))
    out = cfg._subst("curl -s -w '%{http_code}' {target} {unknown_k}",
                     {"target": "http://x/y"})
    check("已知占位符被替换", "http://x/y" in out, out)
    check("未知占位符原样保留", "{unknown_k}" in out and "%{http_code}" in out, out)
    try:
        out2 = cfg.argv_for("speedtest", None)
        check("argv_for 不再抛 KeyError", True)
    except Exception as e:
        check("argv_for 不再抛 KeyError", False, repr(e))

    print("\n== P3 表格选中保持 ==")
    if root is None:
        print("   [SKIP] 需要 Tk")
    else:
        tr = ttk.Treeview(root, columns=("a",), show="headings")
        for i in range(5):
            tr.insert("", "end", iid="r%d" % i, values=(i,))
        tr.selection_set("r2")
        st0 = bc.App._tv_state(tr)
        for row in tr.get_children():
            tr.delete(row)
        for i in range(5):
            tr.insert("", "end", iid="r%d" % i, values=(i,))
        bc.App._tv_restore(tr, st0)
        check("重建后选中保持", tuple(tr.selection()) == ("r2",), tuple(tr.selection()))

    print("\n== P5 按钮忙碌态 ==")
    if root is None:
        print("   [SKIP] 需要 Tk")
    else:
        b = ttk.Button(root, text="立即运行")
        bc.App._btn_busy(None, b, True, "运行中…")
        t1, s1 = b.cget("text"), tuple(b.state())
        bc.App._btn_busy(None, b, True, "运行中…")
        bc.App._btn_busy(None, b, False)
        t2, s2 = b.cget("text"), tuple(b.state())
        check("置忙：禁用 + 改文字", t1 == "运行中…" and "disabled" in s1, "%r %s" % (t1, s1))
        check("复位：恢复原文字", t2 == "立即运行" and "disabled" not in s2, "%r %s" % (t2, s2))

    print("\n== P6 模式文件原子写 ==")
    tmp = tempfile.mkdtemp(prefix="bc_mode_")
    mf = os.path.join(tmp, "aggregator_mode.txt")
    with open(mf, "w", encoding="ascii") as f:
        f.write("rule\n")
    cc.write_atomic(mf, "direct\n", encoding="ascii")
    check("write_atomic：内容正确且无残留 .tmp",
          open(mf, encoding="ascii").read() == "direct\n"
          and not os.path.exists(mf + ".tmp"), repr(open(mf, encoding="ascii").read()))

    # 真跑一遍 set_proxy_mode（注入假控制面），确认生产路径确实走 write_atomic。
    # 原用例自己重抄了一遍 open→fsync→os.replace，从头到尾没调用过 set_proxy_mode，
    # 生产代码退回「截断再写」它照样绿。
    class _ModeCfg:
        aggregation = {"mode_file": mf, "api": "http://fake.invalid",
                       "restart_action": "restart_aggregator"}

        def has_action(self, _n):
            return True

    with open(mf, "w", encoding="ascii") as f:
        f.write("rule\n")
    _seen = {}
    _orig = (bc.agg_api_json, bc.exec_action, bc.time.sleep, cc.write_atomic)

    def _spy_write(path, text, **kw):
        _seen["atomic"] = True
        return _orig[3](path, text, **kw)

    bc.agg_api_json = lambda _c, _p: {"mode": open(mf, encoding="ascii").read().strip()}
    bc.exec_action = lambda *_a, **_k: (0, "")
    bc.time.sleep = lambda _s: None
    cc.write_atomic = _spy_write
    try:
        _ok, _msg = bc.set_proxy_mode(_ModeCfg(), "direct")
        check("set_proxy_mode 经 write_atomic 落盘并确认切换",
              _ok and _seen.get("atomic")
              and open(mf, encoding="ascii").read() == "direct\n", (_ok, _msg))
    finally:
        bc.agg_api_json, bc.exec_action, bc.time.sleep, cc.write_atomic = _orig
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n== P2 真机闸新鲜度 ==")
    # 行为断言。原实现是源码子串检查（"max_age_min" in src），把逻辑取反也能通过。
    check("读不到时间戳 → 拦截（fail-closed）",
          bc.portal_fresh_enough(None, 3.0) == (False, "读取失败"))
    check("超过阈值 → 拦截", bc.portal_fresh_enough(5.0, 3.0)[0] is False)
    check("正好等于阈值 → 放行（不误伤）", bc.portal_fresh_enough(3.0, 3.0)[0] is True)
    check("阈值内 → 放行", bc.portal_fresh_enough(1.0, 3.0) == (True, ""))

    print("\n== P8 配置结构性自检 ==")
    probs = cfg.problems()
    print("   config.example.json problems = %r" % (probs,))
    check("示例配置无结构性问题", probs == [], repr(probs))
    import copy as _copy
    import json as _json
    bad = _copy.deepcopy(cfg.raw)
    bad["buckets"].append(dict(bad["buckets"][0]))          # 复制一个 -> 重复 id/state_key
    _d = tempfile.mkdtemp(prefix="bc_cfg_")
    tmpc = os.path.join(_d, "cfg.json")
    with open(tmpc, "w", encoding="utf-8") as f:
        _json.dump(bad, f, ensure_ascii=False)
    probs2 = cc.load_config(tmpc).problems()
    print("   人为重复桶 problems = %r" % (probs2[:2],))
    check("能检出重复 id / state_key",
          any("重复 id" in p for p in probs2) and any("重复 state_key" in p for p in probs2))
    import shutil as _sh
    _sh.rmtree(_d, ignore_errors=True)

    print("\n== P4 界面状态持久化 ==")
    check("parse_geometry 正常解析", bc.parse_geometry("1000x860+120+80") == (1000, 860, 120, 80))
    check("parse_geometry 非法返回 None", bc.parse_geometry("garbage") is None)
    _tmpd = tempfile.mkdtemp(prefix="bc_uistate_")

    class _C:
        data_dir = _tmpd
    check("save_ui_state 落盘", bc.save_ui_state(_C, geometry="900x700+10+20", interval_ms=30000))
    st = bc.load_ui_state(_C)
    check("load_ui_state 读回",
          st.get("interval_ms") == 30000 and st.get("geometry") == "900x700+10+20")
    _sh.rmtree(_tmpd, ignore_errors=True)

    print("\n== P9 配置编辑器 ==")
    import json as _json2
    _d9 = tempfile.mkdtemp(prefix="bc_p9_")
    src_cfg = os.path.join(os.path.dirname(HERE), "config.example.json")
    with open(src_cfg, encoding="utf-8-sig") as f:
        raw9 = _json2.load(f)
    raw9.setdefault("app", {})["data_dir"] = _d9
    raw9["ui"]["title"] = "P9 原始标题"
    p9cfg = os.path.join(_d9, "cfg.json")
    with open(p9cfg, "w", encoding="utf-8") as f:
        _json2.dump(raw9, f, ensure_ascii=False)
    cfg9 = cc.load_config(p9cfg)
    app9 = bc.App(root, cfg9, auto_boot=False)
    root.update_idletasks()
    msgs = []
    real_info, real_err = bc.messagebox.showinfo, bc.messagebox.showerror
    bc.messagebox.showinfo = lambda *a, **k: msgs.append(("info", a))
    bc.messagebox.showerror = lambda *a, **k: msgs.append(("err", a))
    try:
        dlg = bc.open_config_dialog(app9)
        check("对话框含各配置页签", len(dlg.vars) >= 15, len(dlg.vars))
        dlg.vars["ui.title"][0].set("P9 改后标题")
        dlg._save()
        root.update_idletasks()
        with open(p9cfg, encoding="utf-8-sig") as f:
            newraw = _json2.load(f)
        check("保存后文件已更新", newraw["ui"]["title"] == "P9 改后标题",
              newraw["ui"]["title"])
        check("即时热更新到内存 cfg", app9.cfg.ui["title"] == "P9 改后标题")
        check("生成了 .bak 备份",
              any(x.startswith("cfg.json.bak-") for x in os.listdir(_d9)), os.listdir(_d9))
        check("弹出成功提示", any(t == "info" for t, _ in msgs), msgs)

        msgs.clear()
        dlg2 = bc.open_config_dialog(app9)
        dlg2.vars["ui.interval_ms"][0].set("abc")
        dlg2._save()
        check("非法 int 被拦下并报错", any(t == "err" for t, _ in msgs), msgs)
        with open(p9cfg, encoding="utf-8-sig") as f:
            after_bad = _json2.load(f)
        check("非法输入不写盘", after_bad["ui"]["interval_ms"] == raw9["ui"]["interval_ms"],
              after_bad["ui"]["interval_ms"])
        dlg2._close()
    finally:
        bc.messagebox.showinfo, bc.messagebox.showerror = real_info, real_err
        try:
            app9._quitting = True
            app9._hb_evt.set()
        except Exception:
            pass
    _sh.rmtree(_d9, ignore_errors=True)
    print("\n== P10 分流熔断 ==")
    brs = {"threshold_pct": 50, "trip_after": 2, "recover_below_pct": 10,
           "recover_after": 3, "keep_min": 1}

    def _br_reset():
        bc._BR["tripped"], bc._BR["fail"], bc._BR["ok"] = set(), {}, {}

    _bs = [{"id": "A"}, {"id": "B"}, {"id": "C"}]
    _br_reset()
    _a1 = bc.breaker_round(brs, _bs, {"A": 80, "B": 0, "C": 0})
    _a2 = bc.breaker_round(brs, _bs, {"A": 80, "B": 0, "C": 0})
    check("第 1 轮超阈值不隔离（避免单轮抖动误摘）", _a1 == [], _a1)
    check("连续 2 轮超阈值 → 隔离 A",
          [(b["id"], iso) for b, iso, _l in _a2] == [("A", True)], _a2)
    _a3 = bc.breaker_round(brs, _bs, {"A": 0, "B": 0, "C": 0})
    _a4 = bc.breaker_round(brs, _bs, {"A": 0, "B": 0, "C": 0})
    _a5 = bc.breaker_round(brs, _bs, {"A": 0, "B": 0, "C": 0})
    check("恢复需连续 3 轮达标（前两轮不动）", _a3 == [] and _a4 == [], (_a3, _a4))
    check("连续 3 轮达标 → 恢复 A",
          [(b["id"], iso) for b, iso, _l in _a5] == [("A", False)], _a5)
    _br_reset()
    bc._BR["tripped"] = {"A", "B"}          # 只剩 C 活跃，keep_min=1 → 不许摘
    _a6 = bc.breaker_round(brs, _bs, {"A": 0, "B": 0, "C": 100})
    _a7 = bc.breaker_round(brs, _bs, {"A": 0, "B": 0, "C": 100})
    check("keep_min 保护：不允许摘到 0 条腿", _a6 == [] and _a7 == [], (_a6, _a7))
    _br_reset()
    bc.breaker_round(brs, _bs, {"A": 90, "B": 0, "C": 0})
    _a8 = bc.breaker_round(brs, _bs, {"A": 0, "B": 0, "C": 0})
    _a9 = bc.breaker_round(brs, _bs, {"A": 90, "B": 0, "C": 0})
    check("抖动回落会清零计数（不累计误隔离）", _a8 == [] and _a9 == [], (_a8, _a9))
    _br_reset()
    _a10 = bc.breaker_round(brs, _bs, {"A": 80, "B": 0, "C": 0}, apply=lambda *a: False)
    _a11 = bc.breaker_round(brs, _bs, {"A": 80, "B": 0, "C": 0}, apply=lambda *a: False)
    check("动作未配置/失败 → 不标记已隔离且计数清零",
          _a10 == [] and _a11 == [] and bc._BR["tripped"] == set()
          and bc._BR["fail"].get("A", 0) == 0,
          (_a10, _a11, bc._BR["tripped"], bc._BR["fail"]))
    # 采样范围：停用桶 / mode=none 不参与
    _db = tempfile.mkdtemp(prefix="bc_p10_")
    with open(os.path.join(os.path.dirname(HERE), "config.example.json"),
              encoding="utf-8-sig") as f:
        _raw = _json2.load(f)
    _raw["app"]["data_dir"] = _db
    _raw["buckets"][1]["enabled"] = False
    _raw["buckets"][2].setdefault("probe", {})["mode"] = "none"
    _p10 = os.path.join(_db, "cfg.json")
    with open(_p10, "w", encoding="utf-8") as f:
        _json2.dump(_raw, f, ensure_ascii=False)
    _c10 = cc.load_config(_p10)
    _sel = [b["id"] for b in bc.breaker_buckets(_c10)]
    check("采样只含启用且非 none 的桶", _sel == [_raw["buckets"][0]["id"]], _sel)
    check("breaker 配置已归一（含默认值）",
          _c10.aggregation["breaker"]["threshold_pct"] == 50
          and _c10.aggregation["breaker"]["enabled"] is True,
          _c10.aggregation["breaker"])
    _br_reset()
    bc._BR["last"], bc._BR["fail"], bc._BR["ok"] = {"A": 42}, {"A": 1}, {"B": 2}
    bc._BR["tripped"] = {"B"}
    bc._BR["note"] = "回归测试"
    check("熔断状态落盘", bc.breaker_save(_c10))
    check("状态文件存在", os.path.exists(bc.breaker_state_path(_c10)),
          bc.breaker_state_path(_c10))
    bc._BR["last"], bc._BR["fail"], bc._BR["ok"], bc._BR["tripped"] = {}, {}, {}, set()
    bc.breaker_load(_c10)
    check("状态可读回",
          bc._BR["tripped"] == {"B"} and bc._BR["last"].get("A") == 42
          and bc._BR["ok"].get("B") == 2, (bc._BR["tripped"], bc._BR["last"]))
    _sh.rmtree(_db, ignore_errors=True)

    print("\n== P11 桶停用 / 启用 ==")
    _d11 = tempfile.mkdtemp(prefix="bc_p11_")
    with open(os.path.join(os.path.dirname(HERE), "config.example.json"),
              encoding="utf-8-sig") as f:
        _raw11 = _json2.load(f)
    _raw11["app"]["data_dir"] = _d11
    _raw11["buckets"][0]["enabled"] = False
    _p11 = os.path.join(_d11, "cfg.json")
    with open(_p11, "w", encoding="utf-8") as f:
        _json2.dump(_raw11, f, ensure_ascii=False)
    _c11 = cc.load_config(_p11)
    check("enabled=false 被读入", _c11.buckets[0]["enabled"] is False)
    check("未写 enabled 的桶默认启用", _c11.buckets[1]["enabled"] is True)
    check("新建桶模板默认启用", cc.new_bucket_template("X")["enabled"] is True)
    import probe_buckets as pb
    _res, _upd = pb.probe_all(_c11, only=[_c11.buckets[0]["id"]])
    _r0 = _res[_c11.buckets[0]["id"]]
    check("停用桶不探测（state=n/a 且不写状态）",
          _r0["state"] == "n/a" and not _upd, (_r0["state"], _upd))
    _sh.rmtree(_d11, ignore_errors=True)

    print("\n== P12 门户劫持不计入丢包（避免熔断误摘好腿） ==")
    # 门户劫持时全部请求都拿不到 204，但那是被重定向、不是丢包。若照 100% 上报丢包率，
    # 熔断（默认 50% × 连续 2 轮）会把所有好腿当坏腿逐个摘掉 —— 而"门户要求重新认证"
    # 恰恰是 docs/04 F5 里的常规事件。这里锁住「portal 态不报丢包率」这一条。
    import probe_buckets as pb12
    _d12 = tempfile.mkdtemp(prefix="bc_p12_")
    with open(os.path.join(os.path.dirname(HERE), "config.example.json"),
              encoding="utf-8-sig") as f:
        _raw12 = _json2.load(f)
    _raw12["app"]["data_dir"] = _d12
    _raw12["probe"]["target"] = "http://probe.test/204"
    _raw12["buckets"][0]["probe"]["mode"] = "local"
    _p12 = os.path.join(_d12, "cfg.json")
    with open(_p12, "w", encoding="utf-8") as f:
        _json2.dump(_raw12, f, ensure_ascii=False)
    _c12 = cc.load_config(_p12)
    _b12 = _c12.buckets[0]
    _brs12 = {"threshold_pct": 50, "trip_after": 2, "recover_below_pct": 10,
              "recover_after": 3, "keep_min": 1}

    _orig_run, _orig_icmp = pb12.run, pb12.icmp_loss_pct
    pb12.icmp_loss_pct = lambda *a, **k: (None, "")     # 回归里不真去 ping

    def _mk12(code, body):
        def _fake(args, **_kw):
            return (0, code) if "-w" in args else (0, body)
        return _fake

    try:
        pb12.run = _mk12("200", "WISPAccessGatewayParam NextURL")
        _rp = pb12.probe_bucket(_c12, _b12, tries=3)
        check("门户劫持 -> state=portal 且不报丢包率",
              _rp["state"] == "portal" and _rp["http_loss"] is None,
              (_rp["state"], _rp["http_loss"]))

        bc._BR["tripped"], bc._BR["fail"], bc._BR["ok"] = set(), {}, {}
        _bs12 = [{"id": "T"}, {"id": "U"}]
        _los12 = {"T": _rp["http_loss"], "U": 0}
        _r12a = bc.breaker_round(_brs12, _bs12, _los12)
        _r12b = bc.breaker_round(_brs12, _bs12, _los12)
        check("熔断不会因门户劫持累计不合格轮数 / 摘腿",
              _r12a == [] and _r12b == [] and bc._BR["tripped"] == set(),
              (_r12a, _r12b, bc._BR["tripped"]))

        pb12.run = _mk12("000", "")
        _rl = pb12.probe_bucket(_c12, _b12, tries=3)
        check("真丢包仍照常上报 100%（熔断仍能摘腿）",
              _rl["state"] == "loss" and _rl["http_loss"] == 100,
              (_rl["state"], _rl["http_loss"]))

        # 状态更新必须显式写 null（而不是"省略不写"）：write_state 是合并写，
        # 省略会让上一轮的旧丢包率永远留在状态文件里，界面一直挂着过期数字。
        pb12.run = _mk12("200", "WISPAccessGatewayParam NextURL")
        _res12, _upd12 = pb12.probe_all(_c12, only=[_b12["id"]])
        _k12 = "loss_" + _b12["state_key"]
        check("判定测不出丢包率时显式写 null（覆盖旧值）",
              _k12 in _upd12 and _upd12[_k12] is None, _upd12)
    finally:
        pb12.run, pb12.icmp_loss_pct = _orig_run, _orig_icmp
        _sh.rmtree(_d12, ignore_errors=True)

    print("\n== P13 配置类型写错归一到 ConfigError ==")
    # 裸 AttributeError / ValueError 会让 CLI 与 GUI 直接 traceback 崩掉（入口只捕
    # ConfigError）。这里把典型写错逐条钉住。
    _ex = os.path.join(os.path.dirname(HERE), "config.example.json")
    _base = cc.load_raw(_ex)

    def _cfg_with(mut):
        raw = _copy.deepcopy(_base)
        mut(raw)
        return cc.Cfg(raw, _ex)

    def _sub(d, k, v):
        d[k] = v

    _cases = [
        ("app 写成字符串", lambda r: _sub(r, "app", "x")),
        ("probe 写成字符串", lambda r: _sub(r, "probe", "socks")),
        ("aggregation 写成数组", lambda r: _sub(r, "aggregation", [])),
        ("ui 写成字符串", lambda r: _sub(r, "ui", "wide")),
        ("buckets 写成字符串", lambda r: _sub(r, "buckets", "A")),
        ("ui.interval_ms 写成 abc", lambda r: _sub(r["ui"], "interval_ms", "abc")),
        ("probe.icmp_count 写成 abc", lambda r: _sub(r["probe"], "icmp_count", "abc")),
        ("breaker.threshold_pct 写成 abc",
         lambda r: _sub(r["aggregation"].setdefault("breaker", {}), "threshold_pct", "abc")),
        ("renew.ssh.port 写成 abc",
         lambda r: _sub(r["buckets"][0].setdefault("renew", {}), "ssh", {"port": "abc"})),
        ("桶的 probe 写成字符串", lambda r: _sub(r["buckets"][0], "probe", "local")),
        ("桶的 kind 写成数字", lambda r: _sub(r["buckets"][0], "kind", 3)),
        ("aggregation.legs 写成字符串", lambda r: _sub(r["aggregation"], "legs", "legs.yaml")),
    ]
    for _nm, _mut in _cases:
        try:
            _cfg_with(_mut)
            check(_nm, False, "没抛异常（类型错误被静默吞掉）")
        except cc.ConfigError as _e:
            check(_nm, True, str(_e)[:60])
        except Exception as _e:  # noqa: BLE001
            check(_nm, False, "抛了 %s：%s" % (type(_e).__name__, _e))

    print("\n== P14 占位符判定 ==")
    check("裸占位符 = 未配置", cc.is_unset("<探活目标URL>"))
    check("复合模板 = 未配置（api / socks / provider_name 那种）",
          cc.is_unset("http://<控制API地址>")
          and cc.is_unset("socks5h://<管理IP>:<SOCKS端口-A>")
          and cc.is_unset("<proxy-provider 名>"))
    check("shell 重定向不再被误判成未配置",
          not cc.is_unset("cmd < in > out") and not cc.is_unset("cat<in>out"))
    check("普通值当然不是未配置", not cc.is_unset("curl -s http://real/"))
    check("示例配置仍无结构性问题", cc.load_config(_ex).problems() == [],
          cc.load_config(_ex).problems())

    print("\n== P15 写盘锁 / 备份名 / 尾部读日志 ==")
    import time as _t
    _d15 = tempfile.mkdtemp(prefix="bc_p15_")
    _f15 = os.path.join(_d15, "x.txt")
    with cc.file_lock(_f15, timeout=1.0):
        check("持锁期间 .lock 存在", os.path.exists(_f15 + ".lock"))
    check("释放后 .lock 已清理", not os.path.exists(_f15 + ".lock"))

    # 残留死锁（写进一个不可能存在的 PID）必须能被回收，否则一次 Ctrl+C 就永久只读
    with open(_f15 + ".lock", "w", encoding="ascii") as f:
        f.write("999999999 %f" % _t.time())
    try:
        with cc.file_lock(_f15, timeout=3.0):
            check("残留死锁被回收，仍能拿到锁", True)
    except Exception as _e:  # noqa: BLE001
        check("残留死锁被回收，仍能拿到锁", False, repr(_e))

    _p15 = os.path.join(_d15, "cfg.json")
    with open(_p15, "w", encoding="utf-8") as f:
        f.write("{}")
    _b1 = cc.backup_name(_p15)
    with open(_b1, "w", encoding="utf-8") as f:
        f.write("")
    _b2 = cc.backup_name(_p15)
    check("同一秒内两次备份不互相覆盖", _b1 != _b2, (_b1, _b2))

    _log15 = os.path.join(_d15, "ev.log")
    with open(_log15, "w", encoding="utf-8") as f:
        for _i in range(500):
            f.write("line-%d\n" % _i)
    _txt, _n = bc.tail_log(_log15, n=150)
    check("tail_log 行数正确", _n == 500, _n)
    check("tail_log 只取尾部 150 行",
          len(_txt.splitlines()) == 150 and _txt.splitlines()[-1] == "line-499",
          _txt.splitlines()[:1])
    _sh.rmtree(_d15, ignore_errors=True)

    print("\n== P16 字符串命令的替换值转义 ==")
    # 字符串 argv 走 shell=True，account / iface / target 里有一部分来自门户产物。
    # 不转义时一个空格就换参数、一个 & 或 ; 就能接着跑第二条命令。
    _raw16 = _copy.deepcopy(_base)
    _raw16["actions"]["q_str"] = {"argv": "run {account}", "timeout": 5}
    _raw16["actions"]["q_list"] = {"argv": ["run", "{account}"], "timeout": 5}
    _c16 = cc.Cfg(_raw16, _ex)
    _b16 = dict(_c16.buckets[0])
    _b16["account"] = "a b"
    check("字符串 argv：含空格的值被引起来",
          _c16.argv_for("q_str", _b16) != "run a b",
          _c16.argv_for("q_str", _b16))
    check("列表 argv：不过 shell，值原样保留",
          _c16.argv_for("q_list", _b16) == ["run", "a b"],
          _c16.argv_for("q_list", _b16))
    if os.name == "nt":
        check("Windows：& 与 | 被双引号挡住",
              cc.quote_shell("a&b") == '"a&b"' and cc.quote_shell("a|b") == '"a|b"',
              cc.quote_shell("a&b"))
        check("Windows：内嵌双引号被转义", cc.quote_shell('a"b') == '"a\\"b"',
              cc.quote_shell('a"b'))
        check("Windows：普通值不加引号", cc.quote_shell("eth0") == "eth0")
    else:
        check("POSIX：分号被单引号挡住", cc.quote_shell("a;b") == "'a;b'", cc.quote_shell("a;b"))
        check("POSIX：普通值不加引号", cc.quote_shell("eth0") == "eth0")
finally:
    try:
        root.destroy()
    except Exception:
        pass

print("\n=== 结果 ===")
if fails:
    for f in fails:
        print("  [FAIL] %s" % f)
    raise SystemExit(1)
print("  [OK] P1-P16 回灌修复 / 通用化能力全部通过")
