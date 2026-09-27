# -*- coding: utf-8 -*-
"""已知密钥表 + USM 密钥探测引擎。

验证方式说明（关键）：
- USM 容器本身不加密，usm.load 总是能打开；密钥作用于内容。
- 视频流：错误/缺失密钥时提取出的 VP9/H.264 码流是掩码数据，
  ffmpeg 解码会大量失败（首帧可能碰巧能解，但后续 99%+ 失败）→
  用“解码前 30 帧且无高错误率”做可靠验证。
- 音频流：cipher56 加密 HCA 用错误密钥也能“解码”但输出噪音，
  仅作无视频流时的兜底验证。
"""
import subprocess

# 防止子进程弹出控制台窗口（Windows）
_NO_WIN = 0x08000000
_orig_run = subprocess.run
def _run_nw(*a, **kw):
    kw.setdefault('creationflags', _NO_WIN)
    return _orig_run(*a, **kw)
subprocess.run = _run_nw
from pathlib import Path

from cricodecs import usm

from .paths import WorkDir

# P5R 音频（HCA/ADX）已知密钥：9923540143823782 (0x002341683D2FDBA6)
# 社区长期使用的 P5R 加密密钥，USM 视频掩码通常与音频共用同一游戏密钥体系。
P5R_KEY = 0x002341683D2FDBA6

# 其他 Atlus 作品常见密钥，用于自动尝试
KNOWN_KEYS: list[int] = [
    P5R_KEY,                              # Persona 5 Royal
    0x165CF4E2138F7BDA,                   # P3R 常见示例密钥
    0xCF222F1FE0748978,                   # CriCodecs 文档示例（P3R 系）
]

_MAX_RECOVER_CANDIDATES = 6

_FFMPEG = "ffmpeg"


def _video_stream_decodes(stream_file: Path) -> bool:
    """ffmpeg 解码前 30 帧，返回码 0 且无高错误率表示码流可解码（密钥正确或未加密）。

    只解码第 1 帧不可靠：加密 VP9 流的首帧有时碰巧能解码，但后续 99%+ 帧失败。
    因此解码前 30 帧并检查 ffmpeg 输出中的错误率标志。
    """
    proc = subprocess.run(
        [_FFMPEG, "-v", "error", "-i", str(stream_file),
         "-frames:v", "30", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return False
    stderr = proc.stderr or ""
    # 加密流的典型表现：大量解码错误，ffmpeg 会报告错误率超阈值
    if "Decode error rate" in stderr or "Invalid data found" in stderr:
        return False
    return True


def _extract_and_check(source, key: int | None) -> bool:
    """用给定 key 提取流，检查视频流是否可解码（无视频流则视为未验证）。"""
    work = WorkDir()
    try:
        # cricodecs C++ 层打不开中文路径，必须用 file-like 句柄传入。
        if isinstance(source, (str, Path)):
            with open(source, "rb") as fh:
                movie = usm.load(fh, key=key)
                movie.extract(str(work.path))
        else:
            movie = usm.load(source, key=key)
            movie.extract(str(work.path))
        for s in movie.info().streams:
            if s.filename.lower().endswith((".ivf", ".264", ".vp9", ".m2v", ".mpg")):
                # USM 内记录的流文件名可能是完整原始路径（如 D:\project\...\mov000.ivf），
                # 而提取后的文件名只有 basename，必须用 basename 拼接。
                vf = work.path / Path(s.filename).name
                if vf.exists() and _video_stream_decodes(vf):
                    return True
        # 有视频流但解码失败（密钥错误/缺失），或无视频流：均视为未通过验证
        return False
    except Exception:
        return False
    finally:
        work.cleanup()


def find_usm_key(source) -> int | None:
    """对给定 USM 输入（路径 / 句柄 / bytes）探测可用密钥。

    返回可直接用于 load/mux 的密钥整数；None 表示未加密（无需密钥）。
    """
    # 1) 未加密尝试
    if _extract_and_check(source, None):
        return None

    # 2) 已知密钥
    for k in KNOWN_KEYS:
        if _extract_and_check(source, k):
            return k

    # 3) 自动恢复
    try:
        res = usm.recover_key(source)
        for cand in res.candidates[:_MAX_RECOVER_CANDIDATES]:
            if _extract_and_check(source, cand.key):
                return cand.key
    except Exception:
        pass

    return None


def resolve_usm_key(source) -> int | None:
    """find_usm_key 的稳健版本：探测失败（返回 None）时回退到 P5R 已知密钥。

    P5R 部分过场（如 MOV058 大体积 VP9 流）视频流解码验证会失败，find_usm_key
    因此返回 None，导致音轨用 keycode=0 解密成噪声。但 P5R 全游戏音频共用
    已知密钥 P5R_KEY，直接回退即可正确解密 HCA。
    """
    key = find_usm_key(source)
    if key is None:
        # P5R 容器均有加密：验证失败 ≠ 未加密，优先尝试已知密钥
        return P5R_KEY
    return key


def describe_key(key: int | None) -> str:
    if key is None:
        return "未加密（无需密钥）"
    return "0x%016X" % key
