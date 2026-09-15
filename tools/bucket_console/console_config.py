# -*- coding: utf-8 -*-
"""
console_config.py — 多桶聚合控制台 · 配置模型
================================================================================
一份配置描述「你的聚合环境」，控制台与参考探测脚本共用同一份模型：

  app / ui          界面标题、刷新间隔、走势采样数、数据目录
  lifecycle         UI 心跳文件、启动拉起、退出全链停（UI = 总开关）
  buckets[]         每个「桶」= 一条独立会话出口（本机 / SOCKS / SSH 接口）
  actions{}         所有按钮背后的命令（本工具不实现任何协议逻辑，只调度命令）
  tasks[]           计划任务清单（Windows schtasks；其它平台自动降级为只读）
  aggregation{}     聚合出口的 REST 控制面（可选：用于「聚合腿组」卡片与代理开关）
  portal{}          门户查询产物（在线会话 / 白名单 / 真机判定）的文件约定

设计约定：
  · 配置里出现的 `<...>` 一律视为「未填写的占位符」，控制台会显示为「未配置」并给出提示；
  · 路径支持 ~ 与环境变量；相对路径按配置文件所在目录解析；
  · 命令可用列表（推荐）或字符串（走 shell）；字符串里可用占位符：
      {config} {data_dir} {bucket} {account} {leg} {iface} {wan} {target}
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time


def safe_console():
    """控制台代码页可能不是 UTF-8：把不可编码字符降级为 '?'，避免直接崩掉。

    否则 `--check` 在中文 Windows 控制台会因打印 '✓' 抛 UnicodeEncodeError。
    （2026-09-09 由内部测试版回灌到 bucket_console.py；此处提为共享实现，
    probe_buckets.py / leg_ctl.py 等独立入口一并调用。）
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")   # type: ignore[attr-defined]
        except Exception:
            pass


def decode_console(b: bytes) -> str:
    """子进程输出解码：UTF-8 → CP936 → Latin-1 依次尝试。

    中文 Windows 上 schtasks 的状态文本、ping 的汇总行都随控制台代码页在
    中英文之间变化，固定 utf-8 解码会得到 U+FFFD 乱码（数字仍可解析，
    但显示出来是乱码）。全仓共用这一份，别再各写各的。
    """
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "cp936", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", "replace")


# 占位符 = `<内容>`，内容非空、不含尖括号、不以空白开头/结尾，且两侧不粘着词字符。
# 这三条都是为了和「命令里本来就有尖括号」区分开：
#   · 卡空白     -> `cmd < in > out` 不算占位符（旧规则会把整条命令判成「未配置」）
#   · 卡粘连     -> `cat<in>out` 不算占位符（重定向/比较符）
#   · 仍需两侧是边界/标点 -> `http://<地址>`、`socks5h://<IP>:<端口>` 仍算模板
_PLACEHOLDER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])<[^\s<>](?:[^<>]*[^\s<>])?>(?![A-Za-z0-9_])")


def _is_template(s: str) -> bool:
    """整串是否「由占位符拼成的模板」，如 `<URL>`、`http://<IP>:<端口>`。

    判据：至少有一个占位符，且把占位符挖掉后剩下的部分不含空白。
    于是 `socks5h://<管理IP>:<SOCKS端口-A>` 算模板，而 `cmd < in > out`、`cat<in>out`
    这类把尖括号用在别处的字符串不算。
    """
    if not _PLACEHOLDER_TOKEN_RE.search(s):
        return False
    return not re.search(r"\s", _PLACEHOLDER_TOKEN_RE.sub("", s))

BUCKET_KINDS = ("self", "clone")
PROBE_MODES = ("local", "socks", "ssh", "none")
RENEW_MODES = ("none", "ssh_ifup", "command")

# 未填占位符 → 界面/自检里给出的填写提示
UNSET_HINTS = {
    "probe.target": "填一个会返回 204 的探活地址（用于判断该出口是否真能上网）",
    "aggregation.api": "填聚合出口的控制 API 地址（本机回环亦可）；不填则不做腿组探测",
    "portal.file": "门户查询产物的 JSON 路径（在线会话/白名单/真机判定）；没有就留空",
    "app.data_dir": "控制台数据目录（状态/事件/心跳文件都放这里）",
    "actions.refresh_state.argv": "重新巡检 = 运行你的状态生产脚本（可用 probe_buckets.py）",
    "actions.refresh_portal.argv": "门户刷新 = 运行你的门户查询脚本",
    "actions.restart_aggregator.argv": "重启聚合出口 = 运行你的重启命令",
    "actions.stop_chain.argv": "退出全链停 = 停掉 VM / 聚合出口等（不填则退出只关界面）",
    "actions.boot_chain.argv": "启动拉起 = 拉起 VM / 聚合出口等（不填则不做）",
    "actions.speedtest.argv": "测速 = 运行你的测速脚本；用 {leg} 传入单桶腿名",
    "actions.set_mac.argv": "改 MAC 下发 = 把 {mac} 写到 {iface}（如 ssh 到远端执行 ifconfig/macchanger）",
}


class ConfigError(Exception):
    pass


def is_unset(v) -> bool:
    """空值 / `<占位符>` / 未替换的模板 → 视为未配置。"""
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return s == "" or _is_template(s)
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


# --------------------------------------------------------------------------
# 类型归一：配置写错类型时抛 ConfigError，而不是让裸 AttributeError/ValueError
# 冒到 CLI 与界面上（那些入口只捕 ConfigError，其它异常直接 traceback 崩掉）。
# --------------------------------------------------------------------------
def _as_dict(v, what: str) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    raise ConfigError("%s 必须是对象（当前是 %s）" % (what, type(v).__name__))


def _as_str(v, what: str, default: str = "") -> str:
    if v is None or v == "":
        return default
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    raise ConfigError("%s 必须是字符串（当前是 %s）" % (what, type(v).__name__))


def _str_or(v, default: str = "") -> str:
    """不抛异常版的取字符串（配置校验器用：它只收集问题，不该因为类型错就崩）。"""
    if v is None or v == "":
        return default
    return v if isinstance(v, str) else str(v)


def _as_int(v, what: str, default: int = 0) -> int:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise ConfigError("%s 必须是整数（当前是布尔值）" % what)
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ConfigError("%s 必须是整数（当前是 %r）" % (what, v))


def expand_path(p, base=None) -> str:
    if not p or not isinstance(p, str):
        return ""
    p = os.path.expandvars(os.path.expanduser(p.strip()))
    if base and not os.path.isabs(p):
        p = os.path.join(base, p)
    return os.path.normpath(p)


# --------------------------------------------------------------------------
# MAC 工具（界面「桶管理 / 改 MAC」共用）
# --------------------------------------------------------------------------
MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
BUCKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,31}$")


def mac_ok(v) -> bool:
    return bool(v) and bool(MAC_RE.match(str(v).strip()))


def normalize_mac(v) -> str:
    """统一成小写冒号分隔；非法返回空串。"""
    s = str(v or "").strip().replace("-", ":").lower()
    return s if MAC_RE.match(s) else ""


def mac_is_multicast(v) -> bool:
    """首字节最低位为 1 = 组播地址（一般不能作为网卡 MAC）。"""
    s = normalize_mac(v)
    if not s:
        return False
    try:
        return bool(int(s.split(":")[0], 16) & 0x01)
    except Exception:
        return False


# --------------------------------------------------------------------------
# 桶定义：校验 / 模板 / 安全写回
# --------------------------------------------------------------------------
def validate_bucket(raw: dict, other_ids=()) -> list[str]:
    """校验一条桶定义，返回错误列表（空 = 通过）。
    other_ids：**其它桶**的 id 序列（按位置传，重复 id 也能查出来）。"""
    errs = []
    if not isinstance(raw, dict):
        return ["桶定义必须是对象"]
    others = [str(x) for x in (other_ids or [])]     # 按位置传入「其它桶」的 id，重复也能查出
    bid = str(raw.get("id") or "").strip()
    if not bid:
        errs.append("缺少桶标识 id")
    elif not BUCKET_ID_RE.match(bid):
        errs.append("桶标识只允许字母数字开头、含 [A-Za-z0-9_-]，最长 32")
    elif bid in others:
        errs.append("桶标识 %s 已存在" % bid)
    def _d(v):
        return v if isinstance(v, dict) else {}

    kind = _str_or(raw.get("kind"), "self").strip()
    if kind not in BUCKET_KINDS:
        errs.append("kind 必须是 %s" % "/".join(BUCKET_KINDS))
    pmode = _str_or(_d(raw.get("probe")).get("mode"), "local").strip()
    if pmode not in PROBE_MODES:
        errs.append("probe.mode 必须是 %s" % "/".join(PROBE_MODES))
    rmode = _str_or(_d(raw.get("renew")).get("mode"), "none").strip()
    if rmode not in RENEW_MODES:
        errs.append("renew.mode 必须是 %s" % "/".join(RENEW_MODES))
    mac = raw.get("mac")
    if mac and not is_unset(mac):          # 占位符 <MAC_A> 视为「未填」，不拦保存
        if not mac_ok(mac):
            errs.append("MAC 格式应为 XX:XX:XX:XX:XX:XX（或连字符分隔）")
        elif mac_is_multicast(mac):
            errs.append("MAC %s 是组播地址（首字节最低位为 1），一般不能作为网卡地址" % mac)
    if kind == "clone" and not _d(raw.get("clone")).get("portal_id"):
        errs.append("克隆桶需要 clone.portal_id（用于关联门户判定数据）")
    if rmode == "ssh_ifup":
        ssh = _d(_d(raw.get("renew")).get("ssh"))
        if not ssh.get("host"):
            errs.append("续连方式 ssh_ifup 需要 renew.ssh.host")
        if not _d(raw.get("renew")).get("wan"):
            errs.append("续连方式 ssh_ifup 需要 renew.wan")
    return errs


def new_bucket_template(bid="NEW") -> dict:
    """给界面「新增桶」用的一份可编辑模板。"""
    return {
        "id": bid,
        "label": "新桶",
        "state_key": "b_" + str(bid).lower(),
        "kind": "self",
        "enabled": True,
        "account": "",
        "mac": "",
        "desc": "",
        "color": "",
        "speed_leg": bid,
        "probe": {"mode": "local", "socks": "", "iface": "", "tries": 3, "timeout": 6,
                  "ssh": {"host": "", "user": "root", "port": 22, "key": ""}},
        "renew": {"mode": "none", "wan": "", "iface": "", "gate": False,
                  "owner_label": "", "hint": "",
                  "ssh": {"host": "", "user": "root", "port": 22, "key": ""}},
        "clone": {"portal_id": "", "gate": True, "owner_label": "",
                  "expect_whitelist": True},
    }


def _set_path(d: dict, path: str, val) -> None:
    ks = path.split(".")
    cur = d
    for k in ks[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[ks[-1]] = val


class _FileLock:
    """跨进程独占锁（O_EXCL 创建，超时放弃）+ PID 回收。

    写盘前先拿锁：配置回写、探活状态、腿列表渲染都是「读全文 → 改 → 写回」，
    两个写者会互相抹掉对方的改动；共用的 `<file>.tmp` 还会让 os.replace 直接撞车。

    被 Ctrl+C / kill 掉时锁文件会残留，所以记下 PID 与启动时刻：超时后若持锁进程
    已不存在、或锁文件明显超出本次等待窗口，就当它是死锁一并清掉 —— 否则一次中断
    会让这个文件永久写不进去，只能人工删 .lock。
    """

    def __init__(self, path: str, timeout: float = 10.0):
        self.path = str(path) + ".lock"
        self.timeout = timeout
        self.fd = None

    def _stale(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self.path)
        except OSError:
            return True                      # 读不到 = 已被释放，重试即可
        if age > max(60.0, self.timeout * 6):
            return True
        try:
            with open(self.path, "r", encoding="ascii", errors="replace") as f:
                holder = f.read().split()
            pid = int(holder[0])
            started = float(holder[1]) if len(holder) > 1 else 0.0
        except Exception:  # noqa: BLE001
            return age > self.timeout       # 内容不可读：给足一个等待窗口再清
        if started and started > time.time() + 60:
            return False                     # 时钟回拨，无法判断，保守当活锁
        return not _pid_alive(pid)

    def __enter__(self):
        deadline = time.time() + self.timeout
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, ("%d %f" % (os.getpid(), time.time())).encode("ascii"))
                return self
            except FileExistsError:
                if self._stale():
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass                     # 删不掉就按普通占用走，避免空转
                    else:
                        continue
                if time.time() > deadline:
                    raise ConfigError("文件正被占用（%s），请稍后重试" % self.path)
                time.sleep(0.15)

    def __exit__(self, *_exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
        finally:
            self.fd = None
            try:
                os.remove(self.path)
            except OSError:
                pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # 没有 os.kill(pid, 0) 的等价物；用 tasklist 过滤 PID 列
        try:
            r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                               capture_output=True, timeout=10)
            # 整词比较：子串匹配会让 PID 123 命中 1234，把死锁误判成活锁
            return any(tok.strip('"') == str(pid)
                       for tok in decode_console(r.stdout or b"").split())
        except Exception:  # noqa: BLE001
            return True                      # 查不出来就当它活着，宁可等
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                          # 存在但不属于我
    except OSError:
        return True
    return True


def file_lock(path: str, timeout: float = 10.0) -> _FileLock:
    """取一把 <path>.lock 独占锁（with 语句用）。"""
    return _FileLock(path, timeout)


def write_atomic(path: str, text: str, encoding: str = "utf-8",
                 newline: str | None = None) -> None:
    """原子写文本：tmp + fsync + os.replace；失败不留 .tmp 残骸。

    全仓唯一的原子写实现。此前四处各写一份，两处漏了 fsync，还有一处（已删）
    是「截断再写」—— 读方会拿到半截内容。
    """
    path = str(path)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def backup_name(path: str) -> str:
    """给 path 取一个当下没被占用的备份名。

    只精确到秒时，同一秒内两次保存会互相覆盖 —— 第二次就把第一次的备份抹了，
    而备份的意义正是在这种连续保存里回退。
    """
    path = str(path)
    base = path + ".bak-" + time.strftime("%Y%m%d_%H%M%S")
    cand, i = base, 1
    while os.path.exists(cand):
        i += 1
        cand = "%s-%d" % (base, i)
    return cand


def _write_config(cfg, raw: dict) -> str:
    """备份 + 原子写回配置（JSON 限定）。返回备份路径。调用方需已持有 file_lock。"""
    import shutil
    ext = os.path.splitext(cfg.path)[1].lower()
    if ext not in (".json",):
        raise ConfigError(
            "当前配置是 %s：从界面写回会丢失注释，暂只支持 JSON 配置。"
            "请把配置改成 .json，或手工编辑后点「重新载入」。" % (ext or "未知格式"))
    backup = backup_name(cfg.path)
    try:
        shutil.copy2(cfg.path, backup)
    except Exception as e:  # noqa: BLE001
        raise ConfigError("备份配置失败：%s" % e)
    write_atomic(cfg.path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    return backup


def update_config(cfg, mutate) -> str:
    """锁内「读全文 → mutate(raw) → 原子写回」。返回备份路径。

    读与写必须同在锁内：两个写者各自读了一份旧内容再写回，后写的会把前者的
    改动整个抹掉。
    """
    with file_lock(cfg.path):
        raw = load_raw(cfg.path)
        if not isinstance(raw, dict):
            raise ConfigError("配置根必须是对象")
        mutate(raw)
        return _write_config(cfg, raw)


def save_buckets(cfg, buckets: list) -> str:
    """把桶列表写回配置文件（先备份、再原子替换）。返回备份路径。"""
    if not isinstance(buckets, list) or not buckets:
        raise ConfigError("buckets 不能为空")

    def _mut(raw):
        raw["buckets"] = buckets
    return update_config(cfg, _mut)


def save_config_keys(cfg, updates: dict) -> str:
    """按点路径写回若干配置项（如 aggregation.provider_url）。返回备份路径。"""
    if not isinstance(updates, dict) or not updates:
        raise ConfigError("没有要保存的配置项")

    def _mut(raw):
        for path, val in updates.items():
            _set_path(raw, str(path), val)
    return update_config(cfg, _mut)


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return list(v)
    return [v]


def _norm_ssh(d, base, what="ssh") -> dict:
    d = _as_dict(d, what)
    return {
        "host": _as_str(d.get("host"), what + ".host"),
        "user": _as_str(d.get("user"), what + ".user", "root") or "root",
        "port": _as_int(d.get("port"), what + ".port", 22),
        "key": expand_path(d.get("key"), base),
        "opts": _as_list(d.get("opts")),
        "timeout": _as_int(d.get("timeout"), what + ".timeout", 20),
    }


def _norm_bucket(raw: dict, base: str, idx: int) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("buckets[%d] 必须是对象" % idx)
    bid = str(raw.get("id") or "").strip()
    if not bid:
        raise ConfigError("buckets[%d] 缺少 id" % idx)
    kind = (_as_str(raw.get("kind"), "桶 %s 的 kind" % bid, "self") or "self").strip()
    if kind not in BUCKET_KINDS:
        raise ConfigError("桶 %s 的 kind 必须是 %s" % (bid, "/".join(BUCKET_KINDS)))
    probe = _as_dict(raw.get("probe"), "桶 %s 的 probe" % bid)
    mode = (_as_str(probe.get("mode"), "桶 %s 的 probe.mode" % bid, "local") or "local").strip()
    if mode not in PROBE_MODES:
        raise ConfigError("桶 %s 的 probe.mode 必须是 %s" % (bid, "/".join(PROBE_MODES)))
    renew = _as_dict(raw.get("renew"), "桶 %s 的 renew" % bid)
    rmode = (_as_str(renew.get("mode"), "桶 %s 的 renew.mode" % bid, "none") or "none").strip()
    if rmode not in RENEW_MODES:
        raise ConfigError("桶 %s 的 renew.mode 必须是 %s" % (bid, "/".join(RENEW_MODES)))
    clone = _as_dict(raw.get("clone"), "桶 %s 的 clone" % bid)
    return {
        "id": bid,
        "label": raw.get("label") or bid,
        "state_key": raw.get("state_key") or ("b_" + bid.lower()),
        "desc": raw.get("desc") or "",
        "kind": kind,
        "enabled": bool(raw.get("enabled", True)),   # 停用桶：不探测/不动作/界面置灰
        "account": raw.get("account") or "",
        "owner": raw.get("owner") or "",
        "mac": (raw.get("mac") or ""),
        "color": raw.get("color") or "",
        "speed_leg": raw.get("speed_leg") or bid,
        "target": raw.get("target") or "",
        "probe": {
            "mode": mode,
            "socks": probe.get("socks") or "",
            "iface": probe.get("iface") or "",
            "ssh": _norm_ssh(probe.get("ssh"), base, "桶 %s 的 probe.ssh" % bid),
            "tries": _as_int(probe.get("tries"), "桶 %s 的 probe.tries" % bid, 3),
            "timeout": _as_int(probe.get("timeout"), "桶 %s 的 probe.timeout" % bid, 6),
        },
        "renew": {
            "mode": rmode,
            "wan": renew.get("wan") or "",
            "iface": renew.get("iface") or "",
            "argv": renew.get("argv"),
            "ssh": _norm_ssh(renew.get("ssh") or probe.get("ssh"), base,
                             "桶 %s 的 renew.ssh" % bid),
            "gate": bool(renew.get("gate", kind == "clone")),
            "owner_label": renew.get("owner_label") or raw.get("account") or bid,
            "hint": renew.get("hint") or "",
        },
        "clone": {
            "portal_id": clone.get("portal_id") or bid,
            "gate": bool(clone.get("gate", True)),
            "owner_label": clone.get("owner_label") or raw.get("account") or bid,
            "expect_whitelist": bool(clone.get("expect_whitelist", True)),
        },
    }


def _norm_action(raw, base: str) -> dict:
    if raw is None:
        return {"argv": None, "cwd": "", "timeout": 120, "confirm": "", "desc": "",
                "show": True, "shell": False}
    if isinstance(raw, (list, str)):
        raw = {"argv": raw}
    if not isinstance(raw, dict):
        raise ConfigError("actions 的每个动作必须是对象/列表/字符串")
    argv = raw.get("argv")
    if isinstance(argv, str):
        shell = True
        argv_list = argv
    elif isinstance(argv, list):
        shell = False
        argv_list = [str(x) for x in argv]
    else:
        argv_list = None
        shell = False
    return {
        "argv": argv_list,
        "cwd": expand_path(raw.get("cwd"), base),
        "timeout": _as_int(raw.get("timeout"), "动作 timeout", 120),
        "confirm": raw.get("confirm") or "",
        "desc": raw.get("desc") or "",
        "show": bool(raw.get("show", True)),
        "shell": shell,
    }


def _norm_legs(raw, base: str, data_dir: str, api_fallback: str = "") -> dict:
    """腿列表维护配置（随附的 leg_ctl.py 用）：母本全量文件 + 生效文件 + 热更新入口。

    · provider_file：聚合出口**实际读取**的节点列表（会被脚本按剔除集合重渲染）
    · full_file    ：全量定义母本，留空 = 同目录 <名>.full.yaml（首次自动播种）
    · provider_name：热更新时的 provider 名（mihomo：PUT /providers/proxies/<名>）
    """
    d = _as_dict(raw, "aggregation.legs")
    pf = "" if is_unset(d.get("provider_file")) else expand_path(d.get("provider_file"), base)
    full = "" if is_unset(d.get("full_file")) else expand_path(d.get("full_file"), base)
    if pf and not full:
        root, ext = os.path.splitext(pf)
        full = root + ".full" + (ext or ".yaml")
    return {
        "provider_file": pf,
        "full_file": full,
        "provider_name": "" if is_unset(d.get("provider_name"))
                         else _as_str(d.get("provider_name"), "aggregation.legs.provider_name").strip(),
        "api": (("" if is_unset(d.get("api"))
                 else _as_str(d.get("api"), "aggregation.legs.api"))
                or api_fallback or "").rstrip("/"),
        "state_file": expand_path(d.get("state_file") or "legs_excluded.json", data_dir),
        "keep_min": max(0, _as_int(d.get("keep_min"), "aggregation.legs.keep_min", 1)),
        "backup_keep": max(0, _as_int(d.get("backup_keep"), "aggregation.legs.backup_keep", 10)),
    }


class Cfg:
    """归一化后的配置视图。"""

    def __init__(self, raw: dict, path: str):
        if not isinstance(raw, dict):
            raise ConfigError("配置根必须是对象")
        self.raw = raw
        self.path = os.path.abspath(path)
        self.base_dir = os.path.dirname(self.path)

        app = _as_dict(raw.get("app"), "app")
        self.data_dir = expand_path(app.get("data_dir") or "~/.bucket_console", self.base_dir)
        self.state_file = expand_path(app.get("state_file") or "state.json", self.data_dir)
        self.portal_file = expand_path(app.get("portal_file") or "portal_status.json", self.data_dir)
        self.events_file = expand_path(app.get("events_file") or "console_events.log", self.data_dir)
        self.speedtest_result = expand_path(app.get("speedtest_result") or "speedtest.json", self.data_dir)

        ui = _as_dict(raw.get("ui"), "ui")
        self.ui = {
            "title": ui.get("title") or "多桶聚合控制台",
            "tray_title": ui.get("tray_title") or "多桶聚合 · 总开关",
            "interval_ms": _as_int(ui.get("interval_ms"), "ui.interval_ms", 15000),
            "spark_samples": _as_int(ui.get("spark_samples"), "ui.spark_samples", 240),
            "window_w": _as_int(ui.get("window_w"), "ui.window_w", 1000),
            "window_h": _as_int(ui.get("window_h"), "ui.window_h", 860),
            "font": ui.get("font") or "",
            "mono_font": ui.get("mono_font") or "Consolas",
        }

        lc = _as_dict(raw.get("lifecycle"), "lifecycle")
        self.lifecycle = {
            "heartbeat_file": expand_path(lc.get("heartbeat_file") or "ui_alive.json", self.data_dir),
            "boot_action": lc.get("boot_action") or "",
            "boot_delay_ms": _as_int(lc.get("boot_delay_ms"), "lifecycle.boot_delay_ms", 3000),
            "stop_action": lc.get("stop_action") or "",
            "exit_confirm": lc.get("exit_confirm") or (
                "退出后控制台会执行「全链停」：\n"
                "  · 按配置停掉虚拟路由 / 聚合出口等受管服务\n"
                "  · 撤除 UI 心跳（配套任务随即停手）\n\n确认退出？"),
            "teardown_stamp": expand_path(lc.get("teardown_stamp") or "teardown.stamp", self.data_dir),
            "on_start_remove": [expand_path(p, self.data_dir) for p in _as_list(lc.get("on_start_remove"))],
        }

        pr = _as_dict(raw.get("probe"), "probe")
        self.probe = {
            # 仍是 <占位符> 的一律视为未配置，避免拿占位符去发请求
            "target": "" if is_unset(pr.get("target")) else pr["target"],
            "curl": pr.get("curl") or "curl",
            "portal_markers": _as_list(pr.get("portal_markers")) or [
                "WISPAccessGatewayParam", "NextURL"],
            # 丢包率：ICMP 开关 / 包数 / 目标（留空=取探活地址的主机名）
            "icmp": bool(pr.get("icmp", True)),
            "icmp_count": _as_int(pr.get("icmp_count"), "probe.icmp_count", 5),
            "ping_host": "" if is_unset(pr.get("ping_host")) else (pr.get("ping_host") or ""),
        }

        ag = _as_dict(raw.get("aggregation"), "aggregation")
        br = _as_dict(ag.get("breaker"), "aggregation.breaker")
        self.aggregation = {
            "api": "" if is_unset(ag.get("api")) else (ag["api"] or "").rstrip("/"),
            "group": "" if is_unset(ag.get("group")) else (ag.get("group") or ""),
            "provider": "" if is_unset(ag.get("provider")) else (ag.get("provider") or ""),
            # 节点源地址：界面里填，actions.refresh_provider 可用 {provider_url}
            "provider_url": "" if is_unset(ag.get("provider_url")) else (ag.get("provider_url") or ""),
            "direct_members": _as_list(ag.get("direct_members")) or ["DIRECT"],
            "mode_file": expand_path(ag.get("mode_file") or "aggregator_mode.txt", self.data_dir),
            "restart_action": ag.get("restart_action") or "restart_aggregator",
            "proxy_enabled": bool(ag.get("proxy_enabled", True)),
            "dir": expand_path(ag.get("dir"), self.base_dir),
            # 分流熔断（2026-09-09 由内部测试版通用化回灌）：丢包过高的桶腿自动隔离/恢复。
            # 决策在本控制台，执行走 actions.isolate_leg / restore_leg（用户自己配置的命令）。
            "breaker": {
                "enabled": bool(br.get("enabled", True)),
                "threshold_pct": _as_int(br.get("threshold_pct"), "breaker.threshold_pct", 50),
                "trip_after": _as_int(br.get("trip_after"), "breaker.trip_after", 2),
                "recover_below_pct": _as_int(br.get("recover_below_pct"), "breaker.recover_below_pct", 10),
                "recover_after": _as_int(br.get("recover_after"), "breaker.recover_after", 3),
                "tries": _as_int(br.get("tries"), "breaker.tries", 5),
                "interval_s": max(15, _as_int(br.get("interval_s"), "breaker.interval_s", 60)),
                "keep_min": max(0, _as_int(br.get("keep_min"), "breaker.keep_min", 1)),
                "start_delay_s": max(0, _as_int(br.get("start_delay_s"), "breaker.start_delay_s", 12)),
            },
            # 腿列表维护（供随附的 leg_ctl.py 使用；不填则熔断只做采样与提示）
            "legs": _norm_legs(ag.get("legs"), self.base_dir, self.data_dir,
                               "" if is_unset(ag.get("api")) else (ag.get("api") or "")),
        }

        po = _as_dict(raw.get("portal"), "portal")
        self.portal = {
            "global_tok_file": expand_path(po.get("global_tok_file"), self.data_dir),
            "per_account_pattern": po.get("per_account_pattern") or "portal_tok_{account}.txt",
            # 基准是 data_dir，与 global_tok_file 一致；不用配置文件目录 —— 否则
            # tok_dir 填一个相对路径（模板旧默认值就是 "."）会把逐账号凭据写进仓库树内。
            "tok_dir": expand_path(po.get("tok_dir") or self.data_dir, self.data_dir),
            "owner_field": po.get("owner_field") or "tokenOwner",
            "accounts": [str(a) for a in _as_list(po.get("accounts"))],
        }

        vm = _as_dict(raw.get("vm"), "vm")
        self.vm = {
            "enabled": bool(vm.get("enabled", False)),
            "label": vm.get("label") or "虚拟路由",
            "note": vm.get("note") or "",
        }

        self.buckets = [_norm_bucket(b, self.base_dir, i)
                        for i, b in enumerate(_as_list(raw.get("buckets")))]
        if not self.buckets:
            raise ConfigError("buckets 不能为空：至少配置一个桶")

        self.actions = {str(k): _norm_action(v, self.base_dir)
                        for k, v in _as_dict(raw.get("actions"), "actions").items()
                        if not str(k).startswith("_")}   # 跳过 _note 等说明键

        self.tasks = []
        for t in _as_list(raw.get("tasks")):
            if isinstance(t, str):
                self.tasks.append({"name": t, "desc": "", "gate": "ui_heartbeat"})
            elif isinstance(t, dict) and t.get("name"):
                self.tasks.append({"name": t["name"], "desc": t.get("desc") or "",
                                   "gate": t.get("gate") or ""})

        self.bucket_by_id = {b["id"]: b for b in self.buckets}

    # ---------------- 便捷查询 ----------------
    def action(self, name: str) -> dict | None:
        a = self.actions.get(name)
        if not a or is_unset(a.get("argv")):
            return None
        return a

    def has_action(self, name: str) -> bool:
        return self.action(name) is not None

    def subs(self, bucket: dict | None = None, **extra) -> dict:
        s = {
            "config": self.path,
            "data_dir": self.data_dir,
            "target": self.probe.get("target") or "",
            "group": self.aggregation.get("group") or "",
            "provider": self.aggregation.get("provider") or "",
            "provider_url": self.aggregation.get("provider_url") or "",
        }
        if bucket:
            s.update({
                "bucket": bucket["id"],
                "account": bucket.get("account") or "",
                "mac": bucket.get("mac") or "",
                "leg": bucket.get("speed_leg") or bucket["id"],
                "iface": (bucket.get("probe") or {}).get("iface")
                         or (bucket.get("renew") or {}).get("iface") or "",
                "wan": (bucket.get("renew") or {}).get("wan") or "",
            })
        s.update(extra)
        return s

    # 只替换「已知占位符」，未知 {xxx} 原样保留。
    # 为什么：命令里常有 curl -w '%{http_code}' / awk '{print $1}' 这类花括号，
    # 用 str.format(**s) 会直接 KeyError，按钮一点就抛异常（2026-09-09 由内部测试版回灌）。
    _SUBST_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

    @classmethod
    def _subst(cls, text: str, s: dict) -> str:
        def rep(m):
            k = m.group(1)
            return str(s[k]) if k in s else m.group(0)
        return cls._SUBST_RE.sub(rep, text)

    def argv_for(self, name: str, bucket: dict | None = None, **extra):
        """取动作命令并做占位符替换；未配置返回 None。"""
        a = self.action(name)
        if not a:
            return None
        s = self.subs(bucket, **extra)
        argv = a["argv"]
        if isinstance(argv, str):
            return self._subst(argv, s)
        return [self._subst(str(x), s) for x in argv]

    def problems(self) -> list[str]:
        """结构性配置问题（与 unset_fields 不同：这些是「填了但互相矛盾/会互相覆盖」）。

        --check 会把这些当作 FAIL 并以非零退出码返回（2026-09-09 增强）。
        """
        out = []
        ids = [b["id"] for b in self.buckets]
        dup = sorted({x for x in ids if ids.count(x) > 1})
        if dup:
            out.append("buckets 存在重复 id：%s（后者会覆盖前者）" % dup)
        sks = [b["state_key"] for b in self.buckets]
        dups = sorted({x for x in sks if sks.count(x) > 1})
        if dups:
            out.append("buckets 存在重复 state_key：%s（状态会互相覆盖）" % dups)
        for b in self.buckets:
            p, r, c = b["probe"], b["renew"], b["clone"]
            if p.get("mode") == "socks" and not p.get("socks"):
                out.append("桶 %s：probe.mode=socks 但未填 probe.socks" % b["id"])
            if p.get("mode") == "ssh" and not (p.get("ssh") or {}).get("host"):
                out.append("桶 %s：probe.mode=ssh 但未填 probe.ssh.host" % b["id"])
            if r.get("mode") == "ssh_ifup" and not (r.get("ssh") or {}).get("host"):
                out.append("桶 %s：renew.mode=ssh_ifup 但未填 renew.ssh.host" % b["id"])
            if r.get("mode") == "ssh_ifup" and not r.get("wan"):
                out.append("桶 %s：renew.mode=ssh_ifup 但未填 renew.wan" % b["id"])
            if b.get("kind") == "clone" and not c.get("portal_id"):
                out.append("桶 %s：克隆桶缺 clone.portal_id" % b["id"])
            if b.get("kind") == "clone" and c.get("gate", True) and not b.get("account"):
                out.append("桶 %s：克隆桶开了真机闸但未填 account（判定会一直未知→拦截）" % b["id"])
        lg = self.aggregation.get("legs") or {}
        pf, full = lg.get("provider_file") or "", lg.get("full_file") or ""
        if pf and full and os.path.abspath(pf) == os.path.abspath(full):
            out.append("aggregation.legs：provider_file 与 full_file 是同一个文件"
                       "（渲染会覆盖母本，腿会永久丢失）")
        if pf and not os.path.exists(pf):
            out.append("aggregation.legs.provider_file 不存在：%s" % pf)
        return out

    def unset_fields(self) -> list[tuple[str, str]]:
        """返回 [(配置项, 填写提示)]，供 --check 与界面提示。"""
        raw_app = self.raw.get("app") or {}
        raw_probe = self.raw.get("probe") or {}
        raw_ag = self.raw.get("aggregation") or {}
        out = []
        checks = [
            ("app.data_dir", raw_app.get("data_dir")),
            ("probe.target", raw_probe.get("target")),
            ("aggregation.api", raw_ag.get("api")),
            ("portal.file", raw_app.get("portal_file")),
            ("actions.refresh_state.argv", self.action("refresh_state")),
            ("actions.refresh_portal.argv", self.action("refresh_portal")),
            ("actions.restart_aggregator.argv", self.action("restart_aggregator")),
            ("actions.stop_chain.argv", self.action("stop_chain")),
            ("actions.boot_chain.argv", self.action("boot_chain")),
            ("actions.speedtest.argv", self.action("speedtest")),
        ]
        for key, val in checks:
            if is_unset(val):
                out.append((key, UNSET_HINTS.get(key, "")))
        return out


def load_raw(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError("配置文件不存在: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except Exception:
            raise ConfigError("YAML 配置需要 PyYAML；可改用 JSON 配置（推荐，零依赖）")
        with open(path, "r", encoding="utf-8-sig") as f:
            return yaml.safe_load(f)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError("JSON 解析失败 %s:%d:%d — %s" % (path, e.lineno, e.colno, e.msg))


def load_config(path: str) -> Cfg:
    return Cfg(load_raw(path), path)


def default_config_path(explicit: str | None = None) -> str:
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("config.private.json", "config.json", "config.private.yaml", "config.example.json"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            return p
    return os.path.join(here, "config.example.json")


def split_argv(argv):
    """把字符串命令拆成 argv（仅供需要预览的场合）。"""
    return shlex.split(argv, posix=os.name != "nt")


def config_summary(cfg: Cfg) -> str:
    lines = ["== 配置自检 %s ==" % cfg.path,
             "  数据目录: %s" % cfg.data_dir,
             "  状态文件: %s" % cfg.state_file,
             "  门户文件: %s" % (cfg.portal_file or "（未配置）"),
             "  探活地址: %s" % (cfg.probe.get("target") or "（未配置）"),
             "  聚合控制面: %s" % (cfg.aggregation.get("api") or "（未配置）"),
             "  桶: %d 个" % len(cfg.buckets)]
    for b in cfg.buckets:
        lines.append("    - %-10s kind=%-5s probe=%-6s renew=%-9s 账号=%s"
                     % (b["id"], b["kind"], b["probe"]["mode"], b["renew"]["mode"],
                        b.get("account") or "—"))
    lines.append("  动作: %s" % (", ".join(sorted(cfg.actions)) or "（无）"))
    lines.append("  计划任务: %s" % (", ".join(t["name"] for t in cfg.tasks) or "（无）"))
    missing = cfg.unset_fields()
    if missing:
        lines.append("  未配置项 %d 个（不影响启动，相关按钮会提示）：" % len(missing))
        for key, hint in missing:
            lines.append("    · %s —— %s" % (key, hint))
    else:
        lines.append("  未配置项: 无 ✓")
    probs = cfg.problems()
    if probs:
        lines.append("  !! 结构性问题 %d 个（需修正，--check 将以非零退出）：" % len(probs))
        for p in probs:
            lines.append("    · %s" % p)
    else:
        lines.append("  结构性问题: 无 ✓")
    return "\n".join(lines)
