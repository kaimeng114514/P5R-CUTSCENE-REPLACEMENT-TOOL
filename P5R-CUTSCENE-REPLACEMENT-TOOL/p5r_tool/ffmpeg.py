# -*- coding: utf-8 -*-
"""FFmpeg / FFprobe 封装（子进程调用，路径走原生字符串，支持中文路径）。"""
import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"

def _find_tool(name: str) -> str:
    """优先从工具自带 bin/ 目录找，其次 PATH，最后回退。"""
    local = _BIN / name
    if local.exists():
        return str(local)
    return shutil.which(name) or name

FFMPEG = _find_tool("ffmpeg.exe")
FFPROBE = _find_tool("ffprobe.exe") or FFMPEG

# 防止子进程弹出控制台窗口（Windows）
_NO_WINDOW = 0x08000000
_orig_run = subprocess.run
_orig_popen = subprocess.Popen

def _run_no_window(*args, **kwargs):
    kwargs.setdefault('creationflags', _NO_WINDOW)
    return _orig_run(*args, **kwargs)

def _popen_no_window(*args, **kwargs):
    kwargs.setdefault('creationflags', _NO_WINDOW)
    return _orig_popen(*args, **kwargs)

subprocess.run = _run_no_window
subprocess.Popen = _popen_no_window


# FAAC AAC 编码器（比 aac_mf 快 2 倍以上），优先用工具自带的 bin/faac.exe
_FAAC_PATH = None
for _candidate in [
    str(Path(__file__).resolve().parent.parent / "bin" / "faac.exe"),
    shutil.which("faac"),
]:
    if _candidate and Path(_candidate).exists():
        _FAAC_PATH = str(_candidate)
        break

# 硬件编码器缓存（检测一次后复用）
_fast_encoder_cache: dict | None = None


def detect_fast_encoder() -> dict:
    """检测系统上最快的可用 H.264 编码器，返回 {encoder, extra, label, hardware}。

    优先级：NVIDIA NVENC > Intel QSV > AMD AMF > libx264(ultrafast 软件回退)。
    检测结果全局缓存，避免每次转码都跑 ffmpeg -encoders。
    """
    global _fast_encoder_cache
    if _fast_encoder_cache is not None:
        return _fast_encoder_cache

    candidates = [
        ("h264_nvenc", ["-preset", "p1", "-cq", "23", "-rc", "vbr"], "NVIDIA NVENC"),
        ("h264_qsv", ["-preset", "veryfast", "-global_quality", "23"], "Intel QSV"),
        ("h264_amf", ["-quality", "speed", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"], "AMD AMF"),
    ]
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15).stdout
        for enc, extra, label in candidates:
            if enc in out:
                test = subprocess.run(
                    [FFMPEG, "-hide_banner", "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=1",
                     "-c:v", enc] + extra + ["-frames:v", "1", "-f", "null", "NUL"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
                if test.returncode == 0:
                    _fast_encoder_cache = {"encoder": enc, "extra": extra, "label": label, "hardware": True}
                    return _fast_encoder_cache
    except Exception:
        pass
    _fast_encoder_cache = {
        "encoder": "libx264",
        "extra": ["-preset", "ultrafast", "-crf", "23"],
        "label": "libx264 ultrafast",
        "hardware": False,
    }
    return _fast_encoder_cache


class FFmpegError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    width: int
    height: int
    fps: float
    duration: float | None
    has_audio: bool
    sample_rate: int | None = None
    channels: int | None = None
    video_bitrate: int | None = None

    @property
    def is_16x9(self) -> bool:
        return abs((self.width / self.height) - (16 / 9)) < 0.02


def probe(path: Path | str) -> MediaInfo:
    """用 ffprobe 探测媒体文件信息。"""
    cmd = [
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError("ffprobe 失败: %s" % (proc.stderr or proc.stdout)[:500])
    data = json.loads(proc.stdout or "{}")

    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    astream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if vstream is None:
        raise FFmpegError("文件中没有视频流: %s" % path)

    fps = 30.0
    rate = (vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "30/1")
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except (ValueError, ZeroDivisionError):
        fps = 30.0

    duration = None
    dur = data.get("format", {}).get("duration")
    if dur:
        try:
            duration = float(dur)
        except ValueError:
            duration = None

    vbit = None
    if "bit_rate" in vstream:
        try:
            vbit = int(vstream["bit_rate"])
        except (ValueError, TypeError):
            vbit = None

    return MediaInfo(
        width=int(vstream["width"]),
        height=int(vstream["height"]),
        fps=round(fps, 3),
        duration=duration,
        has_audio=astream is not None,
        sample_rate=int(astream["sample_rate"]) if astream and "sample_rate" in astream else None,
        channels=int(astream["channels"]) if astream and "channels" in astream else None,
        video_bitrate=vbit,
    )


def transcode_vp9(src: Path | str, dst: Path, width: int, height: int,
                  fps: float, bitrate_k: int = 10000, maxrate_k: int = 11000,
                  on_progress=None, two_pass: bool = True, jobs: int | None = None) -> Path:
    """把任意视频转码为匹配目标规格的 VP9 .ivf（带实时进度回调）。

    on_progress(frac, stage)：frac 为编码总进度 0-1，stage 为 "分析"/"编码"。
    two_pass=True：两遍编码（对齐码率，更慢）；two_pass=False：单遍 CRF 21（速度快约 40%，CRF 控质量）。
    宽高比不一致时按 16:9 信箱（letterbox）补齐，保证画面完整。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        dur = float(probe(src).duration) or None
    except Exception:
        dur = None

    def _run(args: list[str], stage: str, start_frac: float, span: float):
        import threading as _threading
        import time as _time
        proc = subprocess.Popen(args + ["-progress", "pipe:1"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        err_tail: list[bytes] = []

        def _err_reader():
            buf = bytearray()
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 8192:
                    del buf[:-8192]
            err_tail.append(bytes(buf))

        _threading.Thread(target=_err_reader, daemon=True).start()
        frame_total = int(dur * fps) if dur else None
        last = 0.0
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            cur = None
            if line.startswith(b"out_time_us="):
                try:
                    cur = int(line.split(b"=", 1)[1]) / 1_000_000.0
                except (ValueError, IndexError):
                    pass
            elif line.startswith(b"frame=") and frame_total:
                try:
                    cur = int(line.split(b"=", 1)[1]) / frame_total * dur
                except (ValueError, IndexError):
                    pass
            if cur is not None and on_progress and dur:
                now = _time.monotonic()
                if now - last >= 0.15:
                    last = now
                    try:
                        on_progress(start_frac + span * min(1.0, cur / dur), stage)
                    except Exception:
                        pass
        rc = proc.wait()
        if rc != 0:
            err = (err_tail[0] if err_tail else b"").decode("utf-8", "replace")[-800:]
            raise FFmpegError("ffmpeg 失败: %s" % err)

    base = [
        FFMPEG, "-hide_banner", "-y", "-i", str(src),
        "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1"
        % (width, height, width, height),
        "-r", str(fps),
        "-c:v", "libvpx-vp9",
        "-row-mt", "1", "-cpu-used", "7", "-deadline", "realtime", "-lag-in-frames", "0", "-auto-alt-ref", "0",
        "-tile-columns", "2", "-tile-rows", "2",
        "-threads", str(max(2, min(8, (os.cpu_count() or 4)))),
    ]
    if two_pass:
        # 并行分片两遍（质量与整段两遍一致：每段独立 -b:v/-maxrate 对齐码率）
        try:
            _transcode_vp9_pass_parallel(src, dst, width, height, fps,
                                         bitrate_k, maxrate_k, on_progress, jobs=jobs)
            for ext in (".ffpass-0.log", ".ffpass-0.log.mbtree"):
                try:
                    (dst.parent / (dst.name + ext)).unlink(missing_ok=True)
                except OSError:
                    pass
            return dst
        except Exception:
            # 分片失败回退整段两遍
            _enc = base + [
                "-b:v", "%dk" % bitrate_k, "-maxrate", "%dk" % maxrate_k,
                "-bufsize", "%dk" % (bitrate_k // 2),
            ]
            _run(_enc + ["-pass", "1", "-an", "-f", "null", "NUL"], "分析", 0.0, 0.4)
            _run(_enc + ["-pass", "2", "-an", "-y", str(dst)], "编码", 0.4, 0.6)
    else:
        enc = base + ["-crf", "21", "-b:v", "0"]
        _run(enc + ["-an", "-y", str(dst)], "编码", 0.0, 1.0)
    for ext in (".ffpass-0.log", ".ffpass-0.log.mbtree"):
        try:
            (dst.parent / (dst.name + ext)).unlink(missing_ok=True)
        except OSError:
            pass
    return dst


def _transcode_vp9_pass_parallel(src, dst, width, height, fps,
                                bitrate_k, maxrate_k, on_progress=None,
                                jobs: int | None = None) -> Path:
    """并行分片两遍 VP9：每段在独立子目录跑 pass1+pass2（避免 .ffpass 冲突），
    拼接产物与整段两遍等价（对齐码率）。"""
    import concurrent.futures as _cf
    import tempfile as _tmp
    import struct as _st
    import shutil as _sh

    dst = Path(dst)
    dur = float(probe(src).duration) or 0.0
    if dur <= 0:
        raise RuntimeError("无法探测时长")
    if jobs is None:
        jobs = max(4, min(8, (os.cpu_count() or 8) // 2))
    n = max(1, min(int(jobs), 8))
    seg_len = dur / n
    root = Path(_tmp.mkdtemp(prefix="vp9p2_"))

    def _seg(i):
        seg_dir = root / ("s%02d" % i)
        seg_dir.mkdir(parents=True, exist_ok=True)
        out = seg_dir / "seg.ivf"
        start = i * seg_len
        length = min(seg_len, dur - start)
        common = [
            FFMPEG, "-hide_banner", "-y", "-v", "error",
            "-ss", "%.4f" % start, "-t", "%.4f" % length, "-i", str(src),
            "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1"
            % (width, height, width, height),
            "-r", str(fps),
            "-c:v", "libvpx-vp9",
            "-row-mt", "1", "-cpu-used", "7", "-deadline", "realtime", "-lag-in-frames", "0", "-auto-alt-ref", "0",
            "-tile-columns", "2", "-tile-rows", "2",
            "-threads", "2",
            "-b:v", "%dk" % bitrate_k, "-maxrate", "%dk" % maxrate_k,
            "-bufsize", "%dk" % (bitrate_k // 2),
        ]
        subprocess.run(common + ["-pass", "1", "-an", "-f", "null", "NUL"],
                       check=True, capture_output=True, cwd=str(seg_dir))
        subprocess.run(common + ["-pass", "2", "-an", "-y", str(out)],
                       check=True, capture_output=True, cwd=str(seg_dir))
        return out

    try:
        with _cf.ThreadPoolExecutor(max_workers=n) as ex:
            futs = [ex.submit(_seg, i) for i in range(n)]
            parts = []
            for i, f in enumerate(futs):
                parts.append(f.result())
                if on_progress:
                    try:
                        on_progress((i + 1) / n, "编码")
                    except Exception:
                        pass
        # 拼接（同 fast 路径）
        head = bytearray(parts[0].read_bytes())
        body = bytearray()
        gi = 0
        for p in parts:
            with open(p, "rb") as fh:
                fh.seek(32)
                while True:
                    hdr = fh.read(12)
                    if len(hdr) < 12:
                        break
                    size = _st.unpack("<I", hdr[:4])[0]
                    data = fh.read(size)
                    if len(data) < size:
                        break
                    body += _st.pack("<IQ", size, gi)
                    body += data
                    gi += 1
        _st.pack_into("<I", head, 24, gi)
        dst.write_bytes(bytes(head) + bytes(body))
        return dst
    finally:
        try:
            _sh.rmtree(root, ignore_errors=True)
        except OSError:
            pass


def transcode_vp9_fast(src: Path | str, dst: Path, width: int, height: int,
                       fps: float, bitrate_k: int = 10000, maxrate_k: int = 11000,
                       on_progress=None, jobs: int | None = None) -> Path:
    """并行分片转码 VP9（CRF 21 恒定质量，质量与整段编码一致，实测快约 2.5 倍）。

    把源视频均分 N 段并行编码为 IVF，再按全局帧序号重写时间戳拼接。
    拼接产物与整段编码等价（无丢帧、可正常解码）。
    """
    import concurrent.futures as _cf
    import tempfile as _tmp
    import struct as _st

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dur = float(probe(src).duration) or None
    except Exception:
        dur = None
    if not dur or dur <= 0:
        # 探测失败退回单遍
        return transcode_vp9(src, dst, width, height, fps, bitrate_k, maxrate_k,
                             on_progress=on_progress, two_pass=False)
    if jobs is None:
        jobs = max(4, min(8, (os.cpu_count() or 8) // 2))
    n = max(1, min(int(jobs), 8))
    seg_len = dur / n
    work = Path(_tmp.mkdtemp(prefix="vp9par_"))

    def _seg(i):
        out = work / ("seg%02d.ivf" % i)
        start = i * seg_len
        length = min(seg_len, dur - start)
        args = [
            FFMPEG, "-hide_banner", "-y", "-v", "error",
            "-ss", "%.4f" % start, "-t", "%.4f" % length, "-i", str(src),
            "-vf", "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1"
            % (width, height, width, height),
            "-r", str(fps),
            "-c:v", "libvpx-vp9", "-crf", "21", "-b:v", "0",
            "-row-mt", "1", "-cpu-used", "7", "-deadline", "realtime", "-lag-in-frames", "0", "-auto-alt-ref", "0",
            "-tile-columns", "2", "-tile-rows", "2",
            "-threads", "2",
            "-an", "-f", "ivf", str(out),
        ]
        subprocess.run(args, check=True, capture_output=True)
        return out

    try:
        with _cf.ThreadPoolExecutor(max_workers=n) as ex:
            futs = [ex.submit(_seg, i) for i in range(n)]
            parts = []
            for i, f in enumerate(futs):
                parts.append(f.result())
                if on_progress:
                    try:
                        on_progress((i + 1) / n, "编码")
                    except Exception:
                        pass
        # 拼接 IVF：保留第一段 32 字节头，帧时间戳按全局帧序号重写
        head = bytearray(parts[0].read_bytes()[:32])
        body = bytearray()
        gi = 0
        for p in parts:
            with open(p, "rb") as fh:
                fh.seek(32)
                while True:
                    hdr = fh.read(12)
                    if len(hdr) < 12:
                        break
                    size = _st.unpack("<I", hdr[:4])[0]
                    data = fh.read(size)
                    if len(data) < size:
                        break
                    body += _st.pack("<IQ", size, gi)
                    body += data
                    gi += 1
        _st.pack_into("<I", head, 24, gi)
        dst.write_bytes(bytes(head) + bytes(body))
        return dst
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except OSError:
            pass


def to_h264_mp4(video: Path | str, audio: Path | str | None, dst: Path,
                width: int | None = None, height: int | None = None,
                fast: bool = True) -> Path:
    """把 VP9 视频流（可选音频）合成 H.264/AAC 的 MP4，便于系统播放器预览。

    fast=True 时自动使用硬件编码器（NVENC/QSV/AMF）或 libx264 ultrafast，
    并默认缩到 720p（预览用，大幅提速）。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    enc = detect_fast_encoder() if fast else {
        "encoder": "libx264", "extra": ["-preset", "fast", "-crf", "20"], "label": "libx264"
    }
    args = [FFMPEG, "-hide_banner", "-y", "-i", str(video)]
    if audio is not None:
        args += ["-i", str(audio)]
    args += ["-c:v", enc["encoder"]] + enc["extra"] + ["-pix_fmt", "yuv420p"]
    if audio is not None:
        args += ["-c:a", "aac_mf", "-b:a", "160k"]
    else:
        args += ["-an"]
    # 预览默认缩到 720p（fast 模式），大幅降低编码量；指定 width/height 时按指定尺寸
    if fast and not (width and height):
        width, height = 1280, 720
    if width and height:
        args += ["-vf", "scale=%d:%d" % (width, height)]
    args += ["-movflags", "+faststart", str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError("ffmpeg 失败: %s" % (proc.stderr or proc.stdout)[-800:])
    return dst


def _export_encoder_args() -> list[str]:
    """导出用视频编码器：NVENC H.264 硬编优先（质量接近 x264、速度快数倍），
    无 NVENC 时回退 libx264 CRF18。返回形如 ["-c:v", ..., ...] 的参数列表。"""
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=10).stdout
        if "h264_nvenc" in out:
            return ["-c:v", "h264_nvenc", "-preset", "p6", "-cq", "19",
                    "-rc", "vbr", "-b:v", "0", "-pix_fmt", "yuv420p"]
    except Exception:
        pass
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"]


def export_mp4_lossless(video: Path | str, audio: Path | str | None, dst: Path,
                        on_progress=None) -> Path:
    """导出 MP4（H.264/AAC，高质量）：原始分辨率 + 视觉无损视频 + 48kHz/320k AAC 音频。

    用于「导出视频为 MP4」：保持原分辨率和原始音轨采样率，不做任何降级。
    优先 NVENC 硬编（质量≈x264 CRF18-20，速度快数倍）；on_progress(p) 回传 0.0~1.0 进度。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 视频时长（进度计算用）
    total_us = 0
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=15).stdout.strip()
        total_us = int(float(out) * 1_000_000)
    except Exception:
        total_us = 0
    args = [FFMPEG, "-hide_banner", "-y", "-i", str(video)]
    if audio is not None:
        args += ["-i", str(audio)]
    args += _export_encoder_args()
    if audio is not None:
        args += ["-c:a", "aac", "-b:a", "320k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(dst)]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")

    import threading

    def _read_progress():
        last_us = 0
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    last_us = int(line.split("=")[1])
                except Exception:
                    pass
            elif line.startswith("progress=") and on_progress and total_us > 0:
                p = min(1.0, last_us / total_us) if last_us else 0.0
                on_progress(p)
        proc.stdout.close()

    t = threading.Thread(target=_read_progress, daemon=True)
    t.start()
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        err = proc.stderr.read() if proc.stderr else ""
        raise FFmpegError("ffmpeg 失败: %s" % err[-800:])
    return dst


def extract_audio_wav(src: Path | str, dst: Path) -> Path:
    """从任意视频提取/重采样音轨为 WAV（48kHz 立体声，供 HCA 编码）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([
        FFMPEG, "-hide_banner", "-y", "-i", str(src),
        "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(dst),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError("提取音轨失败: %s" % (proc.stderr or proc.stdout)[-800:])
    return dst


def make_silence_wav(dst: Path | str, duration: float,
                     sample_rate: int = 48000, channels: int = 2) -> Path:
    """生成指定时长/采样率的静音 WAV（供无音轨视频补音轨用）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, float(duration or 0))
    proc = subprocess.run([
        FFMPEG, "-hide_banner", "-y", "-f", "lavfi",
        "-i", "anullsrc=r=%d:cl=stereo" % sample_rate,
        "-t", "%.3f" % dur, "-ac", str(channels), "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(dst),
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError("生成静音音轨失败: %s" % (proc.stderr or proc.stdout)[-800:])
    return dst


def to_fast_mp4(video: Path | str, audio: Path | str | None, dst: Path) -> Path:
    """快速预览封装：VP9 视频流直接 copy 到 MP4（不重新编码）+ AAC 音频。

    比 to_h264_mp4 快 4-10 倍（跳过视频编码），输出 MP4 内含 VP9 码流，
    工具内嵌播放器（ffmpeg 解码）可正常播放。适用于批量预览转换。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [FFMPEG, "-hide_banner", "-y", "-i", str(video)]
    if audio is not None:
        args += ["-i", str(audio)]
    args += ["-c:v", "copy"]
    if audio is not None:
        args += ["-c:a", "aac_mf", "-b:a", "160k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(dst)]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError("ffmpeg 快速封装失败: %s" % (proc.stderr or proc.stdout)[-800:])
    return dst


def to_fast_mp4_pipe(video: Path | str, wav_bytes: bytes | None, dst: Path) -> Path:
    """快速预览封装（管道版）：VP9 copy + WAV 通过 stdin 传入，避免 WAV 临时文件。

    比 to_fast_mp4 少一次 50MB+ 的 WAV 磁盘读写，批量转换更快。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = [FFMPEG, "-hide_banner", "-y", "-i", str(video)]
    if wav_bytes is not None:
        args += ["-f", "wav", "-i", "pipe:0"]
    args += ["-c:v", "copy"]
    if wav_bytes is not None:
        args += ["-c:a", "aac_mf", "-b:a", "160k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(dst)]
    proc = subprocess.run(args, input=wav_bytes, capture_output=True, timeout=120)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        raise FFmpegError("ffmpeg 快速封装失败: %s" % stderr[-800:])
    return dst


def to_fast_mp4_memory(video_bytes: bytes, wav_bytes: bytes | None, dst: Path) -> Path:
    """零临时文件快速封装：IVF 通过 Windows 命名管道传入，WAV 通过 stdin 传入。

    避免 89MB IVF 临时文件的写读，比 to_fast_mp4_pipe 再快 2-3 倍。
    仅支持 Windows（命名管道）。
    """
    import ctypes
    import threading
    import uuid
    from ctypes import wintypes

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # 创建命名管道（只写，字节流，阻塞模式）
    pipe_name = r"\\.\pipe\p5r_ivf_%s" % uuid.uuid4().hex[:10]
    PIPE_ACCESS_OUTBOUND = 0x00000002
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateNamedPipeW(
        pipe_name, PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT,
        255, 65536, 65536, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise FFmpegError("创建命名管道失败")

    pipe_error: list[Exception] = []

    def _serve():
        try:
            # 等待 ffmpeg 连接（阻塞）
            kernel32.ConnectNamedPipe(handle, None)
            # 写入全部 IVF 数据
            written = wintypes.DWORD(0)
            offset = 0
            while offset < len(video_bytes):
                chunk = video_bytes[offset:offset + 1048576]  # 1MB chunks
                ok = kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None)
                if not ok:
                    break
                offset += written.value
        except Exception as e:
            pipe_error.append(e)
        finally:
            kernel32.CloseHandle(handle)

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()

    # ffmpeg：从命名管道读 IVF，从 stdin 读 WAV，VP9 copy + aac_mf，
    # 普通 MP4 直接写磁盘（高码率 aac_mf 下比碎片化 MP4+stdout 更快）
    args = [FFMPEG, "-hide_banner", "-y", "-f", "ivf", "-i", pipe_name]
    if wav_bytes is not None:
        args += ["-f", "wav", "-i", "pipe:0"]
    args += ["-c:v", "copy"]
    if wav_bytes is not None:
        args += ["-c:a", "aac_mf", "-b:a", "160k"]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(dst)]

    proc = subprocess.run(args, input=wav_bytes, capture_output=True, timeout=120)
    server_thread.join(timeout=5)

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        raise FFmpegError("ffmpeg 内存封装失败: %s" % stderr[-800:])
    if pipe_error:
        raise FFmpegError("命名管道写入失败: %s" % pipe_error[0])
    return dst


# ============================================================================
# Python 原生 MP4 封装（跳过 ffmpeg 的视频解析+封装开销，比 to_fast_mp4_memory 更快）
# 视频：VP9 direct copy（从 IVF 提取帧）；音频：aac_mf 预编码为 AAC ADTS
# ============================================================================

def encode_aac_adts(wav_bytes: bytes, bitrate: str = "160k") -> bytes:
    """把 WAV(PCM) 编码为 AAC ADTS 流。

    优先用 FAAC（比 aac_mf 快 2 倍以上），FAAC 不可用时回退到 ffmpeg aac_mf。
    FAAC 输出到临时文件（stdout 会输出文本信息），读取后删除。
    """
    # 解析码率：FAAC 的 -b 参数接受 kbps 数字（如 160）
    try:
        br_kbps = int(str(bitrate).lower().replace("k", "").replace("kbps", ""))
    except ValueError:
        br_kbps = 160

    if _FAAC_PATH:
        # 用 WorkDir ASCII 临时目录（FAAC 可能不支持中文路径）
        from .paths import WorkDir
        work = WorkDir()
        try:
            tmp_aac = work.path / "encode_tmp.aac"
            # FAAC：从 stdin 读 WAV，输出到文件；cwd 设为 faac 所在目录（含 libfaac-0.dll）
            proc = subprocess.run(
                [_FAAC_PATH, "-b", str(br_kbps), "-o", str(tmp_aac), "-"],
                input=wav_bytes, capture_output=True, timeout=120,
                cwd=str(Path(_FAAC_PATH).parent))
            if proc.returncode == 0 and tmp_aac.exists():
                data = tmp_aac.read_bytes()
                return data
            # FAAC 失败时回退到 ffmpeg aac_mf
        finally:
            work.cleanup()

    # 回退：ffmpeg aac_mf
    args = [FFMPEG, "-hide_banner", "-y", "-f", "wav", "-i", "pipe:0",
            "-c:a", "aac_mf", "-b:a", bitrate, "-f", "adts", "pipe:1"]
    proc = subprocess.run(args, input=wav_bytes, capture_output=True, timeout=120)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        raise FFmpegError("AAC 编码失败: %s" % stderr[-500:])
    return proc.stdout


def _py_box(typ: str, data: bytes) -> bytes:
    return struct.pack(">I", 8 + len(data)) + typ.encode("ascii") + data


def _py_fullbox(typ: str, version: int, flags: int, data: bytes) -> bytes:
    return _py_box(typ, struct.pack(">I", (version << 24) | flags) + data)


def _parse_ivf(data: bytes):
    """解析 IVF，返回 (width, height, timescale_den, timescale_num, [(timestamp, frame_data)])"""
    if data[:4] != b"DKIF":
        raise ValueError("not IVF")
    header_len = struct.unpack_from("<H", data, 6)[0]
    width = struct.unpack_from("<H", data, 12)[0]
    height = struct.unpack_from("<H", data, 14)[0]
    timescale_den = struct.unpack_from("<I", data, 16)[0]
    timescale_num = struct.unpack_from("<I", data, 20)[0]
    pos = header_len
    frames = []
    while pos + 12 <= len(data):
        frame_size = struct.unpack_from("<I", data, pos)[0]
        timestamp = struct.unpack_from("<Q", data, pos + 4)[0]
        if pos + 12 + frame_size > len(data):
            break
        frames.append((timestamp, data[pos + 12:pos + 12 + frame_size]))
        pos += 12 + frame_size
    return width, height, timescale_den, timescale_num, frames


def _parse_adts(data: bytes):
    """解析 AAC ADTS，返回 (sample_rate, channels, [raw_aac_frame])"""
    sr_table = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                16000, 12000, 11025, 8000, 7350]
    pos = 0
    frames = []
    sample_rate = 48000
    channels = 2
    while pos + 7 <= len(data):
        if data[pos] != 0xFF or (data[pos + 1] & 0xF0) != 0xF0:
            pos += 1
            continue
        sr_idx = (data[pos + 2] >> 2) & 0x0F
        chan_cfg = ((data[pos + 2] & 0x01) << 2) | ((data[pos + 3] >> 6) & 0x03)
        frame_len = ((data[pos + 3] & 0x03) << 11) | (data[pos + 4] << 3) | ((data[pos + 5] >> 5) & 0x07)
        if sr_idx < len(sr_table):
            sample_rate = sr_table[sr_idx]
        channels = chan_cfg
        if pos + frame_len > len(data):
            break
        frames.append(data[pos + 7:pos + frame_len])
        pos += frame_len
    return sample_rate, channels, frames


def _build_vp09_entry(width: int, height: int) -> bytes:
    vpcc_data = struct.pack(">BBBBBBB", 0, 40, 0x12, 1, 1, 1, 0)  # profile0, level4.0, 8bit 4:2:0
    vpcc_data += struct.pack(">H", 0)
    vpcc_box = _py_box("vpcC", vpcc_data)
    entry = b"\x00" * 6 + struct.pack(">H", 1)
    entry += struct.pack(">HH", 0, 0) + b"\x00" * 12
    entry += struct.pack(">HH", width, height)
    entry += struct.pack(">II", 0x00480000, 0x00480000)
    entry += struct.pack(">I", 0) + struct.pack(">H", 1) + b"\x00" * 32
    entry += struct.pack(">H", 0x0018) + struct.pack(">h", -1) + vpcc_box
    return _py_box("vp09", entry)


def _build_mp4a_entry(sample_rate: int, channels: int) -> bytes:
    sr_idx_map = {96000: 0, 88200: 1, 64000: 2, 48000: 3, 44100: 4, 32000: 5,
                  24000: 6, 22050: 7, 16000: 8, 12000: 9, 11025: 10, 8000: 11, 7350: 12}
    sr_idx = sr_idx_map.get(sample_rate, 3)
    chan_cfg = channels if channels <= 6 else 2
    asc = ((2 << 11) | (sr_idx << 7) | (chan_cfg << 3)) & 0xFFFF
    asc_bytes = struct.pack(">H", asc)

    def _desc(tag, data):
        size = len(data)
        sb = b""
        while True:
            b = size & 0x7F
            size >>= 7
            if size > 0:
                sb = struct.pack("B", b | 0x80) + sb
            else:
                sb = struct.pack("B", b) + sb
                break
        return struct.pack("B", tag) + sb + data

    dsi = _desc(0x05, asc_bytes)
    dcd_data = bytes([0x40, 0x15]) + struct.pack(">I", 0)[1:] + struct.pack(">I", 160000) + struct.pack(">I", 160000) + dsi
    dcd = _desc(0x04, dcd_data)
    slc = _desc(0x06, bytes([0x02]))
    es = _desc(0x03, struct.pack(">H", 0) + bytes([0x00]) + dcd + slc)
    esds_box = _py_fullbox("esds", 0, 0, es)

    entry = b"\x00" * 6 + struct.pack(">H", 1)
    entry += struct.pack(">II", 0, 0)
    entry += struct.pack(">HH", channels, 16)
    entry += struct.pack(">HH", 0, 0)
    entry += struct.pack(">I", sample_rate << 16) + esds_box
    return _py_box("mp4a", entry)


def _build_video_trak(frames, width, height, frame_duration, chunk_offsets, mdat_start, timescale):
    duration = len(frames) * frame_duration
    tkhd_data = struct.pack(">IIII", 0, 0, 1, 0) + struct.pack(">I", duration)
    tkhd_data += b"\x00" * 8 + struct.pack(">HH", 0, 0) + struct.pack(">H", 0) + b"\x00" * 2
    tkhd_data += struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    tkhd_data += struct.pack(">II", width << 16, height << 16)
    tkhd_box = _py_fullbox("tkhd", 0, 3, tkhd_data)

    mdhd_data = struct.pack(">IIII", 0, 0, timescale, duration) + struct.pack(">HHI", 0x55C4, 0, 0)
    mdhd_box = _py_fullbox("mdhd", 0, 0, mdhd_data)
    hdlr_box = _py_fullbox("hdlr", 0, 0, struct.pack(">I", 0) + b"vide" + b"\x00" * 12 + b"VideoHandler\x00")
    vmhd_box = _py_fullbox("vmhd", 0, 1, struct.pack(">HBBB", 0, 0, 0, 0))
    url_box = _py_fullbox("url ", 0, 1, b"")
    dref_box = _py_fullbox("dref", 0, 0, struct.pack(">I", 1) + url_box)
    dinf_box = _py_box("dinf", dref_box)

    vp09_entry = _build_vp09_entry(width, height)
    stsd_box = _py_fullbox("stsd", 0, 0, struct.pack(">I", 1) + vp09_entry)
    stts_box = _py_fullbox("stts", 0, 0, struct.pack(">I", 1) + struct.pack(">II", len(frames), frame_duration))
    stsc_box = _py_fullbox("stsc", 0, 0, struct.pack(">IIII", 1, 1, 1, 1))
    stsz_data = struct.pack(">II", 0, len(frames)) + b"".join(struct.pack(">I", len(f[1])) for f in frames)
    stsz_box = _py_fullbox("stsz", 0, 0, stsz_data)
    stco_data = struct.pack(">I", len(chunk_offsets)) + b"".join(struct.pack(">I", mdat_start + o) for o in chunk_offsets)
    stco_box = _py_fullbox("stco", 0, 0, stco_data)

    stbl_box = _py_box("stbl", stsd_box + stts_box + stsc_box + stsz_box + stco_box)
    minf_box = _py_box("minf", vmhd_box + dinf_box + stbl_box)
    mdia_box = _py_box("mdia", mdhd_box + hdlr_box + minf_box)
    return _py_box("trak", tkhd_box + mdia_box)


def _build_audio_trak(frames, sample_rate, channels, frame_duration, chunk_offsets, mdat_start, timescale):
    duration = len(frames) * frame_duration
    tkhd_data = struct.pack(">IIII", 0, 0, 2, 0) + struct.pack(">I", duration)
    tkhd_data += b"\x00" * 8 + struct.pack(">HH", 0, 0) + struct.pack(">H", 0x0100) + b"\x00" * 2
    tkhd_data += struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    tkhd_data += struct.pack(">II", 0, 0)
    tkhd_box = _py_fullbox("tkhd", 0, 3, tkhd_data)

    mdhd_data = struct.pack(">IIII", 0, 0, timescale, duration) + struct.pack(">HHI", 0x55C4, 0, 0)
    mdhd_box = _py_fullbox("mdhd", 0, 0, mdhd_data)
    hdlr_box = _py_fullbox("hdlr", 0, 0, struct.pack(">I", 0) + b"soun" + b"\x00" * 12 + b"SoundHandler\x00")
    smhd_box = _py_fullbox("smhd", 0, 0, struct.pack(">Hh", 0, 0))
    url_box = _py_fullbox("url ", 0, 1, b"")
    dref_box = _py_fullbox("dref", 0, 0, struct.pack(">I", 1) + url_box)
    dinf_box = _py_box("dinf", dref_box)

    mp4a_entry = _build_mp4a_entry(sample_rate, channels)
    stsd_box = _py_fullbox("stsd", 0, 0, struct.pack(">I", 1) + mp4a_entry)
    stts_box = _py_fullbox("stts", 0, 0, struct.pack(">I", 1) + struct.pack(">II", len(frames), frame_duration))
    stsc_box = _py_fullbox("stsc", 0, 0, struct.pack(">IIII", 1, 1, 1, 1))
    stsz_data = struct.pack(">II", 0, len(frames)) + b"".join(struct.pack(">I", len(f)) for f in frames)
    stsz_box = _py_fullbox("stsz", 0, 0, stsz_data)
    stco_data = struct.pack(">I", len(chunk_offsets)) + b"".join(struct.pack(">I", mdat_start + o) for o in chunk_offsets)
    stco_box = _py_fullbox("stco", 0, 0, stco_data)

    stbl_box = _py_box("stbl", stsd_box + stts_box + stsc_box + stsz_box + stco_box)
    minf_box = _py_box("minf", smhd_box + dinf_box + stbl_box)
    mdia_box = _py_box("mdia", mdhd_box + hdlr_box + minf_box)
    return _py_box("trak", tkhd_box + mdia_box)


def mux_mp4_python(ivf_bytes: bytes, aac_adts_bytes: bytes | None, dst: Path | str) -> Path:
    """Python 原生封装 MP4：VP9 从 IVF 直接提取帧（无损 copy），AAC 从 ADTS 提取帧。

    比 ffmpeg 一体化封装快，因为跳过了 ffmpeg 的 VP9 帧解析+MP4 封装开销。
    音视频质量完全无损。
    """
    import struct as _struct
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    vw, vh, vden, vnum, vframes = _parse_ivf(ivf_bytes)
    video_timescale = 30000
    fps = vden / vnum if vnum else 30
    frame_duration = int(video_timescale / fps + 0.5)

    a_sample_rate = 48000
    a_channels = 2
    aframes = []
    if aac_adts_bytes:
        a_sample_rate, a_channels, aframes = _parse_adts(aac_adts_bytes)
    audio_timescale = a_sample_rate
    audio_frame_duration = 1024

    # 构建 mdat：所有视频帧在前，音频帧在后
    mdat_parts = []
    video_offsets = []
    audio_offsets = []
    mdat_pos = 0
    for _ts, vdata in vframes:
        video_offsets.append(mdat_pos)
        mdat_parts.append(vdata)
        mdat_pos += len(vdata)
    for adata in aframes:
        audio_offsets.append(mdat_pos)
        mdat_parts.append(adata)
        mdat_pos += len(adata)
    mdat_data = b"".join(mdat_parts)
    mdat_box = _py_box("mdat", mdat_data)

    ftyp_box = _py_box("ftyp", b"isom" + _struct.pack(">I", 0) + b"isom" + b"iso2" + b"mp41")
    mdat_data_start = len(ftyp_box) + 8  # ftyp + mdat header

    # moov
    duration_v = len(vframes) * frame_duration
    duration_a = len(aframes) * audio_frame_duration
    mvhd_duration = max(duration_v, int(duration_a * video_timescale / audio_timescale))
    mvhd_data = _struct.pack(">IIII", 0, 0, video_timescale, mvhd_duration)
    mvhd_data += _struct.pack(">I", 0x00010000) + _struct.pack(">H", 0x0100) + b"\x00" * 10
    mvhd_data += _struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
    mvhd_data += b"\x00" * 24 + _struct.pack(">I", 2)
    mvhd_box = _py_fullbox("mvhd", 0, 0, mvhd_data)

    video_trak = _build_video_trak(vframes, vw, vh, frame_duration, video_offsets, mdat_data_start, video_timescale)
    audio_trak = _build_audio_trak(aframes, a_sample_rate, a_channels, audio_frame_duration, audio_offsets, mdat_data_start, audio_timescale) if aframes else b""
    moov_box = _py_box("moov", mvhd_box + video_trak + audio_trak)

    with open(dst, "wb") as f:
        f.write(ftyp_box)
        f.write(mdat_box)
        f.write(moov_box)
    return dst
