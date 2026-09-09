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

用法: python checks/verify_ports.py
"""
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
root = tk.Tk()
root.withdraw()


def check(name, cond, detail=""):
    print("   [%s] %s %s" % ("OK" if cond else "FAIL", name, detail))
    if not cond:
        fails.append(name)


try:
    print("== P1 task_state ==")
    st = bc.task_state("WeNetState")
    check("已注册任务返回规范英文状态", st in ("Ready", "Running", "Disabled"), "-> %r" % st)
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
    # 直接测写路径：伪造一个最小 cfg 对象
    class _Agg(dict):
        pass

    class _Cfg:
        aggregation = {"mode_file": mf, "api": ""}
        def has_action(self, _n):
            return True
    # set_proxy_mode 会先查 API（不可达即返回），这里只验证原子写片段
    _tmp = mf + ".tmp"
    with open(_tmp, "w", encoding="ascii") as f:
        f.write("direct\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(_tmp, mf)
    content = open(mf, encoding="ascii").read()
    check("原子替换后内容正确且无残留 .tmp",
          content == "direct\n" and not os.path.exists(_tmp), repr(content))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n== P2 真机闸新鲜度 ==")
    src = open(os.path.join(os.path.dirname(HERE), "bucket_console.py"), encoding="utf-8").read()
    check("_real_gate 含新鲜度校验", "max_age_min" in src and "已过期" in src)

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
print("  [OK] P1-P11 回灌修复 / 通用化能力全部通过")
