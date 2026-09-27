# -*- coding: utf-8 -*-
"""核心引擎：CPK 解包 / USM 解复用 / 预览 / 替换转码 / 加密重打包 / mod 生成。

路径策略（重要）：
- cricodecs 的 C++ 层在 Windows 上打不开中文路径 → 所有中间文件放 ASCII 临时目录；
- 大文件输入（CPK / USM）用 Python open() 句柄（file-like）传给 cricodecs，零拷贝；
- 最终产物由 Python（原生支持中文路径）写回用户目录。
"""
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# 防止子进程弹出控制台窗口（Windows）
_NO_WINDOW = 0x08000000
_orig_run = subprocess.run
_orig_popen = subprocess.Popen
def _run_nw(*a, **kw):
    kw.setdefault('creationflags', _NO_WINDOW)
    return _orig_run(*a, **kw)
def _popen_nw(*a, **kw):
    kw.setdefault('creationflags', _NO_WINDOW)
    return _orig_popen(*a, **kw)
subprocess.run = _run_nw
subprocess.Popen = _popen_nw

from cricodecs import adx, cpk, hca, usm
from cricodecs.usm import UsmMuxConfig, UsmMuxAudioTrack

from . import ffmpeg
from .keys import find_usm_key, resolve_usm_key, describe_key
from .paths import WorkDir

ProgressFn = Callable[[int, int, str], None] | None


class ToolError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# CPK
# --------------------------------------------------------------------------
@dataclass
class CpkEntryInfo:
    index: int
    full_path: str
    filename: str
    dirname: str
    size: int


_CPK_LIST_CACHE_PATH = Path(__file__).resolve().parent / ".cpk_list_cache.json"


def list_cpk(cpk_path: Path | str, on_progress: ProgressFn = None) -> list[CpkEntryInfo]:
    """列出 CPK 内全部条目（不落盘）。

    on_progress(cur, total, msg)：total<=0 表示阶段不确定（如正在打开大文件）。
    带持久化缓存：CPK 未变更时秒读，避免每次启动加载整个 2-3GB 文件。
    """
    import json as _json
    p = Path(cpk_path)
    if not p.exists():
        raise ToolError("CPK 文件不存在: %s" % p)
    st = p.stat()
    key = str(p.resolve())
    cache: dict = {}
    try:
        if _CPK_LIST_CACHE_PATH.exists():
            cache = _json.loads(_CPK_LIST_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(key)
    if hit and hit.get("mtime_ns") == st.st_mtime_ns and hit.get("size") == st.st_size:
        try:
            return [CpkEntryInfo(**d) for d in hit["items"]]
        except Exception:
            pass  # 缓存损坏则重新解析

    if on_progress:
        on_progress(0, 0, "正在打开 CPK 文件…（文件较大时需要一点时间）")
    try:
        with open(p, "rb") as fh:
            arch = cpk.load(fh)
    except Exception as e:
        raise ToolError("CPK 打开失败（可能不是有效的 CriWare CPK）: %s" % e)
    files = arch.files
    total = len(files)
    if on_progress:
        on_progress(1, total, "正在读取文件列表…")
    out: list[CpkEntryInfo] = []
    for i, e in enumerate(files, 1):
        out.append(CpkEntryInfo(index=i - 1, full_path=e.full_path, filename=e.filename,
                                dirname=e.dirname, size=e.file_size))
        if on_progress and (i % 100 == 0 or i == total):
            on_progress(i, total, "正在读取文件列表…")
    try:
        cache[key] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "items": [{"index": it.index, "full_path": it.full_path, "filename": it.filename,
                       "dirname": it.dirname, "size": it.size} for it in out],
        }
        _CPK_LIST_CACHE_PATH.write_text(_json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return out


def extract_cpk(cpk_path: Path | str, out_dir: Path | str,
                suffix: str = ".usm",
                on_progress: ProgressFn = None) -> list[Path]:
    """提取 CPK 中指定后缀的文件到输出目录（保持内部目录结构）。"""
    p = Path(cpk_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = [e for e in list_cpk(p) if e.filename.lower().endswith(suffix.lower())]
    if not entries:
        return []

    # 全部已提取且大小一致时直接返回，避免加载整个 CPK（2-3GB）——启动秒进
    all_done = True
    for e in entries:
        dst = out / Path(e.full_path)
        try:
            if not dst.exists() or dst.stat().st_size != e.size:
                all_done = False
                break
        except OSError:
            all_done = False
            break
    if all_done:
        return [out / Path(e.full_path) for e in entries]

    with open(p, "rb") as fh:
        arch = cpk.load(fh)
        total = len(entries)
        written: list[Path] = []
        for n, e in enumerate(entries, 1):
            rel = Path(e.full_path)
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dst.exists() and dst.stat().st_size == e.size:
                    # 已提取且大小一致：跳过，避免每次启动重复解包
                    written.append(dst)
                else:
                    data = arch.file_bytes(e.index)
                    dst.write_bytes(data)
                    written.append(dst)
            except Exception as ex:
                raise ToolError("提取 %s 失败: %s" % (e.full_path, ex))
            if on_progress:
                on_progress(n, total, e.full_path)
            import time as _time; _time.sleep(0.01)  # 释放 GIL 让 UI 刷新
    return written


# --------------------------------------------------------------------------
# USM
# --------------------------------------------------------------------------
@dataclass
class UsmInfo:
    path: Path
    key: int | None
    key_desc: str
    streams: list = field(default_factory=list)
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float | None = None
    has_audio: bool = False
    audio_sample_rate: int = 48000
    audio_channels: int = 2
    video_stream_name: str = ""
    audio_stream_name: str | None = None
    video_bitrate: int | None = None


def _load_usm(p: Path, key: int | None):
    try:
        with open(p, "rb") as fh:
            return usm.load(fh, key=key)
    except Exception as e:
        raise ToolError("USM 打开失败: %s" % e)


def probe_usm(usm_path: Path | str) -> UsmInfo:
    """探测 USM：密钥、流结构；提取视频流后经 ffprobe 得到分辨率/帧率/音频规格。"""
    p = Path(usm_path)
    if not p.exists():
        raise ToolError("USM 文件不存在: %s" % p)

    key = resolve_usm_key(p)
    movie = _load_usm(p, key)

    info = UsmInfo(path=p, key=key, key_desc=describe_key(key),
                   streams=list(movie.info().streams))

    work = WorkDir()
    try:
        movie.extract(str(work.path))
        files = {f.name.lower(): f for f in work.path.iterdir()}

        # 视频流
        for s in info.streams:
            if "video" in str(s.stream_id).lower() or s.filename.lower().endswith((".ivf", ".264", ".vp9")):
                info.video_stream_name = s.filename
                break
        if info.video_stream_name:
            # USM 内记录的流文件名可能是完整原始路径（如 D:\project\...\mov000.ivf），
            # 而提取后的文件名只有 basename，必须用 basename 查找。
            vf = files.get(Path(info.video_stream_name).name.lower())
            if vf:
                try:
                    mi = ffmpeg.probe(vf)
                    info.width, info.height = mi.width, mi.height
                    info.fps = mi.fps
                    info.duration = mi.duration
                    info.video_bitrate = mi.video_bitrate
                except ffmpeg.FFmpegError:
                    pass

        # 音频流
        for s in info.streams:
            if s.filename.lower().endswith((".hca", ".adx", ".ahx")):
                info.audio_stream_name = s.filename
                info.has_audio = True
                break
        if info.has_audio and info.audio_stream_name:
            af = files.get(Path(info.audio_stream_name).name.lower())
            if af:
                sr, ch = _probe_audio(af, key)
                if sr:
                    info.audio_sample_rate = sr
                if ch:
                    info.audio_channels = ch
    finally:
        work.cleanup()
    return info


def _probe_audio(af: Path, key: int | None) -> tuple[int | None, int | None]:
    """解码 HCA/ADX 为 WAV 并探测采样率/声道。失败返回 (None, None)。"""
    wav_path = af.with_suffix(".wav")
    try:
        raw = af.read_bytes()
        if af.name.lower().endswith(".hca"):
            try:
                wav = hca.decode(raw, keycode=key or 0)
            except Exception:
                wav = hca.decode(raw, keycode=0)
            wav_path.write_bytes(wav)
        elif af.name.lower().endswith(".adx"):
            wav = adx.decode(raw)
            wav_path.write_bytes(wav)
        else:
            return None, None
        mi = ffmpeg.probe(wav_path)
        return mi.sample_rate, mi.channels
    except Exception:
        return None, None


def extract_usm_streams(usm_path: Path | str, out_dir: Path | str) -> dict[str, Path]:
    """把 USM 的视频/音频原始码流解出来，写到用户目录。返回 {文件名: 路径}。"""
    p = Path(usm_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = resolve_usm_key(p)
    movie = _load_usm(p, key)
    work = WorkDir()
    try:
        movie.extract(str(work.path))
        result: dict[str, Path] = {}
        for f in work.path.iterdir():
            if f.is_file():
                dst = out / f.name
                dst.write_bytes(f.read_bytes())
                result[f.name] = dst
        return result
    finally:
        work.cleanup()



def _fix_mpeg2_stream(raw: bytes, minbuf: int = 0) -> bytes:
    """修复 CRI 非标准 MPEG-2 流。

    CRI 的 MPEG-2 sequence header 含大量自定义数据（非标准），导致 ffmpeg 无法解析。
    但 picture 帧数据是标准的。方法：解析原始头的分辨率/帧率，用 USM 元数据的 minbuf
    （头数据大小，CRI 官方字段）定位帧数据起始位置，手动构建标准 sequence header +
    sequence extension + GOP header。minbuf 为 0 时回退到搜索第一个 picture start code。
    """
    if len(raw) < 16 or raw[:4] != bytes([0, 0, 1, 0xb3]):
        return raw  # 不是 MPEG-2 sequence header，直接返回

    # 解析原始 sequence header 的分辨率和帧率
    # bytes 4-6: horizontal(12) + vertical(12)
    h = (raw[4] << 4) | (raw[5] >> 4)
    v = ((raw[5] & 0x0F) << 8) | raw[6]
    # byte 7: aspect(4) + frame_rate(4)
    fps_code = raw[7] & 0x0F
    fps_map = {1: 23.976, 2: 24.0, 3: 25.0, 4: 29.97, 5: 30.0, 6: 50.0, 7: 59.94, 8: 60.0}
    fps = fps_map.get(fps_code, 29.97)

    # 用 USM 元数据的 minbuf（头数据大小）定位帧数据起始位置
    # minbuf 是 CRI 官方字段，表示流开头的元数据/头大小，真正的视频帧从 minbuf 偏移开始
    if minbuf and 0 < minbuf < len(raw):
        first_pic = minbuf
    else:
        # 回退：搜索第一个 picture start code (00 00 01 00)
        first_pic = raw.find(bytes([0, 0, 1, 0]))
        if first_pic < 0:
            return raw  # 找不到 picture start code，直接返回

    # 构建标准 MPEG-2 sequence header
    # horizontal(12) + vertical(12)
    b4 = (h >> 4) & 0xFF
    b5 = ((h & 0x0F) << 4) | ((v >> 8) & 0x0F)
    b6 = v & 0xFF
    # aspect=1 (1:1), frame_rate=fps_code
    b7 = (1 << 4) | (fps_code & 0x0F)
    # bit_rate: 用高码率 (0x3FFFF = 262143 * 400 = 105Mbps)
    b8 = 0xFF
    b9 = 0xFF
    b10 = 0xE8  # bit_rate 高 18 位 + marker=1
    # vbv_buffer: 用大值 (0x3FF = 1023 * 16KB = 16MB), constrained=0, load_intra=0, load_non_intra=0, reserved=0
    b11 = 0x08
    b12 = 0x00
    seq_header = bytes([0, 0, 1, 0xb3, b4, b5, b6, b7, b8, b9, b10, b11, b12])

    # sequence extension (MP@HL, 4:2:0, progressive)
    # 00 00 01 b5 + extension_start_code_identifier(4)=1 + profile(3)+level(4)+progressive(1)+chroma(2)+...
    seq_ext = bytes([0, 0, 1, 0xb5, 0x14, 0x8a, 0x00, 0x01, 0x00, 0x00])

    # GOP header (time 00:00:00:00, closed_gop=1, broken_link=0)
    gop = bytes([0, 0, 1, 0xb8, 0x00, 0x08, 0x00, 0x00])

    return seq_header + seq_ext + gop + raw[first_pic:]


_CRID_BIN = Path(__file__).resolve().parent.parent / "bin" / "crid_mod.exe"


def extract_m2v_adx(usm_path: Path | str, key: int | None = None,
                    out_dir: Path | str | None = None) -> tuple[Path | None, Path | None]:
    """用 cricodecs 直接解出 IVF（VP9 视频流），纯 Python 内存操作，无需 crid_mod 子进程。

    返回 (m2v_path, adx_path)；adx 暂不使用（音频由 extract_preview_audio 处理）。
    """
    p = Path(usm_path)
    if key is None:
        key = resolve_usm_key(p)
    try:
        from cricodecs import usm as _usm_mod
        movie = _usm_mod.load(str(p), key=key)
        info = movie.info()
        # stream 0 是视频（.ivf），后续是音频（.hca）
        video_raw = movie.stream_bytes(0)
        if not video_raw or len(video_raw) < 100:
            return None, None
        dst_dir = Path(out_dir) if out_dir else p.parent
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            dst_dir = p.parent
        out_m2v = dst_dir / (p.stem + ".m2v")
        out_m2v.write_bytes(video_raw)
        return (out_m2v, None)
    except Exception:
        return None, None

def extract_audio_original(usm_path: Path | str, key: int | None = None,
                           out_wav: Path | str | None = None,
                           track: str = "jp") -> Path | None:
    """从 USM 解出原始 HCA 音频为无损 WAV（保持 48kHz 原始采样率，不做重采样）。

    与 extract_preview_audio 的区别：预览用 44.1kHz 缓存；导出用本函数保留原始采样率。
    track="jp" 时双音轨动画取第二条（日文配音）；track="en" 取第一条（英文）。
    返回 wav 路径；无音轨或失败返回 None。
    """
    p = Path(usm_path)
    if key is None:
        key = resolve_usm_key(p)
    try:
        tracks = _usm_audio_tracks(p, key)
        if not tracks:
            return None
        if track == "jp" and len(tracks) >= 2:
            wav_bytes = tracks[-1]
        else:
            wav_bytes = tracks[0]
        out = Path(out_wav) if out_wav else p.with_suffix(".orig_jp.wav" if (track == "jp" and len(tracks) >= 2) else ".orig.wav")
        out.write_bytes(wav_bytes)
        return out
    except Exception:
        return None


def extract_preview_audio(usm_path: Path | str, key: int | None = None,
                          out_wav: Path | str | None = None,
                          track: str = "jp") -> Path | None:
    """从 USM 解出预览音轨（HCA 解密 → 44.1kHz/2ch PCM wav）。

    crid_mod 解出的 adx 无法被 cricodecs 解（checksum 失败）、ffmpeg 无 key 解成噪声，
    因此音轨一律用 cricodecs 从 USM 原流解密（与 MP4 转换路径同源，且无 FAAC 重编码损失）。
    track="jp" 时优先取第二条音轨（P5R 双音轨动画的第二条为日文配音）；track="en" 取第一条。
    缓存名：jp 多轨 → <名>.pv_jp.wav，其余 → <名>.pv.wav。
    输出到 out_wav（或默认缓存名）并缓存，返回路径；失败返回 None。
    """
    p = Path(usm_path)
    if key is None:
        key = resolve_usm_key(p)
    try:
        tracks = _usm_audio_tracks(p, key)
        if not tracks:
            return None
        if track == "jp" and len(tracks) >= 2:
            wav_bytes = tracks[-1]
        else:
            wav_bytes = tracks[0]
        if out_wav:
            out = Path(out_wav)
        else:
            out = p.with_suffix(".pv_jp.wav" if (track == "jp" and len(tracks) >= 2) else ".pv.wav")
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # hca.decode 返回的已经是标准 WAV 字节，直接写入，跳过 ffmpeg 转码
        out.write_bytes(wav_bytes)
        if out.exists() and out.stat().st_size > 100:
            return out
        return None
    except Exception:
        return None



def _usm_audio_tracks(usm_path: Path | str, key: int | None = None) -> list[bytes]:
    """返回 USM 中所有可解码音轨（WAV 字节，按流顺序）。

    P5R 部分动画含两条音轨：第一条英文、第二条日文（Atlus 惯例，游戏按语音设置选轨）。
    无音轨返回 []。
    """
    p = Path(usm_path)
    if key is None:
        key = resolve_usm_key(p)
    try:
        movie = _load_usm(p, key)
        info = movie.info()
        tracks: list[bytes] = []
        for i, s in enumerate(info.streams):
            ext = Path(s.filename).suffix.lower() if s.filename else ""
            try:
                raw = movie.stream_bytes(i)
            except Exception:
                continue
            if not raw:
                continue
            if ext == ".hca":
                try:
                    tracks.append(hca.decode(raw, keycode=key or 0))
                    continue
                except Exception:
                    pass
            if ext == ".adx" or (ext == ".avi" and len(raw) >= 2 and raw[:2] == bytes([0x80, 0x00])):
                try:
                    tracks.append(adx.decode(raw))
                except Exception:
                    pass
        return tracks
    except Exception:
        return []


def probe_video_kind(usm_path: Path | str, key: int | None = None) -> str:
    """判断 USM 视频流类型：'vp9'（可 m2v 直播）/ 'mpeg2'（需转 MP4）/ 'unknown'。

    key 可传入共享密钥（同 CPK 同一 key，避免每次探测）；None 时自动探测。
    优先用流文件名后缀判断（info() 仅解析流表，约 0.06s），无需提取整流。
    """
    try:
        p = Path(usm_path)
        if key is None:
            try:
                key = resolve_usm_key(p)
            except Exception:
                key = None
        movie = _load_usm(p, key)
        info = movie.info()
        for s in info.streams:
            ext = Path(s.filename).suffix.lower() if s.filename else ""
            if ext in (".ivf", ".vp9"):
                return "vp9"
            if ext in (".avi", ".m2v", ".mpv", ".264", ".mpg"):
                return "mpeg2"
        # 后缀无法判断时兜底：提取流头判断魔数
        for s in info.streams:
            try:
                raw = movie.stream_bytes(s.id) if hasattr(s, "id") else None
            except Exception:
                raw = None
            if raw and len(raw) >= 4 and raw[:4] == b"DKIF":
                return "vp9"
            if raw and len(raw) >= 4 and raw[:4] == bytes([0, 0, 1, 0xB3]):
                return "mpeg2"
        return "unknown"
    except Exception:
        return "unknown"



def probe_m2v_fps(m2v_path: Path | str) -> tuple[float | None, float | None]:
    """从 m2v 容器头读权威帧率与时长，返回 (fps, duration)，失败返回 (None, None)。

    - VP9/IVF：读 IVF 头 timebase 与帧数（ffprobe 对 IVF 的 r_frame_rate 正确）；
    - MPEG-1/MPEG-2（crid_mod 解的 raw 流）：读序列头 frame_rate_code（权威值），
      并数 picture 头得到真实帧数 → duration = 帧数 / fps。
      ffprobe 对这类流会把帧率报成 2 倍（隔行场编码），必须用序列头。
    """
    import re
    try:
        p = Path(m2v_path)
        with open(p, "rb") as fh:
            head = fh.read(64)
        if head[:4] == b"DKIF":                      # IVF（VP9）：帧率 = den/num（30000/1001=29.97）
            import struct
            den, num, fc = struct.unpack("<III", head[16:28])
            fps = den / num if num else 29.97
            dur = (fc / fps) if fc and fps else None
            return fps, dur
        if head[:4] == b"\x00\x00\x01\xba":          # MPEG 系统流（罕见，兜底）
            return None, None
        # MPEG-1/2 raw 流：找序列头
        data = open(p, "rb").read()
        i = data.find(b"\x00\x00\x01\xb3")
        if i < 0 or i + 8 > len(data):
            return None, None
        frc = data[i + 7] & 0x0F
        fps = {1: 23.976, 2: 24, 3: 25, 4: 29.97, 5: 30, 6: 50, 7: 59.94, 8: 60}.get(frc)
        if not fps:
            return None, None
        pic = len(re.findall(rb"\x00\x00\x01\x00", data))
        dur = pic / fps if pic else None
        return fps, dur
    except Exception:
        return None, None


def prepare_m2v_preview(usm_path: Path | str, key: int | None = None) -> bool:
    """为过场生成 m2v 直播缓存（幂等，已存在则跳过）。"""
    import sys as _sys
    p = Path(usm_path)
    if not p.exists():
        return False
    m2v = p.with_suffix(".m2v")
    wav = p.with_suffix(".pv.wav")
    if m2v.exists() and wav.exists():
        return True
    try:
        if key is None:
            try:
                key = resolve_usm_key(p)
            except Exception:
                key = None
        need_video = not m2v.exists()
        need_audio = not wav.exists()
        # 用 Python 文件句柄传给 cricodecs（C++ 层不支持中文路径）
        fh = open(str(p), 'rb')
        try:
            movie = usm.load(fh, key=key)
            info = movie.info()
            if need_video:
                # 视频流优先用 crid_mod 解包（修复跨 chunk 漏解密帧，如 MOV000 中间定格）
                _crid_ok = False
                try:
                    if _CRID_BIN.exists():
                        from .paths import ascii_link
                        usm_link = ascii_link(p)
                        work_dir = usm_link.parent
                        try:
                            h = f"{key:016X}"
                            r = subprocess.run(
                                [str(_CRID_BIN), "-b", h[:8], "-a", h[8:], "-v", str(usm_link)],
                                cwd=str(work_dir), capture_output=True, timeout=180)
                            crid_m2v = work_dir / (usm_link.stem + ".m2v")
                            if r.returncode == 0 and crid_m2v.exists() and crid_m2v.stat().st_size > 10000:
                                shutil.copyfile(crid_m2v, m2v)
                                _crid_ok = True
                        finally:
                            shutil.rmtree(work_dir, ignore_errors=True)
                except Exception:
                    _crid_ok = False
                # crid_mod 失败时回退到 cricodecs 直取
                if not _crid_ok:
                    video_raw = movie.stream_bytes(0)
                    if video_raw and len(video_raw) > 100:
                        m2v.write_bytes(video_raw)
            if need_audio:
                # 音频流：尝试解码识别（HCA 是加密的，前4字节不是 HCA\x00，必须用 hca.decode 试解）
                # ADX 前1字节是 0x80；.txt 字幕流等非音频会解码失败被跳过。
                audio_candidates = []  # [(index, wav_bytes)]
                for _i in range(1, len(info.streams)):
                    try:
                        _raw = movie.stream_bytes(_i)
                        if not _raw or len(_raw) < 4:
                            continue
                        # 先试 HCA 解码（hca.decode 内部自动解密）
                        try:
                            _wav = hca.decode(_raw, keycode=key or 0)
                            if _wav and len(_wav) > 1000:
                                audio_candidates.append((_i, _wav))
                                continue
                        except Exception:
                            pass
                        # 再试 ADX 解码（前1字节 0x80）
                        if _raw[:1] == b"\x80":
                            try:
                                _wav = adx.decode(_raw)
                                if _wav and len(_wav) > 1000:
                                    audio_candidates.append((_i, _wav))
                            except Exception:
                                pass
                    except Exception:
                        pass
                audio_bytes = None
                if audio_candidates:
                    # 双音轨取最后一条（日文配音），单音轨取唯一一条
                    _, audio_bytes = audio_candidates[-1] if len(audio_candidates) >= 2 else audio_candidates[0]
                if audio_bytes:
                    wav.write_bytes(audio_bytes)
            return m2v.exists()
        finally:
            fh.close()
    except Exception as e:
        import traceback, os
        try:
            if getattr(_sys, 'frozen', False):
                log_dir = Path(_sys.executable).parent
            else:
                log_dir = Path.cwd()
            err_log = log_dir / "preview_error.log"
            err_log.write_text(f"USM: {p}\nKey: {key}\n{traceback.format_exc()}", encoding="utf-8")
        except Exception:
            pass
        return False


def _crid_fix_video(usm_path: Path, mp4_path: Path, key: int) -> None:
    """用 crid_mod 重新解包视频流，修复 cricodecs 漏解密坏帧（如 MOV000 0:39 定格）。

    cricodecs 对个别跨 chunk 帧漏做 MaskVideo 解密，ffmpeg 软解会丢帧/定格；
    crid_mod（CRI .usm Demux Tool v1.02-mod）对这些帧解密正确，解出的流解码 0 错误。
    此处用 crid_mod 解出的视频流与 MP4 原音轨（-c:a copy，零损失）重新封装。
    任何失败都静默保留原 MP4（不破坏现有流程）。
    """
    try:
        if not _CRID_BIN.exists():
            return
        from .paths import ascii_link
        usm_link = ascii_link(usm_path)              # 硬链接/拷贝到 ASCII 目录（crid_mod 打不开中文路径）
        work_dir = usm_link.parent                   # crid_mod 输出到输入文件所在目录
        try:
            h = f"{key:016X}"
            r = subprocess.run(
                [str(_CRID_BIN), "-b", h[:8], "-a", h[8:], "-v", str(usm_link)],
                cwd=str(work_dir), capture_output=True, timeout=180)
            m2v = work_dir / (usm_link.stem + ".m2v")
            if r.returncode != 0 or not m2v.exists():
                return
            fixed = work_dir / "fixed.mp4"
            r2 = subprocess.run(
                [ffmpeg.FFMPEG, "-v", "error", "-y",
                 "-i", str(m2v), "-i", str(mp4_path),
                 "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "copy", "-c:a", "copy",
                 "-movflags", "+faststart", str(fixed)],
                capture_output=True, timeout=600)
            if r2.returncode == 0 and fixed.exists() and fixed.stat().st_size > 4096:
                shutil.copyfile(fixed, mp4_path)     # 原子替换原 MP4
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        pass
    except Exception:
        pass


def make_preview(usm_path: Path | str, out_mp4: Path | str,
                 on_progress: ProgressFn = None, key: int | None = None) -> Path:
    """把 USM 转成 MP4，供系统播放器直接预览。

    支持两种视频流：
    - VP9/IVF（主过场动画）：快速路径，FAAC AAC + Python 原生封装，无损
    - MPEG-2/AVI（游戏内电视/监控/手机视频等特殊过场）：通用路径，ffmpeg 转 H.264/AAC

    key 为已知 USM 加密密钥时直接使用，避免重复探测（批量转换时大幅提速）。
    """
    p = Path(usm_path)
    dst = Path(out_mp4)
    if key is None:
        key = resolve_usm_key(p)
    movie = _load_usm(p, key)
    try:
        info = movie.info()
        video_bytes = None
        video_ext = None
        video_minbuf = 0
        wav_bytes = None
        for i, s in enumerate(info.streams):
            ext = Path(s.filename).suffix.lower() if s.filename else ""
            try:
                raw = movie.stream_bytes(i)
            except Exception:
                continue
            if not raw:
                continue
            # 用数据魔数判断流类型（某些 USM 的视频和音频流扩展名相同，如都是 .avi）
            is_video = False
            if ext in (".ivf", ".vp9") and raw[:4] == b"DKIF":
                is_video = True
            elif ext in (".avi", ".m2v", ".mpv", ".264"):
                # MPEG-1/2: 00 00 01 b3(序列头)/b8(GOP)/00(图片); H.264: 00 00 00 01 或 00 00 01
                if len(raw) >= 4 and raw[:3] == bytes([0, 0, 1]):
                    is_video = True
                elif len(raw) >= 4 and raw[:4] == bytes([0, 0, 0, 1]):
                    is_video = True
            if is_video:
                video_bytes = raw
                video_ext = ext
                video_minbuf = getattr(s, "minbuf", 0) or 0
                continue
            # 音频流（注意：加密的 HCA 数据前 3 字节不是 "HCA"，不能用数据魔数判断）
            if ext == ".hca":
                # HCA 音频（可能加密，hca.decode 内部会处理解密）
                try:
                    wav_bytes = hca.decode(raw, keycode=key or 0)
                except Exception:
                    wav_bytes = None
            elif ext == ".adx" or (ext == ".avi" and len(raw) >= 2 and raw[:2] == bytes([0x80, 0x00])):
                # ADX 音频（可能伪装成 .avi 扩展名）
                try:
                    wav_bytes = adx.decode(raw)
                except Exception:
                    wav_bytes = None
        if video_bytes is None:
            raise ToolError("USM 中未找到视频流")
        if on_progress:
            on_progress(1, 2, "视频合成中…")

        if video_ext in (".ivf", ".vp9"):
            # 快速路径：VP9 无损直拷 + FAAC AAC + Python 原生 MP4 封装
            aac_adts = ffmpeg.encode_aac_adts(wav_bytes) if wav_bytes is not None else None
            ffmpeg.mux_mp4_python(video_bytes, aac_adts, dst)
            # ★ crid_mod 修复：重新解包视频流（修复漏解密坏帧），与 MP4 原音轨重新封装，不牺牲音视频质量
            if key:
                _crid_fix_video(p, dst, key)
        else:
            # 通用路径：MPEG-2/AVI 等非 VP9 视频，用 ffmpeg 转 H.264/AAC MP4
            # CRI 的 MPEG-2 sequence header 是非标准的（含大量自定义数据），需要修复
            from .paths import WorkDir
            work = WorkDir()
            try:
                video_data = _fix_mpeg2_stream(video_bytes, video_minbuf)
                vid_tmp = work.path / "preview_video.m2v"
                vid_tmp.write_bytes(video_data)
                aud_tmp = None
                if wav_bytes is not None:
                    aud_tmp = work.path / "preview_audio.wav"
                    aud_tmp.write_bytes(wav_bytes)
                # 从原始 sequence header 解析分辨率，保持原始质量（不缩到 720p）
                src_w = (video_bytes[4] << 4) | (video_bytes[5] >> 4) if len(video_bytes) > 6 else 1920
                src_h = ((video_bytes[5] & 0x0F) << 8) | video_bytes[6] if len(video_bytes) > 6 else 1080
                ffmpeg.to_h264_mp4(str(vid_tmp), str(aud_tmp) if aud_tmp else None,
                                    dst, fast=True, width=src_w, height=src_h)
            finally:
                work.cleanup()
        if on_progress:
            on_progress(2, 2, "完成")
        return dst
    finally:
        pass


# --------------------------------------------------------------------------
# 替换 & 打包
# --------------------------------------------------------------------------
@dataclass
class BuildOptions:
    bitrate_k: int = 10000      # VP9 目标码率（kbps）
    maxrate_k: int = 11000
    hca_quality: int = 3        # 0-5，HCA 编码质量
    target_width: int = 0       # 0 = 跟随原 USM
    target_height: int = 0
    target_fps: float = 0.0
    use_original_bitrate: bool = False  # False=单遍 CRF 快速高质量；True=两遍编码对齐原码率
    transcode_jobs: int = 0             # 0=自动；>0 指定并行分片数（多视频并行时减半避免超载）


def build_usm(src_usm: Path | str, user_video: Path | str,
              out_usm: Path | str, opts: BuildOptions | None = None,
              on_progress: ProgressFn = None,
              ivf_cache: dict | None = None,
              cache_dir: Path | None = None) -> tuple[Path, UsmInfo]:
    """把用户视频转码打包为与源 USM 同规格（分辨率/帧率/加密）的新 USM。

    返回 (输出路径, 源 USM 探测信息)。
    """
    src = Path(src_usm)
    video = Path(user_video)
    dst = Path(out_usm)
    opts = opts or BuildOptions()

    if not src.exists():
        raise ToolError("源 USM 不存在: %s" % src)
    if not video.exists():
        raise ToolError("视频文件不存在: %s" % video)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 1) 探测源 USM
    if on_progress:
        on_progress(1, 6, "解析源 USM…")
    src_info = probe_usm(src)
    w = opts.target_width or src_info.width or 1920
    h = opts.target_height or src_info.height or 1080
    fps = opts.target_fps or src_info.fps or 30.0
    bitrate = opts.bitrate_k
    if opts.use_original_bitrate:
        if src_info.video_bitrate:
            bitrate = max(2000, min(20000, src_info.video_bitrate // 1000))
        else:
            # ffprobe 读不到时，用 USM 容器记录的平均字节速率（avbps）估算
            for s in src_info.streams:
                avbps = getattr(s, "avbps", 0) or 0
                if avbps:
                    bitrate = max(2000, min(20000, avbps * 8 // 1000))
                    break

    key = src_info.key
    work = WorkDir()
    try:
        # 2) 用户视频 -> VP9 .ivf（匹配分辨率/帧率；子进度实时反馈到进度条）
        #    同视频同规格的转码结果可跨过场复用（ivf_cache），避免重复两遍编码
        two_pass = bool(opts.use_original_bitrate)
        mode_label = "并行 CRF（快速）" if not two_pass else "两遍编码（对齐源码率）"
        cache_key = (str(Path(user_video).resolve()), w, h, fps, bitrate, two_pass)
        # 持久化缓存文件名（含单遍/两遍标记，避免两种模式混用）
        _cached_dir = Path(cache_dir) if cache_dir else (Path(out_usm).parent / ".p5r_cache")
        _cname = "%s_%dx%d_%.3ffps_%dk_%s.ivf" % (
            Path(user_video).stem[:30], w, h, fps, bitrate, "2p" if two_pass else "1p")
        _cname = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in _cname)
        _cfile = _cached_dir / _cname
        # ① 内存缓存 → ② 磁盘持久化缓存 → ③ 新转码
        _src_ivf = None
        if ivf_cache is not None and cache_key in ivf_cache                 and Path(ivf_cache[cache_key]).exists():
            _src_ivf = Path(ivf_cache[cache_key])
        elif _cfile.exists():
            _src_ivf = _cfile
        if _src_ivf is not None:
            ivf = work.file("user_video.ivf")
            try:
                shutil.copy2(_src_ivf, ivf)
            except OSError:
                ivf = _src_ivf
            if ivf_cache is not None:
                ivf_cache[cache_key] = str(_src_ivf)
            if on_progress:
                on_progress(2, 6, "复用已转码 VP9（同一视频同一规格）…")
        else:
            if on_progress:
                on_progress(2, 6, "转码视频为 VP9（%s）…" % mode_label)
            ivf = work.file("user_video.ivf")
            if two_pass:
                ffmpeg.transcode_vp9(
                    video, ivf, w, h, fps, bitrate, opts.maxrate_k,
                    on_progress=((lambda frac, stage: on_progress(
                        (2 + frac * 0.95) if stage != "分析" else -1, 6,
                        ("转码视频为 VP9（%s·编码）…%d%%" % (mode_label, int(frac * 100)))
                        if stage != "分析" else
                        "转码视频为 VP9（分析中，两遍编码第一遍，无需等待太久）…"))
                        if on_progress else None),
                    two_pass=True,
                    jobs=opts.transcode_jobs or None)
            else:
                ffmpeg.transcode_vp9_fast(
                    video, ivf, w, h, fps, bitrate, opts.maxrate_k,
                    jobs=opts.transcode_jobs or None,
                    on_progress=((lambda frac, stage: on_progress(
                        2 + frac * 0.95, 6,
                        "转码视频为 VP9（%s·编码）…%d%%" % (mode_label, int(frac * 100))))
                        if on_progress else None))
            if ivf_cache is not None:
                cd = Path(cache_dir) if cache_dir else (Path(out_usm).parent / ".p5r_cache")
                cd.mkdir(parents=True, exist_ok=True)
                cname = "%s_%dx%d_%.3ffps_%dk_%s.ivf" % (Path(user_video).stem[:30], w, h, fps, bitrate, "2p" if two_pass else "1p")
                cname = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in cname)
                cfile = cd / cname
                try:
                    shutil.copy2(ivf, cfile)
                    ivf_cache[cache_key] = str(cfile)
                except OSError:
                    pass

        # 3) 用户视频音轨 -> WAV -> HCA（加密）
        #    源 USM 可能有多条 SFA 音轨（如 MOV001 开场动画 = 英/日双音轨），
        #    游戏按语音设置读取对应 channel，生成时必须保留相同音轨数，否则播放失败回退原版。
        src_sfa = [s for s in src_info.streams
                   if str(getattr(s, "stream_id", "")).rsplit(".", 1)[-1] == "SFA"]
        audio_track: list[Path] = []
        audio_encrypt: list[bool] = []
        try:
            vmi = ffmpeg.probe(video)
            has_video_audio = vmi.has_audio
        except ffmpeg.FFmpegError:
            has_video_audio = False

        if src_info.has_audio:
            if on_progress:
                on_progress(3, 6, "编码 HCA 音轨…")
            cfg = hca.HcaEncodeConfig()
            cfg.sample_rate = src_info.audio_sample_rate or 48000
            cfg.channel_count = src_info.audio_channels or 2
            cfg.quality = opts.hca_quality
            if has_video_audio:
                wav = work.file("user_audio.wav")
                ffmpeg.extract_audio_wav(video, wav)
                wav_bytes = wav.read_bytes()
            else:
                # 用户视频无音轨：生成静音 WAV（与源同规格），保证音轨数量不缺失
                wav = work.file("silence.wav")
                ffmpeg.make_silence_wav(wav, float(src_info.duration or 0),
                                        cfg.sample_rate, cfg.channel_count)
                wav_bytes = wav.read_bytes()
            n_audio = max(1, len(src_sfa))  # 保留源音轨数（至少 1 条）
            hca_bytes = hca.encode(wav_bytes, cfg)
            if key is not None:
                hca_bytes = hca.encrypt(hca_bytes, cipher_type=56, keycode=key)
            for i in range(n_audio):
                hca_file = work.file("user_audio_%d.hca" % i)
                hca_file.write_bytes(hca_bytes)
                audio_track.append(hca_file)
                audio_encrypt.append(False)

        # 4) 合成加密 USM
        if on_progress:
            on_progress(4, 6, "打包 USM…")
        if audio_track:
            # 必须用 UsmMuxAudioTrack 显式指定 channel_no，
            # 否则简写形式会导致双音轨 USM 在 CRI 解码器中提前截断（P5R MOV001 双音轨 bug）
            _tracks = []
            for _i, (_a) in enumerate(audio_track):
                _tracks.append(UsmMuxAudioTrack(
                    path=str(_a), encrypt=False, channel_no=_i))
            usm_bytes = usm.mux(UsmMuxConfig(
                video_path=str(ivf), audio_tracks=_tracks, key=key))
        else:
            usm_bytes = usm.mux(UsmMuxConfig(video_path=str(ivf), key=key))

        # 5) 回读校验：确保打包结果能被正确解析
        tmp_usm = work.file("built.usm")
        tmp_usm.write_bytes(usm_bytes)
        movie = usm.load(str(tmp_usm), key=key)
        info = movie.info()
        if info.stream_count == 0:
            raise ToolError("打包结果异常：无媒体流")
        if on_progress:
            on_progress(5, 6, "校验通过，写回输出…")

        # 6) 写回用户目录
        dst.write_bytes(usm_bytes)
        if on_progress:
            on_progress(6, 6, "完成")
        return dst, src_info
    finally:
        work.cleanup()


# --------------------------------------------------------------------------
# Reloaded-II mod
# --------------------------------------------------------------------------
def build_mod(files: list[tuple[Path | str, str]], mod_root: Path | str,
              mod_name: str, cpk_name: str = "data_movie.cpk",
              author: str = "P5R Cutscene Tool",
              description: str = "") -> Path:
    """把一个或多个生成的 USM 组装成 Reloaded-II / Persona Essentials 可加载的 mod。

    files：[(out_usm, src_inner_path), ...]，支持一次打包多个过场替换。
    目录结构（Persona Essentials 标准）：
      <mod_root>/<mod_name>/P5REssentials/CPK/<cpk_name>/<内部目录>/同名.usm
    并生成 ModConfig.json（Reloaded-II 识别用的标准配置文件名）。
    """
    root = Path(mod_root)
    if not files:
        raise ToolError("没有要打包的 USM")
    for usm_src, _inner in files:
        if not Path(usm_src).exists():
            raise ToolError("USM 不存在: %s" % usm_src)

    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in mod_name).strip("_")
    if not safe_name:
        safe_name = "P5R_Cutscene_Mod"
    mod_dir = root / safe_name
    for usm_src, src_inner_path in files:
        rel = Path(src_inner_path.strip("/\\"))
        # src_inner_path 形如 "MOVIE/MOV050.USM"：目录部分是父级，文件名放最内层
        inner_dir = rel.parent if rel.parent.as_posix() != "." else Path("")
        target_dir = mod_dir / "P5REssentials" / "CPK" / cpk_name / inner_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        # mod 内 USM 必须与原过场同名（Reloaded 按名字覆盖游戏文件）
        dest = target_dir / rel.name
        shutil.copy2(usm_src, dest)

    mod_json = {
        "ModId": "p5r.cutscene.%s" % safe_name.lower().replace(" ", "."),
        "ModName": mod_name,
        "ModAuthor": author,
        "ModVersion": "1.0.0",
        "ModDescription": description or "P5R Cutscene Tool 生成的过场动画替换 mod。",
        # 只需 Persona Essentials：它会自动解析 crifs.v2.hook、文件模拟框架等自身依赖。
        # （不要声明 ripplehook —— P5R 文件替换不需要，声明了反而会因找不到而被 Reloaded-II 拦截）
        "ModDependencies": [
            "p5rpc.modloader",
        ],
        "LoadPriority": 0,
    }
    import json
    (mod_dir / "ModConfig.json").write_text(json.dumps(mod_json, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    return mod_dir


def find_p5r_game_dir() -> str | None:
    """探测 P5R 游戏目录（Steam 常见安装位置，含 P5R.exe 的目录）。"""
    cands = [
        Path("D:/steam/steamapps/common/P5R/P5R.exe"),
        Path("C:/Program Files (x86)/Steam/steamapps/common/P5R/P5R.exe"),
        Path("C:/Program Files/Steam/steamapps/common/P5R/P5R.exe"),
        Path("E:/steam/steamapps/common/P5R/P5R.exe"),
        Path("D:/SteamLibrary/steamapps/common/P5R/P5R.exe"),
        Path("C:/SteamLibrary/steamapps/common/P5R/P5R.exe"),
    ]
    for c in cands:
        try:
            if c.is_file():
                return str(c.parent)
        except OSError:
            continue
    return None


def enable_mod_in_reloaded(game_dir: str | Path, mod_id: str) -> bool:
    """把 modId 写入游戏目录 mod.json 的 EnabledMods（Reloaded-II 的启用机制）。

    保留原有字段与已启用条目，仅追加。返回是否写入成功。
    """
    import json as _json
    mj = Path(game_dir) / "mod.json"
    data: dict = {}
    if mj.exists():
        try:
            data = _json.loads(mj.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
    enabled = list(data.get("EnabledMods", []) or [])
    if mod_id not in enabled:
        enabled.append(mod_id)
        data["EnabledMods"] = enabled
        mj.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True
