"""
Unicode 安全文件名清理。

werkzeug.secure_filename 的实现把所有非 ASCII 字符整体丢弃，导致中文/日文/韩文
文件名退化成空主名（"中文.docx" → ".docx"），多个 KB 文档因此互相覆盖。

本模块的 unicode_safe_filename：
- 保留 Unicode 字母数字（含 CJK、emoji）
- 删除路径分隔符 / \\ 与连续点（防止目录穿越）
- 删除控制字符与零宽字符
- 删除 Windows 保留字符 < > : " | ? *
- 保留扩展名（最后一个点之后的内容，限制 ASCII 字母数字）
- 空名 / 全空白名 / 仅扩展名 → 时间戳 fallback
- 总长度限制 200 字节（兼顾文件系统和 SQLite 存储）
"""
from __future__ import annotations

import re
import time
import unicodedata

# Windows 禁忌字符
_WINDOWS_RESERVED = '<>:"|?*'
# 路径分隔符
_PATH_SEP = "/\\"
# 控制字符 0x00-0x1F + DEL（动态构造避免源码内 NUL 字节问题）
_CONTROL_CHARS = "".join(chr(c) for c in range(0x20)) + chr(0x7F)
# 零宽字符 / BOM / Word Joiner（动态构造，避免源码内不可见字符）
_ZERO_WIDTH = "".join(chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))

_FORBIDDEN_CHARS = set(_WINDOWS_RESERVED + _PATH_SEP + _CONTROL_CHARS + _ZERO_WIDTH)

# 扩展名只允许 ASCII 字母数字
_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")

_MAX_BYTES = 200


def _split_ext(name: str) -> tuple[str, str]:
    """切出最后一个点为扩展名分界；扩展名必须是 ASCII 字母数字才认。"""
    if "." not in name:
        return name, ""
    stem, ext = name.rsplit(".", 1)
    if not _EXT_RE.match(ext):
        return name, ""
    return stem, "." + ext.lower()


def _strip_forbidden(s: str) -> str:
    return "".join(ch for ch in s if ch not in _FORBIDDEN_CHARS)


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断，且不切断多字节字符。"""
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def unicode_safe_filename(raw: str) -> str:
    """返回适合保存为文件 + 写入 SQLite 的安全文件名。

    保留 Unicode（含 CJK），删除路径分隔符/控制字符/Windows 保留字符。
    空名或全部被剥离时返回时间戳形式的 fallback。
    """
    if not isinstance(raw, str):
        raw = ""

    # 1. 取末段（防 "../../etc/passwd" 这种）
    raw = raw.replace("\\", "/")
    last = raw.rsplit("/", 1)[-1]

    # 2. NFC 归一化
    last = unicodedata.normalize("NFC", last)

    # 3. 切扩展名
    stem, ext = _split_ext(last)

    # 4. 剥离危险字符
    stem = _strip_forbidden(stem)

    # 5. 剥离首尾点和空白
    stem = stem.strip(" .\t\n\r")

    # 6. 多重点压成单点（".." 已被前面处理掉，这里兜底）
    stem = re.sub(r"\.{2,}", ".", stem)

    # 7. 空名 fallback
    if not stem:
        stem = f"upload_{int(time.time() * 1000)}"

    # 8. 长度兜底
    full = stem + ext
    full = _truncate_to_bytes(full, _MAX_BYTES)
    if ext and not full.endswith(ext):
        room = _MAX_BYTES - len(ext.encode("utf-8"))
        if room > 0:
            full = _truncate_to_bytes(stem, room) + ext
        else:
            full = ext.lstrip(".")

    if not full or full == ext:
        full = f"upload_{int(time.time() * 1000)}{ext}"

    return full
