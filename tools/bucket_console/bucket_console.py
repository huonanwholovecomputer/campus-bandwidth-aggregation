# -*- coding: utf-8 -*-
"""
bucket_console.py — 多桶聚合控制台（多桶合一 · 托盘 + 全操控界面）
================================================================================
一眼看桶状态 + 点按钮做全部连接管理。**只读数据 + 调度命令**：本工具不实现任何
认证/协议逻辑，所有动作都复用你自己配置的命令（脚本 / 计划任务 / ssh / curl）。

界面结构（与作者内部测试版同构，可直接对照）：
  ┌ 顶栏   标题 · 数据新鲜度 · 自动刷新间隔(15/30/60/停)
  ├ 总览卡片  桶在线 · 门户劫持 · 白名单 · 真机安全 · 聚合出口 · 聚合腿组 · 新鲜度
  ├ 桶状态表  桶 / 状态 / 真机判定 / 最后IP / 说明（双击看详情，悬停看解读）
  ├ 系统行    聚合出口 + 白名单 + 巡检时间
  ├ 健康走势  本会话每桶状态 step 线（在线 / 劫持·未知 / 丢包 / 离线）
  ├ 标签页    ①在线会话 ②白名单绑定 ③账号密码 ④真机判定 ⑤运维总控 ⑥事件日志 ⑦自检报告
  ├ 按钮区    刷新/巡检/门户/安全续连/重启聚合/代理开关/日志/复制摘要/自检/测速/目录/帮助/退出
  └ 底栏      门户属主与时间 · 状态文件时间

生命周期（UI = 总开关，与内部测试版一致的「主从绑定」）：
  · 启动：写心跳文件 + 按配置执行 boot_chain（拉起虚拟路由 / 聚合出口，幂等）
  · 退出（全链停）：执行 stop_chain + 撤心跳 → 配套任务/巡检随即停手
  · 关闭窗口 = 最小化到托盘（不改生命周期）
  · UI 崩溃：心跳消失后，配套任务凭心跳判定「UI 不在」而停手

依赖：Python 3.9+；tkinter（自带）。
      托盘需要 pystray + pillow（缺失时自动降级为纯窗口模式，功能不减）。
运行：
  python bucket_console.py --config config.private.json
  python bucket_console.py --check            # 只做配置自检，不开界面
  python bucket_console.py --selftest         # 构造全部界面后立刻退出（冒烟）
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from collections import deque
from tkinter import ttk, messagebox, simpledialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from console_config import (  # noqa: E402
    BUCKET_KINDS, ConfigError, PROBE_MODES, RENEW_MODES, config_summary,
    default_config_path, is_unset, load_config, mac_is_multicast, mac_ok,
    new_bucket_template, normalize_mac, save_buckets, save_config_keys,
    validate_bucket,
)

try:
    import probe_buckets as PB
except Exception as _e:  # 参考生产端缺失时仍可用（只是少了内置探测）
    PB = None
    PB_ERR = str(_e)
else:
    PB_ERR = None

try:
    import credstore
    CRED_ERR = None
except Exception as _e:
    credstore = None
    CRED_ERR = "无法导入 credstore: %s" % _e

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
STARS = "*******"

try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
    TRAY_OK = True
    TRAY_ERR = None
except Exception as _e:  # 无托盘依赖 → 纯窗口模式
    pystray = None
    Image = ImageDraw = ImageTk = None
    TRAY_OK = False
    TRAY_ERR = str(_e)

# ---------------- 状态词表（与控制台/生产端约定一致） ----------------
STATUS_TEXT = {
    "ok": "在线", "ok-icmp": "在线(ICMP)", "loss": "丢包", "down": "离线", "noip": "无IP",
    "portal": "门户劫持", "tundeg": "TUN退化", "api-down": "API断", "err": "错误",
    "direct": "直连(非TUN)", "present": "已绑定", "missing": "被删?!", "online": "在线",
    "clear": "离线·可续", "unknown": "未知", "no-tok": "无凭据", "n/a": "—",
    "unbound": "未绑定", "?": "?",
}
COLORS = {
    "ok": "#1a8f3c", "portal": "#e08600", "loss": "#c62828", "down": "#c62828",
    "noip": "#8d8d8d", "tundeg": "#e08600", "api-down": "#c62828", "n/a": "#8d8d8d",
    "present": "#1a8f3c", "missing": "#c62828", "online": "#1a8f3c", "clear": "#1a8f3c",
    "unknown": "#8d8d8d", "no-tok": "#8d8d8d", "err": "#c62828",
    "ok-icmp": "#1a8f3c", "direct": "#4363d8", "unbound": "#8d8d8d",
}
LEVEL = {"ok": 3, "ok-icmp": 3, "online": 3, "clear": 3, "present": 3, "direct": 3,
         "portal": 2, "tundeg": 2, "unknown": 2, "no-tok": 2, "n/a": 2, "?": 2,
         "loss": 1,
         "down": 0, "noip": 0, "api-down": 0, "missing": 0, "err": 0}
LEVEL_TEXT = {3: "在线", 2: "劫持/未知", 1: "丢包", 0: "离线"}
PALETTE = ["#4a90d9", "#3cb371", "#d9a13c", "#9b59b6", "#e06c75", "#56b6c2",
           "#c678dd", "#e5c07b"]


# ---------------- DPI / 缩放 ----------------
DPI_STATE = {"mode": "unknown", "detail": ""}


def enable_dpi_aware():
    """进程级 DPI 感知：优先 **Per-Monitor V2**，其次 Per-Monitor，最后 System Aware。

    必须在 tk.Tk() 之前调用 —— 否则 Windows 会把整个窗口按位图拉伸，文字发虚。
    注意：`SetProcessDpiAwarenessContext` 在 **user32**（不是 shcore），
    且参数是 64 位指针语义，必须用 c_void_p(-4)，用 c_int(-4) 会静默失败。"""
    if not IS_WIN:
        DPI_STATE.update(mode="n/a", detail="非 Windows")
        return
    user32 = ctypes.windll.user32
    # 1) Per-Monitor V2（Win10 1703+）
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_bool
        if fn(ctypes.c_void_p(-4)):          # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            DPI_STATE.update(mode="per-monitor-v2", detail="")
            return
        DPI_STATE["detail"] = "ctx 返回 False"
    except Exception as e:  # noqa: BLE001
        DPI_STATE["detail"] = "ctx: %s" % e
    # 2) Per-Monitor（Win8.1+）
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            DPI_STATE.update(mode="per-monitor")
            return
    except Exception as e:  # noqa: BLE001
        DPI_STATE["detail"] += " / shcore: %s" % e
    # 3) System Aware（老系统兜底）
    try:
        if user32.SetProcessDPIAware():
            DPI_STATE.update(mode="system")
            return
    except Exception as e:  # noqa: BLE001
        DPI_STATE["detail"] += " / dpiaware: %s" % e
    DPI_STATE.update(mode="unaware")


def dpi_awareness_text():
    m = DPI_STATE.get("mode")
    return {"per-monitor-v2": "Per-Monitor V2（清晰）",
            "per-monitor": "Per-Monitor（清晰）",
            "system": "System Aware（基本可用）",
            "unaware": "UNAWARE（会被系统拉伸→模糊）",
            "n/a": "非 Windows"}.get(m, m or "未知")


def get_window_dpi(hwnd):
    if not IS_WIN:
        return 96
    try:
        return int(ctypes.windll.user32.GetDpiForWindow(hwnd))
    except Exception:
        try:
            return int(ctypes.windll.user32.GetDpiForSystem())
        except Exception:
            return 96


# ---------------- 基础工具 ----------------
def run(argv, timeout=25, env=None, cwd=None, shell=False):
    """执行并返回 (rc, stdout)。rc=None 表示超时/异常（stdout 里带 ERR 说明）。"""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env,
                           cwd=cwd or None, shell=shell, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "").strip()
    except subprocess.TimeoutExpired as e:
        head = e.stdout if isinstance(e.stdout, str) else ""
        return None, ("%s\nERR: 命令超时（%ss）" % (head.strip(), timeout)).strip()
    except Exception as e:  # noqa: BLE001
        return None, "ERR: %s" % e


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def file_age_min(path):
    try:
        return (time.time() - os.path.getmtime(path)) / 60.0
    except Exception:
        return None


def task_state(name):
    """schtasks 查询（Windows）；其它平台返回「不适用」。"""
    if not IS_WIN:
        return "不适用"
    try:
        r = subprocess.run(["schtasks.exe", "/Query", "/TN", name, "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15, creationflags=NO_WINDOW)
        if r.returncode != 0:
            return "不存在"
        for row in csv.reader(r.stdout.splitlines()):
            if len(row) >= 4 and row[1].strip("\\ ").split("\\")[-1].lower() == name.lower():
                return row[3]
        return "未知"
    except Exception as e:  # noqa: BLE001
        return "ERR %s" % e


def open_path(path):
    """跨平台「打开目录/文件」。"""
    if not path:
        return False
    if not os.path.exists(path):
        return False
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


# ---------------- 动作执行 / 聚合控制面（简单界面与高级界面共用） ----------------
def exec_action(cfg, name, bucket=None, env=None, **subs):
    """执行配置里的动作，返回 (rc, 带命令原文的输出)。未配置 → (None, 提示)。"""
    argv = cfg.argv_for(name, bucket, **subs)
    a = cfg.action(name)
    if not argv:
        return None, "未配置动作 %s（见配置文件的 actions）" % name
    if isinstance(argv, str):
        rc, out = run(argv, timeout=a["timeout"], cwd=a["cwd"] or None, shell=True, env=env)
    else:
        rc, out = run(argv, timeout=a["timeout"], cwd=a["cwd"] or None, env=env)
    head = "命令: %s\n" % (" ".join(argv) if isinstance(argv, list) else argv)
    tail = "\n(rc=%s)" % rc if rc not in (0, None) else ""
    return rc, head + (out or "(无输出)") + tail


def agg_api_json(cfg, path):
    """直查聚合出口的 REST 控制面（绕过系统代理）。未配置 → None。"""
    api = cfg.aggregation.get("api")
    if not api:
        return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(api + path, timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def proxy_available(cfg) -> bool:
    return bool(cfg.aggregation.get("api")) and cfg.has_action(
        cfg.aggregation.get("restart_action") or "restart_aggregator")


def set_proxy_mode(cfg, target, log=None):
    """把聚合出口切到 target（rule=开代理 / direct=关代理）：写模式文件 + 重启 + 轮询确认。
    阻塞调用，请放在后台线程里。返回 (ok, msg)。"""
    log = log or (lambda _m: None)
    if not proxy_available(cfg):
        return False, "未配置聚合控制面（aggregation.api / restart_action）"
    cur = (agg_api_json(cfg, "/configs") or {}).get("mode")
    if cur is None:
        return False, "聚合控制面不可达，无法切换"
    log("proxy-toggle 目标=%s (当前=%s)" % (target, cur))
    try:
        with open(cfg.aggregation["mode_file"], "w", encoding="ascii") as f:
            f.write(target + "\n")
    except Exception as e:  # noqa: BLE001
        return False, "写模式文件失败：%s" % e
    exec_action(cfg, cfg.aggregation.get("restart_action") or "restart_aggregator")
    ok, last = False, None
    for _ in range(12):
        time.sleep(3)
        c2 = agg_api_json(cfg, "/configs")
        if c2:
            last = c2.get("mode")
            if last == target:
                ok = True
                break
    log("proxy-mode -> %s (%s)" % (target, "ok" if ok else "fail"))
    if ok:
        return True, ("已开启 VPN 代理：聚合出口按「规则配置」重启，聚合/分流生效。"
                      if target == "rule" else
                      "已关闭 VPN 代理：聚合出口按「直连配置」重启，流量走本机直连。")
    return False, "切换可能失败（重启后未检测到目标模式），请查看事件日志/状态卡片。"


def show_result(root, cfg, title, text, after=None, scale=1.0):
    """统一的「结果弹窗（可复制）」——简单界面与高级界面共用。"""
    def px(n):
        return max(1, round(n * scale))
    top = tk.Toplevel(root)
    top.title("%s @ %s" % (title, time.strftime("%H:%M:%S")))
    top.geometry("%dx%d" % (px(880), px(520)))
    top.minsize(px(620), px(360))
    wrap = ttk.Frame(top)
    wrap.pack(fill="both", expand=True)
    txt = tk.Text(wrap, wrap="none", font=(cfg.ui.get("mono_font") or "Consolas", 9))
    sb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    txt.pack(side="left", fill="both", expand=True)
    txt.insert("1.0", text or "(无输出——见事件日志)")
    txt.config(state="disabled")
    txt.bind("<Control-a>", lambda _e: (txt.tag_add("sel", "1.0", "end"), "break"))
    bar = ttk.Frame(top)
    bar.pack(fill="x", pady=px(4))
    ttk.Button(bar, text="复制结果", width=10,
               command=lambda: (top.clipboard_clear(),
                                top.clipboard_append(text or ""))).pack(side="right",
                                                                        padx=px(4))
    ttk.Button(bar, text="关闭", width=8, command=top.destroy).pack(side="right")
    if after:
        after()
    return top


def bind_mousewheel(widget, canvas):
    """把滚轮事件递归绑到控件及其所有子控件上（Canvas 滚动区用）。

    Tk 的 <MouseWheel> 只发给指针下的控件；Canvas 里嵌 Frame 后，指针通常在子控件上，
    所以必须逐个绑定，否则「滚轮没反应」。"""
    def _wheel(event):
        num = getattr(event, "num", None)
        if num == 4:
            step = -1
        elif num == 5:
            step = 1
        else:
            step = -1 if getattr(event, "delta", 0) > 0 else 1
        try:
            canvas.yview_scroll(step, "units")
        except Exception:
            pass
        return "break"

    def _bind(w):
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                w.bind(seq, _wheel, add="+")
            except Exception:
                pass
        for c in w.winfo_children():
            _bind(c)
    _bind(widget)
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        try:
            canvas.bind(seq, _wheel, add="+")
        except Exception:
            pass


class ToolTip:
    """轻量悬停提示（不引第三方依赖）。text 可以是字符串或返回字符串的可调用对象。"""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        self._id = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e):
        self._schedule()

    def _leave(self, _e):
        self._cancel()
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None

    def _schedule(self):
        self._cancel()
        try:
            self._id = self.widget.after(500, self._show)
        except Exception:
            self._id = None

    def _cancel(self):
        if self._id:
            try:
                self.widget.after_cancel(self._id)
            except Exception:
                pass
            self._id = None

    def _show(self):
        try:
            text = self.text() if callable(self.text) else self.text
            if not text:
                return
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 20
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry("+%d+%d" % (x, y))
            tk.Label(self.tip, text=text, justify="left", bg="#ffffe8",
                     relief="solid", borderwidth=1, padx=6, pady=4, anchor="w").pack()
        except Exception:
            pass


# ---------------- 主界面（高级） ----------------
class App:
    def __init__(self, root, cfg, auto_boot=True):
        self.root = root
        self.cfg = cfg
        self.auto_boot = auto_boot
        self.last_st = None
        self.last_pv = None
        self.history = deque(maxlen=max(20, cfg.ui["spark_samples"]))
        self.after_id = None
        self.interval_ms = int(cfg.ui["interval_ms"])
        # 聚合腿组探测缓存（后台限频查询控制 API）
        self.agg_probe = {"t": 0.0, "state": "unknown", "now": "", "members": [],
                          "legs_n": None, "mode": None, "err": ""}
        self._agg_busy = False
        self._chain_booted = False
        self._quitting = False
        self._icon = None
        # 桶管理：内存工作副本（保存后写回配置并重建界面）
        self.bm_buckets = []
        self.bm_dirty = False

        os.makedirs(cfg.data_dir, exist_ok=True)

        # ---- 生命周期：心跳 + 清掉上次的收尾戳/模式残留 ----
        self._hb_evt = threading.Event()
        threading.Thread(target=_hb_loop, args=(cfg, self._hb_evt), daemon=True).start()
        for p in [cfg.lifecycle.get("teardown_stamp")] + list(cfg.lifecycle.get("on_start_remove") or []):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        if cfg.aggregation.get("proxy_enabled"):
            try:
                mf = cfg.aggregation.get("mode_file")
                if mf and os.path.exists(mf):
                    os.remove(mf)      # 暂停态不跨会话保留
            except Exception:
                pass

        # ---- DPI / 缩放 ----
        self.dpi = get_window_dpi(root.winfo_id())
        self.scale = max(0.9, min(3.0, self.dpi / 96.0))
        root.tk.call("tk", "scaling", round(96.0 * self.scale / 72.0, 6))

        root.title(cfg.ui["title"])
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        W = min(int(cfg.ui["window_w"] * self.scale), int(sw * 0.9))
        H = min(int(cfg.ui["window_h"] * self.scale), int(sh * 0.92))
        x = max(0, (sw - W) // 2)
        y = max(0, (sh - H) // 3)
        root.geometry("%dx%d+%d+%d" % (W, H, x, y))
        root.minsize(self.px(620), self.px(520))
        root.resizable(True, True)
        self._last_dpi = self.dpi
        root.bind("<Configure>", self._on_configure, add="+")

        if TRAY_OK:
            try:
                img = make_icon(cfg).resize((32, 32), Image.LANCZOS)
                self._win_icon = ImageTk.PhotoImage(img)
                root.iconphoto(True, self._win_icon)
            except Exception:
                pass

        self._build_ui()
        self.refresh()
        self._schedule()
        if auto_boot and cfg.has_action("boot_chain"):
            self.root.after(int(cfg.lifecycle.get("boot_delay_ms") or 3000), self._boot_chain)

    # ================= 生命周期 =================
    def _log_bind(self, msg):
        try:
            with open(self.cfg.events_file, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S") + " BIND " + msg + "\n")
        except Exception:
            pass

    def _boot_chain(self):
        """启动拉起：按配置执行 boot_chain（幂等，后台跑）。"""
        if self._chain_booted or self._quitting:
            return
        self._chain_booted = True
        argv = self.cfg.argv_for("boot_chain")
        if not argv:
            return

        def f():
            self._log_bind("UI 启动: 执行 boot_chain")
            rc, out = self._run_action("boot_chain")
            self._log_bind("boot_chain rc=%s %s" % (rc, (out or "").splitlines()[-1:] or ""))
        self._run_bg(f)

    def act_quit(self):
        """退出 = 全链停（按配置执行 stop_chain 并撤心跳）。"""
        if self._quitting:
            return
        if not messagebox.askyesno("退出（全链停）", self.cfg.lifecycle["exit_confirm"]):
            return
        self._quitting = True
        stop_argv = self.cfg.argv_for("stop_chain")
        try:
            self.lbl_foot.config(
                text=("全链停止中（执行 stop_chain）…" if stop_argv else "退出中…"), fg="#e08600")
        except Exception:
            pass

        def f():
            if stop_argv:
                rc, out = self._run_action("stop_chain")
                self._log_bind("stop_chain rc=%s" % rc)
            _remove_ui_marker(self.cfg)
            self._hb_evt.set()
        self._run_bg(f, self._final_quit)

    def _final_quit(self):
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass

    # ================= 像素换算 =================
    def px(self, n):
        return max(1, round(n * self.scale))

    def _on_configure(self, _e):
        dpi = get_window_dpi(self.root.winfo_id())
        if abs(dpi - self._last_dpi) >= 2:
            self._last_dpi = dpi
            self.apply_dpi(dpi)

    def apply_dpi(self, dpi):
        """窗口跨到不同 DPI 显示器时动态重算（字体由 tk scaling 自动跟随）。"""
        self.dpi = dpi
        self.scale = max(0.9, min(3.0, dpi / 96.0))
        self.root.tk.call("tk", "scaling", round(96.0 * self.scale / 72.0, 6))
        try:
            for cid, base in (("bucket", 92), ("status", 96), ("loss", 68), ("real", 150),
                              ("ip", 130), ("desc", 300)):
                self.tree.column(cid, width=self.px(base))
            self.root.minsize(self.px(620), self.px(520))
        except Exception:
            pass

    # ================= UI 构建 =================
    def _build_ui(self):
        root = self.root
        outer = ttk.Frame(root, padding=self.px(8))
        outer.pack(fill="both", expand=True)

        # --- 顶栏 ---
        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text=self.cfg.ui["title"],
                  font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.lbl_fresh = ttk.Label(top, text="")
        self.lbl_fresh.pack(side="left", padx=(self.px(10), 0))
        ttk.Label(top, text="自动刷新:").pack(side="right")
        self.var_interval = tk.StringVar(value="15 秒")
        self.cmb_interval = ttk.Combobox(top, state="readonly", width=6,
                                         textvariable=self.var_interval,
                                         values=("15 秒", "30 秒", "60 秒", "停"))
        self.cmb_interval.pack(side="right")
        self.cmb_interval.bind("<<ComboboxSelected>>", self._on_interval)
        self.var_interval.set({15000: "15 秒", 30000: "30 秒", 60000: "60 秒"}.get(
            self.interval_ms, "15 秒"))
        self.interval_ms = {"15 秒": 15000, "30 秒": 30000, "60 秒": 60000,
                            "停": 0}.get(self.var_interval.get(), 15000)

        # --- 总览卡片 ---
        cards = ttk.Frame(outer)
        cards.pack(fill="x", pady=(self.px(6), 0))
        self.card_labels = {}
        for key, base_text in (("buckets", "桶 在线 -"), ("portal", "劫持 -"),
                               ("wl", "白名单 -"), ("real", "真机安全 -"),
                               ("aggout", "聚合出口 -"), ("agg", "聚合 -"),
                               ("age", "新鲜度 -")):
            lbl = ttk.Label(cards, text=base_text, padding=(self.px(8), self.px(4)),
                            relief="groove", anchor="center")
            lbl.pack(side="left", fill="x", expand=True, padx=(0, self.px(4)))
            self.card_labels[key] = lbl
        ToolTip(self.card_labels["agg"], self._agg_card_tip)

        # --- 底栏 + 操作按钮：先占位（side=bottom），保证窗口再矮也不会被裁掉 ---
        self.lbl_foot = tk.Label(outer, text="", anchor="w", fg="#666")
        self.lbl_foot.pack(side="bottom", fill="x", pady=(self.px(4), 0))
        bf = ttk.LabelFrame(outer, text="连接管理 / 工具")
        bf.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        btns = [
            ("刷新状态", self.act_refresh),
            ("重新巡检", self.act_refresh_state),
            ("门户刷新", self.act_refresh_portal),
            ("安全续连", self.act_safe_renew),
            ("重启聚合", self.act_restart_aggregator),
            ("代理开关", self.act_proxy_toggle),
            ("事件日志", self._goto_log),
            ("复制摘要", self.act_copy_summary),
            ("自检", self.act_health),
            ("测速", self.act_speedtest),
            ("打开数据目录", self.act_open_dir),
            ("帮助/图例", self.act_help),
            ("退出(全链停)", self.act_quit),
        ]
        self._tool_btns = {}
        for i, (text, cb) in enumerate(btns):
            b = ttk.Button(bf, text=text, command=cb, width=11)
            b.grid(row=i // 4, column=i % 4, padx=self.px(4), pady=self.px(3), sticky="we")
            self._tool_btns[text] = b
        self.btn_proxy = self._tool_btns.get("代理开关")
        for c in range(4):
            bf.columnconfigure(c, weight=1)

        # --- 桶状态表 ---
        ttk.Label(outer, text="桶状态（数据由状态生产端回写；双击行看详情，悬停看解读）"
                  ).pack(anchor="w", pady=(self.px(8), 0))
        cols = (("bucket", "桶", 92), ("status", "状态", 96), ("loss", "丢包率", 68),
                ("real", "真机判定", 150), ("ip", "最后IP", 130), ("desc", "说明", 300))
        self.tree = ttk.Treeview(outer, columns=[c[0] for c in cols], show="headings",
                                 height=max(3, min(8, len(self.cfg.buckets))))
        for cid, h, w in cols:
            self.tree.heading(cid, text=h)
            self.tree.column(cid, width=self.px(w), anchor="w", stretch=(cid == "desc"))
        self.tree.pack(fill="x", pady=(self.px(2), 0))
        self.tree.bind("<Double-1>", self._on_tree_double)
        ToolTip(self.tree, self._tree_tip_text)
        self.lbl_sys = tk.Label(outer, text="…", anchor="w", fg="#1a8f3c")
        self.lbl_sys.pack(fill="x", pady=(self.px(2), 0))

        # --- 健康走势图 ---
        sparkf = ttk.LabelFrame(outer, text="健康走势（本会话，最近 %d 次采样，随刷新记录）"
                                % self.history.maxlen)
        sparkf.pack(fill="x", pady=(self.px(6), 0))
        self.cv = tk.Canvas(sparkf, height=self.px(74), bg="#fdfcf8",
                            highlightthickness=1, highlightbackground="#ddd")
        self.cv.pack(fill="x")
        self.spark_legend = ttk.Frame(sparkf)
        self.spark_legend.pack(anchor="w", padx=self.px(4))
        self._update_spark_legend()

        # --- 底部标签页 ---
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(self.px(6), 0))
        self.tab_index = {}
        for name in ("sessions", "whitelist", "accounts", "real", "manager", "ops",
                     "log", "health"):
            self.tab_index[name] = len(self.nb.tabs())
            self.nb.add(ttk.Frame(self.nb), text=name)
        self.nb.tab(self.tab_index["accounts"], text="账号密码")
        self.nb.tab(self.tab_index["ops"], text="运维总控")
        self.nb.tab(self.tab_index["real"], text="真机判定")
        self.nb.tab(self.tab_index["manager"], text="桶管理")
        self._build_tab_sessions()
        self._build_tab_whitelist()
        self._build_tab_accounts()
        self._build_tab_real()
        self._build_tab_manager()
        self._build_tab_ops()
        self._build_tab_log()
        self._build_tab_health()

    def _bucket_color(self, idx):
        b = self.cfg.buckets[idx]
        return b.get("color") or PALETTE[idx % len(PALETTE)]

    def _loss_cell(self, bucket, st):
        """丢包率列：状态文件里的 loss_<key>（由 probe_buckets.py 写入）。"""
        lr = (st or {}).get("loss_" + bucket["state_key"])
        if lr is None:
            return "—"
        try:
            loss = int(lr)
        except Exception:
            return "—"
        return "%d%%%s" % (loss, "!" if loss >= 50 else "")

    def _loss_text(self, bucket, st):
        """状态列后缀：HTTP 丢包率（状态文件里有 loss_<key> 才显示）。"""
        lr = (st or {}).get("loss_" + bucket["state_key"])
        if lr is None:
            return ""
        try:
            return "  %d%%" % int(lr)
        except Exception:
            return ""

    def _loss_lines(self, bucket, st):
        """详情/悬停里展示的两层丢包率。"""
        out = []
        lr = (st or {}).get("loss_" + bucket["state_key"])
        ir = (st or {}).get("icmp_" + bucket["state_key"])
        if lr is not None:
            out.append("HTTP 丢包率: %s%%（%d 次探活中未拿到 204 的比例）" % (lr, bucket["probe"]["tries"]))
        if ir is not None:
            out.append("ICMP 丢包率: %s%%" % ir)
        return out

    def _update_spark_legend(self):
        """图例：每个桶一段，文字颜色与走势线一致。"""
        try:
            fr = self.spark_legend
            for w in fr.winfo_children():
                w.destroy()
            for i, b in enumerate(self.cfg.buckets):
                tk.Label(fr, text="■ %s" % b["id"], fg=self._bucket_color(i)).pack(
                    side="left", padx=(0, self.px(10)))
            tk.Label(fr, text="·  上=在线 / 中=劫持·未知 / 下=丢包 / 底=离线",
                     fg="#8d8d8d").pack(side="left")
        except Exception:
            pass

    # ---------------- 标签页 ----------------
    def _tab(self, name):
        return self.nb.nametowidget(self.nb.tabs()[self.tab_index[name]])

    def _build_tab_sessions(self):
        f = self._tab("sessions")
        ttk.Label(f, text="门户会话查询可见的在线会话（凭据属主可见；按设备标识去重）"
                  ).pack(anchor="w", padx=self.px(4), pady=self.px(2))
        self.tree_sessions = ttk.Treeview(f, columns=("mac", "ip", "os", "sid", "owner"),
                                          show="headings", height=6)
        for cid, h, w in (("mac", "设备标识", 170), ("ip", "IP", 130), ("os", "OS", 80),
                          ("sid", "会话ID", 260), ("owner", "凭据属主", 130)):
            self.tree_sessions.heading(cid, text=h)
            self.tree_sessions.column(cid, width=self.px(w), anchor="w")
        self.tree_sessions.pack(fill="both", expand=True, padx=self.px(4), pady=self.px(2))

    def _build_tab_whitelist(self):
        f = self._tab("whitelist")
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=self.px(4), pady=(self.px(2), 0))
        ttk.Label(bar, text="各账号免认证（无感）白名单绑定 · 克隆目标绿色加粗 · 密码库列=该账号是否已录入"
                  ).pack(side="left")
        ttk.Button(bar, text="门户刷新", width=8,
                   command=self.act_refresh_portal).pack(side="right", padx=self.px(2))
        ttk.Button(bar, text="复制所选", width=8,
                   command=self._act_wl_copy).pack(side="right")
        self.tree_wl = ttk.Treeview(f, columns=("acct", "vault", "mac", "os", "ip"),
                                    show="headings", height=8)
        for cid, h, w in (("acct", "账号", 150), ("vault", "密码库", 80),
                          ("mac", "设备标识", 170), ("os", "OS", 90), ("ip", "绑定IP", 130)):
            self.tree_wl.heading(cid, text=h)
            self.tree_wl.column(cid, width=self.px(w), anchor="w")
        self.tree_wl.pack(fill="both", expand=True, padx=self.px(4), pady=self.px(2))
        self.tree_wl.tag_configure("clone", foreground="#1a8f3c",
                                   font=("TkDefaultFont", 9, "bold"))
        ttk.Label(f, text="纪律：本页只读展示 + 复制；白名单的增删请到门户页面处理"
                          "（删除类接口通常无二次校验且会清条目）。",
                  foreground="#8d8d8d").pack(anchor="w", padx=self.px(4))

    def _act_wl_copy(self):
        sel = self.tree_wl.selection()
        if not sel:
            messagebox.showinfo("复制所选", "请先选择白名单行。")
            return
        lines = []
        for iid in sel:
            v = self.tree_wl.item(iid, "values")
            if v:
                lines.append("\t".join(str(x) for x in v))
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("复制所选", str(e))
            return
        messagebox.showinfo("复制所选", "已复制 %d 行到剪贴板。" % len(lines))

    # ---------------- 账号密码页（凭据库 · 录入即锁定） ----------------
    def _build_tab_accounts(self):
        f = self._tab("accounts")
        ttk.Label(f, text=(
            "账号凭据库：加密存放，明文永不落盘，仅脚本调用接口那一刻解密取用。"
            "录入并确认后密码列只显示 %s 且锁定（不可查看/修改/重录）；"
            "更换密码 = 删除该账号后重新录入。" % STARS
        ), wraplength=self.px(880), justify="left").pack(anchor="w", padx=self.px(4),
                                                        pady=self.px(2))
        self.tree_acct = ttk.Treeview(f, columns=("account", "owner", "pw", "created", "updated"),
                                      show="headings", height=6)
        for cid, h, w in (("account", "账号", 170), ("owner", "所属", 90), ("pw", "密码", 120),
                          ("created", "录入时间", 145), ("updated", "更新时间", 145)):
            self.tree_acct.heading(cid, text=h)
            self.tree_acct.column(cid, width=self.px(w), anchor="w")
        self.tree_acct.pack(fill="both", expand=True, padx=self.px(4), pady=self.px(2))
        self.tree_acct.tag_configure("locked", foreground="#8d8d8d")
        self.tree_acct.tag_configure("nopw", foreground="#c62828")
        self.tree_acct.bind("<<TreeviewSelect>>", self._on_acct_select)

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=self.px(4), pady=(self.px(2), 0))
        ttk.Button(bar, text="新增账号", width=10,
                   command=self._act_acct_add).pack(side="left")
        self.btn_acct_set = ttk.Button(bar, text="录入密码", width=12,
                                       command=self._act_acct_set_pw)
        self.btn_acct_set.pack(side="left", padx=self.px(4))
        ttk.Button(bar, text="删除账号", width=10,
                   command=self._act_acct_del).pack(side="left")
        ttk.Button(bar, text="刷新", width=8,
                   command=self._fill_accounts).pack(side="left", padx=self.px(4))

        if credstore is None:
            info = CRED_ERR or "凭据库不可用"
            color = "#c62828"
        else:
            info = ("凭据库: %s\n后端: %s\n"
                    "未录入 → 密码列红色「未配置」｜已录入 → 灰色 %s（锁定，无可操作项）"
                    % (credstore.store_path(), credstore.backend_note(), STARS))
            color = "#e08600" if credstore.insecure() else "#666"
        self.lbl_acct_info = tk.Label(f, text=info, anchor="w", justify="left", fg=color,
                                      font=("TkDefaultFont", 8))
        self.lbl_acct_info.pack(fill="x", padx=self.px(4), pady=(0, self.px(2)))

    def _fill_accounts(self):
        tr = self.tree_acct
        for row in tr.get_children():
            tr.delete(row)
        if credstore is None:
            return
        try:
            rows = credstore.list_accounts()
        except Exception as e:  # noqa: BLE001
            self.lbl_acct_info.config(text="读取凭据库失败: %s" % e, fg="#c62828")
            return
        for it in rows:
            has = bool(it["has"])
            tr.insert("", "end", iid="acct_" + it["account"],
                      values=(it["account"], self._account_owner_text(it["account"]),
                              STARS if has else "未配置",
                              it["created"] or "—", it["updated"] or "—"),
                      tags=("locked",) if has else ("nopw",))
        self._on_acct_select()

    def _account_owner_text(self, acct):
        """账号身份：取该账号对应桶的 owner 字段（配置里自填），没有则显示桶显示名。"""
        labels = []
        for b in self.cfg.buckets:
            if (b.get("account") or "") != acct:
                continue
            lab = (b.get("owner") or "").strip()
            if not lab:
                lab = (b.get("label") or "").strip()
                if lab == b.get("id"):
                    lab = ""          # 显示名就是桶标识时不算身份
            if lab and lab not in labels:
                labels.append(lab)
        return " / ".join(labels) if labels else "—"

    def _select_acct_row(self, account):
        iid = "acct_" + account
        if self.tree_acct.exists(iid):
            self.tree_acct.selection_set(iid)
            self.tree_acct.see(iid)
            self._on_acct_select()

    def _on_acct_select(self, _e=None):
        if not hasattr(self, "btn_acct_set"):
            return
        sel = self.tree_acct.selection()
        if not sel:
            self.btn_acct_set.state(["disabled"])
            self.btn_acct_set.config(text="录入密码")
            return
        vals = self.tree_acct.item(sel[0], "values")
        acct = vals[0] if vals else ""
        locked = bool(vals and vals[1] == STARS)
        if locked:
            self.btn_acct_set.state(["disabled"])
            self.btn_acct_set.config(text="已锁定")
            self.lbl_acct_info.config(
                text="%s：已录入，仅显示 %s，不可查看/修改/重录。"
                     "如需更换：删除该账号后重新录入（或用 credstore.py set 维护）。"
                     % (acct, STARS), fg="#e08600")
        else:
            self.btn_acct_set.state(["!disabled"])
            self.btn_acct_set.config(text="录入密码")
            self.lbl_acct_info.config(
                text="%s：未录入密码 → 点「录入密码」填写并确认（两次一致），确认后即锁定。"
                     % acct, fg="#666")

    def _acct_presets(self):
        presets = list(self.cfg.portal.get("accounts") or [])
        pv = self.last_pv
        if pv:
            for a in (pv.get("accounts") or []):
                u = a.get("username")
                if u and u not in presets:
                    presets.append(u)
            for t in (pv.get("cloneTargets") or []):
                u = t.get("account")
                if u and u not in presets:
                    presets.append(u)
        if credstore is not None:
            try:
                for it in credstore.list_accounts():
                    if it["account"] not in presets:
                        presets.append(it["account"])
            except Exception:
                pass
        return presets

    def _dlg_acct_input(self):
        top = tk.Toplevel(self.root)
        top.title("新增测试账号")
        top.geometry("%dx%d" % (self.px(420), self.px(150)))
        top.resizable(False, False)
        top.transient(self.root)
        top.grab_set()
        ttk.Label(top, text="账号（可手输任意测试账号，用于标记该账号是否已录入凭据）",
                  wraplength=self.px(380)).pack(anchor="w", padx=self.px(10),
                                                pady=(self.px(8), 0))
        var = tk.StringVar()
        cb = ttk.Combobox(top, textvariable=var, values=self._acct_presets())
        cb.pack(fill="x", padx=self.px(10), pady=self.px(8))
        cb.focus_set()
        result = {"val": None}

        def ok(_e=None):
            name = var.get().strip()
            if credstore is not None and not credstore.account_ok(name):
                messagebox.showwarning(
                    "新增账号", "账号名不合法：仅允许字母数字开头，含 [A-Za-z0-9_-] 最长 64。",
                    parent=top)
                return
            result["val"] = name
            top.destroy()

        ttk.Button(top, text="确定", command=ok, width=10).pack(side="right", padx=self.px(10),
                                                              pady=self.px(6))
        ttk.Button(top, text="取消", command=top.destroy, width=10).pack(side="right")
        cb.bind("<Return>", ok)
        top.bind("<Escape>", lambda _e: top.destroy())
        self.root.wait_window(top)
        return result["val"]

    def _act_acct_add(self):
        if credstore is None:
            messagebox.showerror("账号密码", CRED_ERR or "凭据库不可用")
            return
        acct = self._dlg_acct_input()
        if not acct:
            return
        try:
            credstore.register_account(acct)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("新增账号", "登记失败: %s" % e)
            return
        self._fill_accounts()
        self._select_acct_row(acct)
        sel = self.tree_acct.selection()
        vals = self.tree_acct.item(sel[0], "values") if sel else []
        if vals and vals[1] == STARS:
            messagebox.showinfo("账号密码", "%s 已存在且已录入凭据（锁定，仅显示 %s）。"
                                % (acct, STARS))
            return
        if messagebox.askyesno("录入密码",
                               "已登记 %s。现在录入该账号凭据吗？\n（录入并确认后即锁定，仅显示 %s）"
                               % (acct, STARS)):
            self._act_acct_set_pw()

    def _dlg_acct_set_pw(self, account):
        """两次隐藏输入 + 确认。成功返回 True。"""
        top = tk.Toplevel(self.root)
        top.title("录入密码 · %s" % account)
        top.geometry("%dx%d" % (self.px(460), self.px(240)))
        top.resizable(False, False)
        top.transient(self.root)
        top.grab_set()
        ttk.Label(top, text=(
            "账号 %s：填写后将用「%s」加密存入凭据库，\n"
            "确认后仅显示 %s 并锁定，任何操作均不可见、不可改明文。"
            % (account, credstore.backend_note() if credstore else "本机凭据库", STARS)),
            justify="left").pack(anchor="w", padx=self.px(10), pady=(self.px(8), self.px(4)))
        row = ttk.Frame(top)
        row.pack(fill="x", padx=self.px(10))
        ttk.Label(row, text="密码:").pack(side="left")
        e1 = ttk.Entry(row, show="*", width=28)
        e1.pack(side="left", padx=self.px(6))
        row2 = ttk.Frame(top)
        row2.pack(fill="x", padx=self.px(10), pady=(self.px(4), 0))
        ttk.Label(row2, text="确认:").pack(side="left")
        e2 = ttk.Entry(row2, show="*", width=28)
        e2.pack(side="left", padx=self.px(6))
        lbl = ttk.Label(top, text="", foreground="#c62828")
        lbl.pack(anchor="w", padx=self.px(10))
        result = {"ok": False}
        e1.focus_set()

        def ok(_e=None):
            p1, p2 = e1.get(), e2.get()
            if not p1:
                lbl.config(text="密码不能为空")
                return
            if p1 != p2:
                lbl.config(text="两次输入不一致，请重新输入")
                e2.delete(0, "end")
                e2.focus_set()
                return
            if not messagebox.askyesno(
                    "确认录入",
                    "确认将账号 %s 的凭据加密写入凭据库？\n确认后界面即锁定，仅显示 %s（不可再查看/修改）。"
                    % (account, STARS), parent=top):
                return
            try:
                credstore.set_secret(account, p1)
            except Exception as ex:  # noqa: BLE001
                messagebox.showerror("录入失败", str(ex), parent=top)
                return
            result["ok"] = True
            top.destroy()

        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", padx=self.px(10), pady=self.px(8))
        ttk.Button(btns, text="确认录入并加密", command=ok, width=18).pack(side="right")
        ttk.Button(btns, text="取消", command=top.destroy, width=10).pack(side="right",
                                                                        padx=self.px(6))
        e1.bind("<Return>", lambda _e: e2.focus_set())
        e2.bind("<Return>", ok)
        top.bind("<Escape>", lambda _e: top.destroy())
        self.root.wait_window(top)
        return result["ok"]

    def _act_acct_set_pw(self):
        if credstore is None:
            messagebox.showerror("账号密码", CRED_ERR or "凭据库不可用")
            return
        sel = self.tree_acct.selection()
        if not sel:
            messagebox.showinfo("录入密码", "请先在列表中选择一个账号（或点「新增账号」）。")
            return
        vals = self.tree_acct.item(sel[0], "values")
        acct = vals[0] if vals else ""
        if vals and vals[1] == STARS:
            messagebox.showwarning("录入密码", "%s 已录入并锁定，不可修改。\n"
                                               "如需更换请先删除该账号后重新录入。" % acct)
            return
        if self._dlg_acct_set_pw(acct):
            self._fill_accounts()
            self._select_acct_row(acct)
            messagebox.showinfo("录入密码", "%s 已加密写入凭据库。\n界面仅显示 %s，"
                                            "后续调用时自动解密。" % (acct, STARS))

    def _act_acct_del(self):
        if credstore is None:
            messagebox.showerror("账号密码", CRED_ERR or "凭据库不可用")
            return
        sel = self.tree_acct.selection()
        if not sel:
            messagebox.showinfo("删除账号", "请先在列表中选择要删除的账号。")
            return
        vals = self.tree_acct.item(sel[0], "values")
        acct = vals[0] if vals else ""
        has_pw = bool(vals and vals[1] == STARS)
        if has_pw:
            typed = simpledialog.askstring(
                "删除账号（含凭据）",
                "%s 已录入凭据，删除将同时清除其密文且不可恢复。\n"
                "如需继续，请在下框输入账号名 %s 确认：" % (acct, acct), parent=self.root)
            if typed != acct:
                if typed is not None:
                    messagebox.showwarning("删除账号", "输入的账号名不一致，已取消删除。")
                return
        else:
            if not messagebox.askyesno("删除账号", "删除占位账号 %s（无凭据条目）？" % acct):
                return
        try:
            credstore.remove_account(acct)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("删除账号", "删除失败: %s" % e)
            return
        self._fill_accounts()
        messagebox.showinfo("删除账号", "%s 已删除（含其凭据密文）。" % acct)

    # ---------------- 真机判定页 ----------------
    def _build_tab_real(self):
        f = self._tab("real")
        ttk.Label(f, text="克隆目标「真机在线」预检（点选行，下方显示完整判定建议）"
                  ).pack(anchor="w", padx=self.px(4), pady=self.px(2))
        self.tree_real = ttk.Treeview(f, columns=("id", "acct", "real", "wl", "ip", "advice"),
                                      show="headings", height=5)
        for cid, h, w in (("id", "目标", 80), ("acct", "账号", 130), ("real", "真机在线", 100),
                          ("wl", "白名单", 80), ("ip", "最后IP", 130), ("advice", "判定建议", 420)):
            self.tree_real.heading(cid, text=h)
            self.tree_real.column(cid, width=self.px(w), anchor="w")
        self.tree_real.pack(fill="x", padx=self.px(4), pady=self.px(2))
        self.tree_real.tag_configure("bad", foreground="#c62828")
        self.tree_real.tag_configure("good", foreground="#1a8f3c")
        self.tree_real.tag_configure("warn", foreground="#e08600")
        self.tree_real.bind("<<TreeviewSelect>>", self._on_real_select)
        self.lbl_realadvice = tk.Text(f, height=3, wrap="word",
                                      font=(self.cfg.ui["mono_font"], 9), bg="#fbfbfb")
        self.lbl_realadvice.pack(fill="x", padx=self.px(4), pady=self.px(2))
        self.lbl_realadvice.insert("1.0", "（点选上方行查看完整判定）")
        self.lbl_realadvice.config(state="disabled")

    def _build_tab_log(self):
        f = self._tab("log")
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=self.px(4), pady=self.px(2))
        ttk.Label(bar, text="%s（尾部 150 行；本页可见时随刷新同步）"
                  % os.path.basename(self.cfg.events_file)).pack(side="left")
        self.lbl_loginfo = ttk.Label(bar, text="")
        self.lbl_loginfo.pack(side="right")
        ttk.Button(bar, text="重新载入", command=self._fill_log,
                   width=10).pack(side="right")
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        self.txt_log = tk.Text(wrap, wrap="none", font=(self.cfg.ui["mono_font"], 9))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt_log.pack(side="left", fill="both", expand=True)

    def _build_tab_health(self):
        f = self._tab("health")
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=self.px(4), pady=self.px(2))
        ttk.Label(bar, text="自检报告（文件/任务/控制面/环境；「深检」较慢，后台执行）"
                  ).pack(side="left")
        ttk.Button(bar, text="深检", command=self.act_health,
                   width=8).pack(side="right")
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        self.txt_health = tk.Text(wrap, wrap="none", font=(self.cfg.ui["mono_font"], 9),
                                  bg="#fbfbfb")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_health.yview)
        self.txt_health.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt_health.pack(side="left", fill="both", expand=True)

    # ================= 刷新调度 =================
    def _schedule(self):
        if self.after_id:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        if self.interval_ms > 0:
            self.after_id = self.root.after(self.interval_ms, self._tick)

    def _tick(self):
        self.after_id = None
        try:
            self.refresh()
        except Exception as e:  # noqa: BLE001
            import traceback
            try:
                self.lbl_foot.config(text="刷新异常: %s (%s)"
                                          % (e, traceback.format_exc(limit=1).strip()),
                                     fg="#c62828")
            except Exception:
                pass
        self._schedule()

    def _on_interval(self, _e=None):
        self.interval_ms = {"15 秒": 15000, "30 秒": 30000, "60 秒": 60000,
                            "停": 0}.get(self.var_interval.get(), 15000)
        self._schedule()

    # ================= 状态刷新 =================
    def refresh(self):
        st = load_json(self.cfg.state_file)
        pv = load_json(self.cfg.portal_file) if self.cfg.portal_file else None
        self.last_st, self.last_pv = st, pv

        # 数据新鲜度
        a1 = file_age_min(self.cfg.state_file)
        a2 = file_age_min(self.cfg.portal_file) if self.cfg.portal_file else None

        def age_txt(m):
            return "%d分前" % int(m) if m >= 1 else "%d秒前" % int(m * 60)

        if a1 is None:
            self.lbl_fresh.config(text="状态文件不可读!", foreground="#c62828")
        else:
            col = "#1a8f3c" if a1 < 3 else ("#e08600" if a1 < 6 else "#c62828")
            self.lbl_fresh.config(
                text="状态 %s · 门户 %s" % (age_txt(a1), age_txt(a2) if a2 is not None else "无"),
                foreground=col)

        # 系统行
        if st is None:
            self.lbl_sys.config(text="!! 状态文件不可读（状态生产端未运行？%s）" % self.cfg.state_file,
                                fg="#c62828")
        else:
            agg_out = st.get("aggregator", st.get("mihomo"))
            agg_api = st.get("aggregator_api", st.get("mih_api"))
            pxy = "⚠ 代理已暂停·直连 | " if self.agg_probe.get("mode") == "direct" else ""
            wl_txt = " ".join("%s=%s" % (b["id"], st.get("wl_%s" % b["id"], "?"))
                              for b in self.cfg.buckets if b["kind"] == "clone")
            self.lbl_sys.config(
                text="%s聚合出口=%s | 控制面=%s | %s | 巡检 %s"
                     % (pxy, STATUS_TEXT.get(agg_out, agg_out), agg_api, wl_txt,
                        st.get("time", "")),
                fg="#e08600" if self.agg_probe.get("mode") == "direct"
                else ("#1a8f3c" if agg_api == "ok" else "#e08600"))

        # 总览卡片
        self._update_cards(st, pv)

        # 桶表
        for row in self.tree.get_children():
            self.tree.delete(row)
        for b in self.cfg.buckets:
            val = (st or {}).get(b["state_key"], "?")
            real = self._real_for(b, pv)
            ip = self._ip_for(b, pv)
            color = COLORS.get(str(val), "#333")
            tag = "c_" + color.lstrip("#")
            self.tree.tag_configure(tag, foreground=color)
            self.tree.insert("", "end", iid=b["id"],
                             values=(b["id"], STATUS_TEXT.get(val, val),
                                     self._loss_cell(b, st), real, ip, b["desc"]),
                             tags=(tag,))

        # 走势采样
        self.history.append({b["id"]: LEVEL.get(str((st or {}).get(b["state_key"], "?")), 2)
                             for b in self.cfg.buckets})
        self._draw_spark()

        # 标签页按需刷新
        cur = self.nb.index(self.nb.select())
        if cur == self.tab_index["sessions"]:
            self._fill_sessions(pv)
        if cur == self.tab_index["whitelist"]:
            self._fill_whitelist(pv)
        if cur == self.tab_index["accounts"]:
            self._fill_accounts()
        if cur == self.tab_index["real"]:
            self._fill_real(pv)
        if cur == self.tab_index["ops"]:
            self._fill_ops()
        if cur == self.tab_index["log"]:
            self._fill_log()
        if cur == self.tab_index["health"]:
            self._fill_health(pv)

        # 底栏
        ft = ""
        if pv:
            ft = "门户: 凭据可用=%s 属主=%s @ %s" % (
                pv.get("tokenOk"), pv.get(self.cfg.portal["owner_field"], "?"), pv.get("time", ""))
        if st:
            ft += "   （状态 @ %s）" % st.get("time", "")
        if not TRAY_OK:
            ft += "   [托盘依赖缺失: %s]" % TRAY_ERR
        self.lbl_foot.config(text=ft)

        self._ensure_agg_probe()
        self._sync_proxy_ui()

    # ================= 代理开关 =================
    def _sync_proxy_ui(self):
        mode = self.agg_probe.get("mode")
        try:
            if self.btn_proxy is not None:
                if not self._proxy_available():
                    self.btn_proxy.config(text="代理开关")
                elif mode == "direct":
                    self.btn_proxy.config(text="恢复代理")
                elif mode:
                    self.btn_proxy.config(text="暂停代理")
                else:
                    self.btn_proxy.config(text="代理开关")
        except Exception:
            pass
        lbl = getattr(self, "ops_lbl_proxy", None)
        if lbl is not None:
            try:
                if not self._proxy_available():
                    lbl.config(text="代理:未配置", foreground="#8d8d8d")
                elif mode == "direct":
                    lbl.config(text="代理:暂停·直连", foreground="#e08600")
                elif mode:
                    lbl.config(text="代理:规则(聚合)", foreground="#1a8f3c")
                else:
                    lbl.config(text="代理:控制面不可达", foreground="#8d8d8d")
            except Exception:
                pass

    def _proxy_available(self):
        return bool(self.cfg.aggregation.get("api")) and self.cfg.has_action(
            self.cfg.aggregation.get("restart_action") or "restart_aggregator")

    def _write_mode_file(self, mode):
        try:
            with open(self.cfg.aggregation["mode_file"], "w", encoding="ascii") as f:
                f.write(mode + "\n")
            return True
        except Exception:
            return False

    def act_proxy_toggle(self):
        """暂停代理（切直连配置重启）↔ 恢复代理（切回规则配置重启）。"""
        if not self._proxy_available():
            messagebox.showinfo("代理开关",
                                "未配置聚合控制面：请在配置里填 aggregation.api，"
                                "并把 aggregation.restart_action 指向重启动作。")
            return
        cur = self.agg_probe.get("mode")
        if cur not in ("rule", "direct"):
            cfgj = self._api_json("/configs")
            cur = (cfgj or {}).get("mode") if cfgj else None
        if cur is None:
            messagebox.showerror("代理开关", "聚合控制面不可达，无法切换。")
            return
        target = "rule" if cur == "direct" else "direct"
        self._do_set_proxy(target)

    def _do_set_proxy(self, target):
        """在后台把聚合出口切到 target 模式（rule=开代理 / direct=关代理）。"""
        self.lbl_foot.config(text="切换代理模式中…", fg="#e08600")

        def f():
            ok, msg = set_proxy_mode(self.cfg, target, log=self._log_bind)
            self.agg_probe["mode"] = target if ok else None

            def done():
                try:
                    self.lbl_foot.config(text=msg,
                                         fg=("#e08600" if target == "direct" else "#1a8f3c"))
                except Exception:
                    pass
                self._sync_proxy_ui()
                try:
                    self._update_cards(self.last_st, self.last_pv)
                except Exception:
                    pass
                try:
                    if self._icon is not None:
                        self._icon.notify(msg, self.cfg.ui["tray_title"])
                except Exception:
                    pass
            self.root.after(0, done)
        self._run_bg(f)

    # ================= 总览卡片 =================
    def _update_cards(self, st, pv):
        buckets = self.cfg.buckets
        ok_count = sum(1 for b in buckets
                       if str((st or {}).get(b["state_key"], "")).startswith("ok"))
        portal_count = sum(1 for b in buckets
                           if str((st or {}).get(b["state_key"], "")) == "portal")
        self._set_card("buckets", "桶在线 %d/%d" % (ok_count, len(buckets)),
                       "#1a8f3c" if ok_count == len(buckets) else "#e08600")
        self._set_card("portal", "门户劫持 %d" % portal_count,
                       "#e08600" if portal_count else "#666")

        # 白名单：期望绑定但未绑定 → 标红
        clones = [b for b in buckets if b["kind"] == "clone"]
        if clones and pv:
            bound = sum(1 for b in clones if self._clone_target(b, pv) is not None
                        and self._clone_target(b, pv).get("onWhitelist"))
            missing = [b["id"] for b in clones if b["clone"].get("expect_whitelist")
                       and not (self._clone_target(b, pv) or {}).get("onWhitelist")]
            if missing:
                self._set_card("wl", "白名单 %d/%d !!缺 %s"
                               % (bound, len(clones), ",".join(missing)), "#c62828")
            else:
                self._set_card("wl", "白名单 %d/%d" % (bound, len(clones)), "#1a8f3c")
        else:
            self._set_card("wl", "白名单 -", "#666")

        # 真机安全：在线(勿动) / 未知(拦截) / 离线(可续)
        if clones and pv:
            online = sum(1 for b in clones
                         if (self._clone_target(b, pv) or {}).get("realOnline") is True)
            unknown = sum(1 for b in clones
                          if (self._clone_target(b, pv) or {}).get("realOnline") is None)
            txt = "%d在线/%d未知" % (online, unknown) if unknown else "%d在线/可续" % online
            self._set_card("real", "真机 %s" % txt, "#c62828" if online else "#1a8f3c")
        else:
            self._set_card("real", "真机 -", "#666")

        # 聚合出口 / 聚合腿组
        agg_out = (st or {}).get("aggregator", (st or {}).get("mihomo", "?"))
        agg_api = (st or {}).get("aggregator_api", (st or {}).get("mih_api", "?"))
        mode = self.agg_probe.get("mode")
        if mode == "direct":
            self._set_card("aggout", "代理暂停·直连(点恢复代理)", "#e08600")
            self._set_card("agg", "聚合=暂停中(直连)", "#8d8d8d")
        else:
            self._set_card("aggout", "出口 %s %s" % (STATUS_TEXT.get(agg_out, agg_out), agg_api),
                           COLORS.get(str(agg_api), "#666"))
            if not self.cfg.aggregation.get("api"):
                self._set_card("agg", "聚合=未配置控制面", "#8d8d8d")
            else:
                m = self.agg_probe
                if m["state"] == "multi":
                    self._set_card("agg", "聚合=多腿[%d]" % len(m["members"]), "#1a8f3c")
                elif m["state"] == "fallback":
                    tip = "腿组空!" if m.get("legs_n") == 0 else "直连退化!"
                    self._set_card("agg", "聚合=%s" % tip, "#c62828")
                elif m["state"] in ("api-down", "err"):
                    self._set_card("agg", "聚合=控制面?", "#c62828")
                else:
                    self._set_card("agg", "聚合=?", "#8d8d8d")

        a1 = file_age_min(self.cfg.state_file)
        if a1 is None:
            self._set_card("age", "新鲜度 ?", "#c62828")
        else:
            col = "#1a8f3c" if a1 < 3 else ("#e08600" if a1 < 6 else "#c62828")
            self._set_card("age", "新鲜度 %.1f分" % a1, col)

    def _set_card(self, key, text, color):
        try:
            self.card_labels[key].config(text=text, foreground=color)
        except Exception:
            pass

    # ================= 聚合腿组探测 =================
    def _api_json(self, path):
        return agg_api_json(self.cfg, path)

    def _ensure_agg_probe(self):
        """后台查「聚合组」成员与节点提供器数量（60s 限频）。"""
        if not self.cfg.aggregation.get("api"):
            return
        now = time.time()
        if self._agg_busy or now - self.agg_probe["t"] < 60:
            return
        self._agg_busy = True
        group = self.cfg.aggregation.get("group") or ""
        provider = self.cfg.aggregation.get("provider") or ""
        direct = tuple(self.cfg.aggregation.get("direct_members") or ["DIRECT"])

        def w():
            try:
                g = self._api_json("/proxies/" + urllib.parse.quote(group)) if group else None
                prov = self._api_json("/providers/proxies/" + urllib.parse.quote(provider)) \
                    if provider else None
                cfgj = self._api_json("/configs")
                mode = (cfgj or {}).get("mode") if cfgj else None
                legs_n = len((prov or {}).get("proxies") or []) if prov else None
                members = list((g or {}).get("all") or []) if g else []
                now_v = (g or {}).get("now", "") if g else ""
                if g is None and prov is None:
                    state = "api-down"
                elif any(x not in direct for x in members):
                    state = "multi"
                elif legs_n is not None and legs_n == 0:
                    state = "fallback"
                elif members:
                    state = "fallback"
                else:
                    state = "unknown"
                self.agg_probe = {"t": time.time(), "state": state, "now": now_v,
                                  "members": members, "legs_n": legs_n, "mode": mode,
                                  "err": ""}
            except Exception as e:  # noqa: BLE001
                self.agg_probe = {"t": time.time(), "state": "err", "now": "", "members": [],
                                  "legs_n": None, "mode": None, "err": str(e)}
            finally:
                self._agg_busy = False
                try:
                    self.root.after(0, self._render_agg_card)
                except Exception:
                    pass
        threading.Thread(target=w, daemon=True).start()

    def _render_agg_card(self):
        try:
            self._update_cards(self.last_st, self.last_pv)
            self._sync_proxy_ui()
        except Exception:
            pass

    def _agg_card_tip(self):
        if not self.cfg.aggregation.get("api"):
            return ("未配置 aggregation.api —— 该卡片不做腿组探测。\n"
                    "填上聚合出口的 REST 控制面地址后，这里会显示成员与节点数。")
        m = self.agg_probe
        group = self.cfg.aggregation.get("group") or "?"
        if m["state"] == "multi":
            return ("聚合组 %s 成员: %s\n节点提供器 %s 个 → 腿组在聚合"
                    % (group, ", ".join(m["members"]) or "(空)", m.get("legs_n", "?")))
        if m["state"] == "fallback":
            return ("聚合组 %s 成员: %s\n节点提供器 %s 个 → 未走腿（直连退化）。\n"
                    "排查: 提供器文件是否有效 / 聚合出口是否成功加载"
                    % (group, ", ".join(m["members"]) or "(空)", m.get("legs_n", "?")))
        if m["state"] in ("api-down", "err"):
            return "聚合控制面不可达，无法判定腿组状态 (err=%s)" % m.get("err", "")
        return "聚合腿状态探测中/未知（60s 后自动重试）"

    # ================= 桶 → 门户数据映射 =================
    def _clone_target(self, bucket, pv):
        if not pv:
            return None
        tid = bucket["clone"].get("portal_id")
        for t in (pv.get("cloneTargets") or []):
            if t.get("id") == tid:
                return t
        return None

    def _real_for(self, bucket, pv):
        """真机判定列：克隆桶看目标真机是否在线；自有桶看自己的会话在不在。"""
        if bucket["kind"] == "clone":
            if not pv:
                return "无门户数据"
            t = self._clone_target(bucket, pv)
            if t is None:
                return "未知(无该账号凭据)"
            adv = str(t.get("advice") or "")
            if adv.startswith("no-visibility"):
                return "未知(无该账号凭据)"
            if adv.startswith("no-tok"):
                return "未知(无凭据)"
            rl = t.get("realOnline")
            if rl is True:
                return "真机在线! 勿动"
            if rl is False:
                return "真机离线 · 可续"
            return "未知"
        mac = str(bucket.get("mac") or "").lower()
        if not mac or not pv:
            return "—"
        acct = bucket.get("account")
        for a in (pv.get("accounts") or []):
            if acct and a.get("username") != acct:
                continue
            for s in (a.get("online") or []):
                if str(s.get("mac", "")).lower() == mac:
                    return "在线"
            return "离线"
        return "—"

    def _ip_for(self, bucket, pv):
        if not pv:
            return "—"
        if bucket["kind"] == "clone":
            t = self._clone_target(bucket, pv)
            return (t or {}).get("lastIP") or "—"
        mac = str(bucket.get("mac") or "").lower()
        if not mac:
            return "—"
        for a in (pv.get("accounts") or []):
            for e in (a.get("whitelist") or []):
                if str(e.get("mac", "")).lower() == mac:
                    return e.get("ip") or "—"
        for a in (pv.get("accounts") or []):
            for s in (a.get("online") or []):
                if str(s.get("mac", "")).lower() == mac:
                    return s.get("ip") or "—"
        return "—"

    # ================= 走势图 =================
    def _draw_spark(self):
        cv = self.cv
        cv.delete("all")
        w = max(cv.winfo_width(), 60)
        h = max(cv.winfo_height(), 20)
        left, right, top, bottom = 10, 6, 18, 12   # top 留白：避免最高档标签被截断
        n = len(self.history)
        for lv in (0, 1, 2, 3):
            y = top + (h - top - bottom) * (3 - lv) / 3
            cv.create_line(left, y, w - right, y, fill="#ececec")
            cv.create_text(left + 2, y - 8, anchor="w", text=LEVEL_TEXT[lv], fill="#bbb",
                           font=("TkDefaultFont", 7))
        if n < 2:
            cv.create_text(left, top + 6, anchor="w", text="采样中…", fill="#999")
            return
        xstep = (w - left - right) / (n - 1)
        for i, b in enumerate(self.cfg.buckets):
            color = self._bucket_color(i)
            pts = []
            for j, s in enumerate(self.history):
                lv = s.get(b["id"], 2)
                pts += [left + j * xstep, top + (h - top - bottom) * (3 - lv) / 3]
            if len(pts) >= 4:
                cv.create_line(*pts, fill=color, width=2)
                cv.create_oval(pts[-2] - 2.5, pts[-1] - 2.5, pts[-2] + 2.5, pts[-1] + 2.5,
                               fill=color, outline="")
        cv.create_text(w - right - 2, h - 3, anchor="se", text=time.strftime("%H:%M:%S"),
                       fill="#999", font=("TkDefaultFont", 7))

    # ================= 标签页填充 =================
    def _fill_sessions(self, pv):
        tr = self.tree_sessions
        for row in tr.get_children():
            tr.delete(row)
        if not pv:
            tr.insert("", "end", values=("无门户数据", "", "", "", ""))
            return
        seen = set()
        owner = pv.get(self.cfg.portal["owner_field"]) or "?"
        for a in (pv.get("accounts") or []):
            for s in (a.get("online") or []):
                mac = str(s.get("mac", "") or "").strip().lower()
                if not mac or mac in seen:
                    continue
                seen.add(mac)
                tr.insert("", "end", values=(mac, s.get("ip") or "—", s.get("os") or "—",
                                             s.get("sid") or "—", owner))

    def _fill_whitelist(self, pv):
        tr = self.tree_wl
        for row in tr.get_children():
            tr.delete(row)
        pwset = set()
        if credstore is not None:
            try:
                for it in credstore.list_accounts():
                    if it["has"]:
                        pwset.add(it["account"])
            except Exception:
                pass
        if not pv:
            tr.insert("", "end", values=("无门户数据", "", "", "", ""))
            return
        clone_macs = {str(b.get("mac") or "").lower() for b in self.cfg.buckets
                      if b["kind"] == "clone" and b.get("mac")}
        for a in (pv.get("accounts") or []):
            marker = STARS if a.get("username") in pwset else ""
            for e in (a.get("whitelist") or []):
                mac = str(e.get("mac", "") or "")
                tag = "clone" if mac.lower() in clone_macs else ""
                tr.insert("", "end", values=(a.get("username"), marker, mac,
                                             e.get("os") or "—", e.get("ip") or "—"),
                          tags=(tag,) if tag else ())

    def _fill_real(self, pv):
        tr = self.tree_real
        for row in tr.get_children():
            tr.delete(row)
        if not pv:
            tr.insert("", "end", values=("—", "—", "—", "—", "—", "无门户数据"))
            return
        for t in (pv.get("cloneTargets") or []):
            rl = t.get("realOnline")
            wl = "已绑定" if t.get("onWhitelist") else "未绑定"
            if rl is True:
                real, tag = "真机在线!", "bad"
            elif rl is False:
                real, tag = "离线·可续", "good"
            else:
                real, tag = "未知", "warn"
            tr.insert("", "end", values=(t.get("id"), t.get("account"), real, wl,
                                         t.get("lastIP") or "—", t.get("advice") or ""),
                      tags=(tag,))

    def _on_real_select(self, _e):
        sel = self.tree_real.selection()
        self.lbl_realadvice.config(state="normal")
        self.lbl_realadvice.delete("1.0", "end")
        if sel:
            vals = self.tree_real.item(sel[0], "values")
            if len(vals) >= 6:
                self.lbl_realadvice.insert("1.0", "【%s / %s】%s" % (vals[0], vals[1], vals[5]))
        else:
            self.lbl_realadvice.insert("1.0", "（点选上方行查看完整判定）")
        self.lbl_realadvice.config(state="disabled")

    def _fill_log(self):
        try:
            with open(self.cfg.events_file, "r", encoding="utf-8-sig") as f:
                lines = f.readlines()
            self.txt_log.config(state="normal")
            self.txt_log.delete("1.0", "end")
            self.txt_log.insert("1.0", "".join(lines[-150:]))
            self.txt_log.config(state="disabled")
            self.lbl_loginfo.config(text="%d 行" % len(lines))
        except Exception as e:  # noqa: BLE001
            self.txt_log.config(state="normal")
            self.txt_log.delete("1.0", "end")
            self.txt_log.insert("1.0", "读取失败: %s" % e)
            self.txt_log.config(state="disabled")

    def _fill_health(self, pv):
        self.txt_health.config(state="normal")
        self.txt_health.delete("1.0", "end")
        self.txt_health.insert("1.0", self._collect_health(deep=False, pv=pv))
        self.txt_health.config(state="disabled")

    def _collect_health(self, deep, pv=None):
        cfg = self.cfg
        out = ["== 自检 @ %s（缩放 %.2f · DPI %d）=="
               % (time.strftime("%Y-%m-%d %H:%M:%S"), self.scale, self.dpi)]
        out.append("  [..] 配置: %s" % cfg.path)
        for nm, p in (("状态文件", cfg.state_file), ("门户文件", cfg.portal_file),
                      ("事件日志", cfg.events_file)):
            if not p:
                out.append("  [..] %s  未配置" % nm)
                continue
            age = file_age_min(p)
            out.append("  [!!] %s  %s  不存在" % (nm, p) if age is None
                       else "  [ok] %s  更新于 %.1f 分钟前" % (nm, age))
        try:
            out.append("  [..] 系统: python %s · tk %s · 屏幕 %dx%d"
                       % (sys.version.split()[0], tk.TkVersion,
                          self.root.winfo_screenwidth(), self.root.winfo_screenheight()))
        except Exception as e:  # noqa: BLE001
            out.append("  [..] 系统信息: %s" % e)
        out.append("  [..] DPI 感知: %s（缩放 %.2f · DPI %d）"
                   % (dpi_awareness_text(), self.scale, self.dpi))
        out.append("  [%s] 托盘: %s"
                   % ("ok" if TRAY_OK else "!!",
                      "已加载" if TRAY_OK else "缺失（%s）→ 纯窗口模式" % TRAY_ERR))
        out.append("  [%s] 凭据库: %s"
                   % ("!!" if (credstore and credstore.insecure()) else "ok",
                      (credstore.backend_note() if credstore else (CRED_ERR or "不可用"))))
        out.append("  [%s] 状态生产端模块: %s"
                   % ("ok" if PB else "!!", "已加载" if PB else (PB_ERR or "缺失")))
        if pv is not None:
            out.append("  [..] 门户 凭据可用=%s 属主=%s 节流=%s"
                       % (pv.get("tokenOk"), pv.get(cfg.portal["owner_field"], "?"),
                          pv.get("throttled")))
        m = self.agg_probe
        if not cfg.aggregation.get("api"):
            out.append("  [..] 聚合控制面: 未配置（不做腿组探测）")
        elif m["state"] == "multi":
            out.append("  [ok] 聚合组 %s 成员=[%s] 节点=%s → 腿组生效"
                       % (cfg.aggregation.get("group"), ",".join(m["members"]) or "(空)",
                          m.get("legs_n")))
        elif m["state"] == "fallback":
            out.append("  [!!] 聚合组 %s 仅[%s] 节点=%s → 腿组未生效（直连退化）"
                       % (cfg.aggregation.get("group"), ",".join(m["members"]) or "(空)",
                          m.get("legs_n")))
        elif m["state"] in ("api-down", "err"):
            out.append("  [!!] 聚合控制面不可达 (err=%s) → 无法判定腿组" % m.get("err", ""))
        else:
            out.append("  [..] 聚合腿状态: 探测中/未知")

        sp = load_json(cfg.speedtest_result)
        if sp:
            bests = []
            for leg in (sp.get("legs") or []):
                rounds = leg.get("rounds") or []
                best = max([float(r.get("mbps") or 0) for r in rounds], default=0.0)
                bests.append("%s=%s" % (leg.get("name", "?"),
                                        ("%.1fM" % best) if best else "—"))
            age = file_age_min(cfg.speedtest_result)
            out.append("  [..] 上次测速 (%s): %s"
                       % ("%.1f分前" % age if age is not None else "?", "  ".join(bests)))
        missing = cfg.unset_fields()
        if missing:
            out.append("  [..] 未配置项 %d 个（相关按钮会提示）:" % len(missing))
            for key, hint in missing:
                out.append("      · %s —— %s" % (key, hint))
        if not deep:
            out.append("")
            out.append("（点「深检」进一步检查计划任务 / 控制面 / 探活可达性，后台执行）")
            return "\n".join(out)

        for t in cfg.tasks:
            out.append("  [..] 任务 %s: %s" % (t["name"], task_state(t["name"])))
        if cfg.aggregation.get("api"):
            out.append("  [%s] 聚合控制面 /configs: %s"
                       % ("ok" if self._api_json("/configs") else "!!",
                          "可达" if self._api_json("/configs") else "不可达"))
        if cfg.probe.get("target") and PB is not None:
            for b in cfg.buckets:
                if b["probe"]["mode"] == "none":
                    continue
                r = PB.probe_bucket(cfg, b)
                out.append("  [%s] 桶 %s 探活: %s %s"
                           % ("ok" if r["state"] == "ok" else "!!", b["id"], r["state"],
                              ",".join(r["codes"]) or r.get("detail") or ""))
        return "\n".join(out)

    # ================= 桶管理（增 / 删 / 改 / 排序 / MAC 下发） =================
    BM_FIELDS = (
        ("基本", (
            ("id", "桶标识", "entry"),
            ("label", "显示名", "entry"),
            ("state_key", "状态键", "entry"),
            ("kind", "类型", "combo", BUCKET_KINDS),
            ("account", "账号", "entry"),
            ("owner", "所属（账号身份，如 自己/室友A）", "entry"),
            ("mac", "MAC", "mac"),
            ("desc", "说明", "entry"),
            ("color", "颜色（可选）", "entry"),
            ("speed_leg", "测速腿名", "entry"),
            ("target", "探活地址（可选，覆盖全局）", "entry"),
        )),
        ("探测", (
            ("probe.mode", "探测方式", "combo", PROBE_MODES),
            ("probe.socks", "SOCKS 出口", "entry"),
            ("probe.iface", "远端接口", "entry"),
            ("probe.ssh.host", "ssh 主机", "entry"),
            ("probe.ssh.user", "ssh 用户", "entry"),
            ("probe.ssh.port", "ssh 端口", "entry"),
            ("probe.ssh.key", "ssh 私钥路径", "entry"),
            ("probe.tries", "探测次数", "entry"),
            ("probe.timeout", "单次超时（秒）", "entry"),
        )),
        ("续连 / 克隆", (
            ("renew.mode", "续连方式", "combo", RENEW_MODES),
            ("renew.wan", "wan 接口", "entry"),
            ("renew.iface", "物理接口", "entry"),
            ("renew.gate", "互顶闸（克隆腿续连前必须验证真机）", "check"),
            ("renew.owner_label", "闸目标称呼", "entry"),
            ("renew.hint", "无需续连时的提示", "entry"),
            ("renew.ssh.host", "ssh 主机", "entry"),
            ("renew.ssh.user", "ssh 用户", "entry"),
            ("renew.ssh.port", "ssh 端口", "entry"),
            ("renew.ssh.key", "ssh 私钥路径", "entry"),
            ("clone.portal_id", "门户判定 id", "entry"),
            ("clone.expect_whitelist", "期望已绑定（缺失即告警）", "check"),
        )),
    )

    def _build_tab_manager(self, f=None):
        f = f or self._tab("manager")
        for w in f.winfo_children():
            w.destroy()
        ttk.Label(f, text=(
            "桶 = 一条独立会话出口。这里可直接增 / 删 / 改 / 排序并写回配置文件"
            "（保存前自动备份）；「改 MAC」既改配置字段，也可下发到网卡 / 接口。"),
            wraplength=self.px(900), justify="left").pack(anchor="w", padx=self.px(4),
                                                          pady=self.px(2))
        cols = (("id", "桶", 90), ("kind", "类型", 60), ("account", "账号", 110),
                ("mac", "MAC", 150), ("probe", "探测", 70), ("renew", "续连", 80),
                ("leg", "测速腿", 110), ("desc", "说明", 240))
        self.tree_bm = ttk.Treeview(f, columns=[c[0] for c in cols], show="headings",
                                    height=7)
        for cid, h, w in cols:
            self.tree_bm.heading(cid, text=h)
            self.tree_bm.column(cid, width=self.px(w), anchor="w",
                                stretch=(cid == "desc"))
        self.tree_bm.pack(fill="both", expand=True, padx=self.px(4), pady=self.px(2))
        self.tree_bm.bind("<Double-1>", lambda _e: self._bm_edit())
        self.tree_bm.tag_configure("dirty", foreground="#c62828")
        ToolTip(self.tree_bm, "双击行 = 编辑；「改 MAC」可同时改配置字段并下发到接口")

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=self.px(4), pady=(self.px(2), 0))
        for text, cb, w in (("新增桶", self._bm_add, 9), ("编辑", self._bm_edit, 7),
                            ("删除", self._bm_del, 7), ("上移", self._bm_up, 6),
                            ("下移", self._bm_down, 6), ("改 MAC", self._bm_mac, 9)):
            ttk.Button(bar, text=text, width=w, command=cb).pack(side="left",
                                                                 padx=self.px(2))
        bar2 = ttk.Frame(f)
        bar2.pack(fill="x", padx=self.px(4), pady=(self.px(2), self.px(4)))
        self.btn_bm_save = ttk.Button(bar2, text="保存到配置", width=12,
                                      command=self._bm_save)
        self.btn_bm_save.pack(side="left", padx=self.px(2))
        ttk.Button(bar2, text="放弃改动 / 重新载入", width=18,
                   command=self._bm_reload).pack(side="left", padx=self.px(2))
        self.lbl_bm = ttk.Label(bar2, text="", foreground="#666")
        self.lbl_bm.pack(side="left", padx=self.px(6))
        self._bm_load_from_cfg()
        self._fill_manager()

    def _bm_load_from_cfg(self):
        import copy
        self.bm_buckets = [copy.deepcopy(b) for b in (self.cfg.raw.get("buckets") or [])]
        self.bm_dirty = False

    def _bm_touch(self, idx=None):
        """标记改动：只有被改动的那一行变红（idx=None 表示整表结构变了，无单行可标）。"""
        self.bm_dirty = True
        if idx is not None and 0 <= idx < len(self.bm_buckets):
            self.bm_buckets[idx]["_dirty"] = True
        self._fill_manager()

    def _fill_manager(self):
        tr = self.tree_bm
        for row in tr.get_children():
            tr.delete(row)
        for i, b in enumerate(self.bm_buckets):
            probe = b.get("probe") or {}
            renew = b.get("renew") or {}
            tr.insert("", "end", iid="bm%d" % i, values=(
                b.get("id") or "?", b.get("kind") or "self", b.get("account") or "—",
                b.get("mac") or "—", probe.get("mode") or "local",
                renew.get("mode") or "none", b.get("speed_leg") or "—",
                b.get("desc") or ""), tags=("dirty",) if b.get("_dirty") else ())
        if self.bm_dirty:
            self.lbl_bm.config(text="有未保存的改动（红字）", foreground="#c62828")
        else:
            self.lbl_bm.config(text="已与配置文件一致", foreground="#1a8f3c")
        try:
            self.btn_bm_save.state(["!disabled"] if self.bm_dirty else ["disabled"])
        except Exception:
            pass

    def _bm_index(self):
        sel = self.tree_bm.selection()
        if not sel:
            return None
        try:
            return int(str(sel[0]).replace("bm", ""))
        except ValueError:
            return None

    def _bm_pick(self, verb):
        idx = self._bm_index()
        if idx is None:
            messagebox.showinfo(verb, "请先在列表中选择一个桶。")
        return idx

    def _bm_add(self):
        existing = {str(b.get("id") or "") for b in self.bm_buckets}
        n = 1
        while ("NEW%d" % n) in existing:
            n += 1
        tpl = new_bucket_template("NEW%d" % n)
        tpl["state_key"] = "b_new%d" % n
        tpl["speed_leg"] = tpl["id"]
        out = self._dlg_bucket(tpl, is_new=True,
                               other_ids=[str(b.get("id") or "") for b in self.bm_buckets])
        if out:
            self.bm_buckets.append(out)
            self._bm_touch(len(self.bm_buckets) - 1)
            self.tree_bm.selection_set("bm%d" % (len(self.bm_buckets) - 1))

    def _bm_edit(self):
        idx = self._bm_pick("编辑桶")
        if idx is None:
            return
        ids = [str(b.get("id") or "") for b in self.bm_buckets]
        others = [ids[j] for j in range(len(ids)) if j != idx]
        out = self._dlg_bucket(self.bm_buckets[idx], is_new=False, other_ids=others)
        if out:
            self.bm_buckets[idx] = out
            self._bm_touch(idx)
            self.tree_bm.selection_set("bm%d" % idx)

    def _bm_del(self):
        idx = self._bm_pick("删除桶")
        if idx is None:
            return
        b = self.bm_buckets[idx]
        bid = b.get("id")
        warn = ""
        if b.get("kind") == "clone":
            warn = "\n注意：这是克隆桶，删除后其真机判定/互顶闸也随之消失。"
        if len(self.bm_buckets) <= 1:
            messagebox.showwarning("删除桶", "至少需要保留一个桶。")
            return
        if not messagebox.askyesno("删除桶", "确认从配置中删除桶 %s（%s）？%s\n"
                                             "（保存后才真正写入文件）"
                                             % (bid, b.get("label") or "", warn)):
            return
        self.bm_buckets.pop(idx)
        self._bm_touch()

    def _bm_move(self, delta):
        idx = self._bm_pick("移动桶")
        if idx is None:
            return
        new = idx + delta
        if new < 0 or new >= len(self.bm_buckets):
            return
        self.bm_buckets[idx], self.bm_buckets[new] = \
            self.bm_buckets[new], self.bm_buckets[idx]
        self._bm_touch(new)
        self.tree_bm.selection_set("bm%d" % new)

    def _bm_up(self):
        self._bm_move(-1)

    def _bm_down(self):
        self._bm_move(1)

    def _bm_reload(self, silent=False):
        if not silent and getattr(self, "bm_dirty", False):
            if not messagebox.askyesno("重新载入", "放弃未保存的改动，重新从配置文件载入？"):
                return
        self._bm_load_from_cfg()
        self._fill_manager()

    def _bm_save(self):
        ids = [str(b.get("id") or "") for b in self.bm_buckets]
        errs = []
        for i, b in enumerate(self.bm_buckets):
            others = [ids[j] for j in range(len(ids)) if j != i]
            errs += ["桶 %d（%s）：%s" % (i + 1, ids[i] or "?", e)
                     for e in validate_bucket(b, others)]
        if errs:
            messagebox.showerror("保存桶配置", "校验未通过：\n\n" + "\n".join(errs[:14]))
            return
        try:
            clean = [{k: v for k, v in b.items() if not str(k).startswith("_")}
                     for b in self.bm_buckets]
            backup = save_buckets(self.cfg, clean)
        except ConfigError as e:
            messagebox.showerror("保存桶配置", str(e))
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存桶配置", "写入失败：%s" % e)
            return
        self.bm_dirty = False
        self._rebuild_after_bucket_change()
        messagebox.showinfo("保存桶配置",
                            "已写回配置并重新载入。\n备份：%s" % backup)

    def _rebuild_after_bucket_change(self):
        """桶列表变化后：重载配置 + 重建运维总控/桶管理/走势图例 + 刷新。"""
        try:
            self.cfg = load_config(self.cfg.path)
        except ConfigError as e:
            messagebox.showerror("重新载入配置", str(e))
            return
        self.history.clear()
        self.last_st = self.last_pv = None
        self._bm_load_from_cfg()
        self._fill_manager()
        self._build_tab_manager()
        self._build_tab_ops()
        self._update_spark_legend()
        self.refresh()

    # ---------------- 桶编辑对话框 ----------------
    @staticmethod
    def _dg(d, path):
        cur = d
        for k in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    @staticmethod
    def _ds(d, path, val):
        ks = path.split(".")
        cur = d
        for k in ks[:-1]:
            nxt = cur.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[k] = nxt
            cur = nxt
        cur[ks[-1]] = val

    def _dlg_bucket(self, raw, is_new=False, other_ids=None):
        import copy
        top = tk.Toplevel(self.root)
        top.title("新增桶" if is_new else "编辑桶 · %s" % (raw.get("id") or ""))
        top.geometry("%dx%d" % (self.px(680), self.px(560)))
        top.minsize(self.px(520), self.px(420))
        top.transient(self.root)
        top.grab_set()
        ttk.Label(top, text=("填写后点「确定」只改内存；回到列表点「保存到配置」才写入文件"
                             "（保存前自动备份）。"),
                  foreground="#8d8d8d").pack(anchor="w", padx=self.px(10),
                                             pady=(self.px(8), 0))
        nb = ttk.Notebook(top)
        nb.pack(fill="both", expand=True, padx=self.px(10), pady=self.px(8))
        varz = {}
        mac_hint = {}
        for tab_title, spec in self.BM_FIELDS:
            fr = ttk.Frame(nb, padding=self.px(8))
            nb.add(fr, text=tab_title)
            for i, item in enumerate(spec):
                path, label, kind = item[0], item[1], item[2]
                val = self._dg(raw, path)
                if kind == "check":
                    var = tk.BooleanVar(value=bool(val))
                    w = ttk.Checkbutton(fr, variable=var)
                elif kind == "combo":
                    var = tk.StringVar(value=str(val or item[3][0]))
                    w = ttk.Combobox(fr, textvariable=var, values=list(item[3]),
                                     state="readonly", width=28)
                else:
                    var = tk.StringVar(value="" if val is None else str(val))
                    w = ttk.Entry(fr, textvariable=var, width=30)
                ttk.Label(fr, text=label, anchor="w").grid(row=i, column=0, sticky="w",
                                                           padx=(0, self.px(8)),
                                                           pady=self.px(3))
                w.grid(row=i, column=1, sticky="we", pady=self.px(3))
                varz[path] = (var, kind)
                if kind == "mac":
                    mac_hint[path] = ttk.Label(fr, text="", foreground="#666")
                    mac_hint[path].grid(row=i, column=2, sticky="w", padx=self.px(6))
                    var.trace_add("write", lambda *_a, p=path: self._mac_hint(varz, mac_hint, p))
            fr.columnconfigure(1, weight=1)
        self._mac_hint(varz, mac_hint, "mac")

        lbl_err = ttk.Label(top, text="", foreground="#c62828", wraplength=self.px(640),
                            justify="left")
        lbl_err.pack(anchor="w", padx=self.px(10))
        result = {"out": None}

        def ok(_e=None):
            out = copy.deepcopy(raw)
            for path, (var, kind) in varz.items():
                if kind == "check":
                    self._ds(out, path, bool(var.get()))
                else:
                    txt = var.get().strip()
                    if path.endswith((".port", ".tries", ".timeout")) and txt.isdigit():
                        self._ds(out, path, int(txt))
                    else:
                        self._ds(out, path, txt)
            if self._dg(out, "mac"):
                self._ds(out, "mac", normalize_mac(self._dg(out, "mac")))
            errs = validate_bucket(out, other_ids or [])
            if errs:
                lbl_err.config(text="；".join(errs))
                return
            result["out"] = out
            top.destroy()

        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", padx=self.px(10), pady=self.px(8))
        ttk.Button(btns, text="确定", command=ok, width=10).pack(side="right")
        ttk.Button(btns, text="取消", command=top.destroy, width=10).pack(side="right",
                                                                        padx=self.px(6))
        top.bind("<Escape>", lambda _e: top.destroy())
        self.root.wait_window(top)
        return result["out"]

    def _mac_hint(self, varz, mac_hint, path):
        lbl = mac_hint.get(path)
        if lbl is None:
            return
        var = varz[path][0]
        txt = var.get().strip()
        if not txt:
            lbl.config(text="（可留空）", foreground="#8d8d8d")
        elif is_unset(txt):
            lbl.config(text="占位符（未填写，保存不拦）", foreground="#8d8d8d")
        elif mac_ok(txt) and not mac_is_multicast(txt):
            lbl.config(text="✓ " + normalize_mac(txt), foreground="#1a8f3c")
        elif mac_is_multicast(txt):
            lbl.config(text="组播地址", foreground="#c62828")
        else:
            lbl.config(text="格式: XX:XX:XX:XX:XX:XX", foreground="#c62828")

    # ---------------- 改 MAC（改配置 + 可下发） ----------------
    def _bm_mac(self):
        idx = self._bm_pick("改 MAC")
        if idx is None:
            return
        self._dlg_mac(idx)

    def _sub_bucket(self, raw):
        """把原始桶定义包成 subs() 需要的最小结构（用于命令占位符替换）。"""
        return {
            "id": raw.get("id") or "",
            "account": raw.get("account") or "",
            "mac": raw.get("mac") or "",
            "speed_leg": raw.get("speed_leg") or raw.get("id") or "",
            "probe": raw.get("probe") or {},
            "renew": raw.get("renew") or {},
        }

    def _dlg_mac(self, idx):
        raw = self.bm_buckets[idx]
        bid = raw.get("id") or "?"
        probe = raw.get("probe") or {}
        renew = raw.get("renew") or {}
        top = tk.Toplevel(self.root)
        top.title("改 MAC · %s" % bid)
        top.geometry("%dx%d" % (self.px(560), self.px(330)))
        top.resizable(False, False)
        top.transient(self.root)
        top.grab_set()
        ttk.Label(top, text=(
            "两件事分开做：\n"
            "  ① 改配置字段 —— 影响状态匹配、白名单/在线会话比对与显示；\n"
            "  ② 下发到接口 —— 按 actions.set_mac 执行命令（会短暂断链），并回读校验。"),
            justify="left").pack(anchor="w", padx=self.px(10), pady=(self.px(8), self.px(4)))
        row = ttk.Frame(top)
        row.pack(fill="x", padx=self.px(10))
        ttk.Label(row, text="当前 MAC:").pack(side="left")
        ttk.Label(row, text=raw.get("mac") or "（未设置）",
                  foreground="#666").pack(side="left", padx=self.px(6))
        row2 = ttk.Frame(top)
        row2.pack(fill="x", padx=self.px(10), pady=(self.px(6), 0))
        ttk.Label(row2, text="新 MAC:", width=10, anchor="w").pack(side="left")
        var_mac = tk.StringVar(value=raw.get("mac") or "")
        ttk.Entry(row2, textvariable=var_mac, width=22).pack(side="left")
        lbl_chk = ttk.Label(row2, text="", foreground="#666")
        lbl_chk.pack(side="left", padx=self.px(6))
        row3 = ttk.Frame(top)
        row3.pack(fill="x", padx=self.px(10), pady=(self.px(6), 0))
        ttk.Label(row3, text="目标接口:", width=10, anchor="w").pack(side="left")
        var_iface = tk.StringVar(value=probe.get("iface") or renew.get("iface") or "")
        ttk.Entry(row3, textvariable=var_iface, width=22).pack(side="left")
        ttk.Label(row3, text="（下发时用；远端接口写 ethX）",
                  foreground="#8d8d8d").pack(side="left", padx=self.px(6))
        var_save = tk.BooleanVar(value=True)
        var_apply = tk.BooleanVar(value=bool(self.cfg.action("set_mac")))
        ttk.Checkbutton(top, text="保存到配置字段", variable=var_save).pack(anchor="w",
                                                                        padx=self.px(10),
                                                                        pady=(self.px(8), 0))
        cb = ttk.Checkbutton(top, text="保存后立即下发到接口（执行 actions.set_mac）",
                             variable=var_apply)
        cb.pack(anchor="w", padx=self.px(10))
        if not self.cfg.action("set_mac"):
            cb.state(["disabled"])
        lbl_msg = ttk.Label(top, text="", foreground="#c62828", wraplength=self.px(520),
                            justify="left")
        lbl_msg.pack(anchor="w", padx=self.px(10))

        def upd(*_a):
            txt = var_mac.get().strip()
            if not txt:
                lbl_chk.config(text="（留空 = 不设 MAC）", foreground="#8d8d8d")
            elif is_unset(txt):
                lbl_chk.config(text="占位符（视为未填写）", foreground="#8d8d8d")
            elif mac_ok(txt) and not mac_is_multicast(txt):
                lbl_chk.config(text="✓ " + normalize_mac(txt), foreground="#1a8f3c")
            elif mac_is_multicast(txt):
                lbl_chk.config(text="组播地址，不可用", foreground="#c62828")
            else:
                lbl_chk.config(text="格式: XX:XX:XX:XX:XX:XX", foreground="#c62828")
        var_mac.trace_add("write", upd)
        upd()

        def go(_e=None):
            txt = var_mac.get().strip()
            if is_unset(txt):
                txt = ""                      # 占位符一律视为「未填写」
            new_mac = normalize_mac(txt) if txt else ""
            if txt and not new_mac:
                lbl_msg.config(text="MAC 格式不合法：XX:XX:XX:XX:XX:XX")
                return
            if new_mac and mac_is_multicast(new_mac):
                lbl_msg.config(text="该 MAC 是组播地址，不能作为网卡地址")
                return
            iface = var_iface.get().strip()
            if var_save.get():
                raw["mac"] = new_mac
                self._bm_touch(idx)
            do_apply = bool(var_apply.get())
            top.destroy()
            if do_apply:
                self._apply_mac(bid, new_mac, iface, raw)
            else:
                messagebox.showinfo("改 MAC",
                                    "已更新内存中的 MAC（%s）。\n回到列表点「保存到配置」写入文件。"
                                    % (new_mac or "（留空）"))

        btns = ttk.Frame(top)
        btns.pack(side="bottom", fill="x", padx=self.px(10), pady=self.px(8))
        ttk.Button(btns, text="确定", command=go, width=10).pack(side="right")
        ttk.Button(btns, text="取消", command=top.destroy, width=10).pack(side="right",
                                                                        padx=self.px(6))
        top.bind("<Return>", go)
        top.bind("<Escape>", lambda _e: top.destroy())
        self.root.wait_window(top)

    def _apply_mac(self, bid, mac, iface, raw):
        """按 actions.set_mac 下发 MAC 并回读校验。"""
        a = self.cfg.action("set_mac")
        if not a:
            messagebox.showinfo("下发 MAC", (
                "未配置 actions.set_mac，无法下发。\n\n"
                "在配置文件的 actions 里加一段（示例，按你的环境改）：\n"
                "  \"set_mac\": {\n"
                "    \"argv\": [\"ssh\", \"root@<OpenWrt管理IP>\",\n"
                "              \"ifconfig {iface} down; ifconfig {iface} hw ether {mac};"
                " ifconfig {iface} up\"],\n"
                "    \"timeout\": 60\n"
                "  }\n"
                "占位符：{mac} {iface} {bucket} {wan}"))
            return
        if not mac:
            if not messagebox.askyesno("下发 MAC", "新 MAC 为空，仍要执行下发命令吗？"):
                return
        if not iface and not messagebox.askyesno(
                "下发 MAC", "没有填目标接口，仍要执行吗？"):
            return
        if not messagebox.askyesno("下发 MAC", a.get("confirm") or
                                   "将把 MAC %s 下发到接口 %s（会短暂断链）。继续？"
                                   % (mac or "(空)", iface or "(未填)")):
            return
        argv = self.cfg.argv_for("set_mac", self._sub_bucket(raw), mac=mac, iface=iface)
        if not argv:
            messagebox.showerror("下发 MAC", "命令拼装失败（检查 actions.set_mac）。")
            return
        self.lbl_foot.config(text="下发 MAC 中…", fg="#e08600")

        def f():
            if isinstance(argv, str):
                rc, out = run(argv, timeout=a["timeout"], cwd=a["cwd"] or None, shell=True)
            else:
                rc, out = run(argv, timeout=a["timeout"], cwd=a["cwd"] or None)
            head = "命令: %s\n" % (" ".join(argv) if isinstance(argv, list) else argv)
            self.root.after(0, lambda: self._show_result(
                "下发 MAC · %s" % bid, head + (out or "(无输出)") +
                ("\n(rc=%s)" % rc if rc not in (0, None) else "")))
            if rc == 0 and PB is not None:
                b = next((x for x in self.cfg.buckets if x["id"] == bid), None)
                if b is not None:
                    r = PB.probe_bucket(self.cfg, b)
                    self.root.after(0, lambda: messagebox.showinfo(
                        "下发 MAC", "命令已执行。回读探测：%s（%s）"
                        % (r["state"], STATUS_TEXT.get(r["state"], r["state"]))))
        self._run_bg(f)

    # ================= 运维总控 =================
    def _build_tab_ops(self, f=None):
        f = f or self._tab("ops")
        for w in f.winfo_children():      # 桶列表变化后支持原地重建
            w.destroy()
        self._ops_scroll = tk.Canvas(f, highlightthickness=0)
        sb = ttk.Scrollbar(f, orient="vertical", command=self._ops_scroll.yview)
        inner = ttk.Frame(self._ops_scroll, padding=self.px(4))
        inner.bind("<Configure>",
                   lambda _e: self._ops_scroll.configure(scrollregion=self._ops_scroll.bbox("all")))
        _win = self._ops_scroll.create_window((0, 0), window=inner, anchor="nw")
        self._ops_scroll.configure(yscrollcommand=sb.set)
        self._ops_scroll.bind("<Configure>",
                              lambda e: self._ops_scroll.itemconfigure(_win, width=e.width))
        sb.pack(side="right", fill="y")
        self._ops_scroll.pack(side="left", fill="both", expand=True)
        self.ops_inner = inner
        self.ops_task_state = {}
        self.ops_bucket_state = {}

        def sec(text):
            lf = ttk.LabelFrame(inner, text=text)
            lf.pack(fill="x", pady=(self.px(2), self.px(4)))
            return lf

        # ① 每桶快捷操作
        fA = sec("① 每桶快捷操作（探测 / 判定 / 续连 / 测速 —— 克隆腿受「真机闸」约束）")
        for b in self.cfg.buckets:
            row = ttk.Frame(fA)
            row.pack(fill="x", padx=self.px(2), pady=self.px(1))
            ttk.Label(row, text="%s  %s" % (b["id"], b["account"] or "—"), width=18,
                      anchor="w").pack(side="left")
            st = ttk.Label(row, text="—", width=10, anchor="w", foreground="#666")
            st.pack(side="left")
            for btext, cb in (("详情", lambda x=b: self._bucket_detail(x)),
                              ("204探测", lambda x=b: self._act_op_probe(x)),
                              ("刷新真机", lambda x=b: self._act_op_real(x)),
                              ("续连/恢复", lambda x=b: self._act_op_renew(x)),
                              ("单桶测速", lambda x=b: self._act_op_speed(x))):
                ttk.Button(row, text=btext, width=9,
                           command=cb).pack(side="left", padx=self.px(1))
            ToolTip(st, self._op_row_tip(b))
            self.ops_bucket_state[b["id"]] = st

        # ② 聚合出口（本机）
        fB = sec("② 聚合出口（本机 · 多会话出口合并 / TUN 接管）")
        rowB = ttk.Frame(fB)
        rowB.pack(fill="x", padx=self.px(2), pady=self.px(2))
        self.ops_lbl_agg = ttk.Label(rowB, text="—", width=34, anchor="w")
        self.ops_lbl_agg.pack(side="left")
        self._ops_btn(rowB, "重启聚合", "restart_aggregator", lambda: self.act_restart_aggregator())
        self._ops_btn(rowB, "刷新节点源", "refresh_provider",
                      lambda: self._run_named_action("refresh_provider", "刷新节点源",
                                                     follow="完成。请点「重启聚合」使其生效"
                                                            "（网络将短暂抖动）。"))
        self._ops_btn(rowB, "打开目录", "open_aggregator_dir",
                      lambda: self._act_open_aggregator_dir(),
                      available=bool(self.cfg.aggregation.get("dir")))
        rowB2 = ttk.Frame(fB)
        rowB2.pack(fill="x", padx=self.px(2), pady=(0, self.px(2)))
        self.ops_lbl_proxy = ttk.Label(rowB2, text="代理:?", width=20, anchor="w")
        self.ops_lbl_proxy.pack(side="left")
        ttk.Label(rowB2, text="「暂停代理」= 聚合出口切直连（不经 TUN/节点，进程保留）；"
                              "再点「恢复代理」切回规则。",
                  foreground="#8d8d8d").pack(side="left")
        # 节点源地址：填了之后简单界面的 VPN 开关才可用
        rowB3 = ttk.Frame(fB)
        rowB3.pack(fill="x", padx=self.px(2), pady=(0, self.px(2)))
        ttk.Label(rowB3, text="节点源地址:", width=12, anchor="w").pack(side="left")
        self.var_provider_url = tk.StringVar(
            value=self.cfg.aggregation.get("provider_url") or "")
        self._provider_visible = False
        self.ent_provider_url = ttk.Entry(rowB3, textvariable=self.var_provider_url,
                                          width=54, show="*")
        self.ent_provider_url.pack(side="left", padx=self.px(2))
        self.btn_provider_show = ttk.Button(rowB3, text="显示", width=6,
                                            command=self._toggle_provider_visible)
        self.btn_provider_show.pack(side="left", padx=self.px(1))
        ttk.Button(rowB3, text="保存", width=6,
                   command=self._act_save_provider_url).pack(side="left", padx=self.px(1))
        ttk.Label(fB, text="填了之后：简单界面的「开启/关闭 VPN 代理」才可用；"
                           "刷新节点源的命令里可用 {provider_url} 占位符。",
                  foreground="#8d8d8d").pack(anchor="w", padx=self.px(4))

        # ③ 虚拟路由（可选）
        if self.cfg.vm.get("enabled"):
            fC = sec("③ %s（承载若干克隆腿 / 内部服务）" % self.cfg.vm.get("label"))
            rowC = ttk.Frame(fC)
            rowC.pack(fill="x", padx=self.px(2), pady=self.px(2))
            self.ops_lbl_vm = ttk.Label(rowC, text="—", width=34, anchor="w")
            self.ops_lbl_vm.pack(side="left")
            self._ops_btn(rowC, "开机", "vm_power_on",
                          lambda: self._run_named_action("vm_power_on", "开机"))
            self._ops_btn(rowC, "关机", "vm_power_off",
                          lambda: self._run_named_action("vm_power_off", "关机", confirm=True))
            self._ops_btn(rowC, "状态详情", "vm_status",
                          lambda: self._run_named_action("vm_status", "状态详情"))
            if self.cfg.vm.get("note"):
                ttk.Label(fC, text=self.cfg.vm["note"], foreground="#8d8d8d",
                          wraplength=self.px(880)).pack(anchor="w", padx=self.px(4))

        # ④ 计划任务
        if self.cfg.tasks:
            fT = sec("④ 计划任务（状态 / 立即运行 / 启停 · 建议以 UI 心跳为门控）")
            for t in self.cfg.tasks:
                rowT = ttk.Frame(fT)
                rowT.pack(fill="x", padx=self.px(2), pady=self.px(1))
                ttk.Label(rowT, text=t["name"], width=22, anchor="w").pack(side="left")
                lbl = ttk.Label(rowT, text="—", width=12, anchor="w")
                lbl.pack(side="left")
                ttk.Label(rowT, text=t["desc"], anchor="w").pack(side="left", fill="x",
                                                                expand=True)
                ttk.Button(rowT, text="立即运行", width=8,
                           command=lambda x=t["name"]: self._act_task_run(x)
                           ).pack(side="right", padx=self.px(1))
                ttk.Button(rowT, text="启停", width=6,
                           command=lambda x=t["name"]: self._act_task_toggle(x)
                           ).pack(side="right", padx=self.px(1))
                self.ops_task_state[t["name"]] = lbl

        # ⑤ 门户凭据
        fF = sec("⑤ 门户凭据（真机判定可见性 · 会话查询只认凭据属主）")
        ttk.Label(fF, wraplength=self.px(920), justify="left", foreground="#666", text=(
            "门户的会话查询接口通常**只返回凭据属主自己的会话**——只有自己账号的全局凭据时，"
            "别人的克隆腿必然判定为「未知」（真机闸默认拦截）。\n"
            "要让判定看清某账号真机是否在线，需由**该账号本人真实登录门户**后，"
            "从浏览器开发者工具复制请求头里的凭据值，在此粘贴为该账号的「专属凭据」。\n"
            "⚠️ 不要用凭据库里的密码去代取：以他人账号登录会占用/踢掉其会话，违背互顶纪律。"
        )).pack(anchor="w", padx=self.px(4), pady=self.px(2))
        self.ops_tok_state = {}
        for acct in self._tok_accounts():
            rowTk = ttk.Frame(fF)
            rowTk.pack(fill="x", padx=self.px(2), pady=self.px(1))
            ttk.Label(rowTk, text=acct, width=24, anchor="w").pack(side="left")
            lblT = ttk.Label(rowTk, text="—", width=30, anchor="w")
            lblT.pack(side="left")
            ttk.Button(rowTk, text="粘贴/更新", width=9,
                       command=lambda a=acct: self._act_tok_paste(a)
                       ).pack(side="right", padx=self.px(1))
            ttk.Button(rowTk, text="清除", width=6,
                       command=lambda a=acct: self._act_tok_clear(a)
                       ).pack(side="right", padx=self.px(1))
            self.ops_tok_state[acct] = lblT
        ttk.Button(fF, text="立即重刷门户", width=16,
                   command=self.act_refresh_portal).pack(anchor="w", padx=self.px(2),
                                                         pady=self.px(2))

        # ⑥ 纪律提示
        fZ = ttk.LabelFrame(inner, text="⑥ 纪律提示（互顶闸 · 生命周期）")
        ttk.Label(fZ, justify="left", anchor="w", wraplength=self.px(900), text=(
            "· 克隆腿续连前必须先看「真机判定」：目标真机在线 → 拦截；判定不了 → 也拦截（不主动撞人）。\n"
            "· 关停承载克隆腿的虚拟路由，会同时断掉该机上的所有腿（重新开机 + 续约后自愈）。\n"
            "· 重启聚合出口 / 刷新节点源会导致整机网络短暂抖动；测速有流量消耗。\n"
            "· 生命周期：本控制台 = 总开关。退出 = 全链停（按配置执行 stop_chain）；"
            "启动会自动拉起；UI 不在则配套任务停手。"
        )).pack(fill="x", padx=self.px(4), pady=self.px(3))
        bind_mousewheel(inner, self._ops_scroll)

    def _ops_btn(self, parent, text, action, cb, width=12, available=None):
        """只在动作已配置（或 available 显式为真）时创建按钮；否则给一行灰色说明。"""
        if self.cfg.has_action(action) or available:
            ttk.Button(parent, text=text, width=width, command=cb).pack(side="left",
                                                                       padx=self.px(2))
        else:
            ttk.Label(parent, text="%s：未配置" % text, foreground="#8d8d8d"
                      ).pack(side="left", padx=self.px(2))

    def _op_row_tip(self, bucket):
        def tip():
            st = self.last_st or {}
            val = st.get(bucket["state_key"], "?")
            pv = self.last_pv
            real = self._real_for(bucket, pv) if pv else "—"
            egress = {"local": "本机直连", "socks": "SOCKS 出口",
                      "ssh": "远端接口"}.get(bucket["probe"]["mode"], bucket["probe"]["mode"])
            return ("桶 %s %s\n当前状态=%s  真机=%s\n账号=%s  探测=%s\n"
                    "提示：204探测=只读 ｜ 续连/恢复按桶走真机闸或二次确认"
                    % (bucket["id"], bucket["desc"], STATUS_TEXT.get(val, val), real,
                       bucket["account"] or "—", egress))
        return tip

    def _set_ops_bucket_state(self):
        st = self.last_st or {}
        for b in self.cfg.buckets:
            lbl = self.ops_bucket_state.get(b["id"])
            if not lbl:
                continue
            val = st.get(b["state_key"], "?")
            lbl.config(text=STATUS_TEXT.get(val, val), foreground=COLORS.get(str(val), "#333"))

    def _fill_ops(self):
        self._set_ops_bucket_state()
        self._refresh_tok_states()
        st = self.last_st or {}
        agg_out = st.get("aggregator", st.get("mihomo", "?"))
        agg_api = st.get("aggregator_api", st.get("mih_api", "?"))
        try:
            self.ops_lbl_agg.config(text="出口=%s 控制面=%s"
                                    % (STATUS_TEXT.get(agg_out, agg_out), agg_api),
                                    foreground=COLORS.get(str(agg_api), "#333"))
        except Exception:
            pass
        if hasattr(self, "ops_lbl_vm"):
            clones = [b for b in self.cfg.buckets if b["kind"] == "clone"]
            txt = " ".join("%s=%s" % (b["id"], STATUS_TEXT.get(
                st.get(b["state_key"], "?"), st.get(b["state_key"], "?"))) for b in clones)
            try:
                self.ops_lbl_vm.config(text=txt or "—")
            except Exception:
                pass
        for t in self.cfg.tasks:
            lbl = self.ops_task_state.get(t["name"])
            if not lbl:
                continue
            s = task_state(t["name"])
            col = "#1a8f3c" if s == "Ready" else ("#e08600" if s == "Running"
                                                  else ("#8d8d8d" if s in ("Disabled", "不适用")
                                                        else "#c62828"))
            lbl.config(text=s, foreground=col)

    # ---------------- 运维动作实现 ----------------
    def _show_result(self, title, text, refresh=True):
        show_result(self.root, self.cfg, title, text,
                    after=(self.refresh if refresh else None), scale=self.scale)

    def _toggle_provider_visible(self):
        self._provider_visible = not self._provider_visible
        try:
            self.ent_provider_url.config(show="" if self._provider_visible else "*")
            self.btn_provider_show.config(text="隐藏" if self._provider_visible else "显示")
        except Exception:
            pass

    def _act_save_provider_url(self):
        url = self.var_provider_url.get().strip()
        if url and not url.startswith(("http://", "https://")):
            if not messagebox.askyesno("保存节点源地址",
                                       "地址看起来不是 http(s) 链接，仍要保存？"):
                return
        try:
            backup = save_config_keys(self.cfg, {"aggregation.provider_url": url})
        except ConfigError as e:
            messagebox.showerror("保存节点源地址", str(e))
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存节点源地址", "写入失败：%s" % e)
            return
        try:
            self.cfg = load_config(self.cfg.path)   # 重新载入，让 {provider_url} 生效
        except ConfigError:
            pass
        messagebox.showinfo("保存节点源地址", "已写回配置。\n备份：%s" % backup)
        self.refresh()

    def _ops_env(self, **kw):
        env = dict(os.environ)
        env.update({k: str(v) for k, v in kw.items()})
        return env

    def _run_action(self, name, bucket=None, env=None, **subs):
        """执行配置里的动作，返回 (rc, 输出)。未配置 → (None, 提示文本)。"""
        return exec_action(self.cfg, name, bucket, env, **subs)

    def _run_named_action(self, name, title, confirm=False, follow=None, bucket=None):
        a = self.cfg.action(name)
        if not a:
            messagebox.showinfo(title, "未配置动作 %s（见配置文件 actions）。" % name)
            return
        if confirm or a.get("confirm"):
            if not messagebox.askyesno(title, a.get("confirm") or "确认执行「%s」？" % title):
                return
        self.lbl_foot.config(text="%s 中…" % title, fg="#e08600")

        def f():
            rc, out = self._run_action(name, bucket)
            self.root.after(0, lambda: self._show_result(title, out))
            if follow and rc == 0:
                self.root.after(0, lambda: messagebox.showinfo(title, follow))
        self._run_bg(f)

    def _act_open_aggregator_dir(self):
        d = self.cfg.aggregation.get("dir")
        if d and open_path(d):
            return
        a = self.cfg.action("open_aggregator_dir")
        if a:
            self._run_named_action("open_aggregator_dir", "打开目录")
            return
        if not open_path(self.cfg.data_dir):
            messagebox.showinfo("打开目录", "未配置聚合出口目录（aggregation.dir）。")

    def _act_op_probe(self, bucket):
        """204 探测：只读，识别门户劫持。"""
        if PB is None:
            messagebox.showerror("204 探测", "缺少探测模块 probe_buckets.py：%s" % PB_ERR)
            return
        self.lbl_foot.config(text="探测 %s 中…" % bucket["id"], fg="#e08600")

        def f():
            r = PB.probe_bucket(self.cfg, bucket)
            lines = []
            if r["codes"]:
                lines.append("探活 %d 次: %s" % (len(r["codes"]), ", ".join(r["codes"])))
            if r.get("detail"):
                lines.append(r["detail"])
            lines.append("判定: %s（%s）" % (r["state"], STATUS_TEXT.get(r["state"], r["state"])))
            if r.get("http_loss") is not None:
                lines.append("HTTP 丢包率: %d%%（%d/%d 次未拿到 204）"
                             % (r["http_loss"], r["codes"].count("204") if r["codes"] else 0,
                                len(r["codes"])))
            if r.get("icmp_loss") is not None:
                lines.append("ICMP 丢包率: %d%%" % r["icmp_loss"])
                if r.get("icmp_note"):
                    lines.append("  " + r["icmp_note"])
            elif r.get("icmp_note"):
                lines.append("ICMP 丢包率: 未测（%s）" % r["icmp_note"])
            if r["portal"]:
                lines.append("（流量被门户劫持：需先完成认证 / 补齐免认证绑定）")
            if r["body"]:
                lines.append("\n响应体片段:\n" + r["body"])
            self.root.after(0, lambda: self._show_result("探测 %s" % bucket["id"],
                                                         "\n".join(lines)))
        self._run_bg(f)

    def _act_op_real(self, bucket):
        """刷新门户并显示该桶的真机判定（克隆腿的关键闸）。"""
        if not self.cfg.has_action("refresh_portal"):
            messagebox.showinfo("刷新真机判定",
                                "未配置 actions.refresh_portal：请指向你的门户查询脚本。")
            return
        self.lbl_foot.config(text="刷新门户判定（%s）…" % bucket["id"], fg="#e08600")

        def f():
            rc, out = self._run_action("refresh_portal")
            pv = load_json(self.cfg.portal_file) if self.cfg.portal_file else None
            lines = [out or ""]
            if not pv:
                lines.append("门户数据不可读: %s" % self.cfg.portal_file)
            else:
                lines.append("凭据可用=%s  属主=%s"
                             % (pv.get("tokenOk"), pv.get(self.cfg.portal["owner_field"], "?")))
                if bucket["kind"] == "clone":
                    t = self._clone_target(bucket, pv)
                    if t:
                        lines.append(json.dumps(t, ensure_ascii=False, indent=2))
                    else:
                        lines.append("无 %s 判定（无该账号凭据 → 弱信号，续连会被拦截）"
                                     % bucket["clone"]["portal_id"])
                else:
                    lines.append("%s 判定: %s" % (bucket["id"], self._real_for(bucket, pv)))
            self.root.after(0, lambda: self._show_result("门户判定 · %s" % bucket["id"],
                                                         "\n".join(x for x in lines if x)))
        self._run_bg(f)

    def _real_gate(self, bucket):
        """真机闸：刷新门户后判定。返回 (ok, reason)。"""
        rc, out = self._run_action("refresh_portal")
        pv = load_json(self.cfg.portal_file) if self.cfg.portal_file else None
        label = bucket["clone"].get("owner_label") or bucket["account"] or bucket["id"]
        if not pv:
            return False, "门户数据不可读，已取消"
        t = self._clone_target(bucket, pv)
        if t is None:
            return False, "无该账号的判定数据（缺其专属凭据？）。按互顶纪律拦截。"
        if t.get("realOnline") is True:
            return False, "%s 真机在线！已拦截（避免互顶）" % label
        if t.get("realOnline") is False:
            return True, ""
        return False, ("无法验证 %s 真机状态（无该账号凭据 / 未知）。按纪律拦截；"
                       "确需强拉请先人工确认真机离线。" % label)

    def _act_op_renew(self, bucket):
        """按桶恢复：ssh_ifup（可带真机闸）/ 自定义命令 / 无需手动。"""
        renew = bucket["renew"]
        mode = renew["mode"]
        if mode == "none":
            messagebox.showinfo("续连 · %s" % bucket["id"],
                                renew.get("hint") or
                                "该桶未配置续连动作（renew.mode=none）。\n"
                                "如怀疑已掉线：点「204探测」确认，或点「重新巡检」。")
            return
        if mode == "command":
            self._run_named_action("renew_bucket", "续连 · %s" % bucket["id"],
                                   confirm=True, bucket=bucket)
            return
        # ssh_ifup
        if not renew["ssh"].get("host"):
            messagebox.showinfo("续连 · %s" % bucket["id"], "续连缺少 renew.ssh.host 配置。")
            return
        if renew.get("gate") and bucket["kind"] == "clone":
            if not self.cfg.has_action("refresh_portal"):
                messagebox.showwarning("续连 · %s（已拦截）" % bucket["id"],
                                       "需要「真机闸」但未配置 actions.refresh_portal，"
                                       "无法验证真机状态 → 按纪律拦截。")
                return
            self.lbl_foot.config(text="安全续 %s：先刷真机判定…" % bucket["id"], fg="#e08600")

            def f():
                ok, reason = self._real_gate(bucket)
                if not ok:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "续 %s（已拦截）" % bucket["id"], reason))
                    return
                self.root.after(0, lambda: self._do_ifup(bucket))
            self._run_bg(f)
            return
        self._do_ifup(bucket)

    def _do_ifup(self, bucket):
        renew = bucket["renew"]
        wan, iface = renew.get("wan") or "", renew.get("iface") or ""
        label = renew.get("owner_label") or bucket["account"] or bucket["id"]
        extra = ""
        if bucket["kind"] == "clone":
            extra = "\n（该桶是克隆腿：闸通过代表目标真机离线；仍请确认不是误判）"
        if not messagebox.askyesno(
                "续连 · %s" % bucket["id"],
                "将对 %s 执行 ifup %s%s 续约。%s\n继续？"
                % (renew["ssh"].get("host"), wan or "(未配置 wan)",
                   ("（%s）" % iface) if iface else "", extra)):
            return
        if PB is None:
            messagebox.showerror("续连", "缺少 probe_buckets.py（提供 ssh 执行能力）")
            return

        def f():
            cmd = "ifup %s 2>/dev/null; sleep 6; ip -4 addr show %s 2>/dev/null | grep -o 'inet [0-9.]*' | head -1" % (wan, iface)
            rc, out = run(PB.ssh_argv(renew["ssh"], cmd),
                          timeout=int(renew["ssh"].get("timeout") or 20) + 20)
            txt = out or "%s 未取到新 IP（可能已被拒绝）" % iface
            self.root.after(0, lambda: self._show_result("续 %s 结果" % bucket["id"], txt))
        self._run_bg(f)

    def _act_op_speed(self, bucket):
        if not self.cfg.has_action("speedtest"):
            messagebox.showinfo("单桶测速", "未配置 actions.speedtest。")
            return
        self._run_speedtest(bucket=bucket,
                            title="单桶测速 · %s" % bucket["id"])

    def _run_speedtest(self, bucket=None, title="分桶测速"):
        a = self.cfg.action("speedtest")
        if not a:
            messagebox.showinfo(title, "未配置 actions.speedtest（见配置文件）。")
            return
        self.lbl_foot.config(text="%s 中…" % title, fg="#e08600")

        def f():
            rc, out = self._run_action("speedtest", bucket)
            self.root.after(0, lambda: self._show_result("%s 结果" % title, out))
        self._run_bg(f)

    # ---------------- 计划任务 ----------------
    def _act_task_run(self, tn):
        if not IS_WIN:
            messagebox.showinfo("计划任务", "当前平台不是 Windows，请在配置里改用动作命令。")
            return

        def f():
            rc, out = run(["schtasks.exe", "/Run", "/TN", tn], timeout=20)
            msg = "已触发。" if rc == 0 else (out or "失败 rc=%s（需要管理员/任务不存在？）" % rc)
            self.root.after(0, lambda: messagebox.showinfo("任务 %s" % tn,
                                                           "%s\n%s" % (msg, out)
                                                           if rc != 0 else msg))
        self._run_bg(f)

    def _act_task_toggle(self, tn):
        if not IS_WIN:
            messagebox.showinfo("计划任务", "当前平台不是 Windows，请在配置里改用动作命令。")
            return
        state = task_state(tn)
        if state == "不存在":
            messagebox.showerror("计划任务", "%s 不存在，无法切换。" % tn)
            return
        want_disable = state != "Disabled"
        verb = "停用" if want_disable else "启用"
        if not messagebox.askyesno("%s任务" % verb, "%s：当前状态 %s。确认%s？" % (tn, state, verb)):
            return

        def f():
            rc, out = run(["schtasks.exe", "/Change", "/TN", tn,
                           "/DISABLE" if want_disable else "/ENABLE"], timeout=20)
            msg = "已%s。" % verb if rc == 0 else (out or "失败 rc=%s（权限不足："
                                                  "请以管理员身份运行本控制台）" % rc)
            self.root.after(0, lambda: messagebox.showinfo("%s任务 %s" % (verb, tn), msg))
            self.root.after(0, self._fill_ops)
        self._run_bg(f)

    # ---------------- 门户凭据管理 ----------------
    def _tok_file(self, acct):
        pat = self.cfg.portal.get("per_account_pattern") or "portal_tok_{account}.txt"
        return os.path.join(self.cfg.portal["tok_dir"], pat.format(account=acct))

    def _tok_accounts(self):
        accts = list(self.cfg.portal.get("accounts") or [])
        for b in self.cfg.buckets:
            if b.get("account") and b["account"] not in accts:
                accts.append(b["account"])
        pv = self.last_pv
        if pv:
            for a in (pv.get("accounts") or []):
                u = a.get("username")
                if u and u not in accts:
                    accts.append(u)
            for t in (pv.get("cloneTargets") or []):
                u = t.get("account")
                if u and u not in accts:
                    accts.append(u)
        return accts

    def _tok_state_text(self, acct):
        per = self._tok_file(acct)
        if os.path.exists(per):
            try:
                n = os.path.getsize(per)
            except Exception:
                n = 0
            return "专属凭据（%s B）" % n, "#1a8f3c"
        g = self.cfg.portal.get("global_tok_file")
        if g and os.path.exists(g):
            try:
                n = os.path.getsize(g)
            except Exception:
                n = 0
            first = self.cfg.portal.get("accounts") or []
            if first and acct == first[0]:
                return "全局凭据（%s B）" % n, "#e08600"
            return "无专属（只有全局凭据）", "#c62828"
        return "无凭据（判定全未知）", "#c62828"

    def _refresh_tok_states(self):
        for acct, lbl in getattr(self, "ops_tok_state", {}).items():
            try:
                text, col = self._tok_state_text(acct)
                lbl.config(text=text, foreground=col)
            except Exception:
                pass

    def _act_tok_paste(self, acct):
        top = tk.Toplevel(self.root)
        top.title("粘贴门户凭据 · %s" % acct)
        top.geometry("%dx%d" % (self.px(640), self.px(300)))
        top.transient(self.root)
        top.grab_set()
        ttk.Label(top, text=(
            "把账号 %s 在门户真实登录后，从开发者工具→网络→任意接口请求头里复制的凭据值粘到下面。\n"
            "仅供门户查询脚本只读查询会话列表（不登录、不改任何状态）。" % acct),
            justify="left", wraplength=self.px(600)).pack(anchor="w", padx=self.px(10),
                                                          pady=self.px(6))
        txt = tk.Text(top, height=6, wrap="none", font=(self.cfg.ui["mono_font"], 9))
        txt.pack(fill="both", expand=True, padx=self.px(10))
        st = tk.Label(top, text="", fg="#c62828")
        st.pack(anchor="w", padx=self.px(10))
        result = {"ok": False}

        def ok(_e=None):
            val = txt.get("1.0", "end").strip()
            if not val:
                st.config(text="不能为空")
                return
            if "\n" in val or " " in val or len(val) < 8:
                st.config(text="看起来不像凭据值（不应含空格/换行，长度 ≥8）")
                return
            try:
                os.makedirs(self.cfg.portal["tok_dir"], exist_ok=True)
                with open(self._tok_file(acct), "w", encoding="ascii") as f:
                    f.write(val)
            except Exception as ex:  # noqa: BLE001
                messagebox.showerror("粘贴凭据", str(ex), parent=top)
                return
            result["ok"] = True
            top.destroy()

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=self.px(10), pady=self.px(6))
        ttk.Button(btns, text="保存", command=ok, width=10).pack(side="right")
        ttk.Button(btns, text="取消", command=top.destroy, width=10).pack(side="right",
                                                                        padx=self.px(6))
        top.bind("<Control-Return>", ok)
        top.bind("<Escape>", lambda _e: top.destroy())
        txt.focus_set()
        self.root.wait_window(top)
        self._refresh_tok_states()
        if result["ok"] and messagebox.askyesno("粘贴凭据",
                                                "%s 专属凭据已保存。立即重刷门户看判定变化？" % acct):
            self.act_refresh_portal()

    def _act_tok_clear(self, acct):
        per = self._tok_file(acct)
        if not os.path.exists(per):
            messagebox.showinfo("清除凭据", "%s 无专属凭据文件。" % acct)
            return
        if not messagebox.askyesno("清除凭据",
                                   "删除 %s 的专属凭据文件？\n删除后该账号判定退回「未知」"
                                   "（真机闸拦截）。" % acct):
            return
        try:
            os.remove(per)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("清除凭据", str(e))
            return
        self._refresh_tok_states()
        messagebox.showinfo("清除凭据", "已删除 %s 专属凭据。" % acct)

    # ================= 后台执行 =================
    def _run_bg(self, fn, done=None):
        def w():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                try:
                    self.root.after(0, lambda: self.lbl_foot.config(
                        text="后台任务异常: %s" % e, fg="#c62828"))
                except Exception:
                    pass
            finally:
                if done:
                    try:
                        self.root.after(0, done)
                    except Exception:
                        pass
        threading.Thread(target=w, daemon=True).start()

    def _goto_log(self):
        self.nb.select(self.tab_index["log"])
        self.root.deiconify()
        self._fill_log()

    # ================= 顶部按钮动作 =================
    def act_refresh(self):
        self.refresh()

    def act_refresh_state(self):
        if not self.cfg.has_action("refresh_state"):
            messagebox.showinfo("重新巡检", "未配置 actions.refresh_state：\n"
                                            "指向你的状态生产脚本（可用 probe_buckets.py）。")
            return
        self.lbl_foot.config(text="重新巡检中…", fg="#e08600")

        def f():
            rc, out = self._run_action("refresh_state")
            self._log_bind("refresh_state rc=%s" % rc)
            self.root.after(0, self.refresh)
            if rc not in (0, None):
                self.root.after(0, lambda: self._show_result("重新巡检", out, refresh=False))
        self._run_bg(f)

    def act_refresh_portal(self):
        if not self.cfg.has_action("refresh_portal"):
            messagebox.showinfo("门户刷新", "未配置 actions.refresh_portal：\n"
                                            "指向你的门户查询脚本（产物写入 portal.file）。")
            return
        self.lbl_foot.config(text="刷新门户状态中…", fg="#e08600")

        def f():
            rc, out = self._run_action("refresh_portal")
            self._log_bind("refresh_portal rc=%s" % rc)
            self.root.after(0, self.refresh)
            if rc not in (0, None):
                self.root.after(0, lambda: self._show_result("门户刷新", out, refresh=False))
        self._run_bg(f)

    def act_restart_aggregator(self):
        name = self.cfg.aggregation.get("restart_action") or "restart_aggregator"
        a = self.cfg.action(name)
        if not a:
            messagebox.showinfo("重启聚合", "未配置动作 %s。" % name)
            return
        if not messagebox.askyesno("重启聚合",
                                   a.get("confirm") or
                                   "将重启聚合出口（网络会短暂抖动）。继续？"):
            return

        def f():
            rc, out = self._run_action(name)
            self._log_bind("restart_aggregator rc=%s" % rc)
            self.root.after(0, lambda: self._show_result("重启聚合", out))
        self._run_bg(f)

    def act_safe_renew(self):
        """顶部「安全续连」：优先用表格选中行；否则取第一个带闸的克隆桶。"""
        sel = self.tree.selection()
        bucket = self.cfg.bucket_by_id.get(sel[0]) if sel else None
        if bucket is None:
            cands = [b for b in self.cfg.buckets
                     if b["kind"] == "clone" and b["renew"]["mode"] != "none"]
            bucket = cands[0] if cands else None
        if bucket is None:
            messagebox.showinfo("安全续连", "没有可续连的克隆桶（或都未配置续连动作）。\n"
                                            "先在下方桶表选中一行，或到「运维总控」按桶操作。")
            return
        self._act_op_renew(bucket)

    def act_copy_summary(self):
        st, pv = self.last_st, self.last_pv
        lines = ["多桶聚合控制台 · 状态摘要", "=" * 46]
        for b in self.cfg.buckets:
            val = (st or {}).get(b["state_key"], "?")
            lines.append("  %-10s %-12s 真机=%-16s IP=%s"
                         % (b["id"], STATUS_TEXT.get(val, val), self._real_for(b, pv),
                            self._ip_for(b, pv)))
        if st:
            lines.append("  聚合出口=%s 控制面=%s"
                         % (st.get("aggregator", st.get("mihomo")),
                            st.get("aggregator_api", st.get("mih_api"))))
        if pv:
            lines.append("  门户 凭据可用=%s 属主=%s @ %s"
                         % (pv.get("tokenOk"), pv.get(self.cfg.portal["owner_field"], "?"),
                            pv.get("time", "")))
        if self.cfg.aggregation.get("api"):
            m = self.agg_probe
            lines.append("  聚合腿组=%s 成员=%s 节点=%s"
                         % (m["state"], ",".join(m["members"]) or "(空)", m.get("legs_n")))
        txt = "\n".join(lines)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(txt)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("复制摘要", "剪贴板失败: %s" % e)
            return
        messagebox.showinfo("复制摘要", "已复制到剪贴板。\n\n" + txt)

    def act_open_dir(self):
        if not open_path(self.cfg.data_dir):
            messagebox.showerror("打开数据目录", "打不开: %s" % self.cfg.data_dir)

    def act_speedtest(self):
        self._run_speedtest()

    def act_health(self):
        self.nb.select(self.tab_index["health"])
        self.txt_health.config(state="normal")
        self.txt_health.delete("1.0", "end")
        self.txt_health.insert("1.0", "深检中（任务/控制面/探活约需几秒）…")
        self.txt_health.config(state="disabled")

        def f():
            txt = self._collect_health(deep=True, pv=self.last_pv)
            self.root.after(0, lambda: self._health_done(txt))
        self._run_bg(f)

    def _health_done(self, txt):
        self.txt_health.config(state="normal")
        self.txt_health.delete("1.0", "end")
        self.txt_health.insert("1.0", txt)
        self.txt_health.config(state="disabled")

    def act_help(self):
        cfg = self.cfg
        buckets_txt = "\n".join(
            "  %-10s %-6s 探测=%-6s 续连=%-9s 账号=%s"
            % (b["id"], b["kind"], b["probe"]["mode"], b["renew"]["mode"],
               b["account"] or "—") for b in cfg.buckets)
        missing = cfg.unset_fields()
        miss_txt = "\n".join("  · %s —— %s" % (k, h) for k, h in missing) or "  （无）"
        msg = (
            "状态词（桶列）:\n"
            "  在线 ok       探活通过（真正能上网）\n"
            "  门户劫持      流量被强制重定向到认证页（需先认证/补绑定）\n"
            "  丢包 / 离线 / 无IP / 未知\n"
            "  直连(非TUN)   直连探测成功但 TUN 未在用\n\n"
            "真机判定列:\n"
            "  真机在线! 勿动   目标真机在门户，克隆腿会互顶 → 拦截\n"
            "  真机离线 · 可续  可安全续（仍会二次确认）\n"
            "  未知(无凭据)     无法验证 → 闸默认拦截\n\n"
            "本机桶:\n%s\n\n"
            "配置: %s\n"
            "  数据目录: %s\n"
            "  未配置项:\n%s\n\n"
            "刷新间隔 15s/30s/60s/停 可调；关闭窗口 = 最小化到托盘。\n"
            "所有动作复用你配置的命令，本工具不实现任何协议逻辑。\n\n"
            "「账号密码」页：录入并确认后仅显示 %s 且锁定；明文永不落盘；"
            "脚本调用接口时才解密取用。\n"
            "「桶管理」页：新增/编辑/删除/排序桶 → 保存到配置（自动备份）；"
            "「改 MAC」= 改配置字段 + 可下发到接口（actions.set_mac）。\n"
            "「运维总控」页：①每桶操作 ②聚合出口 ③虚拟路由(可选) ④计划任务 "
            "⑤门户凭据 ⑥纪律提示\n"
            "生命周期：本控制台 = 总开关。退出 = 全链停；启动自动拉起；UI 不在则任务停手。"
            % (buckets_txt, cfg.path, cfg.data_dir, miss_txt, STARS)
        )
        messagebox.showinfo("帮助 / 图例", msg)

    def _tree_tip_text(self):
        iid = self.tree.identify_row(self.tree.winfo_pointery() - self.tree.winfo_rooty())
        if not iid:
            return ""
        b = self.cfg.bucket_by_id.get(iid)
        if not b:
            return ""
        st = self.last_st or {}
        val = st.get(b["state_key"], "?")
        extra = "".join("\n" + x for x in self._loss_lines(b, st))
        return ("桶 %s 状态=%s\n解读: %s%s\n双击看完整详情（含白名单/在线会话原始 JSON）"
                % (b["id"], val, STATUS_TEXT.get(str(val), str(val)), extra))

    def _on_tree_double(self, _e):
        sel = self.tree.selection()
        if not sel:
            return
        b = self.cfg.bucket_by_id.get(sel[0])
        if b:
            self._bucket_detail(b)

    def _bucket_detail(self, bucket):
        st, pv = self.last_st, self.last_pv
        val = (st or {}).get(bucket["state_key"], "?")
        lines = ["桶 %s — %s" % (bucket["id"], bucket["label"]),
                 "=" * 50,
                 "状态: %s  (%s)  @ %s" % (val, STATUS_TEXT.get(val, val),
                                           (st or {}).get("time", "?")),
                 "真机: %s    最后IP: %s" % (self._real_for(bucket, pv),
                                             self._ip_for(bucket, pv)),
                 "账号: %s    探测方式: %s" % (bucket["account"] or "—",
                                               bucket["probe"]["mode"])]
        lines += self._loss_lines(bucket, st)
        if bucket["kind"] == "clone":
            t = self._clone_target(bucket, pv)
            if t:
                lines += ["", json.dumps(t, ensure_ascii=False, indent=2)]
        else:
            mac = str(bucket.get("mac") or "").lower()
            if mac:
                for a in (pv or {}).get("accounts") or []:
                    for e in (a.get("whitelist") or []):
                        if str(e.get("mac", "") or "").lower() == mac:
                            lines.append("白名单[%s]: %s"
                                         % (a.get("username"),
                                            json.dumps(e, ensure_ascii=False)))
                    for s in (a.get("online") or []):
                        if str(s.get("mac", "") or "").lower() == mac:
                            lines.append("在线会话[%s]: %s"
                                         % (a.get("username"),
                                            json.dumps(s, ensure_ascii=False)))
        top = tk.Toplevel(self.root)
        top.title("桶详情 · %s" % bucket["id"])
        top.geometry("%dx%d" % (self.px(560), self.px(420)))
        top.minsize(self.px(420), self.px(300))
        txt = tk.Text(top, wrap="none", font=(self.cfg.ui["mono_font"], 9))
        sb = ttk.Scrollbar(top, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", "\n".join(lines))
        txt.config(state="disabled")
        txt.bind("<Control-a>", lambda _e: (txt.tag_add("sel", "1.0", "end"), "break"))


# ---------------- 心跳（UI = 总开关的存活标记） ----------------
def _write_ui_marker(cfg, pid):
    """原子写心跳文件（配套脚本据此判定 UI 是否存活）。"""
    try:
        tmp = cfg.lifecycle["heartbeat_file"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "t": round(time.time(), 1)}, f)
        os.replace(tmp, cfg.lifecycle["heartbeat_file"])
    except Exception:
        pass


def _remove_ui_marker(cfg):
    try:
        os.remove(cfg.lifecycle["heartbeat_file"])
    except Exception:
        pass


def _hb_loop(cfg, stop_evt):
    """心跳线程：每 20s 刷一次（与界面刷新间隔解耦，避免「停」时被误判离线）。"""
    pid = os.getpid()
    _write_ui_marker(cfg, pid)
    while not stop_evt.wait(20):
        _write_ui_marker(cfg, pid)


# ---------------- 简单界面 ----------------
class SimpleApp:
    """简单界面：各桶状态 + 刷新 + 桶服务总开关 + VPN 代理开关 + 高级界面入口。

    与高级界面同进程：点「高级界面」弹出完整控制台（Toplevel），关闭它只是收回，
    不会另起进程、不会重复写心跳。退出（全链停）与高级界面语义一致。
    """

    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.adv_win = None
        self.adv_app = None
        self.after_id = None
        self.interval_ms = int(cfg.ui["interval_ms"])
        self.last_st = None
        self.last_pv = None
        self._icon = None
        self._quitting = False
        self.proxy_mode = None
        self._probe_t = 0.0
        self._probe_busy = False

        os.makedirs(cfg.data_dir, exist_ok=True)
        self._hb_evt = threading.Event()
        threading.Thread(target=_hb_loop, args=(cfg, self._hb_evt), daemon=True).start()

        self.dpi = get_window_dpi(root.winfo_id())
        self.scale = max(0.9, min(3.0, self.dpi / 96.0))
        root.tk.call("tk", "scaling", round(96.0 * self.scale / 72.0, 6))
        root.title(cfg.ui["title"] + "（简单）")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        W = min(self.px(520), int(sw * 0.92))
        H = min(self.px(340), int(sh * 0.92))   # 约为原高度的 60%
        root.geometry("%dx%d+%d+%d" % (W, H, max(0, (sw - W) // 2), max(0, (sh - H) // 3)))
        root.minsize(self.px(420), self.px(300))
        if TRAY_OK:
            try:
                img = make_icon(cfg).resize((32, 32), Image.LANCZOS)
                self._win_icon = ImageTk.PhotoImage(img)
                root.iconphoto(True, self._win_icon)
            except Exception:
                pass
        self._build_ui()
        self.refresh()
        self._schedule()
        # UI = 总开关：启动后按配置自动拉起服务链（幂等，不弹确认）
        if self.cfg.has_action("boot_chain"):
            self.root.after(int(self.cfg.lifecycle.get("boot_delay_ms") or 3000),
                            lambda: self.act_service(True, confirm=False))

    # ---------- 工具 ----------
    def px(self, n):
        return max(1, round(n * self.scale))

    def _log_bind(self, msg):
        try:
            with open(self.cfg.events_file, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S") + " SIMPLE " + msg + "\n")
        except Exception:
            pass

    def _run_bg(self, fn, done=None):
        def w():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                try:
                    self.root.after(0, lambda: self.lbl_foot.config(
                        text="后台任务异常: %s" % e, fg="#c62828"))
                except Exception:
                    pass
            finally:
                if done:
                    try:
                        self.root.after(0, done)
                    except Exception:
                        pass
        threading.Thread(target=w, daemon=True).start()

    def _show_result(self, title, text):
        show_result(self.root, self.cfg, title, text, after=self.refresh, scale=self.scale)

    # ---------- UI ----------
    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=self.px(10))
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x")
        ttk.Label(top, text=self.cfg.ui["title"],
                  font=("TkDefaultFont", 11, "bold")).pack(side="left")
        self.lbl_fresh = ttk.Label(top, text="")
        self.lbl_fresh.pack(side="left", padx=(self.px(8), 0))
        ttk.Button(top, text="高级界面", width=10,
                   command=self.open_advanced).pack(side="right")

        # 桶列表 / 控制区 之间可拖拽调节高度（上下分隔条）
        self.paned = ttk.PanedWindow(outer, orient="vertical")
        self.paned.pack(fill="both", expand=True, pady=(self.px(6), 0))
        list_pane = ttk.Frame(self.paned)
        ctrl_pane = ttk.Frame(self.paned)
        self.paned.add(list_pane, weight=1)
        self.paned.add(ctrl_pane, weight=0)
        self.ctrl_pane = ctrl_pane
        self.paned.bind("<ButtonRelease-1>", lambda _e: self._clamp_sash())

        ttk.Label(list_pane, text="各桶状态（拖中间的分隔条可调高度）").pack(anchor="w")
        self.tree = ttk.Treeview(list_pane, columns=("b", "s", "d"), show="headings",
                                 height=max(5, min(9, len(self.cfg.buckets) + 2)))
        for cid, h, w, st in (("b", "桶", 100, False), ("s", "状态", 90, False),
                              ("d", "说明", 260, True)):
            self.tree.heading(cid, text=h)
            self.tree.column(cid, width=self.px(w), anchor="w", stretch=st)
        self.tree.pack(fill="both", expand=True, pady=(self.px(2), 0))

        row = ttk.Frame(ctrl_pane)
        row.pack(fill="x", pady=(self.px(2), 0))
        self.lbl_service = ttk.Label(row, text="桶服务: …", anchor="w")
        self.lbl_service.pack(side="left", fill="x", expand=True)
        self.btn_srv_on = ttk.Button(row, text="开启桶服务", width=11,
                                     command=lambda: self.act_service(True))
        self.btn_srv_on.pack(side="right", padx=self.px(2))
        self.btn_srv_off = ttk.Button(row, text="关闭桶服务", width=11,
                                      command=lambda: self.act_service(False))
        self.btn_srv_off.pack(side="right", padx=self.px(2))

        row2 = ttk.Frame(ctrl_pane)
        row2.pack(fill="x", pady=(self.px(2), 0))
        self.lbl_vpn = ttk.Label(row2, text="VPN 代理: …", anchor="w")
        self.lbl_vpn.pack(side="left", fill="x", expand=True)
        self.btn_vpn_on = ttk.Button(row2, text="开启代理", width=10,
                                     command=lambda: self.act_vpn(True))
        self.btn_vpn_on.pack(side="right", padx=self.px(2))
        self.btn_vpn_off = ttk.Button(row2, text="关闭代理", width=10,
                                      command=lambda: self.act_vpn(False))
        self.btn_vpn_off.pack(side="right", padx=self.px(2))

        bar = ttk.Frame(ctrl_pane)
        bar.pack(fill="x", pady=(self.px(4), 0))
        ttk.Button(bar, text="刷新", width=10, command=self.refresh).pack(side="left")
        ttk.Label(bar, text="自动刷新:").pack(side="left", padx=(self.px(10), 0))
        self.var_interval = tk.StringVar(
            value={15000: "15 秒", 30000: "30 秒", 60000: "60 秒"}.get(
                self.interval_ms, "15 秒"))
        cmb = ttk.Combobox(bar, state="readonly", width=6, textvariable=self.var_interval,
                           values=("15 秒", "30 秒", "60 秒", "停"))
        cmb.pack(side="left")
        cmb.bind("<<ComboboxSelected>>", self._on_interval)
        ttk.Button(bar, text="退出(全链停)", width=13,
                   command=self.act_quit).pack(side="right")

        self.lbl_foot = tk.Label(ctrl_pane, text="", anchor="w", fg="#666")
        self.lbl_foot.pack(fill="x", pady=(self.px(2), 0))
        # 默认分隔条位置：让列表默认就有 ~6 行
        self.root.after(80, self._init_sash)

    def _init_sash(self, tries=12):
        """默认分隔条位置：列表默认 ~6 行，同时保证下方控制区完整可见。"""
        try:
            total = self.paned.winfo_height()
            if total < self.px(120):          # 窗口还没布局好 → 稍后重试
                if tries > 0:
                    self.root.after(120, lambda: self._init_sash(tries - 1))
                return
            self._clamp_sash(total)
            if self.paned.sashpos(0) <= 1:
                self.paned.sashpos(0, min(self.px(150),
                                          max(self.px(70), total - self.px(170))))
        except Exception:
            pass

    def _clamp_sash(self, total=None):
        """拖拽分隔条后限制：下方控制区必须完整可见，不让控件被挤出窗口。"""
        try:
            total = total or self.paned.winfo_height()
            need = self.ctrl_pane.winfo_reqheight() + self.px(6)
            maxpos = max(self.px(50), total - need)
            if self.paned.sashpos(0) > maxpos:
                self.paned.sashpos(0, maxpos)
        except Exception:
            pass

    def _schedule(self):
        if self.after_id:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        if self.interval_ms > 0:
            self.after_id = self.root.after(self.interval_ms, self._tick)

    def _tick(self):
        self.after_id = None
        try:
            self.refresh()
        except Exception as e:  # noqa: BLE001
            try:
                self.lbl_foot.config(text="刷新异常: %s" % e, fg="#c62828")
            except Exception:
                pass
        self._schedule()

    def _on_interval(self, _e=None):
        self.interval_ms = {"15 秒": 15000, "30 秒": 30000, "60 秒": 60000,
                            "停": 0}.get(self.var_interval.get(), 15000)
        self._schedule()

    def refresh(self):
        st = load_json(self.cfg.state_file)
        pv = load_json(self.cfg.portal_file) if self.cfg.portal_file else None
        self.last_st, self.last_pv = st, pv

        tr = self.tree
        for row in tr.get_children():
            tr.delete(row)
        for b in self.cfg.buckets:
            val = (st or {}).get(b["state_key"], "?")
            color = COLORS.get(str(val), "#333")
            tag = "c_" + color.lstrip("#")
            tr.tag_configure(tag, foreground=color)
            tr.insert("", "end", values=(b["id"], STATUS_TEXT.get(val, val), b["desc"]),
                      tags=(tag,))

        age = file_age_min(self.cfg.state_file)
        if age is None:
            self.lbl_fresh.config(text="状态文件不可读!", foreground="#c62828")
        else:
            col = "#1a8f3c" if age < 3 else ("#e08600" if age < 6 else "#c62828")
            self.lbl_fresh.config(text="数据 %s"
                                  % ("%d分前" % int(age) if age >= 1
                                     else "%d秒前" % int(age * 60)), foreground=col)
        self._update_service_ui(st, age)
        self._ensure_proxy_probe()
        self._update_vpn_ui()
        ft = "状态文件: %s" % self.cfg.state_file
        if st:
            ft += "   @ %s" % st.get("time", "")
        self.lbl_foot.config(text=ft)

    def _update_service_ui(self, st, age):
        buckets = self.cfg.buckets
        ok_n = sum(1 for b in buckets
                   if str((st or {}).get(b["state_key"], "")).startswith("ok"))
        agg_out = (st or {}).get("aggregator", (st or {}).get("mihomo"))
        agg_api = (st or {}).get("aggregator_api", (st or {}).get("mih_api"))
        running = (str(agg_out).startswith("ok") or str(agg_api) == "ok" or ok_n > 0)
        if st is None or age is None:
            self.lbl_service.config(text="桶服务: 未知（状态文件不可读）", foreground="#8d8d8d")
        elif running:
            self.lbl_service.config(text="桶服务: 运行中 · 在线桶 %d/%d" % (ok_n, len(buckets)),
                                    foreground="#1a8f3c")
        else:
            self.lbl_service.config(text="桶服务: 已停止", foreground="#c62828")
        for btn, name in ((self.btn_srv_on, "boot_chain"), (self.btn_srv_off, "stop_chain")):
            try:
                btn.state(["!disabled"] if self.cfg.has_action(name) else ["disabled"])
            except Exception:
                pass

    def _ensure_proxy_probe(self):
        if not proxy_available(self.cfg):
            self.proxy_mode = None
            return
        if self._probe_busy or time.time() - self._probe_t < 60:
            return
        self._probe_busy = True

        def w():
            try:
                c = agg_api_json(self.cfg, "/configs")
                self.proxy_mode = (c or {}).get("mode") if c else None
            except Exception:
                self.proxy_mode = None
            finally:
                self._probe_t = time.time()
                self._probe_busy = False
                try:
                    self.root.after(0, self._update_vpn_ui)
                except Exception:
                    pass
        threading.Thread(target=w, daemon=True).start()

    def _update_vpn_ui(self):
        url_ok = bool(self.cfg.aggregation.get("provider_url"))
        avail = proxy_available(self.cfg)
        if not avail:
            self.lbl_vpn.config(text="VPN 代理: 不可用（未配置聚合控制面）", foreground="#8d8d8d")
            hint = "需要 aggregation.api + restart_action；在「高级界面 → 运维总控 → ②」里配置。"
        elif not url_ok:
            self.lbl_vpn.config(text="VPN 代理: 未填写节点源地址", foreground="#e08600")
            hint = ("先到「高级界面 → 运维总控 → ② 聚合出口」填「节点源地址」并保存，"
                    "这里的开关才会启用。")
        elif self.proxy_mode == "rule":
            self.lbl_vpn.config(text="VPN 代理: 已开启（规则模式）", foreground="#1a8f3c")
            hint = "关闭 = 切直连（不经聚合/节点）。"
        elif self.proxy_mode == "direct":
            self.lbl_vpn.config(text="VPN 代理: 已关闭（直连模式）", foreground="#e08600")
            hint = "开启 = 切回规则模式（聚合/分流生效）。"
        else:
            self.lbl_vpn.config(text="VPN 代理: 未知（控制面不可达）", foreground="#8d8d8d")
            hint = "聚合出口控制面暂时不可达，稍后自动重试。"
        try:
            if hasattr(self, "lbl_vpn_hint"):
                self.lbl_vpn_hint.config(text=hint)
        except Exception:
            pass
        state = ["!disabled"] if (avail and url_ok) else ["disabled"]
        for b in (self.btn_vpn_on, self.btn_vpn_off):
            try:
                b.state(state)
            except Exception:
                pass

    # ---------- 动作 ----------
    def act_service(self, on, confirm=True):
        name = "boot_chain" if on else "stop_chain"
        a = self.cfg.action(name)
        if not a:
            messagebox.showinfo("桶服务", "未配置动作 %s（见配置文件的 actions）。" % name)
            return
        title = "开启桶服务" if on else "关闭桶服务"
        if confirm and not messagebox.askyesno(title, a.get("confirm") or (
                "确认开启桶服务（按配置拉起虚拟路由 / 聚合出口）？" if on
                else "确认关闭桶服务（全链停）？")):
            return
        self.lbl_foot.config(text="%s 中…" % title, fg="#e08600")

        def f():
            rc, out = exec_action(self.cfg, name)
            self._log_bind("%s rc=%s" % (name, rc))
            self.root.after(0, lambda: self._show_result(title, out))
        self._run_bg(f)

    def act_vpn(self, on):
        target = "rule" if on else "direct"
        if not proxy_available(self.cfg):
            messagebox.showinfo("VPN 代理", "未配置聚合控制面（aggregation.api / restart_action）。")
            return
        if not self.cfg.aggregation.get("provider_url"):
            messagebox.showinfo("VPN 代理",
                                "还没填写节点源地址。\n请到「高级界面 → 运维总控 → ② 聚合出口」"
                                "填写并保存后再试。")
            return
        title = "开启 VPN 代理" if on else "关闭 VPN 代理"
        if not messagebox.askyesno(title, (
                "将把聚合出口切到「规则模式」并重启（数秒抖动）。继续？" if on
                else "将把聚合出口切到「直连模式」并重启（流量不经聚合/节点）。继续？")):
            return
        self.lbl_foot.config(text="%s 中（约 3~36 秒）…" % title, fg="#e08600")

        def f():
            ok, msg = set_proxy_mode(self.cfg, target, log=self._log_bind)
            self.proxy_mode = target if ok else None
            self._probe_t = time.time()

            def done():
                try:
                    self.lbl_foot.config(text=msg,
                                         fg=("#1a8f3c" if ok else "#c62828"))
                except Exception:
                    pass
                self._update_vpn_ui()
                try:
                    if self._icon is not None:
                        self._icon.notify(msg, self.cfg.ui["tray_title"])
                except Exception:
                    pass
            self.root.after(0, done)
        self._run_bg(f)

    def act_quit(self):
        if self._quitting:
            return
        if not messagebox.askyesno("退出（全链停）", self.cfg.lifecycle["exit_confirm"]):
            return
        self._quitting = True
        try:
            self.lbl_foot.config(text="全链停止中…", fg="#e08600")
        except Exception:
            pass

        def f():
            if self.cfg.has_action("stop_chain"):
                exec_action(self.cfg, "stop_chain")
                self._log_bind("stop_chain（退出）")
            _remove_ui_marker(self.cfg)
            self._hb_evt.set()
        self._run_bg(f, self._final_quit)

    def _final_quit(self):
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass

    # ---------- 高级界面 ----------
    def open_advanced(self):
        if self.adv_win is None or not self.adv_win.winfo_exists():
            self.adv_win = tk.Toplevel(self.root)
            self.adv_win.title(self.cfg.ui["title"])
            self.adv_win.protocol("WM_DELETE_WINDOW", self._hide_advanced)
            self.adv_app = App(self.adv_win, self.cfg, auto_boot=False)
        try:
            self.adv_win.deiconify()
            self.adv_win.state("normal")
            self.adv_win.lift()
            self.adv_win.focus_force()
        except Exception:
            pass
        return self.adv_app

    def _hide_advanced(self):
        try:
            self.adv_win.withdraw()
        except Exception:
            pass


# ---------------- 托盘图标 ----------------
def make_icon(cfg=None):
    """程序化生成托盘图标（无外部资源文件）。"""
    img = Image.new("RGB", (64, 64), "#1a8f3c")
    d = ImageDraw.Draw(img)
    d.rectangle((10, 10, 54, 54), outline="#ffffff", width=6)
    # 三条水平线 = 多桶并行
    for y in (24, 32, 40):
        d.line((18, y, 46, y), fill="#ffffff", width=4)
    return img


def ensure_single_instance():
    """单实例互斥（开机自启 + 手动重复启动时不产生第二份托盘/窗口）。"""
    if IS_WIN:
        ctypes.windll.kernel32.CreateMutexW(None, False, "BucketConsoleSingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            print("bucket_console 已在运行，本次启动退出。")
            sys.exit(0)
    else:
        import fcntl
        lock_path = os.path.join(os.path.expanduser("~"), ".bucket_console.lock")
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("bucket_console 已在运行，本次启动退出。")
            sys.exit(0)
        ensure_single_instance._fh = fh  # 保活


def build_tray(app, root, cfg, on_advanced=None):
    items = [
        pystray.MenuItem("打开界面", lambda i, item: root.after(0, root.deiconify)),
    ]
    if on_advanced:
        items.append(pystray.MenuItem(
            "高级界面", lambda i, item: root.after(0, on_advanced)))
    items += [
        pystray.MenuItem("刷新状态", lambda i, item: root.after(0, app.refresh)),
        pystray.MenuItem("复制摘要", lambda i, item: root.after(0, app.act_copy_summary)),
        pystray.MenuItem("事件日志", lambda i, item: root.after(0, app._goto_log)),
        pystray.MenuItem("打开数据目录", lambda i, item: root.after(0, app.act_open_dir)),
        pystray.MenuItem("重启聚合", lambda i, item: root.after(0, app.act_restart_aggregator)),
        pystray.MenuItem("暂停/恢复代理", lambda i, item: root.after(0, app.act_proxy_toggle)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出(全链停)", lambda i, item: root.after(0, app.act_quit)),
    ]
    return pystray.Icon("bucket-console", make_icon(cfg), cfg.ui["tray_title"],
                        menu=pystray.Menu(*items))


def build_simple_tray(app, root, cfg):
    """简单界面的托盘菜单（不依赖高级界面的方法）。"""
    return pystray.Icon(
        "bucket-console", make_icon(cfg), cfg.ui["tray_title"] + "（简单）",
        menu=pystray.Menu(
            pystray.MenuItem("打开界面", lambda i, item: root.after(0, root.deiconify)),
            pystray.MenuItem("高级界面", lambda i, item: root.after(0, app.open_advanced)),
            pystray.MenuItem("刷新状态", lambda i, item: root.after(0, app.refresh)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开启桶服务", lambda i, item: root.after(
                0, lambda: app.act_service(True))),
            pystray.MenuItem("关闭桶服务", lambda i, item: root.after(
                0, lambda: app.act_service(False))),
            pystray.MenuItem("开启 VPN 代理", lambda i, item: root.after(
                0, lambda: app.act_vpn(True))),
            pystray.MenuItem("关闭 VPN 代理", lambda i, item: root.after(
                0, lambda: app.act_vpn(False))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出(全链停)", lambda i, item: root.after(0, app.act_quit)),
        ))


def main(argv=None):
    ap = argparse.ArgumentParser(description="多桶聚合控制台")
    ap.add_argument("--config", "-c", default=None, help="配置文件（默认自动查找）")
    ap.add_argument("--check", action="store_true", help="只做配置自检并退出")
    ap.add_argument("--selftest", action="store_true", help="构造全部界面后立即退出（冒烟）")
    ap.add_argument("--no-tray", action="store_true", help="不启用托盘（纯窗口）")
    ap.add_argument("--simple", action="store_true",
                    help="简单界面（默认；各桶状态 + 总开关 + VPN 开关）")
    ap.add_argument("--advanced", action="store_true",
                    help="直接打开高级界面（完整控制台）")
    args = ap.parse_args(argv)

    path = default_config_path(args.config)
    try:
        cfg = load_config(path)
    except ConfigError as e:
        print("配置错误: %s" % e, file=sys.stderr)
        return 2

    if args.check:
        print(config_summary(cfg))
        return 0

    ensure_single_instance()
    enable_dpi_aware()          # 必须在 tk.Tk() 之前
    root = tk.Tk()
    use_simple = not args.advanced
    if args.selftest:
        root.withdraw()         # 冒烟测试：不闪窗口、不起托盘

    if args.selftest:
        app = SimpleApp(root, cfg)
        app.open_advanced()     # 顺带把高级界面也建出来
        root.update_idletasks()
        root.update()
        root.after(500, root.destroy)
        root.mainloop()
        print("selftest ok: 简单界面 + 高级界面构建 + 首次刷新完成")
        return 0

    use_tray = TRAY_OK and not args.no_tray
    if use_simple:
        app = SimpleApp(root, cfg)
        if use_tray:
            icon = build_simple_tray(app, root, cfg)
            app._icon = icon
            root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw())
            icon.run_detached()
    else:
        app = App(root, cfg)
        if use_tray:
            icon = build_tray(app, root, cfg, on_advanced=lambda: root.deiconify())
            app._icon = icon
            root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw())
            icon.run_detached()

    root.mainloop()
    try:
        if app._icon is not None:
            app._icon.stop()
    except Exception:
        pass
    _remove_ui_marker(cfg)      # 兜底：撤心跳，防孤儿标记
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
