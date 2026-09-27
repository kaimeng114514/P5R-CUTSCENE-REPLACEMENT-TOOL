# -*- coding: utf-8 -*-
"""ASCII 临时工作区管理。

cricodecs（nanobind/C++ 层）在 Windows 上无法打开含非 ASCII 字符的路径，
因此所有需要传给 cricodecs 的中间文件一律放在纯 ASCII 临时目录中，
输入侧大文件（CPK/USM）用 file-like 句柄或硬链接桥接，最终产物由 Python
（原生支持中文路径）复制回用户目录。
"""
import os
import shutil
import tempfile
import uuid
from pathlib import Path

# 优先使用的 ASCII 工作区根目录（避免 %TEMP% 位于中文用户目录下）
_ASCII_ROOTS = [r"C:\Temp\P5R_CutsceneTool", r"C:\P5R_CutsceneTool", r"C:\Windows\Temp\P5R_CutsceneTool"]


def _pick_root() -> Path:
    for root in _ASCII_ROOTS:
        try:
            p = Path(root)
            p.mkdir(parents=True, exist_ok=True)
            # 验证该目录可写
            probe = p / ".write_test"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            return p
        except OSError:
            continue
    # 兜底：当前工作目录（若为 ASCII 则可用）
    cwd = Path.cwd()
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        if all(ord(ch) < 128 for ch in str(cwd)):
            return cwd / ".p5r_work"
    except OSError:
        pass
    raise RuntimeError("无法创建 ASCII 工作目录（C:\\Temp 等不可写）")


_ROOT: Path | None = None


def get_root() -> Path:
    global _ROOT
    if _ROOT is None:
        _ROOT = _pick_root()
    return _ROOT


class WorkDir:
    """一次任务专用的 ASCII 工作目录，任务结束可整体清理。"""

    def __init__(self) -> None:
        self.path = get_root() / uuid.uuid4().hex[:12]
        self.path.mkdir(parents=True, exist_ok=True)

    def file(self, name: str) -> Path:
        return self.path / name

    def cleanup(self) -> None:
        try:
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)
        except OSError:
            pass


def ascii_link(src: Path) -> Path:
    """在同一卷内为 src 创建硬链接到 ASCII 工作区（零拷贝）。

    src 在另一卷时退化为复制。返回链接后的 ASCII 路径。
    """
    wd = WorkDir()
    link = wd.path / src.name
    try:
        os.link(src, link)
    except OSError:
        shutil.copy2(src, link)
    return link


def is_ascii_path(p: Path) -> bool:
    return all(ord(ch) < 128 for ch in str(p))
