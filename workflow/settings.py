"""配置与 Windows 当前用户凭据。"""

from ctypes import POINTER, Structure, byref, c_char, c_uint32, c_void_p, cast, create_string_buffer, windll
from json import dumps, loads
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"


class WorkflowError(Exception):
    """向用户展示且不自动重试的工作流错误。"""


def load_settings():
    settings = loads((ROOT / "settings.json").read_text(encoding="utf-8-sig"))
    if settings.get("feishu_enabled"):
        raise WorkflowError("第一版尚未实现飞书读取，请保持 feishu_enabled 为 false。")
    if not isinstance(settings["review"]["max_fix_rounds"], int) or settings["review"]["max_fix_rounds"] < 0:
        raise WorkflowError("修复轮次上限必须为非负整数。")
    if not settings["codex"]["command"] or not isinstance(settings["codex"]["command"], list):
        raise WorkflowError("codex.command 必须是明确的命令参数数组。")
    compile_check = settings.get("compile_check", {"enabled": False})
    if not isinstance(compile_check, dict) or type(compile_check.get("enabled")) is not bool:
        raise WorkflowError("compile_check.enabled 必须是布尔值。")
    settings["compile_check"] = compile_check
    return settings


def save_settings(settings):
    destination = ROOT / "settings.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


class DataBlob(Structure):
    _fields_ = [("length", c_uint32), ("data", POINTER(c_char))]


def _protect(data, decrypt=False):
    source_buffer = create_string_buffer(data)
    source = DataBlob(len(data), cast(source_buffer, POINTER(c_char)))
    result = DataBlob()
    if decrypt:
        ok = windll.crypt32.CryptUnprotectData(byref(source), None, None, None, None, 1, byref(result))
    else:
        ok = windll.crypt32.CryptProtectData(byref(source), None, None, None, None, 1, byref(result))
    if not ok:
        raise WorkflowError("Windows 凭据加解密失败，请在原 Windows 用户下重新保存密码。")
    try:
        return bytes(result.data[:result.length])
    finally:
        windll.kernel32.LocalFree.argtypes = [c_void_p]
        windll.kernel32.LocalFree(cast(result.data, c_void_p))


def save_password(password):
    if not password:
        raise WorkflowError("密码不能为空。")
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / "zentao-password.bin").write_bytes(_protect(password.encode("utf-8")))


def load_password():
    path = LOCAL / "zentao-password.bin"
    if not path.is_file():
        raise WorkflowError("尚未保存禅道密码，请在客户端的账号设置中保存。")
    return _protect(path.read_bytes(), decrypt=True).decode("utf-8")
