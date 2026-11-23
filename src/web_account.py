import ctypes as ct
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import re
import sys
import time


class DataBlob(ct.Structure):
    _fields_ = [("size", w.DWORD), ("data", ct.POINTER(ct.c_ubyte))]


def account_path():
    return Path(os.environ["LOCALAPPDATA"]) / "DesktopAI" / "web-account.bin"


def protect(value, decrypt=False):
    buffer = ct.create_string_buffer(value)
    source = DataBlob(len(value), ct.cast(buffer, ct.POINTER(ct.c_ubyte)))
    output = DataBlob()
    crypt = ct.WinDLL("crypt32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = (ct.POINTER(DataBlob), ct.c_void_p, ct.c_void_p, ct.c_void_p,
                         ct.c_void_p, w.DWORD, ct.POINTER(DataBlob))
    kernel.LocalFree.argtypes = (ct.c_void_p,)
    if not function(ct.byref(source), None, None, None, None, 1, ct.byref(output)):
        raise OSError(ct.get_last_error(), "Windows could not access the saved DeepSeek account.")
    try:
        return ct.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def save_account(account):
    path = account_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(protect(json.dumps(account, ensure_ascii=False).encode("utf-8")))
    temporary.replace(path)


def load_account():
    account = json.loads(protect(account_path().read_bytes(), decrypt=True))
    if not isinstance(account, dict) or not isinstance(account.get("token"), str) or not account["token"]:
        raise ValueError("本机保存的 DeepSeek 登录数据不完整，请重新导入。")
    return account


def edge_data_directory():
    return Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Edge/User Data"


def edge_profiles():
    base = edge_data_directory()
    try:
        state = json.loads((base / "Local State").read_text(encoding="utf-8"))
        info = state.get("profile", {}).get("info_cache", {})
        last_used = state.get("profile", {}).get("last_used", "Default")
    except (OSError, ValueError):
        info, last_used = {}, "Default"
    directories = {name for name in info if re.fullmatch(r"Default|Profile \d+", name)}
    if base.is_dir():
        directories.update(path.name for path in base.iterdir()
                           if path.is_dir() and re.fullmatch(r"Default|Profile \d+", path.name))
    directories.add("Default")
    return [
        (directory, str(info.get(directory, {}).get("name") or
                        ("默认用户" if directory == "Default" else directory)))
        for directory in sorted(directories, key=lambda name: (name != last_used, name != "Default", name))
    ]


def import_edge_login(profile="Default"):
    if not re.fullmatch(r"Default|Profile \d+", profile):
        raise ValueError("请选择列表中的 Edge 用户配置。")
    sys.path.insert(0, str(Path(__file__).with_name("_vendor")))
    from ccl_leveldb import RawLevelDb, KeyState
    directory = edge_data_directory() / profile / "Local Storage/leveldb"
    if not directory.is_dir():
        raise ValueError("所选 Edge 用户配置尚未保存登录，请先点击「登录 DeepSeek」。")
    key = b"_https://chat.deepseek.com\x00\x01userToken"
    for attempt in range(3):
        try:
            with RawLevelDb(directory) as database:
                records = (row for row in database.iterate_records_raw() if row.user_key == key)
                latest = max(records, key=lambda row: row.seq, default=None)
            break
        except FileNotFoundError:
            if attempt == 2:
                raise
            time.sleep(.05)
    if latest is None or latest.state != KeyState.Live or not latest.value:
        raise ValueError("所选用户配置尚未登录 DeepSeek，请完成登录后再导入。")
    encoded = latest.value
    try:
        wrapper = json.loads(encoded[1:].decode("latin1" if encoded[0] == 1 else "utf-16-le"))
        token = wrapper.get("value") if isinstance(wrapper, dict) else wrapper
    except (ValueError, UnicodeError):
        raise ValueError("DeepSeek 登录数据格式已变化，请重新登录后再试。") from None
    if not isinstance(token, str) or len(token) < 20:
        raise ValueError("DeepSeek 登录数据格式已变化。")
    try:
        previous = load_account()
    except (OSError, ValueError):
        previous = {}
    # Reuse a conversation only when the imported credentials match.
    account = previous.copy() if previous.get("token") == token else {}
    account.update(token=token, edge_profile=profile)
    return account
