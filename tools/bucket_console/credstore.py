# -*- coding: utf-8 -*-
"""
credstore.py — 多桶聚合控制台 · 凭据库（跨平台 · 明文永不落盘）
================================================================================
用途：把「探测 / 认证脚本需要用的账号与凭据」加密存放，界面只显示占位星号，
      脚本在真正调用接口的那一刻才解密取用。

后端选择（自动，按可用性降级）：

  | 顺序 | 后端        | 条件                       | 安全性                                   |
  |------|-------------|----------------------------|------------------------------------------|
  | 1    | dpapi       | Windows                    | 由系统按当前用户凭据托管密钥，无密钥文件 |
  | 2    | keyring     | 已安装 keyring 且有后端    | 走系统钥匙串（GNOME/KDE/macOS 钥匙串）   |
  | 3    | file        | 以上都不可用               | **明文 base64 + 0600 权限**，仅防误看    |

  `backend()` 返回当前后端；`insecure()` 为真时界面会显著提示「当前后端不加密」。

设计边界（与内部测试版一致）：
  · 明文只存在于进程内存，绝不写盘、不打日志；
  · 写入用同目录 .lock 独占创建 + 临时文件原子替换；
  · 「录入即锁定」：录入后界面不可查看 / 修改 / 重录，更换 = 删除后重新录入。

CLI：
  python credstore.py list
  python credstore.py has  <账号>
  python credstore.py set  <账号>            # 交互隐藏输入两次
  echo -n '<凭据>' | python credstore.py set <账号> --stdin
  python credstore.py get  <账号>            # 仅此刻输出明文，供脚本捕获；勿留日志
  python credstore.py remove <账号>
  python credstore.py backend
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time

ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
ENTROPY = b"bucket-console/credstore/v1"     # 应用标识，不是密钥
DEFAULT_STORE_NAME = "credentials.json"
LOCK_TIMEOUT = 10.0


# --------------------------------------------------------------------------
# 平台后端
# --------------------------------------------------------------------------
def _dpapi_available() -> bool:
    return os.name == "nt"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI（Scope=CurrentUser）。ctypes 直调，不依赖 pywin32。"""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _make(raw: bytes):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    # 注意：Protect 与 Unprotect 的第二个参数语义不同（Protect=说明串输入，
    # Unprotect=说明串输出指针），两者都传 NULL 即可，不要复用同一份 argtypes。
    common = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.POINTER(BLOB),
              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(BLOB)]
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = common
    fn.restype = wintypes.BOOL

    blob_in, _keep = _make(data)
    ent, _keep2 = _make(ENTROPY)
    blob_out = BLOB()
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(ent), None, None, 0,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI 调用失败（GetLastError=%d）" % kernel32.GetLastError())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _keyring():
    try:
        import keyring  # type: ignore
        return keyring
    except Exception:
        return None


def backend() -> str:
    if _dpapi_available():
        return "dpapi"
    kr = _keyring()
    if kr is not None:
        try:
            kr.get_keyring()
            return "keyring"
        except Exception:
            pass
    return "file"


def insecure() -> bool:
    return backend() == "file"


def backend_note() -> str:
    b = backend()
    if b == "dpapi":
        return "DPAPI（当前 Windows 用户 · 无密钥文件，密文换机/换用户不可解）"
    if b == "keyring":
        return "系统钥匙串（keyring）"
    return "⚠ 本机文件（明文 base64 + 0600 权限）——仅防误看，不是加密；建议安装 keyring"


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------
def store_path() -> str:
    env = os.environ.get("BUCKET_CONSOLE_STORE")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = os.environ.get("BUCKET_CONSOLE_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".bucket_console")
    return os.path.join(os.path.abspath(os.path.expanduser(base)), DEFAULT_STORE_NAME)


def _lock_path() -> str:
    return store_path() + ".lock"


class _Lock:
    """跨进程独占锁（O_EXCL 创建，超时放弃）。"""

    def __init__(self, path: str, timeout: float = LOCK_TIMEOUT):
        self.path = path
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.time() > deadline:
                    raise TimeoutError("凭据库被占用（%s），请稍后重试" % self.path)
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


def _read() -> dict:
    path = store_path()
    if not os.path.exists(path):
        return {"version": 1, "backend": backend(), "accounts": {}}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        raise OSError("凭据库损坏或不可读: %s (%s)" % (path, e))
    if not isinstance(data, dict):
        raise OSError("凭据库格式异常: %s" % path)
    data.setdefault("accounts", {})
    return data


def _write(data: dict) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def account_ok(name: str) -> bool:
    return bool(name and ACCOUNT_RE.match(name))


# --------------------------------------------------------------------------
# 对外 API（供 bucket_console.py 使用）
# --------------------------------------------------------------------------
def list_accounts() -> list[dict]:
    data = _read()
    out = []
    for acct in sorted(data.get("accounts", {})):
        item = data["accounts"][acct] or {}
        out.append({
            "account": acct,
            "has": bool(item.get("secret")),
            "created": item.get("created") or "",
            "updated": item.get("updated") or "",
        })
    return out


def has_secret(acct: str) -> bool:
    data = _read()
    return bool((data.get("accounts", {}).get(acct) or {}).get("secret"))


def register_account(acct: str) -> None:
    """登记账号（可先无凭据占位）。已存在则保持原状。"""
    if not account_ok(acct):
        raise ValueError("账号名不合法：字母数字开头，含 [A-Za-z0-9_-]，最长 64")
    with _Lock(_lock_path()):
        data = _read()
        item = data["accounts"].get(acct) or {}
        item.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))
        data["accounts"][acct] = item
        _write(data)


def set_secret(acct: str, plain: str) -> None:
    """写入凭据。已存在则覆盖（界面层不提供覆盖入口，走删除后重录）。"""
    if not account_ok(acct):
        raise ValueError("账号名不合法")
    if plain is None or plain == "":
        raise ValueError("凭据不能为空")
    raw = plain.encode("utf-8")
    b = backend()
    if b == "dpapi":
        blob = base64.b64encode(_dpapi(raw, True)).decode("ascii")
        payload = {"secret": blob, "enc": "dpapi"}
    elif b == "keyring":
        _keyring().set_secret("bucket-console", acct, plain)
        payload = {"secret": "keyring", "enc": "keyring"}
    else:
        payload = {"secret": base64.b64encode(raw).decode("ascii"), "enc": "plain"}

    with _Lock(_lock_path()):
        data = _read()
        item = data["accounts"].get(acct) or {}
        item.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))
        item.update(payload)
        item["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data["accounts"][acct] = item
        data["backend"] = b
        _write(data)


def get_secret(acct: str) -> str:
    """解密取用（仅供脚本调用接口那一刻使用，勿落日志）。"""
    data = _read()
    item = data.get("accounts", {}).get(acct)
    if not item or not item.get("secret"):
        raise KeyError("凭据库中没有 %s 的凭据" % acct)
    enc = item.get("enc") or data.get("backend") or "plain"
    if enc == "dpapi":
        return _dpapi(base64.b64decode(item["secret"]), False).decode("utf-8")
    if enc == "keyring":
        v = _keyring().get_secret("bucket-console", acct)
        if v is None:
            raise KeyError("系统钥匙串中未找到 %s 的凭据" % acct)
        return v
    return base64.b64decode(item["secret"]).decode("utf-8")


def remove_account(acct: str) -> None:
    with _Lock(_lock_path()):
        data = _read()
        data.get("accounts", {}).pop(acct, None)
        _write(data)
    if backend() == "keyring":
        try:
            _keyring().delete_password("bucket-console", acct)
        except Exception:
            pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    acct = argv[1] if len(argv) > 1 else ""

    if cmd == "backend":
        print("后端: %s" % backend())
        print("说明: %s" % backend_note())
        print("密文库: %s" % store_path())
        return 0
    if cmd == "list":
        rows = list_accounts()
        print("后端=%s  密文库=%s" % (backend(), store_path()))
        for it in rows:
            print("  %-24s %s  录入=%s 更新=%s"
                  % (it["account"], "已录入 *******" if it["has"] else "未配置",
                     it["created"] or "—", it["updated"] or "—"))
        if not rows:
            print("  （空）")
        return 0
    if cmd == "has":
        return 0 if has_secret(acct) else 1
    if cmd == "get":
        sys.stdout.write(get_secret(acct))
        return 0
    if cmd == "set":
        if "--stdin" in argv[2:]:
            plain = sys.stdin.read().rstrip("\r\n")
        else:
            import getpass
            p1 = getpass.getpass("凭据: ")
            p2 = getpass.getpass("确认: ")
            if p1 != p2:
                print("两次输入不一致", file=sys.stderr)
                return 2
            plain = p1
        set_secret(acct, plain)
        print("已写入 %s（后端 %s）" % (acct, backend()))
        return 0
    if cmd == "remove":
        remove_account(acct)
        print("已删除 %s" % acct)
        return 0
    print("未知子命令: %s（见 --help）" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
