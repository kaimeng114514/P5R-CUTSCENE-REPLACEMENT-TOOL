# -*- coding: utf-8 -*-
# 进程 DPI 感知：保证 PhotoImage/Canvas 像素 1:1 显示，
# 避免系统缩放对位图拉伸导致描边出现缺口、文字模糊。
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass
"""P5R过场动画替换工具 - 图形界面（界面风格参考 P5R BGM Editor）。

功能：
  1. 解包：从 MOVIE_JE.CPK 一键提取全部过场动画（.usm）
  2. 预览：右侧面板内嵌视频播放器，选中过场即可直接预览
  3. 替换：把自己的视频替换为过场动画，打包成加密 USM 并生成 Reloaded-II mod
"""
import io
import json
import os
import queue
import shutil
import subprocess

# 防止子进程弹出控制台窗口（Windows）
_NO_WIN = 0x08000000
_orig_run = subprocess.run
_orig_popen = subprocess.Popen
def _run_nw(*a, **kw):
    kw.setdefault('creationflags', _NO_WIN)
    return _orig_run(*a, **kw)
def _popen_nw(*a, **kw):
    kw.setdefault('creationflags', _NO_WIN)
    return _orig_popen(*a, **kw)
subprocess.run = _run_nw
subprocess.Popen = _popen_nw
import sys
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from p5r_tool import engine, ffmpeg
from p5r_tool.ffmpeg import FFmpegError

APP_TITLE = "P5R过场动画替换工具 (Persona 5 Royal Cutscene Replacement Tool)"
APP_VERSION = "1.3.0"

MOVIE_CPK_NAMES = ("MOVIE_JE.CPK", "movie_je.cpk")


def _register_bundled_fonts():
    """注册工具自带开源字体（得意黑/粉圆，OFL 协议，可自由商用与随工具分发）。

    仅以 FR_PRIVATE 方式注册到当前进程，不写入系统，工具移除后自动失效。
    """
    try:
        import ctypes
        gdi32 = ctypes.windll.gdi32
        FR_PRIVATE = 0x10
        fonts_dir = Path(__file__).parent / "assets" / "fonts"
        if not fonts_dir.exists():
            return
        for f in sorted(list(fonts_dir.glob("*.ttf")) + list(fonts_dir.glob("*.otf"))):
            try:
                gdi32.AddFontResourceExW(str(f), FR_PRIVATE, 0)
            except Exception:
                pass
    except Exception:
        pass


_register_bundled_fonts()
# 华康系字形的开源替代：标题/强调用「得意黑」，正文用「粉圆」；缺失时回退微软雅黑
F_TITLE_FONT = "得意黑"
F_BODY_FONT = "jf-openhuninn-2.1"
# P5 列表字体：思源黑体粗体（Noto Sans SC Bold，OFL 可商用），字形最接近 P5 的华康中黑体
F_LIST_FONT = "Noto Sans SC"

# ---------------- 配置（记住上次输出目录 / Reloaded-II Mods 目录） ----------------
_CFG_PATH = Path(__file__).resolve().parent / "p5r_tool_config.json"
_CFG_KEY_OUTDIR = "last_mod_outdir"
_CFG_KEY_RELOADED = "reloaded_mods_dir"


def _load_cfg() -> dict:
    try:
        if _CFG_PATH.exists():
            return json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cfg(cfg: dict):
    try:
        _CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _find_reloaded_mods_dir() -> str | None:
    """探测 Reloaded-II 的 Mods 扫描目录（内置优先，其次常见安装位置）。"""
    # 工具内置的 Reloaded-II 优先（用户选择集成方式）
    builtin = Path(__file__).resolve().parent / "Reloaded-II" / "Mods"
    try:
        if builtin.is_dir():
            return str(builtin)
    except OSError:
        pass
    cfg = _load_cfg()
    remembered = cfg.get(_CFG_KEY_RELOADED)
    if remembered:
        p = Path(remembered)
        if p.is_dir():
            return str(p)
    home = Path.home()
    cands = [
        home / "Downloads" / "Release" / "Mods",
        home / "Downloads" / "Reloaded-II" / "Mods",
        Path("C:/Reloaded-II/Mods"),
        Path("D:/Reloaded-II/Mods"),
        Path("E:/Reloaded-II/Mods"),
    ]
    for c in cands:
        try:
            if c.is_dir():
                return str(c)
        except OSError:
            continue
    return None


def _find_builtin_reloaded_exe() -> Path | None:
    """工具内置的 Reloaded-II.exe 路径（存在时返回）。"""
    p = Path(__file__).resolve().parent / "Reloaded-II" / "Reloaded-II.exe"
    return p if p.exists() else None


def _remember_outdir(d: str):
    cfg = _load_cfg()
    cfg[_CFG_KEY_OUTDIR] = d
    _save_cfg(cfg)

# ---------------- 配色（与 P5R BGM Editor 保持同一套视觉语言） ----------------
C_MAIN = "#E60012"         # 主操作（P5 红）
C_PLAY = "#E60012"         # 播放（红）
C_REPLACE = "#FF3B3B"      # 替换相关（亮红）
C_EXTRACTED = "#9FA8A3"    # 已解包（灰白）
C_MOD_MADE = "#FFC400"     # 已生成 Mod（金黄）
C_ERROR = "#FF1744"
C_GRAY = "#8C8C8C"
C_BLUE = "#E60012"         # 次要操作按钮（P5 红）
C_LEGEND_BG = "#1A1A1A"
C_HEADER = "#000000"       # 关于窗口头部（黑）
C_HEADER_SUB = "#8C8C8C"
C_BODY = "#1A1A1A"
C_LINK = "#E60012"
C_DIV = "#333333"
C_BG = "#0D0D0D"           # 窗口背景（黑）
C_PANEL = "#1A1A1A"        # 面板背景（深灰黑）
C_TOOLBTN = "#2A2A2A"      # 工具栏按钮底

# ---------------- 字体 ----------------
F_MENU = (F_BODY_FONT, 9)
F_LIST = (F_LIST_FONT, 10, "bold")
F_LABEL = (F_BODY_FONT, 9)
F_LABEL_BOLD = (F_TITLE_FONT, 9)
F_SMALL = (F_BODY_FONT, 8)
F_CN = (F_BODY_FONT, 9)
F_CN_BOLD = (F_TITLE_FONT, 11, "bold")
F_ABOUT_TITLE = (F_TITLE_FONT, 18, "bold")

# 预览画面显示尺寸（16:9）
PREVIEW_W, PREVIEW_H = 560, 315


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return str(n)


def _wav_peak(path: str, limit: int = 200000) -> int:
    """读取 wav 样本峰值（削波检测用）。

    P5R 部分过场 HCA 解码后峰值达 100% 满幅，pygame 播放时 D/A 削波会爆音，
    检测峰值后按比例降音量播放。返回 0 表示无法检测（按正常音量处理）。
    """
    try:
        import wave, struct
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                return 0
            n = min(w.getnframes(), limit)
            data = w.readframes(n)
        if not data:
            return 0
        peak = 0
        fmt = "<%dh" % (len(data) // 2)
        for s in struct.unpack(fmt, data)[::8]:
            a = abs(s)
            if a > peak:
                peak = a
        return peak
    except Exception:
        return 0


def _fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    return "%02d:%02d" % (sec // 60, sec % 60)


@dataclass
class MovieItem:
    """左侧列表中的一条过场记录。"""
    name: str                 # CPK 内路径，如 movie/event/e001.usm
    size: int = 0
    abs_path: str | None = None   # 已解包到本地的路径
    mod_made: bool = False        # 是否已生成 Mod
    replaced_with: str | None = None  # 标记替换的视频文件名（点「替换所选视频」时设置）
    has_audio: bool = True        # USM 中是否有音频流（游戏内电视/监控画面可能无音轨）
    extras: dict = field(default_factory=dict)


class TaskRunner(threading.Thread):
    """后台执行任务，进度/结果通过 queue 回传 UI。"""

    def __init__(self, fn, on_message):
        super().__init__(daemon=True)
        self._fn = fn
        self._q: queue.Queue = queue.Queue()
        self.on_message = on_message

    def run(self):
        try:
            result = self._fn(self._q)
            self._q.put(("done", result))
        except Exception as e:  # noqa: BLE001
            self._q.put(("error", "%s\n%s" % (e, traceback.format_exc()[-600:])))

    def poll(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                self.on_message(kind, payload)
        except queue.Empty:
            pass


class ProgressDialog(tk.Toplevel):
    """模态进度弹窗：深蓝标题栏 + 进度条 + 阶段文本 + 明细/计数。

    - total<=0：进度条为不确定模式（转动），只显示阶段与明细文字；
    - total>0 ：进度条为确定模式，显示百分比与 n/total 计数。
    不允许用户手动关闭（避免后台任务仍在跑时弹窗被关掉）。
    """

    def __init__(self, master, title="进度", phase="正在准备…"):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # 禁止关闭

        # 深蓝标题头
        head = tk.Frame(self, bg=C_HEADER, padx=14, pady=8)
        head.pack(side=tk.TOP, fill=tk.X)
        tk.Label(head, text=title, bg=C_HEADER, fg="white",
                 font=F_CN_BOLD).pack(anchor=tk.W)

        body = tk.Frame(self, padx=18, pady=14)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.phase_var = tk.StringVar(value=phase)
        tk.Label(body, textvariable=self.phase_var, font=F_CN_BOLD,
                 fg=C_BODY).pack(anchor=tk.W)

        self._bar = ttk.Progressbar(body, length=460, mode="indeterminate", maximum=1000)
        self._bar.pack(fill=tk.X, pady=(10, 2))
        self._bar.start(12)

        self.detail_var = tk.StringVar(value="")
        tk.Label(body, textvariable=self.detail_var, font=F_SMALL,
                 fg="#555", anchor=tk.W).pack(fill=tk.X, pady=(2, 0))

        self.count_var = tk.StringVar(value="")
        tk.Label(body, textvariable=self.count_var, font=F_SMALL,
                 fg=C_GRAY, anchor=tk.E).pack(fill=tk.X, pady=(2, 0))
        self._cur_max = 0.0

        # 居中于主窗口
        self.update_idletasks()
        try:
            mw, mh = master.winfo_width(), master.winfo_height()
            mx, my = master.winfo_rootx(), master.winfo_rooty()
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
            self.geometry("+%d+%d" % (mx + max(0, (mw - w) // 2), my + max(0, (mh - h) // 3)))
        except Exception:
            pass

    def set_phase(self, text: str):
        self.phase_var.set(text)

    def update(self, cur: float, total: int, detail: str = ""):
        if detail:
            self.detail_var.set(detail)
        if cur is not None and cur < 0:
            # 子阶段无真实进度（如两遍编码第一遍）：不确定模式转动
            self._bar.configure(mode="indeterminate")
            try:
                self._bar.start(12)
            except Exception:
                pass
            self.count_var.set("")
        elif total and total > 0:
            self._cur_max = max(self._cur_max, float(cur))
            try:
                self._bar.stop()
            except Exception:
                pass
            self._bar.configure(mode="determinate", maximum=1000,
                                value=int(min(cur, total) * 1000 / total))
            if isinstance(cur, float) and not cur.is_integer():
                self.count_var.set("%.1f / %d" % (cur, total))
            else:
                self.count_var.set("%d / %d" % (int(cur), total))
        else:
            self._bar.configure(mode="indeterminate")
            try:
                self._bar.start(12)
            except Exception:
                pass
            self.count_var.set("")

    def close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


class P5Scrollbar(tk.Canvas):
    """P5 风格自绘滚动条：黑色轨道 + 红色细滑块，支持拖动/轨道翻页。

    Windows 上经典 tk.Scrollbar 与 ttk 各主题都无法稳定渲染自定义颜色，
    这里用 Canvas 完全自绘：8px 细条，视觉与 P5 黑红主题一致。
    接口兼容 tk.Scrollbar：set(first, last) 供 yscrollcommand/xscrollcommand 调用，
    command 为滚动回调（listbox.yview / canvas.yview 等，支持 moveto/scroll）。
    """

    def __init__(self, master, orient="vertical", command=None, width=24, **kw):
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        kw.setdefault("bg", "#1A1A1A")
        # 垂直：固定 9px 宽，高度交给 pack fill=Y 拉伸；
        # 水平：固定 9px 高，宽度交给 pack fill=X 拉伸。
        if orient == "horizontal":
            kw.setdefault("height", width)
            kw.setdefault("width", 200)
        else:
            kw.setdefault("width", width)
            kw.setdefault("height", 200)
        super().__init__(master, **kw)
        self._orient = orient
        self.command = command          # 晚绑定：Listbox/Canvas 创建后赋值
        self._first = 0.0
        self._last = 1.0
        self._drag_off = None           # 拖动时鼠标在滑块内的偏移
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_off", None))
        self.bind("<MouseWheel>", self._on_wheel)

    # ---- 与 tk.Scrollbar 兼容的接口 ----
    def set(self, first, last):
        try:
            self._first = min(1.0, max(0.0, float(first)))
            self._last = min(1.0, max(self._first, float(last)))
        except Exception:
            return
        self._redraw()

    def _cmd(self, *args):
        if self.command:
            try:
                self.command(*args)
            except Exception:
                pass

    # ---- 绘制 ----
    def _redraw(self):
        try:
            self.delete("all")
            w, h = self.winfo_width(), self.winfo_height()
            if w < 4 or h < 4:
                return
            # 轨道：白色斜切四边形（整体不规则，与滑块同风格）
            if self._orient == "vertical":
                pts = [(6, 0), (w, 0), (w - 6, h), (0, h)]
            else:
                pts = [(0, 6), (0, h), (w, h - 6), (w, 0)]
            self.create_polygon(pts, fill="#FFFFFF", outline="#000000", width=1)
            first, last = self._first, self._last
            if last <= first:
                return
            margin = 2
            if self._orient == "vertical":
                y1 = int(first * h)
                y2 = max(y1 + 24, int(last * h))
                if y2 > h:
                    y2 = h
                if y1 < 0:
                    y1 = 0
                # P5 不规则滑块：纯黑细斜切四边形（居中 8px，与轨道斜向一致）
                pts = [(8, y1 + 9), (16, y1), (16, y2 - 8), (8, y2)]
                self.create_polygon(pts, fill="#000000", outline="#000000", width=1)
            else:
                x1 = int(first * w)
                x2 = max(x1 + 24, int(last * w))
                if x2 > w:
                    x2 = w
                if x1 < 0:
                    x1 = 0
                # 水平：纯黑细斜切四边形（居中 8px，与轨道斜向一致）
                pts = [(x1, 8), (x1 + 9, 16), (x2 - 8, 16), (x2, 8)]
                self.create_polygon(pts, fill="#000000", outline="#000000", width=1)
        except Exception:
            pass

    # ---- 交互 ----
    def _on_press(self, e):
        w, h = self.winfo_width(), self.winfo_height()
        if self._orient == "vertical":
            y1, y2 = self._first * h, self._last * h
            if y1 <= e.y <= y2:
                self._drag_off = e.y - y1
            else:
                self._drag_off = None
                self._cmd("scroll", -1 if e.y < y1 else 1, "pages")
        else:
            x1, x2 = self._first * w, self._last * w
            if x1 <= e.x <= x2:
                self._drag_off = e.x - x1
            else:
                self._drag_off = None
                self._cmd("scroll", -1 if e.x < x1 else 1, "pages")

    def _on_drag(self, e):
        if self._drag_off is None:
            return
        w, h = self.winfo_width(), self.winfo_height()
        span = self._last - self._first
        if span <= 0:
            return
        if self._orient == "vertical":
            thumb_h = span * h
            track = max(1, h - thumb_h)
            frac = (e.y - self._drag_off) / track
        else:
            thumb_w = span * w
            track = max(1, w - thumb_w)
            frac = (e.x - self._drag_off) / track
        frac = max(0.0, min(1.0, frac))
        self._cmd("moveto", frac)

    def _on_wheel(self, e):
        step = -1 if e.delta > 0 else 1
        self._cmd("scroll", step, "units")


class P5ListCanvas(tk.Canvas):
    """P5 风格自绘列表：倾斜平行四边形行条 + 红色选中高亮，兼容 Listbox 常用接口。

    Windows 上 tk.Listbox 只能渲染矩形行，无法表达 P5 的不规则倾斜排版；
    这里用 Canvas 自绘：红色斜切表头、每行左上切角的斜条、选中行红色高亮。
    """

    ROW_H = 40
    GAP = 10
    SLANT = 20

    def __init__(self, master, font=None, **kw):
        kw.setdefault("bg", "#0D0D0D")
        kw.setdefault("highlightthickness", 0)
        kw.setdefault("bd", 0)
        super().__init__(master, **kw)
        self._font = font or F_LIST
        self._rows = []        # [(text, fg)]
        self._sel = -1
        self._top = 0.0        # 滚动偏移（0-1）
        self._yscroll = None   # P5Scrollbar.set
        self._xscroll = None
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Double-Button-1>", self._on_double)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self._wheel_delta(120))
        self.bind("<Button-5>", lambda e: self._wheel_delta(-120))

    # ---- 滚动条关联 ----
    def set_yscroll(self, cb):
        self._yscroll = cb

    def set_xscroll(self, cb):
        self._xscroll = cb

    # ---- Listbox 兼容接口 ----
    def insert(self, idx, text):
        self._rows.append((str(text), "#FFFFFF"))

    def itemconfig(self, idx, fg=None, **kw):
        if fg:
            if isinstance(idx, str) and idx.lower() == "end":
                idx = len(self._rows) - 1
            if 0 <= idx < len(self._rows):
                t, _ = self._rows[idx]
                self._rows[idx] = (t, fg)
        self._redraw()

    def delete(self, first, last=None):
        # 兼容 tk.Canvas 语义：delete("all") 清空画布
        if isinstance(first, str) and first == "all":
            super().delete("all")
            return
        def _norm(i):
            # tk.END 是字符串 "end"，需归一为列表长度
            if isinstance(i, str) and i.lower() == "end":
                return len(self._rows)
            return int(i)
        if last is None:
            f = _norm(first)
            if 0 <= f < len(self._rows):
                del self._rows[f]
        else:
            del self._rows[_norm(first):_norm(last)]
        if self._sel >= len(self._rows):
            self._sel = len(self._rows) - 1
        self._redraw()

    def curselection(self):
        return (self._sel,) if 0 <= self._sel < len(self._rows) else ()

    def selection_set(self, idx):
        self._sel = int(idx)
        self._redraw()

    def selection_clear(self, first=0, last=None):
        self._sel = -1
        self._redraw()

    def see(self, idx):
        total = len(self._rows)
        if total == 0:
            return
        vis = self._visible_count()
        if vis >= total:
            self._top = 0.0
        else:
            max_top = total - vis
            first_row = int(self._top * max_top)
            if idx < first_row:
                self._top = idx / max_top
            elif idx >= first_row + vis:
                self._top = (idx - vis + 1) / max_top
        self._top = max(0.0, min(1.0, self._top))
        self._redraw()

    # ---- 滚动 ----
    def _visible_count(self):
        h = self.winfo_height()
        if h < 20:
            return 10
        return max(1, (h - 8) // (self.ROW_H + self.GAP))

    def _visible_frac(self):
        total = len(self._rows)
        if total == 0:
            return (0.0, 1.0)
        vis = self._visible_count()
        max_top = max(0.0, 1.0 - vis / total)
        top = min(self._top, max_top)
        return (top, min(1.0, top + vis / total))

    def yview(self, *args):
        total = len(self._rows)
        vis = self._visible_count()
        if args:
            if args[0] == "moveto" and len(args) > 1:
                self._top = max(0.0, min(1.0, float(args[1])))
                self._redraw()
            elif args[0] == "scroll" and len(args) > 2:
                n = int(args[1])
                unit = args[2]
                step = 3 if unit == "units" else vis
                # _top 是 0..1 比例：0=顶，1=底（_start_row 内部自动 clamp）
                self._top = max(0.0, min(1.0, self._top + n * step / max(1, total)))
                self._redraw()
        first, last = self._visible_frac()
        if self._yscroll:
            try:
                self._yscroll(first, last)
            except Exception:
                pass
        return (first, last)

    def xview(self, *args):
        if self._xscroll:
            try:
                self._xscroll(0.0, 1.0)
            except Exception:
                pass
        return (0.0, 1.0)

    # ---- 绘制 ----
    def _start_row(self):
        total = len(self._rows)
        vis = self._visible_count()
        if total == 0:
            return 0
        max_top = max(0, total - vis)
        return int(self._top * max_top)

    def _redraw(self):
        try:
            self.delete("all")
            w, h = self.winfo_width(), self.winfo_height()
            if w < 10 or h < 10:
                return
            start = self._start_row()
            y = 6
            for i in range(start, len(self._rows)):
                if y + self.ROW_H > h - 4:
                    break
                self._draw_row(w, y, self._rows[i][0], self._rows[i][1], i == self._sel)
                y += self.ROW_H + self.GAP
        except Exception:
            pass

    def _draw_row(self, w, y, text, fg, sel):
        rh = self.ROW_H
        s = self.SLANT
        fill = C_MAIN if sel else "#1E1E1E"
        # 倾斜平行四边形：左上大切角、右缘微斜（与 P5 菜单条一致）
        pts = [(s, y), (w - 6, y), (w - 14, y + rh), (4, y + rh)]
        self.create_polygon(pts, fill=fill, outline="#000000", width=1)
        if sel:
            # 选中行左侧亮红强调
            self.create_polygon([(s, y), (s + 9, y), (s + 9, y + rh), (s - 4, y + rh)],
                                fill="#FF3B3B", outline="")
        fcol = "#FFFFFF" if sel else fg
        tx = s + 12
        max_w = w - tx - 18
        shown = self._fit_text(text, max_w)
        self.create_text(tx, y + rh // 2, text=shown, anchor="w", fill=fcol,
                         font=self._font)
        if sel:
            # 选中行右侧白色三角指示
            self.create_polygon([(w - 24, y + rh // 2 - 5), (w - 14, y + rh // 2),
                                 (w - 24, y + rh // 2 + 5)],
                                fill="#FFFFFF", outline="")

    def _fit_text(self, text, max_w):
        try:
            from tkinter import font as tkfont
            f = tkfont.Font(font=self._font)
            if f.measure(text) <= max_w:
                return text
            s = text
            while len(s) > 4 and f.measure(s + "…") > max_w:
                s = s[:-1]
            return s + "…"
        except Exception:
            return text[:60]

    # ---- 交互 ----
    def _row_at(self, y):
        start = self._start_row()
        yy = 6
        for i in range(start, len(self._rows)):
            if yy + self.ROW_H > self.winfo_height() - 4:
                break
            if yy <= y <= yy + self.ROW_H + self.GAP:
                return i
            yy += self.ROW_H + self.GAP
        return None

    def _on_click(self, e):
        row = self._row_at(e.y)
        self._sel = row if row is not None else -1
        self._redraw()
        if row is not None:
            self.event_generate("<<ListboxSelect>>")

    def _on_double(self, e):
        row = self._row_at(e.y)
        if row is None:
            return
        self._sel = row
        self._redraw()
        self.event_generate("<<ListboxSelect>>")
        cb = getattr(self, "double_cb", None)
        if cb:
            cb()

    def _on_wheel(self, e):
        step = -1 if e.delta > 0 else 1
        self.yview("scroll", step, "units")

    def _wheel_delta(self, delta):
        self.yview("scroll", -1 if delta > 0 else 1, "units")


class FrameReader(threading.Thread):
    """后台线程：从 ffmpeg 的 rawvideo 管道按固定帧大小读取完整帧，放入队列。"""

    def __init__(self, proc, q, frame_size: int):
        super().__init__(daemon=True)
        self._proc = proc
        self._q = q
        self._frame_size = frame_size

    def run(self):
        try:
            while True:
                # 精确读取一帧 raw RGB 数据
                data = b""
                while len(data) < self._frame_size:
                    chunk = self._proc.stdout.read(self._frame_size - len(data))
                    if not chunk:
                        break
                    data += chunk
                if len(data) < self._frame_size:
                    break
                # 有界队列满时阻塞 → 对 ffmpeg 产生背压，防止全速解码丢帧
                self._q.put(data)
        except Exception:
            pass
        finally:
            try:
                self._q.put(None)
            except Exception:
                pass


class App(tk.Tk):
    """主应用（布局参考 P5R BGM Editor：菜单 + 工具栏 + 左列表 / 右操作面板 + 状态栏）。"""

    def __init__(self):
        super().__init__()
        self.title("%s v%s" % (APP_TITLE, APP_VERSION))
        self.geometry("1200x760")
        self.minsize(980, 660)

        # 深色窗口标题栏（Windows 10 1809+ 沉浸式深色标题栏）
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            _dark = ctypes.c_int(1)
            for _attr in (20, 19):
                try:
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, _attr, ctypes.byref(_dark), ctypes.sizeof(_dark))
                    break
                except Exception:
                    continue
        except Exception:
            pass

        # P5 深色菜单
        self.option_add("*Menu.background", "#1A1A1A")
        self.option_add("*Menu.foreground", "#FFFFFF")
        self.option_add("*Menu.activeBackground", C_MAIN)
        self.option_add("*Menu.activeForeground", "#FFFFFF")
        self.option_add("*Menu.borderWidth", 1)

        # 全局异常兜底：任何未捕获异常弹窗提示，避免窗口静默消失
        def _hook(exc_type, exc_value, exc_tb):
            detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            try:
                messagebox.showerror(APP_TITLE, "程序遇到未处理的错误：\n\n%s" % detail)
            except Exception:
                pass
            sys.exit(1)
        sys.excepthook = _hook
        self.report_callback_exception = lambda exc, val, tb: _hook(exc, val, tb)

        # 状态
        self.movies: list[MovieItem] = []
        self._runner: TaskRunner | None = None
        self._entries: list[engine.CpkEntryInfo] = []
        self._cpk_path: Path | None = None
        self._progress: ProgressDialog | None = None

        # 变量
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪。请通过 文件菜单 导入 MOVIE_JE.CPK，或点击「导入 CPK」。")
        self.count_var = tk.StringVar(value="过场: 0")

        self._build_menu()
        self._build_ui()
        # 延迟 400ms 自动恢复上次会话（不阻塞首屏渲染）
        self.after(400, self._restore_session)

    # ==================== 菜单（自绘，P5 黑红配色） ====================
    def _build_menu(self):
        _menu_opt = dict(
            font=F_MENU, bg="#1A1A1A", fg="#FFFFFF",
            activebackground="#E60012", activeforeground="#FFFFFF",
            bd=1, relief="flat", tearoff=0,
        )
        file_menu = tk.Menu(self, **_menu_opt)
        file_menu.add_command(label="导入 MOVIE_JE.CPK...", command=self.import_cpk, accelerator="Ctrl+I")
        file_menu.add_command(label="自动定位游戏目录...", command=self.locate_game_dir)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.quit, accelerator="Ctrl+Q")

        tools_menu = tk.Menu(self, **_menu_opt)
        tools_menu.add_command(label="检查 FFmpeg", command=self.check_ffmpeg)
        tools_menu.add_command(label="打开说明文档", command=self.open_help)


        help_menu = tk.Menu(self, **_menu_opt)
        help_menu.add_command(label="使用说明", command=self.open_help)
        help_menu.add_command(label="技术调查报告", command=self._show_tech_report)
        help_menu.add_command(label="关于", command=self.show_about)

        # 自绘菜单栏：黑底 Frame + 菜单按钮（Windows 原生 menubar 无法改背景色）
        bar = tk.Frame(self, bg=C_BG, height=30)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        def _mk_item(label, menu=None, cmd=None):
            lb = tk.Label(bar, text=label, font=F_MENU,
                          bg=C_BG, fg="#FFFFFF", padx=14, cursor="hand2")
            lb.pack(side="left", fill="y")

            def _enter(_e):
                lb.config(bg=C_MAIN, fg="#FFFFFF")

            def _leave(_e):
                lb.config(bg=C_BG, fg="#FFFFFF")

            def _click(_e):
                if menu is not None:
                    try:
                        menu.post(lb.winfo_rootx(),
                                  lb.winfo_rooty() + lb.winfo_height())
                    except tk.TclError:
                        pass
                elif cmd is not None:
                    cmd()

            lb.bind("<Enter>", _enter)
            lb.bind("<Leave>", _leave)
            lb.bind("<Button-1>", _click)
            return lb

        _mk_item("文件", menu=file_menu)
        _mk_item("工具", menu=tools_menu)
        _mk_item("帮助", menu=help_menu)
        _mk_item("作者", cmd=self._show_author)
        _mk_item("魔罗", cmd=self._show_mara)

        self._menu_bar = bar
        self.bind("<Control-i>", lambda e: self.import_cpk())
        self.bind("<Control-q>", lambda e: self.quit())

    # ==================== 主界面 ====================
    @staticmethod
    def _configure_p5_scrollbar_style():
        """ttk clam 主题 P5 滚动条：无箭头细条，红滑块 + 深灰轨道。

        Windows 上经典 tk.Scrollbar 忽略 bg/troughcolor 强制渲染系统亮白，
        必须用 ttk Scrollbar（clam 主题支持自定义颜色）。
        """
        try:
            root = tk._default_root
            if root is None:
                return
            style = ttk.Style(root)
            # 垂直：无箭头，纯滑块
            style.layout("P5.Vertical.TScrollbar", [
                ("Vertical.Scrollbar.trough", {
                    "children": [("Vertical.Scrollbar.thumb",
                                  {"expand": "1", "sticky": "nswe"})],
                    "sticky": "ns"})])
            style.configure("P5.Vertical.TScrollbar",
                            background=C_MAIN, troughcolor="#1A1A1A",
                            bordercolor="#1A1A1A", lightcolor=C_MAIN,
                            darkcolor=C_MAIN, arrowcolor="#FFFFFF",
                            relief="flat", borderwidth=0)
            # 水平：无箭头，纯滑块
            style.layout("P5.Horizontal.TScrollbar", [
                ("Horizontal.Scrollbar.trough", {
                    "children": [("Horizontal.Scrollbar.thumb",
                                  {"expand": "1", "sticky": "nswe"})],
                    "sticky": "we"})])
            style.configure("P5.Horizontal.TScrollbar",
                            background=C_MAIN, troughcolor="#1A1A1A",
                            bordercolor="#1A1A1A", lightcolor=C_MAIN,
                            darkcolor=C_MAIN, arrowcolor="#FFFFFF",
                            relief="flat", borderwidth=0)
        except Exception:
            pass

    def _build_ui(self):
        self._configure_p5_scrollbar_style()
        # ---- P5 顶部横幅 ----
        self.config(bg=C_BG)
        banner = tk.Frame(self, bg="#000000")
        banner.pack(side=tk.TOP, fill=tk.X)
        try:
            from PIL import Image, ImageTk
            logo_p = Path(__file__).parent / "assets" / "h_logo.png"
            if logo_p.exists():
                img = Image.open(str(logo_p)).convert("RGBA")
                img.thumbnail((60, 60), Image.LANCZOS)
                self._banner_logo = ImageTk.PhotoImage(img)
                tk.Label(banner, image=self._banner_logo, bg="#000000").pack(
                    side=tk.LEFT, padx=(10, 8), pady=2)
        except Exception:
            pass
        tk.Label(banner, text="P5R过场动画替换工具", font=(F_TITLE_FONT, 15),
                 fg="#FFFFFF", bg="#000000").pack(side=tk.LEFT, pady=4)
        tk.Label(banner, text="PERSONA 5 ROYAL · CUTSCENE REPLACEMENT TOOL",
                 font=(F_BODY_FONT, 9, "bold"), fg=C_MAIN, bg="#000000").pack(side=tk.LEFT, padx=(10, 0), pady=6)
        tk.Label(banner, text="v%s  作者:凯梦" % APP_VERSION, font=F_SMALL, fg="#8C8C8C",
                 bg="#000000").pack(side=tk.LEFT, padx=8)
        tk.Frame(self, bg=C_MAIN, height=3, relief=tk.FLAT, bd=0,
                highlightthickness=0).pack(side=tk.TOP, fill=tk.X)

        # ---- 顶部工具栏 ----
        toolbar = tk.Frame(self, height=40, relief=tk.FLAT, bd=0, bg="#000000")
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=2, pady=2)

        # P5 风格搜索：放大镜图标 + RGB 色差重影文字 + 白色斜切四边形输入框
        try:
            from PIL import Image, ImageTk
            _icon = Image.open(str(Path(__file__).resolve().parent / "assets" / "search_icon.png"))
            _icon = _icon.convert("RGBA").resize((20, 20), Image.LANCZOS)
            self._search_icon_img = ImageTk.PhotoImage(_icon)
            tk.Label(toolbar, image=self._search_icon_img,
                     bg="#000000").pack(side=tk.LEFT, padx=(10, 0))
        except Exception:
            pass
        self._search_label_img = self._make_glitch_label("搜索：")
        tk.Label(toolbar, image=self._search_label_img,
                 bg="#000000").pack(side=tk.LEFT, padx=(10, 4))
        search_canvas = tk.Canvas(toolbar, width=216, height=30,
                                  bg="#000000", highlightthickness=0, bd=0)
        search_canvas.pack(side=tk.LEFT, padx=2)
        # 白色斜切四边形（tk 原生矢量绘制：顶/底水平边保证渲染完整，左右陡斜切）
        search_canvas.create_polygon(18, 2, 212, 2, 202, 29, 6, 29,
                                     outline="#FFFFFF", width=2,
                                     fill="#0D0D0D", joinstyle="miter")
        search_entry = tk.Entry(search_canvas, textvariable=self.search_var,
                                bg="#0D0D0D", fg="#FFFFFF", insertbackground="#FFFFFF",
                                bd=0, relief=tk.FLAT, highlightthickness=0)
        search_canvas.create_window(20, 5, anchor="nw", window=search_entry,
                                    width=174, height=20)
        search_entry.bind("<KeyRelease>", lambda e: self.refresh_list())
        search_canvas.bind("<Button-1>", lambda e: search_entry.focus_set())

        tk.Button(toolbar, text="清除记录", command=self._clear_all_records, width=10,
                  bg=C_TOOLBTN, fg="#FFFFFF", activebackground=C_MAIN,
                  activeforeground="#FFFFFF", bd=0, relief=tk.FLAT,
                  cursor="hand2").pack(side=tk.RIGHT, padx=5)
        tk.Button(toolbar, text="启动 Reloaded-II", command=self.launch_reloaded, width=15,
                  bg=C_TOOLBTN, fg="#FFFFFF", activebackground=C_MAIN,
                  activeforeground="#FFFFFF", bd=0, relief=tk.FLAT,
                  cursor="hand2").pack(side=tk.RIGHT, padx=5)
        tk.Button(toolbar, text="打开 Mod 文件夹", command=self.open_reloaded_mods, width=14,
                  bg=C_TOOLBTN, fg="#FFFFFF", activebackground=C_MAIN,
                  activeforeground="#FFFFFF", bd=0, relief=tk.FLAT,
                  cursor="hand2").pack(side=tk.RIGHT, padx=5)
        tk.Button(toolbar, text="一键生成 Mod", command=self.do_build_mod, width=12,
                  bg=C_MAIN, fg="#FFFFFF", activebackground="#B0000E",
                  activeforeground="#FFFFFF", bd=0, relief=tk.FLAT,
                  cursor="hand2").pack(side=tk.RIGHT, padx=5)

        # ---- 主内容区：左列表 / 右操作面板 ----
        main_frame = tk.Frame(self, bg=C_BG)
        main_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=2, pady=2)

        left_frame = tk.Frame(main_frame, width=500, bg=C_BG)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2))

        # 颜色图例
        legend = tk.Frame(left_frame, relief=tk.FLAT, bd=0, bg=C_LEGEND_BG)
        legend.pack(side=tk.TOP, fill=tk.X)
        tk.Label(legend, text="■", fg="black", bg=C_LEGEND_BG, font=F_LABEL_BOLD).pack(side=tk.LEFT, padx=(8, 1))
        tk.Label(legend, text="原始", bg=C_LEGEND_BG, font=F_SMALL).pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(legend, text="■", fg=C_EXTRACTED, bg=C_LEGEND_BG, font=F_LABEL_BOLD).pack(side=tk.LEFT, padx=(2, 1))
        tk.Label(legend, text="已解包", bg=C_LEGEND_BG, font=F_SMALL).pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(legend, text="■", fg=C_MOD_MADE, bg=C_LEGEND_BG, font=F_LABEL_BOLD).pack(side=tk.LEFT, padx=(2, 1))
        tk.Label(legend, text="已替换", bg=C_LEGEND_BG, font=F_SMALL).pack(side=tk.LEFT, padx=(0, 8))

        # 列表
        list_frame = tk.Frame(left_frame)
        list_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        v_scroll = P5Scrollbar(list_frame)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        h_scroll = P5Scrollbar(list_frame, orient="horizontal")
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        # P5 红色斜切表头（名称 / 大小 / 状态）
        list_header = tk.Canvas(list_frame, height=36, bg="#0D0D0D",
                                highlightthickness=0, bd=0)
        list_header.pack(side=tk.TOP, fill=tk.X)
        list_header.bind("<Configure>", lambda e: self._draw_list_header(list_header))

        self.movie_listbox = P5ListCanvas(list_frame, font=F_LIST)
        self.movie_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.movie_listbox.set_yscroll(v_scroll.set)
        self.movie_listbox.set_xscroll(h_scroll.set)
        v_scroll.command = self.movie_listbox.yview
        h_scroll.command = self.movie_listbox.xview
        self.movie_listbox.bind("<<ListboxSelect>>", self.on_list_select)
        self.movie_listbox.double_cb = self.on_list_double

        # 列表底部提示
        hint = tk.Label(left_frame, text="提示：单击选中过场 → 右侧预览/替换；双击 → 直接播放",
                        fg=C_GRAY, font=F_SMALL)
        hint.pack(side=tk.TOP, fill=tk.X, pady=2)

        # ---- 右侧：可滚动操作面板（固定三区：解包 / 视频预览 / 替换） ----
        right_frame = tk.Frame(main_frame, width=680)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(2, 0))

        right_canvas = tk.Canvas(right_frame, highlightthickness=0, bd=0)
        right_scroll = P5Scrollbar(right_frame, orient="vertical",
                                   command=right_canvas.yview)
        right_inner = tk.Frame(right_canvas)
        right_inner.bind("<Configure>",
                         lambda e: right_canvas.configure(scrollregion=right_canvas.bbox("all")))
        _inner_win = right_canvas.create_window((0, 0), window=right_inner, anchor="nw")
        right_canvas.configure(yscrollcommand=right_scroll.set)
        right_canvas.bind("<Configure>",
                          lambda e: right_canvas.itemconfigure(_inner_win, width=e.width))
        right_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        right_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _right_mousewheel(event):
            right_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        right_canvas.bind("<MouseWheel>", _right_mousewheel)
        right_inner.bind("<MouseWheel>", _right_mousewheel)

        self.panel_preview = VideoPreviewPanel(right_inner, self, export_command=self._export_mp4)
        self.panel_replace_video = VideoPreviewPanel(right_inner, self, title="待替换视频预览",
                                                     import_command=self._import_replace_video,
                                                     apply_command=self._apply_replace,
                                                     undo_command=self._undo_replace,
                                                     undo_import_command=self._undo_import_replace_video)
        self.panel_replace = ReplacePanel(right_inner, self)
        self._cpk_var = tk.StringVar()
        # 定位 P5R_movies：优先 exe 所在目录，回退 cwd
        import sys as _sys
        if getattr(_sys, 'frozen', False):
            _base_dir = Path(_sys.executable).parent
        else:
            _base_dir = Path.cwd()
        self._out_dir = _base_dir / "P5R_movies"


        # ---- 底部状态栏 ----
        status_bar = tk.Frame(self, bg=C_BG, relief=tk.FLAT, bd=0)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Label(status_bar, textvariable=self.status_var, anchor=tk.W).pack(side=tk.LEFT, padx=10, pady=2)
        tk.Label(status_bar, textvariable=self.count_var, anchor=tk.E).pack(side=tk.RIGHT, padx=10, pady=2)

        # ---- P5 深色主题（统一未显式配色的控件为黑底白字） ----
        self._apply_dark_theme()

    _DEFAULT_BGS = {"#f0f0f0", "#d9d9d9", "#ececec", "#c8c8c8", "#e6e6e6",
                    "#f5f5f5", "#e4e4e4", "SystemButtonFace", "SystemWindow",
                    "SystemScrollbar", "SystemMenu"}

    def _make_glitch_label(self, text, size=15):
        """P5 风格 RGB 色差重影文字（红/青偏移叠影 + 白色主体）。"""
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            fp = Path(__file__).parent / "assets" / "fonts" / "SmileySans.ttf"
            font = ImageFont.truetype(str(fp), size)
            tmp = Image.new("RGBA", (10, 10))
            td = ImageDraw.Draw(tmp)
            tw = int(td.textlength(text, font=font)) + 8
            img = Image.new("RGBA", (tw, size + 12), (0, 0, 0, 255))
            d = ImageDraw.Draw(img)
            y = (img.height - size) // 2 - 2
            d.text((3, y), text, font=font, fill=(255, 0, 0, 220))      # 红影（左）
            d.text((7, y), text, font=font, fill=(0, 255, 255, 220))    # 青影（右）
            d.text((5, y), text, font=font, fill=(255, 255, 255, 255))  # 白色主体
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _make_search_frame(self, w=216, h=30):
        """P5 白色斜切四边形搜索框轮廓（内部深色，供输入框叠加）。"""
        try:
            from PIL import Image, ImageDraw, ImageTk
            img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
            d = ImageDraw.Draw(img)
            pts = [(16, 2), (w - 4, 0), (w - 14, h - 2), (4, h - 4)]
            d.polygon(pts, fill=(13, 13, 13, 255))
            d.line(pts + [pts[0]], fill="#FFFFFF", width=3, joint="curve")
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _apply_dark_theme(self, parent=None):
        """把未显式配色的控件统一为 P5 深色（黑底白字），已配色的（彩色按钮等）保持不动。"""
        parent = parent if parent is not None else self
        for c in parent.winfo_children():
            try:
                cls = c.winfo_class()
                # 统一去白边：所有控件高亮/边框置黑
                try:
                    c.config(highlightbackground="#000000", highlightcolor="#000000",
                             highlightthickness=0)
                except Exception:
                    pass
                if cls in ("Frame", "Canvas", "TFrame"):
                    if str(c.cget("bg")) in self._DEFAULT_BGS:
                        c.config(bg=C_BG)
                    try:
                        c.config(relief=tk.FLAT, bd=0)
                    except Exception:
                        pass
                elif cls in ("LabelFrame", "Labelframe"):
                    if str(c.cget("bg")) in self._DEFAULT_BGS:
                        c.config(bg=C_PANEL)
                    try:
                        c.config(fg="#FFFFFF", bd=0)
                    except Exception:
                        pass
                elif cls == "Label":
                    if str(c.cget("bg")) in self._DEFAULT_BGS:
                        c.config(bg=C_BG)
                    if str(c.cget("fg")) in ("black", "#000000", "SystemWindowText",
                                             "SystemButtonText", "SystemMenuText"):
                        c.config(fg="#FFFFFF")
                    try:
                        c.config(bd=0, relief=tk.FLAT)
                    except Exception:
                        pass
                elif cls == "Entry":
                    c.config(bg="#141414", fg="#FFFFFF", insertbackground="#FFFFFF")
                elif cls == "Listbox":
                    c.config(bg="#0D0D0D", fg="#FFFFFF", selectbackground=C_MAIN,
                             selectforeground="#FFFFFF", highlightbackground=C_BG,
                             bd=0, relief=tk.FLAT, highlightthickness=1)
                elif cls == "Button":
                    if str(c.cget("bg")) in self._DEFAULT_BGS:
                        c.config(bg=C_TOOLBTN, fg="#FFFFFF", activebackground=C_MAIN,
                                 activeforeground="#FFFFFF", bd=0, relief=tk.FLAT)
                elif cls == "Scrollbar":
                    # P5 黑红细条风格（经典 tk 兜底；Windows 主渲染走 ttk）
                    try:
                        c.config(bg=C_MAIN, activebackground="#FF3B3B",
                                 troughcolor="#1A1A1A", relief=tk.FLAT, bd=0,
                                 highlightthickness=0, elementborderwidth=0,
                                 borderwidth=0, width=9)
                    except Exception:
                        pass
                elif cls in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
                    try:
                        c.configure(style=("P5.Horizontal.TScrollbar"
                                           if str(c.cget("orient")) == "horizontal"
                                           else "P5.Vertical.TScrollbar"))
                    except Exception:
                        pass
            except Exception:
                pass
            self._apply_dark_theme(c)

    # ==================== 列表 ====================
    def _draw_list_header(self, cv):
        """P5 红色斜切表头条：名称 / 大小 / 状态。"""
        try:
            cv.delete("all")
            w = cv.winfo_width()
            h = cv.winfo_height()
            if w < 20:
                return
            pts = [(18, 5), (w - 6, 3), (w - 16, h - 4), (5, h - 6)]
            cv.create_polygon(pts, fill=C_MAIN, outline="#000000", width=1)
            cv.create_text(20, h // 2, text="名称", anchor="w", fill="#FFFFFF",
                           font=F_LABEL_BOLD)
            cv.create_text(int(w * 0.60), h // 2, text="大小", anchor="w",
                           fill="#FFFFFF", font=F_LABEL_BOLD)
            cv.create_text(int(w * 0.76), h // 2, text="状态", anchor="w",
                           fill="#FFFFFF", font=F_LABEL_BOLD)
        except Exception:
            pass

    def refresh_list(self):
        kw = self.search_var.get().strip().lower()
        self.movie_listbox.delete(0, tk.END)
        for it in self.movies:
            if kw and kw not in it.name.lower():
                continue
            status = ""
            if it.replaced_with:
                status = "已替换: " + Path(it.replaced_with).name[:18]
            elif it.mod_made:
                status = "已替换"
            elif it.abs_path:
                status = "已解包"
            if not it.has_audio and it.abs_path:
                status = (status + " 无音轨").strip() if status else "无音轨"
            line = "%s  %12s  %s" % (it.name[:52], _human_size(it.size), status)
            self.movie_listbox.insert(tk.END, line)
            if it.replaced_with or it.mod_made:
                self.movie_listbox.itemconfig(tk.END, fg=C_MOD_MADE)
            elif it.abs_path:
                self.movie_listbox.itemconfig(tk.END, fg=C_EXTRACTED)
        self._update_count()

    def _update_count(self):
        n_all = len(self.movies)
        n_ext = sum(1 for m in self.movies if m.abs_path)
        n_mod = sum(1 for m in self.movies if m.replaced_with or m.mod_made)
        self.count_var.set("过场: %d ｜ 已解包: %d ｜ 已替换: %d" % (n_all, n_ext, n_mod))

    def _detect_audio_tracks(self):
        """检测所有已解包 USM 是否有音频流，设置 MovieItem.has_audio。

        优化：同 CPK 的所有 USM 共用同一密钥（P5R 全部同一 key），只探测一次；
        其余文件只做轻量 load+info（约 0.06s/个），8 线程并行，
        103 个文件从约 87 秒降到约 1.5 秒。
        """
        from cricodecs import usm as _usm
        from p5r_tool.keys import find_usm_key, resolve_usm_key
        items = [it for it in self.movies if it.abs_path]
        if not items:
            return
        shared = getattr(self, "_shared_usm_key", None)
        if shared is None:
            try:
                shared = resolve_usm_key(items[0].abs_path)
            except Exception:
                shared = None
            self._shared_usm_key = shared

        def _check(it):
            try:
                with open(it.abs_path, "rb") as fh:
                    m = _usm.load(fh, key=shared)
                return any(s.audio_codec is not None for s in m.info().streams)
            except Exception:
                return True  # 检测失败时默认有音频，避免误标

        from concurrent.futures import ThreadPoolExecutor
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                for it, has in zip(items, ex.map(_check, items)):
                    it.has_audio = has
        except Exception:
            for it in items:
                it.has_audio = True

    def selected_movie(self) -> MovieItem | None:
        sel = self.movie_listbox.curselection()
        if not sel:
            return None
        # Listbox 行与 self.movies 一一对应（refresh_list 顺序一致）
        return self.movies[sel[0]]

    def _on_toggle_replaced(self):
        """勾选切换时：重新加载当前选中项，立即切换预览源。"""
        it = getattr(self, '_current_item', None)
        if it is not None:
            self.set_usm(it)

    def on_list_select(self, _event=None):
        it = self.selected_movie()
        if not it:
            return
        self.panel_preview.set_usm(it)

    def on_list_double(self):
        it = self.selected_movie()
        if not it:
            return
        # 双击：自动提取（如未提取）→ 转换 → 播放
        self.panel_preview.load_and_play(it)

    # ==================== 状态/任务 ====================
    def set_status(self, text: str):
        self.status_var.set(text)
        self.update_idletasks()

    def run_task(self, fn, on_done=None, on_error=None):
        """启动后台任务；on_done(result) 在成功时于 UI 线程调用。"""
        if self._runner and self._runner.is_alive():
            messagebox.showwarning(APP_TITLE, "已有任务正在运行，请稍候。")
            return False

        def _on_message(kind, payload):
            if kind == "progress":
                cur, total, msg = payload
                self.set_status("[%d/%d] %s" % (cur, total, msg))
                self.update_progress(cur, total, msg)
            elif kind == "phase":
                self.update_progress_phase(payload)
            elif kind == "done":
                self.set_status("完成。")
                self.hide_progress()
                if on_done:
                    on_done(payload)
            elif kind == "error":
                self.set_status("出错。")
                self.hide_progress()
                if on_error:
                    on_error(str(payload))
                else:
                    messagebox.showerror(APP_TITLE, str(payload))

        self._runner = TaskRunner(fn, _on_message)
        self._runner.start()
        self._poll_runner()
        return True

    # ==================== 进度弹窗 ====================
    def show_progress(self, title: str, phase: str = "正在准备…"):
        """打开模态进度弹窗（任务开始前调用）。"""
        self.hide_progress()
        try:
            self._progress = ProgressDialog(self, title=title, phase=phase)
            self._progress.grab_set()
        except Exception:
            self._progress = None

    def update_progress(self, cur: int, total: int, detail: str = ""):
        if self._progress is not None:
            try:
                self._progress.update(cur, total, detail)
            except Exception:
                pass

    def update_progress_phase(self, text: str):
        if self._progress is not None:
            try:
                self._progress.set_phase(text)
            except Exception:
                pass

    def hide_progress(self):
        if self._progress is not None:
            try:
                self._progress.close()
            except Exception:
                pass
            self._progress = None

    def _poll_runner(self):
        """轮询后台任务消息。先无条件 poll 一次（任务可能已瞬时完成，
        消息仍留在队列中），线程存活才继续排下一次轮询。"""
        if self._runner is None:
            return
        self._runner.poll()
        if self._runner.is_alive():
            self.after(120, self._poll_runner)
        else:
            self._runner = None

    # ==================== 文件/工具菜单动作 ====================
    def load_list(self, restore_map: dict | None = None):
        """导入 CPK：加载列表 + 解包全部 USM + 全量生成 m2v 预览，同一个进度窗全程显示。
        完成时列表出现，全部过场已解包、预览已就绪，点击即可直接播放。"""
        # 手动导入成功后清除 _cleared 标记
        try:
            _c = _load_cfg()
            if _c.pop("_cleared", None) is not None:
                _save_cfg(_c)
        except Exception:
            pass
        p = self._cpk_var.get().strip()
        if not p or not Path(p).exists():
            messagebox.showwarning(APP_TITLE, "请先选择有效的 CPK 文件。")
            return
        outdir = self._out_dir
        outdir.mkdir(parents=True, exist_ok=True)

        # 快速路径：先显示加载窗口，后台检查缓存
        import threading as _th2
        self.show_progress("加载中", "正在读取过场动画缓存…")
        def _fast_check():
            try:
                # 快速预检：输出目录没有 .usm 文件就直接走正常流程，不加载大 JSON 缓存
                existing_usms = list(outdir.rglob("*.usm")) + list(outdir.rglob("*.USM"))
                if not existing_usms:
                    self.after(0, lambda: self._normal_load(p, outdir, restore_map))
                    return
                entries_quick = engine.list_cpk(p)
                usm_entries = [e for e in entries_quick if e.filename.lower().endswith(".usm")]
                all_done = True
                for e in usm_entries:
                    dst = outdir / e.full_path.replace("\\", "/")
                    if not dst.exists() or dst.stat().st_size != e.size:
                        all_done = False
                        break
                if all_done and usm_entries:
                    # 检查 m2v 预览是否全部存在
                    missing_m2v = [str(outdir / e.full_path.replace("\\", "/"))
                                   for e in usm_entries
                                   if not Path(str(outdir / e.full_path.replace("\\", "/"))).with_suffix(".m2v").exists()]
                    if missing_m2v:
                        # m2v 未全部生成，走完整流程带进度条
                        self.after(0, lambda: self._normal_load(p, outdir, restore_map))
                        return
                    def _load_fast():
                        self.hide_progress()
                        self._entries = entries_quick
                        self._cpk_path = Path(p)
                        self.movies = []
                        for e in usm_entries:
                            it = MovieItem(name=e.full_path, size=e.size)
                            it.abs_path = str(outdir / e.full_path.replace("\\", "/"))
                            self.movies.append(it)
                        if restore_map:
                            for it in self.movies:
                                v = (restore_map.get(it.name) or restore_map.get(it.name.replace("\\", "/")))
                                if v and Path(v).exists():
                                    it.replaced_with = str(v)
                        self.refresh_list()
                        def _async_detect():
                            try:
                                self._detect_audio_tracks()
                            except Exception:
                                pass
                            self.after(0, self.refresh_list)
                        _th2.Thread(target=_async_detect, daemon=True).start()
                        cfg = _load_cfg()
                        cfg["last_cpk"] = str(Path(p).resolve())
                        _save_cfg(cfg)
                        self.set_status("已加载 %d 个过场动画（预览已就绪，点击即播）" % len(usm_entries))
                    self.after(0, _load_fast)
                    return
                    return
            except Exception:
                pass
            # 快速路径失败：走正常流程（弹进度窗）
            self.after(0, lambda: self._normal_load(p, outdir, restore_map))
        _th2.Thread(target=_fast_check, daemon=True).start()
        return

    def _normal_load(self, p, outdir, restore_map=None):
        self.set_status("正在导入 CPK、解包过场动画并生成预览…")
        self.show_progress("导入 CPK", "正在解析 CPK 文件…")

        def fn(q):
            # 阶段 1：加载 CPK 列表
            def prog_load(n, total, msg):
                q.put(("progress", (n, total, msg)))
            entries = engine.list_cpk(p, on_progress=prog_load)
            # 检查是否已全部解包（输出目录已有全部 .usm 且大小一致）
            usm_entries = [e for e in entries if e.filename.lower().endswith(".usm")]
            already_extracted = True
            for e in usm_entries:
                dst = outdir / e.full_path.replace("\\", "/")
                try:
                    if not dst.exists() or dst.stat().st_size != e.size:
                        already_extracted = False
                        break
                except OSError:
                    already_extracted = False
                    break
            if already_extracted:
                # 已解包：跳过阶段2，直接收集路径
                q.put(("phase", "解包 USM"))
                q.put(("progress", (len(usm_entries), len(usm_entries), "已解包（跳过）")))
                extracted = [outdir / e.full_path.replace("\\", "/") for e in usm_entries]
            else:
                q.put(("phase", "解包 USM"))
                q.put(("progress", (0, 0, "正在准备解包…")))
                def prog_extract(n, total, msg):
                    q.put(("progress", (n, total, "解包中：%s" % msg)))
                extracted = engine.extract_cpk(p, outdir, suffix=".usm", on_progress=prog_extract)
            extracted_paths = [str(x) for x in extracted]
            # 阶段 3：全量生成 m2v 预览（已存在的自动跳过）
            q.put(("phase", "生成预览"))
            q.put(("progress", (0, 0, "正在准备预览缓存…")))
            import threading
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from p5r_tool import keys as _keys
            shared_key = None
            if extracted_paths:
                try:
                    shared_key = _keys.resolve_usm_key(extracted_paths[0])
                except Exception:
                    shared_key = None
            v_total = len(extracted_paths)
            v_done = 0
            v_ok = 0
            v_lock = threading.Lock()

            def _prep_one(fp: str):
                try:
                    ok = engine.prepare_m2v_preview(fp, key=shared_key)
                    return fp, ok
                except Exception:
                    return fp, False

            if extracted_paths:
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [executor.submit(_prep_one, fp) for fp in extracted_paths]
                    for future in as_completed(futures):
                        fp, ok = future.result()
                        with v_lock:
                            v_done += 1
                            if ok:
                                v_ok += 1
                            q.put(("progress", (v_done, v_total,
                                                 "生成预览：%s" % Path(fp).name)))
            return entries, extracted_paths, v_ok, 0

        def done(result):
            entries, extracted_paths, mp4_ok, mp4_fail = result
            self._entries = entries
            self._cpk_path = Path(p)
            self.movies = []
            # 路径映射：提取路径 = outdir / CPK 内路径（extract_cpk 保持内部目录结构）
            rel_to_fp = {Path(fp).resolve().as_posix().lower(): fp for fp in extracted_paths}
            base_to_fp = {}
            for fp in extracted_paths:
                base_to_fp.setdefault(Path(fp).name.lower(), fp)
            for e in entries:
                if e.filename.lower().endswith(".usm"):
                    it = MovieItem(name=e.full_path, size=e.size)
                    expect = (outdir / e.full_path.replace("\\", "/")).resolve().as_posix().lower()
                    fp = rel_to_fp.get(expect) or base_to_fp.get(e.filename.lower())
                    if fp:
                        it.abs_path = fp
                    self.movies.append(it)
            # 恢复上次会话的替换标记（视频文件仍在则恢复）
            if restore_map:
                for it in self.movies:
                    v = (restore_map.get(it.name) or restore_map.get(it.name.replace("\\", "/")))
                    if v and Path(v).exists():
                        it.replaced_with = str(v)
            # 先立即显示列表，音轨检测放后台线程完成后再刷新（避免 UI 卡死）
            self.refresh_list()
            def _async_detect_audio():
                try:
                    self._detect_audio_tracks()
                except Exception:
                    pass
                self.after(0, self.refresh_list)
            import threading as _th
            _th.Thread(target=_async_detect_audio, daemon=True).start()
            # 记住 CPK（下次启动自动恢复）
            cfg = _load_cfg()
            cfg["last_cpk"] = str(Path(p).resolve())
            _save_cfg(cfg)
            msg = ("导入完成：CPK 共 %d 个条目，已解包 %d 个过场，已生成 %d 个快速预览"
                   % (len(entries), len(extracted_paths), mp4_ok))
            if restore_map:
                n_restored = sum(1 for it in self.movies if it.replaced_with)
                if n_restored:
                    msg += "；已恢复上次 %d 个替换标记" % n_restored
            msg += " → %s（点击列表任意过场即可直接预览）" % outdir
            self.set_status(msg)

        self.run_task(fn, done)

    def import_cpk(self):
        p = filedialog.askopenfilename(
            title="选择过场动画 CPK（MOVIE_JE.CPK）",
            filetypes=[("CriWare CPK", "*.cpk"), ("所有文件", "*.*")])
        if not p:
            return
        if Path(p).name.lower() != "movie_je.cpk":
            messagebox.showwarning(
                APP_TITLE,
                "只支持导入 P5R 的过场动画包 MOVIE_JE.CPK。\n"
                "你选择的文件：%s\n"
                "（其他 CPK 可能不是 P5R 过场动画，导入会造成误解包）" % Path(p).name)
            return
        self._cpk_var.set(p)
        self.load_list()

    def _restore_session(self):
        """启动时恢复上次会话：替换视频、mod 输出目录、替换标记，并自动重新导入 CPK。"""
        cfg = _load_cfg()
        if cfg.get("_cleared"):
            # 用户主动清除了记录：不自动恢复，等待手动导入
            self.set_status("记录已清除。请通过 文件 → 导入 MOVIE_JE.CPK 开始。")
            return
        rv = cfg.get("last_replace_video")
        if rv and Path(rv).exists():
            try:
                self.panel_replace_video.play_external_file(rv, autoplay=False)
                self.set_status("已恢复上次待替换视频：%s（未自动播放）" % Path(rv).name)
            except Exception:
                pass
        mo = cfg.get(_CFG_KEY_OUTDIR)
        if mo and Path(mo).is_dir():
            try:
                self.panel_replace.mod_root_var.set(mo)
            except Exception:
                pass
        mn = cfg.get("last_mod_name")
        if mn:
            try:
                self.panel_replace.mod_name_var.set(mn)
            except Exception:
                pass
        cpk = cfg.get("last_cpk")
        if not cpk or not Path(cpk).exists():
            # 回退：自动探测游戏目录的过场 CPK（P5R 为 MOVIE_JE.CPK）
            try:
                game = engine.find_p5r_game_dir()
                if game:
                    for name in ("MOVIE_JE.CPK", "data_movie.cpk", "MOVIE.CPK", "movie_je.cpk", "DATA_MOVIE.CPK"):
                        cand = Path(game) / "CPK" / name
                        if cand.is_file():
                            cpk = str(cand)
                            break
            except Exception:
                pass
        rmap = cfg.get("replaced_map") or {}
        if cpk and Path(cpk).exists():
            self._cpk_var.set(cpk)
            self.set_status("检测到上次会话，正在自动导入 %s …" % Path(cpk).name)
            self.load_list(restore_map=rmap)
        elif rmap:
            self.set_status("上次的 CPK 不存在，跳过自动导入（替换标记已保留在配置中）。")
    def locate_game_dir(self):
        d = filedialog.askdirectory(title="选择 P5R 游戏目录（含 CPK 子目录）")
        if not d:
            return
        root = Path(d)
        cands: list[Path] = []
        for sub in list(root.rglob("*.cpk"))[:2000]:
            if sub.name.lower() in {n.lower() for n in MOVIE_CPK_NAMES}:
                cands.append(sub)
        if cands:
            cands.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
            self._cpk_var.set(str(cands[0]))
            self.load_list()
            if len(cands) > 1:
                self.set_status("找到 %d 个可能的过场 CPK，已选用：%s" % (len(cands), cands[0].name))
        else:
            messagebox.showinfo(APP_TITLE, "在所选目录下没有找到 MOVIE_JE.CPK。\n请手动浏览选择 CPK 文件。")

    def _show_author(self):
        """作者信息弹窗（参考 P5R BGM Editor 样式）：作者、主页链接、工具简介、参考开源工具。"""
        win = tk.Toplevel(self)
        win.title("作者信息")
        win.geometry("760x660")
        win.minsize(640, 480)
        win.transient(self)
        win.grab_set()
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass

        canvas = tk.Canvas(win, highlightthickness=0)
        sb = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = tk.Frame(canvas)
        _w = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        body.bind("<MouseWheel>",
                  lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        PAD = 30
        BLUE = "#1a73e8"
        GRAY = "#555"

        def _link(parent, text, url, font=F_LABEL, padx=PAD, anchor="w"):
            lnk = tk.Label(parent, text=text, fg=BLUE, cursor="hand2", font=font,
                           justify="left", anchor="w")
            lnk.pack(padx=padx, anchor=anchor, pady=1)
            lnk.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))
            return lnk

        # 标题区
        tk.Label(body, text="作者信息", font=(F_TITLE_FONT, 12, "bold"),
                 fg="#c00000").pack(pady=(20, 4))
        tk.Label(body, text="%s v%s" % (APP_TITLE, APP_VERSION),
                 font=F_ABOUT_TITLE).pack(pady=(2, 2))
        tk.Label(body, text="女神异闻录5皇家版 · 过场动画替换工具 v%s" % APP_VERSION,
                 font=F_CN_BOLD, fg=GRAY).pack(pady=(0, 8))

        # 信息行（表格）
        info = tk.Frame(body)
        info.pack(fill=tk.X, padx=PAD, pady=(4, 2))
        info_rows = [
            ("工具作者", "凯梦", None),
            ("B站主页", "b23.tv/tIBHJEj（点击打开）", "https://b23.tv/tIBHJEj"),
            ("抖音主页", "抖音（点击打开）",
             "https://www.douyin.com/user/MS4wLjABAAAANIhiC7YSQ6uOCJyK-RUly7hNlrV98mYxxOxHbnCCmKYAGK24aAHNe02L9O1XlchI?from_tab_name=main"),
        ]
        for r, (k, v, url) in enumerate(info_rows):
            tk.Label(info, text=k, font=F_LABEL_BOLD, width=9, anchor="w").grid(
                row=r, column=0, sticky="w", pady=3)
            if url:
                lnk = tk.Label(info, text=v, fg=BLUE, cursor="hand2", font=F_LABEL)
                lnk.grid(row=r, column=1, sticky="w", pady=3)
                lnk.bind("<Button-1>", lambda _e, u=url: webbrowser.open(u))
            else:
                tk.Label(info, text=v, font=F_LABEL).grid(row=r, column=1, sticky="w", pady=3)

        # 工具简介
        tk.Label(body, text="工具简介", font=F_LABEL_BOLD).pack(padx=PAD, anchor="w", pady=(16, 4))
        intro = ("专为《女神异闻录5皇家版》(P5R) 打造的过场动画替换工具。\n"
                 "支持 CPK 一键解包、动画预览（含日英双音轨），\n"
                 "可一键将自己的视频替换进游戏，\n"
                 "并自动完成编码加密、生成 Reloaded-II Mod，\n"
                 "让游戏过场随心而变。")
        tk.Label(body, text=intro, font=F_CN, fg="#333", justify="left").pack(padx=PAD, anchor="w")

        # 参考工具
        tk.Label(body, text="参考工具 (向开源作者致谢)", font=F_LABEL_BOLD).pack(
            padx=PAD, anchor="w", pady=(18, 2))
        tk.Label(body, text="(点击蓝色项目名可打开对应开源项目主页)",
                 font=F_SMALL, fg=C_GRAY).pack(padx=PAD, anchor="w", pady=(0, 8))
        groups = [
            ("【CPK 解包】", [
                ("cricodecs / WannaCRI (USM 解包、解密) · donmai-me",
                 "https://github.com/donmai-me/WannaCRI"),
                ("CriPakTools (CPK 解包备选) · esperknight",
                 "https://github.com/esperknight/CriPakTools"),
            ]),
            ("【视频/音频编解码】", [
                ("FFmpeg (视频/音频格式转换) · FFmpeg 开源团队", "https://ffmpeg.org/"),
                ("VGAudio (ADX/HCA 编码、加密) · Thealexbarney (Alex Barney)",
                 "https://github.com/Thealexbarney/VGAudio"),
                ("vgmstream (游戏音频解码) · bnnm 等", "https://github.com/vgmstream/vgmstream"),
            ]),
            ("【Mod 加载与文件替换】", [
                ("Reloaded-II (Mod 加载器) · Sewer56",
                 "https://github.com/Reloaded-Project/Reloaded-II"),
                ("Persona Essentials (P5R 文件替换) · Sewer56 等",
                 "https://github.com/Sewer56/Persona-Essentials"),
                ("FileEmulationFramework (文件模拟框架) · Sewer56",
                 "https://github.com/Sewer56/FileEmulationFramework"),
            ]),
            ("【社区参考】", [
                ("Persona-Modding / unpackMovie.py · lraty-li",
                 "https://github.com/lraty-li/Persona-Modding"),
                ("P5R Modding Guide (Anime Cutscenes USM) · ShrineFox",
                 "https://shrinefox.com/blog/2025/10/16/p5r-modding-guide-2025-7-anime-cutscenes-usm/"),
            ]),
        ]
        for gtitle, items in groups:
            tk.Label(body, text=gtitle, font=F_LABEL_BOLD, fg="#333").pack(
                padx=PAD, anchor="w", pady=(8, 2))
            for name, url in items:
                _link(body, "· " + name, url, font=F_SMALL, padx=PAD + 12)

        tk.Button(body, text="关闭", command=win.destroy, width=10).pack(pady=(18, 20))

    def _show_mara(self):
        """魔罗展示弹窗：P5 人格面具图鉴风格（蓝底 + 倾斜黑框 + 魔罗立绘）。"""
        try:
            from PIL import Image, ImageTk
        except Exception:
            messagebox.showinfo(APP_TITLE, "缺少 Pillow 库，无法显示图片。\n请安装：pip install pillow")
            return
        img_path = Path(__file__).parent / "assets" / "mara.png"
        if not img_path.exists():
            messagebox.showwarning(APP_TITLE, "未找到图片文件：%s" % img_path)
            return

        P5_BLUE = "#1B3FBB"
        P5_RED = "#E60012"
        P5_WHITE = "#FFFFFF"

        win = tk.Toplevel(self)
        win.title("魔罗")
        try:
            _ico = Image.open(str(Path(__file__).parent / "assets" / "mask_icon.png")).convert("RGBA")
            self._mara_win_ico = ImageTk.PhotoImage(_ico.resize((64, 64), Image.LANCZOS))
            win.iconphoto(True, self._mara_win_ico)
        except Exception:
            pass
        win.geometry("1000x600")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass

        # 蓝色背景主画布
        canvas = tk.Canvas(win, bg=P5_BLUE, highlightthickness=0, bd=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        # 加载魔罗立绘（去背 PNG）
        try:
            img = Image.open(str(img_path)).convert("RGBA")
            img.thumbnail((480, 480), Image.LANCZOS)
            mara_photo = ImageTk.PhotoImage(img)
        except Exception as e:
            messagebox.showerror(APP_TITLE, "图片加载失败：%s" % e)
            win.destroy()
            return

        # 魔罗立绘放右侧
        mara_item = canvas.create_image(750, 300, image=mara_photo, anchor="center")

        # 顶部标题 "ARCANA  魔羅"
        canvas.create_text(80, 70, text="ARCANA",
                           font=(F_TITLE_FONT, 28, "bold"), fill=P5_WHITE, anchor="w")
        canvas.create_text(310, 70, text="魔羅",
                           font=(F_TITLE_FONT, 28, "bold"), fill=P5_WHITE, anchor="w")
        # 标题下方红色斜线装饰
        canvas.create_line(80, 88, 380, 88, fill=P5_RED, width=4)

        # 左侧倾斜文本框：红色外框(右下偏移) + 白色内框 + 黑色填充
        # 用多边形模拟 P5 的倾斜矩形（左边短、右边长）
        bx0, by0 = 40, 150
        bx1, by1 = 400, 490
        skew = 18  # 倾斜量
        # 红色底层（右下偏移）
        canvas.create_polygon(
            bx0 + skew + 8, by0 + 8,
            bx1 + 8, by0 + 8,
            bx1 + 8, by1 + 8,
            bx0 + 8, by1 + 8,
            fill=P5_RED, outline="")
        # 白色中层
        canvas.create_polygon(
            bx0 + skew, by0,
            bx1, by0,
            bx1, by1,
            bx0, by1,
            fill=P5_WHITE, outline="")
        # 黑色内层（内缩 6px）
        pad = 8
        canvas.create_polygon(
            bx0 + skew + pad, by0 + pad,
            bx1 - pad, by0 + pad,
            bx1 - pad, by1 - pad,
            bx0 + pad, by1 - pad,
            fill="#0A0A0A", outline="")

        # 框内文字
        desc = ("印度神话中统领恶灵的魔王，\n"
                "也是带来死亡的存在。\n"
                "擅长煽动恐惧的法术，\n"
                "也曾试图诱惑\n"
                "修行中的释迦摩尼。\n"                "此魔王的影响遍布全世界，\n"
                "并因此产生了梅尔与\n"
                "摩拉等黑暗的恶魔。")
        canvas.create_text(bx0 + skew + 25, by0 + 35,
                           text=desc, font=(F_BODY_FONT, 11),
                           fill=P5_WHITE, anchor="nw", justify="left")

        # 左下角小字
        canvas.create_text(50, 540,
                           text="这个选项并没有什么用，就是作者单纯想展示一下魔罗。",
                           font=(F_BODY_FONT, 9), fill="#AAAAAA", anchor="w")

        # 右下角关闭按钮（P5 风格）
        close_btn = tk.Button(win, text="✕ 关闭", command=win.destroy,
                              font=(F_TITLE_FONT, 10, "bold"),
                              bg=P5_RED, fg=P5_WHITE,
                              activebackground="#FF3B3B", activeforeground=P5_WHITE,
                              relief="flat", bd=0, padx=16, pady=6,
                              cursor="hand2")
        canvas.create_window(950, 560, window=close_btn, anchor="se")

        win._mara_photo = mara_photo  # 防 GC

    def _show_tech_report(self):
        """技术调查报告：P5R 过场动画替换的完整技术调研记录。"""
        win = tk.Toplevel(self)
        win.title("技术调查报告 — P5R 过场动画替换")
        win.geometry("860x720")
        win.minsize(680, 500)
        win.transient(self)
        win.grab_set()

        # 标题
        header = tk.Frame(win, bg=C_HEADER)
        header.pack(fill=tk.X)
        tk.Label(header, text="技术调查报告", font=F_ABOUT_TITLE,
                 fg="white", bg=C_HEADER).pack(pady=(16, 2))
        tk.Label(header, text="P5R 过场动画替换 · 逆向工程与 Mod 开发全过程",
                 font=F_CN, fg=C_HEADER_SUB, bg=C_HEADER).pack(pady=(0, 14))

        # 滚动文本区
        body = tk.Frame(win)
        body.pack(fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(body)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt = tk.Text(body, wrap="word", font=F_CN, padx=20, pady=16,
                      yscrollcommand=sb.set, bg="#1e1e1e", fg="#e0e0e0",
                      insertbackground="#e0e0e0", relief="flat")
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=txt.yview)

        report = r"""
P5R 过场动画替换工具 — 技术调查报告
======================================

一、项目概述
------------
本工具用于《女神异闻录5 皇家版》(Persona 5 Royal, P5R) PC 版的
过场动画 (Cutscene / USM) 解包、预览与视频替换。

游戏过场动画封装在 CPK 文件 (MOVIE_JE.CPK) 中，内部为 CRI USM
容器，视频编码 VP9，音频编码 HCA，整体使用 CRI 密钥加密。

工具流程：导入 CPK → 解包 USM → 预览 → 导入自定义视频 →
标记替换 → 生成 Reloaded-II Mod。


二、文件格式与加密
------------------
1. CPK (CRI PackFile)
   - 游戏把所有过场打包在 MOVIE_JE.CPK 中
   - 工具使用 cricodecs (WannaCRI) 解包

2. USM (CRI Movie 2)
   - 每个过场是一个 .USM 文件
   - 包含视频流 (SFV) 和音频流 (SFA)
   - P5R 全部 USM 使用同一解密密钥
   - 密钥通过 cricodecs 自动探测（P5R PC 版 key 已社区公开）

3. 视频流
   - 编码：VP9 (IVF 容器)
   - 分辨率：1920x1080
   - 帧率：30fps
   - 工具转码时自动匹配源 USM 的分辨率和帧率

4. 音频流
   - 编码：HCA (High Compression Audio)
   - 采样率：48000Hz
   - 声道：2 (立体声)
   - 部分过场（如 MOV001 赌场序幕）有双音轨：英语 + 日语
   - 工具自动检测源音轨数量，替换时保留相同音轨数


三、核心技术发现：双音轨 channel_no Bug
--------------------------------------
【问题现象】
替换双音轨过场（如 MOV001）后，游戏内仅播放约 14.5 秒就
强制截断到实机画面，而单音轨过场（如 MOV050）替换为 97 秒
视频能完整播放。

【诊断过程】
通过 C# Reloaded-II Mod hook CRI Movie 库函数，后台线程每 50ms
读取 player+0x04 状态字段，捕获完整状态时间线：

  - MOV050 (单音轨, 97秒视频):
    status=5 (PLAYING) 持续 97 秒 → 自然变为 6 (PLAYEND) ✓

  - MOV001 (双音轨, 97秒视频):
    status=5 仅持续 14.5 秒 → 变为 8/9 (被强制停止) ✗

游戏每帧调用 CRI poll 函数 (0x14035D7C0)，该函数检查内部流
对象 [player+0x12F8] 的返回值；若返回 4 (流结束)，则设置
player+0x1644=1 (停止标志)。

MOV001 的 CRI 解码器在 14.5 秒后就认为视频流数据耗尽，
而实际 USM 文件中包含完整的 97 秒视频数据。

【根因】
cricodecs 的简写 usm.mux() 函数在处理多音轨时，没有正确设置
音频流的 channel_no 字段。CRI 解码器在读取双音轨 USM 时，
因 channel 编号不正确而提前判定流结束。

【修复方案】
改用 UsmMuxConfig + UsmMuxAudioTrack 显式指定 channel_no：

  from cricodecs.usm import UsmMuxConfig, UsmMuxAudioTrack

  config = UsmMuxConfig(
      video_path=ivf_path,
      audio_tracks=[
          UsmMuxAudioTrack(path=hca0, encrypt=False, channel_no=0),
          UsmMuxAudioTrack(path=hca1, encrypt=False, channel_no=1),
      ],
      key=key,
  )
  usm_bytes = usm.mux(config)

修复后双音轨过场可完整播放任意时长视频。

【影响范围】
所有双音轨过场（MOV001 等）均受此 bug 影响。单音轨过场不受影响。


四、视频时长限制
----------------
【早期误解】
最初认为 P5R 过场有硬编码时长限制——替换视频比原版长就会被
截断。经诊断，这并非游戏脚本的时长限制，而是上述双音轨 mux bug
导致的提前流结束。

【实际情况】
- CRI Movie 播放器是流式读取 USM 文件的
- 读到文件末尾就自然结束 (status=6 PLAYEND)
- 游戏脚本收到播放结束事件后继续执行
- 替换视频可以比原版长，也可以比原版短
- 不需要加速/减速/裁剪来适配原版时长

【注意事项】
- 如果替换视频比原版短，剧情会跳得快一些，但不会报错
- 如果替换视频比原版长，会完整播放后再继续游戏
- 音视频质量不可牺牲：工具使用 VP9 高质量编码 + HCA 48kHz


五、Mod 生成与加载
------------------
1. Mod 目录结构 (Persona Essentials 标准):
   Mod名称/P5REssentials/CPK/MOVIE_JE.CPK/MOVIE/MOV0XX.USM

2. Reloaded-II 集成:
   - 工具内置 Reloaded-II 加载器
   - 一键启动 Reloaded-II 管理器
   - Mod 输出目录可选择自定义或内置 Reloaded-II Mods 目录
   - Reloaded-II 通过 FileEmulationFramework 模拟 CPK 文件读取

3. C# Hook 调试:
   - 通过 C# Reloaded-II Mod hook CRI Movie 库函数
   - 定位了 CRI Movie 库代码范围 (0x140350000-0x140370000)
   - hook 了 Create/Start/Open/Destroy/Poll 等函数
   - 用于诊断播放状态、帧解码、流结束等问题


六、预览技术
------------
1. 视频预览:
   - USM 解包后提取 VP9 视频流
   - ffmpeg 硬解 (d3d11va) 解码帧
   - Tkinter Canvas 渲染
   - 自动适配竖屏/横屏比例

2. 音频预览:
   - HCA 音频流通过 cricodecs 解密解码为 WAV
   - pygame.mixer 播放
   - 高响度自动归一化 (loudnorm)

3. 双音轨处理:
   - MOV001 等有英/日双音轨的过场
   - 工具自动检测音轨数量
   - 替换时将同一音频复制为多条音轨 (channel_no=0,1,...)


七、性能优化
------------
1. CPK 解包:
   - 8 线程并行探测 USM 密钥和流信息
   - 同 CPK 所有 USM 共用同一密钥，只探测一次

2. 视频转码:
   - VP9 编码，支持两遍编码对齐源码率
   - IVF 缓存：同视频同规格不重复编码
   - GPU 硬解预览加速

3. 进度反馈:
   - 导入 CPK → 解包 USM → 转码 MP4 各阶段独立进度条


八、已知问题与限制
------------------
1. 双音轨必须保留：游戏按语音设置读取对应 channel，
   替换时音轨数量必须与原版一致，否则游戏可能不加载。

2. 音频质量：工具使用 HCA 编码，质量等级可配置。
   替换视频音频会重新编码为 HCA，不是无损透传。

3. 视频编码：替换视频统一转码为 VP9 (匹配源 USM)。
   不支持直接透传其他编码格式。

4. Reloaded-II 依赖：Mod 加载需要安装 Reloaded-II
   和 Persona Essentials。工具已内置集成。


九、打包分发与兼容性发现
------------------------
1. 中文路径问题（最关键）:
   cricodecs 的 C++ 底层在 Windows 上不支持非 ASCII 文件路径。
   当工具安装在含中文用户名的目录下时，直接传路径字符串给
   usm.load() 会静默失败（不报错但返回空数据，预览缓存全部
   生成失败）。
   解决方案：用 Python 的 open(path, 'rb') 文件句柄传给
   usm.load(fileobj, key=...)，绕过 C++ 路径解析。

2. PyInstaller 打包 cricodecs:
   cricodecs 是 nanobind 编译的单个 .pyd 文件，PyInstaller 的
   自动依赖分析无法发现。必须在 .spec 文件的 binaries 中
   手动指定 .pyd 文件路径到 cricodecs/ 目录，否则打包后
   import cricodecs 失败。

3. 子进程弹窗:
   打包成 exe 后，所有 subprocess 调用（ffmpeg 等）必须加
   creationflags=0x08000000 (CREATE_NO_WINDOW)，否则运行时
   会频繁弹出黑色控制台窗口。

4. GIL 争用与并行数:
   cricodecs 是 C 扩展，不释放 GIL。预览生成时并行数超过 3-4
   会因 GIL 争用导致 UI 卡死/未响应。最优并行数为 3。

5. VP9 硬件编码:
   NVIDIA NVENC 不支持 VP9 编码，只能用 CPU 软编。
   优化参数：CRF 21（视觉无损），cpu-used 7，deadline realtime。


十、预览技术优化
----------------
1. 直接 IVF 流提取:
   不需要外部 crid_mod.exe 子进程，cricodecs 的
   movie.stream_bytes(0) 直接返回 IVF/VP9 流（文件头 "DKIF"），
   零子进程启动开销，内存中直接写出 m2v 缓存。

2. 一次读取同时取视频+音频:
   一次 usm.load() 同时取视频流 (stream 0) 和音频流 (stream N)，
   不重复读取 USM 文件，减少磁盘 IO。

3. 音轨选择:
   双音轨过场自动选择最后一条 HCA 音轨（日文配音）。
   单音轨过场自动选择唯一 HCA 音轨。

4. cricodecs 漏解密帧:
   部分过场在特定时间点画面定格但音频正常，这是 cricodecs
   对跨 chunk VP9 帧漏做 MaskVideo 解密导致的。
   对此类帧可用 crid_mod.exe 单独解包修复。



十一、特殊过场：ADX 音频与字幕流
-----------------------------
【问题现象】
预览 MOV092~MOV097 等后段过场时，画面正常但完全没有声音。
这些文件的大小和普通过场无异，列表也没有标注"无音轨"。

【诊断过程】
用 cricodecs 遍历 USM 流结构后发现：

  MOV097_J.USM 的流结构：
    stream 0: mov097.avi (视频, MPEG-2, 头 00 00 01 B3)
    stream 1: mov097.avi (音频, ADX, 头 80 00 00 1C)

  MOV092.USM 的流结构：
    stream 0: mov092.avi (MPEG-2 视频)
    stream 1: mov092.avi (ADX 音频)
    stream 2: :mov090.txt (字幕文本, 27 字节)

两个关键发现：
1. 这些后段过场不是 VP9+HCA，而是 MPEG-2 + ADX 格式
   （游戏内电视、监控、手机视频等特殊画面）。
   音频流的 filename 字段不是 .hca 而是 .avi，
   导致按后缀过滤的旧逻辑把音频流整个漏掉。

2. 这些 USM 末尾还附加了一条 .txt 字幕流。
   旧逻辑"双音轨取最后一条流"会错误地选中字幕流，
   而字幕流不是音频，解码直接失败。

【修复方案】
不再按文件后缀过滤音频流，而是按二进制文件头识别：
  - HCA: 文件头 48 43 41 00 ("HCA\0")
  - ADX: 文件头第一个字节 0x80
  - 其他流（.txt 字幕等）：跳过

遍历所有非视频流，只保留文件头匹配 HCA/ADX 的流，
再从真正的音频流中选最后一条（日文配音优先）。

解码器：
  - HCA: cricodecs.hca.decode(raw, keycode=key)
  - ADX: cricodecs.adx.decode(raw)

【经验教训】
  - CRI 容器内的流 filename 不可信（.avi 扩展名下可能是
    MPEG-2 视频或 ADX 音频）。
  - 必须以二进制魔数（magic bytes）作为格式判据，
    不要依赖文件扩展名。
  - USM 可能包含非音视频流（字幕、元数据），
    遍历时要显式跳过。


十二、HCA 加密音频的识别陷阱（最关键发现）
--------------------------------------
【问题现象】
打包后预览大部分视频完全没有声音。检查 P5R_movies 目录发现：
103 个 m2v 视频缓存全部生成成功，但只有 10 个 pv.wav 音频缓存。
缺失 wav 的全部是普通过场（VP9+HCA 格式），而有 wav 的 10 个
恰好是 MOV090~097 等后段特殊过场（MPEG-2+ADX 格式）。

【诊断过程】
编写测试脚本遍历 USM 流结构，逐流打印前 16 字节十六进制：

  MOV000.USM 的流结构：
    stream 0: mov000.ivf (VP9 视频, 头 44 4B 49 46 "DKIF")
    stream 1: mov000#00.hca (HCA 音频, 前4字节 C8 C3 C1 00)

关键发现：HCA 音频流的前 4 字节是 C8 C3 C1 00，而不是
明文的 48 43 41 00 ("HCA\0")！

【根因】
P5R 的 USM 文件中，HCA 音频流是加密存储的。
cricodecs 的 movie.stream_bytes() 返回的是加密后的原始字节，
没有自动解密。解密是在 hca.decode() 内部完成的。

之前的音频识别逻辑用二进制魔数判断：
  if raw[:4] == b"HCA\x00":   # 是 HCA
  elif raw[:1] == b"\x80":     # 是 ADX

加密后的 HCA 数据前 4 字节不是 "HCA\0"，因此全部被误判为
非音频流而跳过。只有 ADX 音频（前 1 字节固定 0x80，未加密）
能被正确识别，所以只有 10 个 ADX 文件生成了 wav。

【修复方案】
不再依赖魔数识别，改为尝试解码识别：

  for each non-video stream:
      # 先试 HCA 解码（hca.decode 内部自动解密）
      try:
          wav = hca.decode(raw, keycode=key)
          if len(wav) > 1000:
              是 HCA 音频，加入候选
              continue
      except: pass
      # 再试 ADX 解码
      if raw[:1] == b"\x80":
          try:
              wav = adx.decode(raw)
              if len(wav) > 1000:
                  是 ADX 音频，加入候选
          except: pass

解码成功即认定为对应格式，解码失败则跳过（字幕流等非音频
会解码失败被自然过滤）。

修复后全部 103 个过场都能正确生成 pv.wav 音频缓存。

【经验教训】
  - 加密格式的魔数（magic bytes）不可信：加密后文件头会
    完全改变，不能用明文魔数判断加密数据的格式。
  - 对于自带解密能力的解码器（如 cricodecs.hca.decode），
    最可靠的识别方式是"尝试解码，成功即认定"。
  - 排查"部分文件有声音、部分没有"时，要对比有声音和
    没声音的文件在格式上的系统性差异（本例中是 HCA vs ADX，
    加密 vs 未加密），而不是只看单个文件。
  - 打包后出现的问题要先怀疑打包环境差异，但本例中打包前
    也有同样问题，只是旧缓存掩盖了——清除缓存重新生成后
    才暴露出来。


十三、参考开源项目
----------------
- WannaCRI / cricodecs (USM 解包加密) - donmai-me
- FFmpeg (视频编解码)
- VGAudio (HCA 编解码) - Thealexbarney
- Reloaded-II (Mod 加载器) - Sewer56
- Persona Essentials (P5R 文件替换)
- FileEmulationFramework (文件模拟框架)
- cri_demux_p5r (USM Demux) - Hengle
- crid_mod.exe (USM Demux 工具)
- vgmstream (游戏音频解码)
"""
        txt.insert("1.0", report)
        txt.config(state="disabled")

        tk.Button(win, text="关闭", command=win.destroy, width=10).pack(pady=(0, 10))

    def _ask_mod_meta(self):
        """弹窗输入 Mod 名称和简介；返回 (名称, 简介)，取消返回 None。"""
        win = tk.Toplevel(self)
        win.title("Mod 信息")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        try:
            win.attributes("-topmost", True)
        except Exception:
            pass
        result = {"ok": False, "name": "", "desc": ""}

        tk.Label(win, text="Mod 名称：", font=F_LABEL_BOLD).grid(
            row=0, column=0, padx=(12, 4), pady=(12, 4), sticky="e")
        name_var = tk.StringVar(
            value=self.panel_replace.mod_name_var.get().strip() or "我的过场替换")
        tk.Entry(win, textvariable=name_var, width=34).grid(
            row=0, column=1, padx=(4, 12), pady=(12, 4))

        tk.Label(win, text="简介：", font=F_LABEL_BOLD).grid(
            row=1, column=0, padx=(12, 4), pady=4, sticky="ne")
        desc_var = tk.StringVar(value=self.panel_replace.mod_desc_var.get().strip())
        tk.Entry(win, textvariable=desc_var, width=34).grid(
            row=1, column=1, padx=(4, 12), pady=4)

        def _ok(_e=None):
            if not name_var.get().strip():
                messagebox.showwarning(APP_TITLE, "Mod 名称不能为空。", parent=win)
                return
            result["ok"] = True
            result["name"] = name_var.get().strip()
            result["desc"] = desc_var.get().strip()
            win.destroy()

        def _cancel(_e=None):
            win.destroy()

        btns = tk.Frame(win)
        btns.grid(row=2, column=0, columnspan=2, pady=(8, 12))
        tk.Button(btns, text="确定", command=_ok, width=10).pack(side=tk.LEFT, padx=6)
        tk.Button(btns, text="取消", command=_cancel, width=10).pack(side=tk.LEFT, padx=6)
        win.bind("<Return>", _ok)
        win.bind("<Escape>", _cancel)
        win.wait_window()
        return (result["name"], result["desc"]) if result["ok"] else None

    def do_build_mod(self):
        # 先弹窗输入 Mod 名称和简介
        meta = self._ask_mod_meta()
        if meta is None:
            return
        name, desc = meta
        self.panel_replace.mod_name_var.set(name)
        self.panel_replace.mod_desc_var.set(desc)
        # 再弹窗选择 Mod 输出位置（生成的 Mod 文件夹将放在所选目录下）
        # 优先定位到 Reloaded-II 的 Mods 目录（探测到就直接生成进去，重启即可见）
        init = _find_reloaded_mods_dir()
        if not init:
            cur = self.panel_replace.mod_root_var.get().strip()
            if cur:
                p = Path(cur)
                init = str(p if p.exists() else (p.parent if p.parent.exists() else None) or "")
            else:
                cfg = _load_cfg()
                last = cfg.get(_CFG_KEY_OUTDIR)
                if last and Path(last).exists():
                    init = last
        d = filedialog.askdirectory(
            title="选择 Mod 输出位置（生成的 Mod 文件夹将放在这里；已定位到 Reloaded-II 的 Mods 目录）",
            initialdir=init or None)
        if not d:
            return
        self.panel_replace.mod_root_var.set(d)
        _remember_outdir(d)
        if Path(d).name.lower() == "mods" or _load_cfg().get(_CFG_KEY_RELOADED) == d:
            pass
        self.panel_replace.build()

    def launch_reloaded(self):
        """打开工具内置（或已安装）的 Reloaded-II 管理器。"""
        rex = _find_builtin_reloaded_exe()
        if not rex:
            r2mods = _find_reloaded_mods_dir()
            if r2mods:
                alt = Path(r2mods).parent / "Reloaded-II.exe"
                if alt.exists():
                    rex = alt
        if not rex:
            messagebox.showwarning(APP_TITLE, "未找到内置 Reloaded-II。\n请把 Reloaded-II 完整目录放入工具目录的 Reloaded-II\\ 文件夹。")
            return
        try:
            subprocess.Popen([str(rex)], cwd=str(rex.parent))
            self.set_status("已启动 Reloaded-II 管理器：%s" % rex)
        except OSError as e:
            messagebox.showerror(APP_TITLE, "启动 Reloaded-II 失败：%s" % e)

    def open_reloaded_mods(self):
        """打开 Reloaded-II 的 Mods 文件夹（内置目录或已配置的目录）。"""
        d = _find_reloaded_mods_dir()
        if not d or not Path(d).is_dir():
            messagebox.showwarning(
                APP_TITLE,
                "未找到 Reloaded-II 的 Mods 文件夹。\n"
                "请把 Reloaded-II 完整目录放入工具目录的 Reloaded-II\ 文件夹，"
                "或在「生成 Mod」时选择已安装的 Reloaded-II Mods 目录。")
            return
        try:
            import os as _os
            _os.startfile(d)
            self.set_status("已打开 Reloaded-II Mods 文件夹：%s" % d)
        except Exception as e:
            messagebox.showerror(APP_TITLE, "打开 Mods 文件夹失败：%s" % e)

    def _import_replace_video(self):
        p = filedialog.askopenfilename(
            title="选择要替换进去的视频（自定义）",
            filetypes=[("视频文件", "*.mp4 *.mkv *.avi *.webm *.mov *.flv *.wmv *.m4v"),
                       ("所有文件", "*.*")])
        if not p:
            return
        try:
            self.panel_replace_video.play_external_file(p)
        except Exception:
            pass
        cfg = _load_cfg()
        cfg["last_replace_video"] = str(Path(p).resolve())
        _save_cfg(cfg)
        self.set_status("已导入待替换视频：%s（已记住，下次启动自动恢复）" % p)

    def _undo_import_replace_video(self):
        """撤销导入待替换视频：清空待替换视频播放器，移除记忆配置（不影响已标记的替换）。"""
        pv = self.panel_replace_video
        try:
            pv.stop()
            pv.usm_path = None
            pv.mp4_path = None
            pv._current_item = None
            pv._show_placeholder()
        except Exception:
            pass
        cfg = _load_cfg()
        cfg.pop("last_replace_video", None)
        _save_cfg(cfg)
        self.set_status("已撤销导入待替换视频。")

    def _save_replacements_manual(self):
        """手动保存当前替换记录（与自动保存等效，给用户一个显式确认）。"""
        cfg = _load_cfg()
        rmap = {}
        for it in self._movies:
            if getattr(it, 'replaced_with', None) and Path(it.replaced_with).exists():
                rmap[it.name] = str(Path(it.replaced_with).resolve())
        cfg["replaced_map"] = rmap
        _save_cfg(cfg)
        messagebox.showinfo(APP_TITLE, "替换记录已保存。共 %d 条替换。" % len(rmap))

    def _clear_all_records(self):
        """清除所有替换记录和会话记忆，下次启动必须重新导入 CPK。"""
        if not messagebox.askyesno(APP_TITLE,
                "将清除以下内容：\n  - 全部替换记录\n  - 上次导入的 CPK 路径\n  - 上次 Mod 输出目录\n"
                "  - P5R_movies 解包缓存（USM/预览文件）\n\n"
                "下次启动工具需要重新导入 MOVIE_JE.CPK 并重新解包。\n\n确定继续？"):
            return
        try:
            # 写入 _cleared 标记：下次启动不自动恢复，用户必须手动导入
            _CFG_PATH.write_text(json.dumps({"_cleared": True}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        # 删除 P5R_movies 解包缓存
        try:
            movies_dir = getattr(self, '_out_dir', None)
            if movies_dir and Path(movies_dir).exists():
                shutil.rmtree(movies_dir, ignore_errors=True)
        except Exception:
            pass
        # 清空列表和替换状态
        self.movies = []
        self._entries = []
        self._cpk_path = None
        self._current_item = None
        self._cpk_var.set("")
        self.search_var.set("")
        try:
            self.refresh_list()
        except Exception:
            pass
        try:
            self.panel_replace_video._show_placeholder()
        except Exception:
            pass
        self.set_status("已清除所有记录。请重新导入 MOVIE_JE.CPK。")
        messagebox.showinfo(APP_TITLE, "已清除全部记录。\n下次启动将回到初始状态。")

    def _apply_replace(self):
        """标记替换：把当前导入的视频指定为左侧选中过场的替换视频（列表显示已替换+文件名）。"""
        it = self.selected_movie()
        if not it or not it.abs_path or not Path(it.abs_path).exists():
            messagebox.showwarning(APP_TITLE, "请先在左侧列表选择要替换的过场动画（源 USM）。")
            return
        pv = self.panel_replace_video
        video = pv.mp4_path if pv else None
        if not video or not Path(video).exists():
            messagebox.showwarning(APP_TITLE, "请先通过「导入视频…」导入要替换的视频。")
            return
        it.replaced_with = str(video)  # 存完整路径，列表显示文件名
        cfg = _load_cfg()
        rmap = dict(cfg.get("replaced_map") or {})
        rmap[it.name] = str(Path(video).resolve())
        cfg["replaced_map"] = rmap
        _save_cfg(cfg)
        self.refresh_list()
        self.set_status("已标记替换：%s ← %s（已记住，下次启动自动恢复；点击「一键生成 Mod」生成）"
                        % (it.name, Path(video).name))

    def _undo_replace(self):
        """撤销替换：清掉当前选中过场的替换标记（列表恢复「已解包」，配置同步移除）。"""
        it = self.selected_movie()
        if not it or not it.replaced_with:
            messagebox.showinfo(APP_TITLE,
                                "当前选中的过场没有替换标记，无需撤销。\n（请先在左侧列表选中已替换的过场）")
            return
        old = it.replaced_with
        it.replaced_with = None
        it.mod_made = False
        cfg = _load_cfg()
        rmap = dict(cfg.get("replaced_map") or {})
        key = None
        for k in rmap.keys():
            if k == it.name or k.replace("\\", "/") == it.name.replace("\\", "/"):
                key = k
                break
        if key:
            rmap.pop(key, None)
        cfg["replaced_map"] = rmap
        _save_cfg(cfg)
        self.refresh_list()
        self.set_status("已撤销替换：%s ← %s（列表已恢复「已解包」）"
                        % (it.name, Path(old).name))

    def _export_mp4(self):
        """把当前选中的过场导出为原始分辨率 MP4（H.264 + AAC），自定义保存目录。"""
        it = self.selected_movie()
        if not it or not it.abs_path or not Path(it.abs_path).exists():
            messagebox.showwarning(APP_TITLE,
                                   "请先在左侧列表选择要导出的过场动画（需已解包）。")
            return
        self.set_status("正在准备 %s 的视频流…" % it.name)
        try:
            engine.prepare_m2v_preview(it.abs_path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, "准备视频流失败：%s" % e)
            return
        m2v = Path(it.abs_path).with_suffix(".m2v")
        if not m2v.exists():
            messagebox.showerror(APP_TITLE, "未找到视频流文件：%s\n\n%s 可能无法正常解包。" % (m2v, it.name))
            return
        outdir = filedialog.askdirectory(title="选择 MP4 导出目录")
        if not outdir:
            return
        name = Path(it.name).stem + ".mp4"
        dst = Path(outdir) / name
        if dst.exists():
            if not messagebox.askyesno(APP_TITLE, "%s 已存在，是否覆盖？" % dst):
                return
        self.set_status("正在导出 %s（原始分辨率 + 48kHz 无损音轨）…" % name)
        self.show_progress("导出 MP4", "正在导出 %s…" % name)

        def fn(q):
            q.put(("progress", (0, 1, "正在合成 H.264/AAC MP4（NVENC 硬编，视觉无损）…")))
            try:
                wav = engine.extract_audio_original(it.abs_path)
            except Exception:
                wav = None

            def _prog(p):
                q.put(("progress", (int(p * 100), 100,
                                    "正在合成 H.264/AAC MP4… %d%%" % int(p * 100))))

            out = ffmpeg.export_mp4_lossless(m2v, wav, dst, on_progress=_prog)
            return out, bool(wav)

        def done(result):
            out, has_audio = result
            self.set_status("已导出：%s" % out)
            messagebox.showinfo(
                APP_TITLE,
                "导出完成：\n%s\n\n%s\n\n文件大小：%.1f MB" % (
                    out,
                    "视频：原始分辨率 H.264（CRF18 视觉无损）\n音频：日文配音 48kHz AAC 320kbps（原始采样率，无损解码）"
                    if has_audio else "视频：原始分辨率 H.264（CRF18 视觉无损）\n（该过场无音轨）",
                    out.stat().st_size / 1024 / 1024))

        self.run_task(fn, done)

    def check_ffmpeg(self):
        def fn(q):
            q.put(("progress", (1, 1, "正在检查 FFmpeg…")))
            import shutil
            for name in ("ffmpeg", "ffprobe"):
                exe = shutil.which(name)
                if not exe:
                    raise RuntimeError("未找到 %s.exe，请安装 FFmpeg 并加入 PATH。" % name)
            out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                                 timeout=20).stdout.splitlines()[0]
            return out

        def done(info):
            self.set_status(info)
            messagebox.showinfo(APP_TITLE, "FFmpeg 正常：\n%s" % info)

        self.run_task(fn, done)

    def open_help(self):
        p = Path(__file__).resolve().parent / "使用说明.md"
        win = tk.Toplevel(self)
        win.title("使用说明")
        win.geometry("780x620")
        win.transient(self)
        txt = tk.Text(win, wrap=tk.WORD, bg="#1A1A1A", fg="#E0E0E0",
                      insertbackground="#E0E0E0", relief=tk.FLAT, bd=0,
                      font=("Microsoft YaHei", 10), padx=16, pady=12)
        sc = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sc.set)
        sc.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        try:
            txt.insert("1.0", p.read_text(encoding="utf-8"))
        except Exception as e:
            txt.insert("1.0", "无法读取使用说明：%s" % e)
        txt.config(state=tk.DISABLED)

    # ==================== 关于 ====================
    def show_about(self):
        import webbrowser
        win = tk.Toplevel(self)
        win.title("关于")
        win.geometry("520x620")
        win.resizable(False, False)
        win.transient(self)
        win.grab_set()
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - 520) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 600) // 2
        win.geometry("+%d+%d" % (x, y))

        header = tk.Frame(win, bg=C_HEADER)
        header.pack(fill=tk.X)
        tk.Label(header, text="P5R过场动画替换工具", font=F_ABOUT_TITLE,
                 fg="white", bg=C_HEADER).pack(pady=(18, 2))
        tk.Label(header, text="女神异闻录5皇家版 · 过场动画 解包/预览/替换 工具",
                 font=F_CN, fg=C_HEADER_SUB, bg=C_HEADER).pack(pady=(2, 16))

        body = tk.Frame(win)
        body.pack(fill=tk.BOTH, expand=True, padx=24, pady=16)

        tk.Label(body, text="功能特性", font=F_CN_BOLD, anchor="w").pack(fill=tk.X)
        for feat in [
            "MOVIE_JE.CPK 一键解包全部过场动画（USM）",
            "加密 USM 自动探测密钥 → 转 MP4 正常预览",
            "右侧内嵌视频播放器，选中过场直接播放",
            "自定义视频一键替换 → 重打包加密 USM",
            "自动生成 Reloaded-II (P5R Essentials) 兼容 Mod",
            "界面风格与 P5R BGM Editor 保持一致",
        ]:
            tk.Label(body, text="  • %s" % feat, font=F_CN, fg=C_BODY, anchor="w").pack(fill=tk.X, pady=1)

        tk.Frame(body, height=1, bg=C_DIV).pack(fill=tk.X, pady=12)

        tk.Label(body, text="技术参考", font=F_CN_BOLD, anchor="w").pack(fill=tk.X)
        refs = [
            ("shrinefox 过场替换指南", "shrinefox.com/blog/2025/10/16/p5r-modding-guide-2025-7-anime-cutscenes-usm"),
            ("GameBanana · PS4 Cutscenes", "gamebanana.com/mods/522792"),
            ("Persona Modding wiki", "personamodding.com"),
            ("CriCodecs (开源工具链)", "github.com/Youjose/CriCodecs"),
        ]
        for label, url in refs:
            row = tk.Frame(body)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text=label, font=F_CN, width=24, anchor="w").pack(side=tk.LEFT)
            link = tk.Label(row, text=url, font=F_SMALL, fg=C_LINK, cursor="hand2")
            link.pack(side=tk.LEFT)
            link.bind("<Button-1>", lambda e, u="https://" + url: webbrowser.open(u))

        tk.Frame(body, height=1, bg=C_DIV).pack(fill=tk.X, pady=12)

        tk.Label(body, text="关于作者", font=F_CN_BOLD, anchor="w").pack(fill=tk.X)
        tk.Label(body, text="工具作者：凯梦", font=F_CN, fg=C_BODY, anchor="w").pack(fill=tk.X, pady=1)
        tk.Label(body, text="版本 v%s" % APP_VERSION, font=F_SMALL, fg=C_GRAY, anchor="w").pack(fill=tk.X)


# ======================================================================
# 内嵌视频预览面板
# ======================================================================
class VideoPreviewPanel(tk.Frame):
    """右侧内嵌视频播放器：ffmpeg 解码帧 + pygame 播放音轨 + tkinter 显示。"""

    def __init__(self, master, app: App, title: str = "视频预览", import_command=None,
                 apply_command=None, undo_command=None, export_command=None,
                 undo_import_command=None):
        super().__init__(master)
        self.app = app
        self.export_command = export_command

        self.usm_path: str | None = None
        self.mp4_path: str | None = None
        self._fps = 30.0
        self._duration = 0.0
        self._frames: queue.Queue = queue.Queue()
        self._reader: FrameReader | None = None
        self._ffproc: subprocess.Popen | None = None
        self._wav_path: str | None = None
        self._wav_work = None
        self._playing = False
        self._paused = False
        self._tick_after = None
        self._photo = None          # 保持 PhotoImage 引用
        self._converting = False
        self._current_item: MovieItem | None = None
        self._frames_displayed = 0  # 已显示的帧索引（音频时钟驱动用）
        self._play_start_time = 0.0  # 播放开始的墙钟时间（fallback）
        self._last_shown = None      # 上一次显示的帧数据（坏帧副本检测）
        self._dup_run = 0            # 连续重复帧计数
        self._dup_waiting = False    # 坏帧段同步暂停中
        self._audio_seek = 0.0       # 音频时钟偏移（坏帧段恢复 seek 用）
        self._total_frames = 0       # 视频总帧数（坏帧段判定用）
        self._hwaccel_ok: bool | None = None  # d3d11va 硬解可用性（惰性探测）
        self._v_w = 0                # 视频实际宽（竖屏适配）
        self._v_h = 0                # 视频实际高（竖屏适配）
        self._disp_w = PREVIEW_W     # 当前等比缩放显示宽（首帧/预览图用）
        self._disp_h = PREVIEW_H     # 当前等比缩放显示高（首帧/预览图用）
        self._frame_w = PREVIEW_W    # 播放管道输出帧宽（管道启动时锁定）
        self._frame_h = PREVIEW_H    # 播放管道输出帧高（管道启动时锁定）

        frame = tk.LabelFrame(self, text=title, padx=10, pady=10)
        frame.pack(fill=tk.X)

        # 画面区：Canvas 承载五角星背景图案 + 视频帧 + 占位文字
        placeholder = ("点击「导入视频…」选择要替换进去的视频" if import_command is not None
                       else "在左侧列表选择已解包的过场\n双击或点击「播放」即可预览")
        self.video_canvas = tk.Canvas(frame, bg="#000000", highlightthickness=0,
                                      width=PREVIEW_W, height=PREVIEW_H)
        self.video_canvas.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self._bg_src = Path(__file__).parent / "assets" / "bg_starfield.png"
        self._bg_photo = None
        self._bg_item = None
        self._frame_item = None
        self._placeholder_item = None
        self._placeholder_box_item = None
        self._placeholder_icon_item = None
        self._placeholder_text = placeholder
        self.video_canvas.bind("<Configure>", self._on_canvas_configure)
        self._render_bg()
        self._show_placeholder(placeholder)

        # 控制行：P5 风格控制键（深灰底 + 白色图标 + 白色文字）
        ctrl = tk.Frame(frame)
        ctrl.pack(side=tk.TOP, fill=tk.X)
        self.play_icon = self._make_ctrl_icon("play", "播放", bg=C_PLAY, fg="#FFFFFF",
                                              icon_fg="#FFFFFF")
        self.pause_icon = self._make_ctrl_icon("pause", "暂停", bg="#FFFFFF", fg=C_MAIN,
                                               icon_fg=C_MAIN)
        self.stop_icon = self._make_ctrl_icon("stop", "停止", bg="#2A2A2A", fg="#FFFFFF",
                                              icon_fg="#FFFFFF")
        self.play_btn = tk.Button(ctrl, image=self.play_icon, command=self.play,
                                  bg="#0D0D0D", bd=0, relief=tk.FLAT,
                                  state=tk.DISABLED, cursor="hand2")
        self.play_btn.pack(side=tk.LEFT, padx=3)
        self.pause_btn = tk.Button(ctrl, image=self.pause_icon, command=self.pause_resume,
                                   bg="#0D0D0D", bd=0, relief=tk.FLAT,
                                   state=tk.DISABLED, cursor="hand2")
        self.pause_btn.pack(side=tk.LEFT, padx=3)
        self.stop_btn = tk.Button(ctrl, image=self.stop_icon, command=self.stop,
                                  bg="#0D0D0D", bd=0, relief=tk.FLAT,
                                  state=tk.DISABLED, cursor="hand2")
        self.stop_btn.pack(side=tk.LEFT, padx=3)
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        tk.Label(ctrl, textvariable=self.time_var, font=F_SMALL, fg=C_GRAY).pack(side=tk.LEFT, padx=8)

        # 勾选框：播放替换后的视频（仅「视频预览」面板）
        self.play_replaced_var = tk.BooleanVar(value=False)
        if export_command is not None:
            self.play_replaced_chk = tk.Checkbutton(
                ctrl, text="播放替换后视频", variable=self.play_replaced_var,
                font=F_SMALL, fg="#FFFFFF", bg="#1A1A1A", selectcolor="#0D0D0D",
                activebackground="#1A1A1A", activeforeground="#FFFFFF",
                bd=0, highlightthickness=0, cursor="hand2")
            self.play_replaced_chk.pack(side=tk.LEFT, padx=(12, 0))

        # 导出行（仅「视频预览」面板）：把当前过场导出为原始分辨率 MP4
        if export_command is not None:
            ex_row = tk.Frame(frame, bg=C_BG)
            ex_row.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))
            self.export_btn_img = self._make_ctrl_icon("text", "导出 MP4…", bg=C_BLUE, fg="#FFFFFF",
                                                        w=140, h=38, slant=20)
            self.export_btn = tk.Button(ex_row, image=self.export_btn_img, command=export_command,
                                          bg="#0D0D0D", bd=0, relief=tk.FLAT, cursor="hand2")
            self.export_btn.pack(side=tk.LEFT)
            tk.Label(ex_row, text="把当前选中的过场导出为原始分辨率 MP4（H.264 + AAC，可自选保存目录）",
                     fg=C_GRAY, font=F_SMALL).pack(side=tk.LEFT, padx=8)

        # 勾选切换时重新加载当前选中项
        if hasattr(self, 'play_replaced_chk'):
            self.play_replaced_chk.config(command=self._on_toggle_replaced)
        self.usm_var = tk.StringVar()
        self.mp4_var = tk.StringVar()
        tk.Label(frame, text="USM:", font=F_SMALL).pack(side=tk.LEFT, anchor=tk.W, pady=(6, 0))
        tk.Label(frame, textvariable=self.usm_var, fg=C_GRAY, font=F_SMALL,
                 wraplength=620, justify=tk.LEFT, anchor=tk.W).pack(side=tk.TOP, fill=tk.X)
        self.preview_status = tk.StringVar(
            value=("未导入视频。点击「导入视频…」选择要替换的过场视频。" if import_command is not None
                   else "未选择过场。"))
        tk.Label(frame, textvariable=self.preview_status, fg=C_GRAY, font=F_SMALL,
                 anchor=tk.W).pack(side=tk.TOP, fill=tk.X, pady=(2, 0))
        # 导入按钮（仅待替换视频播放器显示）
        if import_command is not None:
            import_row = tk.Frame(frame)
            import_row.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))
            self.import_btn_img = self._make_ctrl_icon("text", "导入视频…", bg=C_BLUE, fg="#FFFFFF",
                                                        w=152, h=38, slant=20)
            self.import_btn = tk.Button(import_row, image=self.import_btn_img, command=import_command,
                                        bg="#0D0D0D", bd=0, relief=tk.FLAT, cursor="hand2")
            self.import_btn.pack(side=tk.LEFT)
            if undo_import_command is not None:
                self.undo_import_btn_img = self._make_ctrl_icon("text", "撤销导入视频", bg=C_GRAY, fg="#FFFFFF",
                                                                w=172, h=38, slant=20)
                self.undo_import_btn = tk.Button(import_row, image=self.undo_import_btn_img,
                                                 command=undo_import_command,
                                                 bg="#0D0D0D", bd=0, relief=tk.FLAT, cursor="hand2")
                self.undo_import_btn.pack(side=tk.LEFT, padx=(8, 0))
            if apply_command is not None:
                self.apply_btn_img = self._make_ctrl_icon("text", "替换所选视频", bg=C_MAIN, fg="#FFFFFF",
                                                          w=172, h=38, slant=20)
                self.apply_btn = tk.Button(import_row, image=self.apply_btn_img,
                                           command=apply_command,
                                           bg="#0D0D0D", bd=0, relief=tk.FLAT, cursor="hand2")
                self.apply_btn.pack(side=tk.LEFT, padx=(8, 0))
            if undo_command is not None:
                self.undo_btn_img = self._make_ctrl_icon("text", "撤销替换", bg=C_GRAY, fg="#FFFFFF",
                                                         w=142, h=38, slant=20)
                self.undo_btn = tk.Button(import_row, image=self.undo_btn_img,
                                          command=undo_command,
                                          bg="#0D0D0D", bd=0, relief=tk.FLAT, cursor="hand2")
                self.undo_btn.pack(side=tk.LEFT, padx=(8, 0))
            tk.Label(frame, text="选择你的自定义视频（mp4/mkv/webm/mov 等），将替换左侧选中的过场。",
                     fg=C_GRAY, font=F_SMALL, anchor=tk.W).pack(side=tk.TOP, fill=tk.X, pady=(4, 0))
        self.pack(fill=tk.X, pady=(0, 8))

    # ---- 背景图案 / 占位 / 帧显示 ----
    def _on_canvas_configure(self, event=None):
        """画布尺寸变化（窗口/面板调整）时重铺背景并居中占位文字与视频帧。"""
        self._render_bg()
        self._center_placeholder()
        self._center_frame()

    def _render_bg(self):
        """把五角星素材 cover 缩放铺满播放器画布（不变形，居中裁切）。"""
        c = self.video_canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 10 or h < 10 or not Path(self._bg_src).exists():
            return
        try:
            from PIL import Image, ImageTk
            src_img = Image.open(str(self._bg_src)).convert("RGB")
            sw, sh = src_img.size
            scale = max(w / sw, h / sh)
            nw, nh = max(2, int(sw * scale + 0.5)), max(2, int(sh * scale + 0.5))
            img = src_img.resize((nw, nh), Image.LANCZOS)
            x0 = max(0, (nw - w) // 2)
            y0 = max(0, (nh - h) // 2)
            img = img.crop((x0, y0, x0 + w, y0 + h))
            self._bg_photo = ImageTk.PhotoImage(img)
            if self._bg_item is None:
                self._bg_item = c.create_image(0, 0, anchor="nw", image=self._bg_photo)
            else:
                c.itemconfig(self._bg_item, image=self._bg_photo)
            c.tag_lower(self._bg_item)
        except Exception:
            pass

    def _center_frame(self):
        """画布尺寸变化后重新居中视频帧（启动恢复首帧时 Canvas 可能尚未布局）。"""
        c = self.video_canvas
        if self._frame_item is None:
            return
        w, h = c.winfo_width(), c.winfo_height()
        c.coords(self._frame_item, (w // 2) or PREVIEW_W // 2, (h // 2) or PREVIEW_H // 2)
        c.tag_raise(self._frame_item)

    def _center_placeholder(self):
        c = self.video_canvas
        if self._placeholder_item is None:
            return
        w, h = c.winfo_width(), c.winfo_height()
        W = min(420, max(240, w - 40))
        H = 84
        x0 = (w - W) // 2
        y0 = (h - H) // 2
        if self._placeholder_box_item is not None:
            pts = [
                x0 + 10, y0, x0 + W, y0, x0 + W - 30, y0 + H, x0, y0 + H,
            ]
            c.coords(self._placeholder_box_item, *pts)
            c.tag_raise(self._placeholder_item)
            c.tag_lower(self._placeholder_box_item, self._placeholder_item)
            if self._placeholder_icon_item is not None:
                try:
                    c.coords(self._placeholder_icon_item, x0 - 16, y0 - 14)
                    c.tag_raise(self._placeholder_icon_item)
                except Exception:
                    pass
        c.coords(self._placeholder_item, x0 + W // 2, y0 + H // 2)

    def _show_placeholder(self, text=None):
        """显示 P5 提示框 + 占位文字，并移除视频帧（停止/未加载状态）。"""
        c = self.video_canvas
        if self._frame_item is not None:
            try:
                c.delete(self._frame_item)
            except Exception:
                pass
            self._frame_item = None
        if self._placeholder_item is None:
            self._placeholder_item = c.create_text(
                c.winfo_width() // 2 or PREVIEW_W // 2,
                c.winfo_height() // 2 or PREVIEW_H // 2,
                text=text or self._placeholder_text,
                fill="#FFFFFF", font=F_CN, justify=tk.CENTER)
        else:
            c.itemconfig(self._placeholder_item, text=text or self._placeholder_text,
                         state="normal")
        self._ensure_placeholder_box()
        self._center_placeholder()

    def _ensure_placeholder_box(self):
        """P5 提示框：白色斜切边框 + 左侧中部 V 尖角 + 纯黑填充（垫在占位文字下）。"""
        c = self.video_canvas
        if self._placeholder_box_item is None:
            self._placeholder_box_item = c.create_polygon(
                (0, 0, 10, 0, 10, 10, 0, 10),
                outline="#FFFFFF", fill="#000000", width=2, joinstyle="miter")
        w, h = c.winfo_width(), c.winfo_height()
        W = min(420, max(240, w - 40))
        H = 84
        x0 = (w - W) // 2
        y0 = (h - H) // 2
        pts = [
            x0 + 10, y0,                # 左上斜角起点（左斜边短）
            x0 + W, y0,                 # 右上
            x0 + W - 30, y0 + H,        # 右下斜角（右斜边长）
            x0, y0 + H,                 # 左下
        ]
        c.coords(self._placeholder_box_item, *pts)
        c.tag_raise(self._placeholder_item)
        c.tag_lower(self._placeholder_box_item, self._placeholder_item)
        # 左上角 P5 装饰图标
        try:
            if self._placeholder_icon_item is None:
                from PIL import Image, ImageTk
                _icon = Image.open(str(Path(__file__).resolve().parent / "assets" / "placeholder_icon.png"))
                _icon = _icon.convert("RGBA").resize((48, 48), Image.LANCZOS)
                self._placeholder_icon_img = ImageTk.PhotoImage(_icon)
                self._placeholder_icon_item = c.create_image(
                    x0 - 16, y0 - 14, anchor="nw", image=self._placeholder_icon_img)
            else:
                c.coords(self._placeholder_icon_item, x0 - 16, y0 - 14)
                c.itemconfig(self._placeholder_icon_item, state="normal")
            c.tag_raise(self._placeholder_icon_item)
        except Exception:
            pass

    def _show_frame_photo(self, photo):
        """在画布中央显示一帧视频（覆盖背景图案）。"""
        c = self.video_canvas
        w, h = c.winfo_width(), c.winfo_height()
        cx, cy = (w // 2) or PREVIEW_W // 2, (h // 2) or PREVIEW_H // 2
        if self._frame_item is None:
            self._frame_item = c.create_image(cx, cy, image=photo)
        else:
            c.itemconfig(self._frame_item, image=photo)
            c.coords(self._frame_item, cx, cy)
        if self._placeholder_item is not None:
            c.itemconfig(self._placeholder_item, state="hidden")

    def _make_ctrl_icon(self, kind, text="", bg="#2A2A2A", fg="#FFFFFF", icon_fg=None,
                        w=124, h=38, slant=20):
        """P5 风格按钮图：右低左高的不规则平行四边形（黑描边，左侧边短、右上直角）+ 图标 + 文字一体。

        kind: play/pause/stop；text 为按钮文字；bg 底色、fg 文字色、icon_fg 图标色。
        背景填面板深色（与按钮所在行融合，避免 tk 透明通道显示黑块）。
        """
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            if icon_fg is None:
                icon_fg = fg
            img = Image.new("RGBA", (w, h), (13, 13, 13, 255))  # #0D0D0D 面板底
            d = ImageDraw.Draw(img)
            # 左短右长：左上大切角、左边上宽下窄、底边向右下倾斜、右缘近乎垂直
            pts = [(slant, 0), (w, 0), (w - 8, h), (6, h - 6)]
            d.polygon(pts, fill=bg, outline="#000000", width=2)
            # 图标（左侧）
            if kind == "play":
                d.polygon([(38, 10), (50, 19), (38, 28)], fill=icon_fg)
            elif kind == "pause":
                d.rectangle([37, 10, 42, 28], fill=icon_fg)
                d.rectangle([46, 10, 51, 28], fill=icon_fg)
            elif kind == "stop":
                d.rectangle([37, 11, 51, 27], fill=icon_fg)
            # 文字（得意黑）
            if text:
                try:
                    fp = Path(__file__).parent / "assets" / "fonts" / "SmileySans.ttf"
                    font = ImageFont.truetype(str(fp), 16)
                except Exception:
                    font = ImageFont.load_default()
                if kind == "text":
                    # 纯文字按钮：文字居中
                    try:
                        tw = d.textlength(text, font=font)
                    except Exception:
                        tw = len(text) * 16
                    d.text((w // 2, h // 2), text, font=font, fill=fg, anchor="mm")
                else:
                    d.text((58, h // 2), text, font=font, fill=fg, anchor="lm")
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _set_play_btn(self, text, state):
        self.play_icon = self._make_ctrl_icon("play", text, bg=C_PLAY, fg="#FFFFFF",
                                              icon_fg="#FFFFFF")
        self.play_btn.config(image=self.play_icon, state=state)

    def _set_pause_btn(self, text, state):
        self.pause_icon = self._make_ctrl_icon("pause", text, bg="#FFFFFF", fg=C_MAIN,
                                               icon_fg=C_MAIN)
        self.pause_btn.config(image=self.pause_icon, state=state)

    # ---- 加载 ----
    def _on_toggle_replaced(self):
        """勾选切换时：重新加载当前选中项。"""
        it = getattr(self, '_current_item', None)
        if it is not None:
            self.set_usm(it)

    def set_usm(self, it: MovieItem):
        self._current_item = it
        if not it.abs_path or not Path(it.abs_path).exists():
            # 未提取：仍允许点播放，播放时自动从 CPK 提取该单个文件
            self.usm_path = None
            self.mp4_path = None
            self.usm_var.set(it.name)
            self.mp4_var.set("（尚未提取，点击播放将自动从 CPK 提取）")
            self.preview_status.set("该过场尚未解包，点击「播放」将自动从 CPK 提取并预览。")
            self.play_btn.config(state=tk.NORMAL)
            self._show_placeholder("在左侧列表选择已解包的过场\n双击或点击「播放」即可预览")
            self._photo = None
            return
        self.usm_path = it.abs_path
        self.usm_var.set(it.name)
        # 勾选"播放替换后视频"且该过场已被替换：直接预览替换视频
        if (getattr(self, 'play_replaced_var', None) and self.play_replaced_var.get()
                and getattr(it, 'replaced_with', None) and Path(it.replaced_with).exists()):
            self.mp4_path = it.replaced_with
            self._audio_wav = None
            self.mp4_var.set(it.replaced_with)
            self.preview_status.set("正在播放替换后的视频（%s）" % Path(it.replaced_with).name)
            self.play_btn.config(state=tk.NORMAL)
            self._show_first_frame(it.replaced_with)
            return
        m2v = str(Path(it.abs_path).with_suffix(".m2v"))
        mp4 = str(Path(it.abs_path).with_suffix(".mp4"))
        first = mp4 if Path(mp4).exists() else (m2v if Path(m2v).exists() else None)
        if first:
            self.mp4_path = first
            self.mp4_var.set(first)
            self.preview_status.set("已就绪，点击「播放」预览（或双击列表行直接播放）。")
            self.play_btn.config(state=tk.NORMAL)
            self._show_first_frame(first)
        else:
            self.mp4_path = None
            self.mp4_var.set("（点击播放时自动生成 m2v，约 2 秒）")
            self.preview_status.set("点击「播放」或双击列表行即可预览（首次自动生成 m2v，约 2 秒）。")
            self.play_btn.config(state=tk.NORMAL)

    def load_and_play(self, it: MovieItem):
        """双击列表调用：自动提取（如未提取）→ 转换 → 播放。"""
        self.set_usm(it)
        self._ensure_extracted_then(self._do_load_and_play)

    def play_external_file(self, path: str, autoplay: bool = True):
        """直接预览任意视频文件（mp4/mkv/webm 等），用于「要替换的视频」预览。
        不走 USM 准备流程；音轨由 ffmpeg 从视频文件提取。
        autoplay=False 时只加载信息并显示首帧，不自动播放（用于启动恢复）。"""
        p = Path(path)
        if not p.exists():
            return
        self._current_item = None
        self.usm_path = None
        self.mp4_path = str(p)
        self.mp4_var.set(str(p))
        self.usm_var.set("（外部视频文件）")
        self._audio_wav = None
        if autoplay:
            self._start_playback()
        else:
            # 只加载不播放：探针信息 + 显示首帧，避免启动时突然开始放视频
            try:
                self._probe_mp4(str(p))
                dur = getattr(self, "_duration", 0.0) or 0.0
                try:
                    self.time_var.set("00:00 / %02d:%02d" % (int(dur) // 60, int(dur) % 60))
                except Exception:
                    pass
                self._show_first_frame(str(p))
            except Exception:
                pass
            self.play_btn.config(state=tk.NORMAL)
            self.preview_status.set("已恢复上次视频（未自动播放），点击「播放」预览。")

    def _do_load_and_play(self):
        it = self._current_item
        if not it or not it.abs_path:
            return
        self.usm_path = it.abs_path
        self.usm_var.set(it.name)
        self._do_play()

    # ---- 按需自动提取单个过场 ----
    def _ensure_extracted_then(self, callback):
        """确保当前过场已提取到本地；未提取则自动从 CPK 提取该单个文件，
        完成后设置 abs_path、刷新列表、设置 usm_path，再调用 callback。"""
        it = self._current_item
        if it is None:
            return
        if it.abs_path and Path(it.abs_path).exists():
            self.usm_path = it.abs_path
            self.usm_var.set(it.name)
            callback()
            return
        cpk_path = self.app._cpk_path
        entries = self.app._entries
        if not cpk_path or not entries:
            messagebox.showwarning(APP_TITLE, "请先导入 CPK 并加载列表。")
            return
        target = [e for e in entries if e.full_path == it.name]
        if not target:
            messagebox.showwarning(APP_TITLE, "在 CPK 中找不到该过场：%s" % it.name)
            return
        e = target[0]
        # 输出目录：优先用户设置的提取目录，否则默认 ASCII 目录
        out_dir = str(self.app._out_dir)
        if not out_dir:
            out_dir = r"C:\Temp\P5R_CutsceneTool\extracted"
        outdir = Path(out_dir)
        outdir.mkdir(parents=True, exist_ok=True)
        dst = outdir / e.full_path
        dst.parent.mkdir(parents=True, exist_ok=True)

        self.app.show_progress("提取过场", "正在从 CPK 提取 %s…" % e.filename)
        self.preview_status.set("正在从 CPK 提取 %s（%.1f MB）…" % (e.filename, e.size / 1024 / 1024))
        self.play_btn.config(state=tk.DISABLED)

        def fn(q):
            q.put(("progress", (0, 0, "正在打开 CPK 文件…")))
            with open(str(cpk_path), "rb") as fh:
                from cricodecs import cpk
                arch = cpk.load(fh)
                q.put(("progress", (1, 1, "正在提取 %s…" % e.filename)))
                data = arch.file_bytes(e.index)
                dst.write_bytes(data)
            return str(dst)

        def done(path):
            it.abs_path = path
            self.app.refresh_list()
            # 重新在列表中选中该条目（refresh_list 会重建列表、丢失选中）
            try:
                for idx, m in enumerate(self.app.movies):
                    if m.name == it.name:
                        self.app.movie_listbox.selection_set(idx)
                        self.app.movie_listbox.see(idx)
                        break
            except Exception:
                pass
            self.usm_path = path
            self.usm_var.set(it.name)
            self.app.set_status("已提取：%s" % path)
            self.play_btn.config(state=tk.NORMAL)
            callback()

        def on_error(msg):
            self.play_btn.config(state=tk.NORMAL)
            self.preview_status.set("提取失败：" + msg.splitlines()[0])

        self.app.run_task(fn, done, on_error=on_error)

    # ---- m2v 懒准备（VP9 直接预览，不转 MP4） ----
    def _prepare_m2v_then_play(self):
        if self._converting:
            return
        if not self.usm_path or not Path(self.usm_path).exists():
            messagebox.showwarning(APP_TITLE, "请先选择 USM 文件。")
            return
        self._converting = True
        self.preview_status.set("正在准备预览（解包 m2v + 音轨，约 2 秒）…")
        self.play_btn.config(state=tk.DISABLED)

        def fn(q):
            usm = self.usm_path
            from p5r_tool import keys as _keys
            key = getattr(self.app, "_shared_usm_key", None)
            if key is None:
                try:
                    key = _keys.resolve_usm_key(usm)
                    self.app._shared_usm_key = key
                except Exception:
                    key = None
            if not Path(usm).with_suffix(".m2v").exists():
                engine.extract_m2v_adx(usm, key=key)
            engine.extract_preview_audio(usm, key=key)
            return str(Path(usm).with_suffix(".m2v"))

        def done(path):
            self._converting = False
            jp_wav = Path(self.usm_path).with_suffix(".pv_jp.wav")
            wav = Path(self.usm_path).with_suffix(".pv.wav")
            # 双音轨动画优先日文轨（pv_jp.wav），否则英文/唯一轨（pv.wav）
            use = jp_wav if jp_wav.exists() else wav
            self.mp4_path = path
            self._audio_wav = str(use) if use.exists() else None  # 无音轨文件纯视频
            self.mp4_var.set(path)
            self.preview_status.set("预览就绪，开始播放…")
            self.play()

        def on_error(msg):
            self._converting = False
            self.play_btn.config(state=tk.NORMAL)
            self.preview_status.set("准备失败：" + msg.splitlines()[0])

        self.app.run_task(fn, done, on_error=on_error)

    # ---- 转换 ----
    def _convert_then_play(self):
        if self._converting:
            return
        if not self.usm_path or not Path(self.usm_path).exists():
            messagebox.showwarning(APP_TITLE, "请先选择 USM 文件。")
            return
        self._converting = True
        out = str(Path(self.usm_path).with_suffix(".mp4"))
        self.preview_status.set("正在转换 MP4（首次需 1~3 分钟）…")
        self.play_btn.config(state=tk.DISABLED)

        def fn(q):
            def prog(n, total, msg):
                q.put(("progress", (n, total, msg)))
            return engine.make_preview(self.usm_path, out, on_progress=prog)

        def done(path):
            self._converting = False
            self.mp4_path = str(path)
            self.mp4_var.set(str(path))
            self.preview_status.set("转换完成，开始播放…")
            self.play()

        def on_error(msg):
            self._converting = False
            self.play_btn.config(state=tk.NORMAL)
            self.preview_status.set("转换失败：" + msg.splitlines()[0])

        self.app.run_task(fn, done, on_error=on_error)

    # ---- 播放控制 ----
    def _hwaccel_args(self) -> list:
        """d3d11va 硬解参数（GPU 解码：坏帧区由 ~1.8s 定格降为 ~1.2s，且解码速度数十倍于软解）。

        惰性探测一次：ffmpeg -hwaccels 包含 d3d11va 才启用，否则回退纯软解，
        保证在无 GPU / 旧驱动的机器上也能正常预览。
        """
        if self._hwaccel_ok is None:
            ok = False
            try:
                r = subprocess.run(["ffmpeg", "-hwaccels"],
                                   capture_output=True, text=True, timeout=10)
                ok = "d3d11va" in r.stdout
            except Exception:
                ok = False
            self._hwaccel_ok = ok
        if self._hwaccel_ok:
            return ["-hwaccel", "d3d11va", "-hwaccel_output_format", "nv12"]
        return []

    def play(self):
        """点播放：未提取则自动从 CPK 提取，再转换/播放。
        外部视频（待替换视频预览，无 _current_item）直接重播。"""
        if self._current_item is None and self.mp4_path and Path(self.mp4_path).exists():
            self._audio_wav = None
            self._start_playback()
            return
        self._ensure_extracted_then(self._do_play)

    def _do_play(self):
        # 勾选"播放替换后视频"且有替换视频：直接播放替换文件
        it = getattr(self, '_current_item', None)
        if (it and getattr(self, 'play_replaced_var', None) and self.play_replaced_var.get()
                and getattr(it, 'replaced_with', None) and Path(it.replaced_with).exists()):
            self.mp4_path = it.replaced_with
            self._audio_wav = None
            self._start_playback()
            return
        usm = self.usm_path
        if not usm or not Path(usm).exists():
            if self.mp4_path and Path(self.mp4_path).exists():
                self._start_playback()
            return
        m2v = Path(usm).with_suffix(".m2v")
        jp_wav = Path(usm).with_suffix(".pv_jp.wav")
        wav = Path(usm).with_suffix(".pv.wav")
        mp4 = Path(usm).with_suffix(".mp4")
        if m2v.exists():
            # m2v 就绪即播：双音轨动画优先日文轨（pv_jp.wav），无音轨直接纯视频
            self.mp4_path = str(m2v)
            use = jp_wav if jp_wav.exists() else wav
            self._audio_wav = str(use) if use.exists() else None
            self._start_playback()
            return
        try:
            kind = engine.probe_video_kind(usm)
        except Exception:
            kind = "unknown"
        if kind in ("vp9", "mpeg2"):
            self._prepare_m2v_then_play()   # 全部类型都走 m2v 直播（首次懒生成）
            return
        if mp4.exists():
            self.mp4_path = str(mp4)
            self._audio_wav = None
            self._start_playback()
            return
        self._convert_then_play()

    def pause_resume(self):
        if not self._playing:
            return
        try:
            import pygame
            if self._paused:
                pygame.mixer.music.unpause()
                self._paused = False
                self._set_pause_btn("暂停", self.pause_btn.cget("state"))
                self._schedule_tick()
            else:
                pygame.mixer.music.pause()
                self._paused = True
                self._set_pause_btn("继续", self.pause_btn.cget("state"))
                self._cancel_tick()
        except Exception:
            pass

    def stop(self):
        self._cancel_tick()
        self._teardown_playback()
        self._playing = False
        self._paused = False
        self._set_play_btn("播放", tk.NORMAL)
        self._set_pause_btn("暂停", tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)
        self._frames_displayed = 0
        self._last_shown = None
        self._dup_run = 0
        self._dup_waiting = False
        self.time_var.set("00:00 / %s" % _fmt_ts(self._duration))
        if self.mp4_path and Path(self.mp4_path).exists():
            self._show_first_frame(self.mp4_path)
        self.preview_status.set("已停止。")

    # ---- 内部实现 ----
    def _ensure_normalized_wav(self, wav: str) -> str | None:
        """高响度/削波音轨转响度归一化版本（loudnorm 限幅），缓存 .pv_norm.wav。

        部分 P5R 过场 HCA 解码后峰值 100% 满幅（如 MOV058 长过场音乐轨），
        pygame 直接播放会削波失真、听感"发炸/不对劲"。loudnorm 平衡响度并限幅到
        -1.5dBTP，消除失真。成功返回归一化路径；失败返回 None（调用方回退降音量）。
        """
        try:
            norm = str(Path(wav).with_suffix("")) + "_norm.wav"
            if Path(norm).exists() and Path(norm).stat().st_size > 1000:
                return norm
            self.preview_status.set("检测到高响度音轨，正在优化音频（首次需几秒）…")
            r = subprocess.run(
                [ffmpeg.FFMPEG, "-v", "error", "-y", "-i", wav,
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", norm],
                capture_output=True, timeout=300)
            if r.returncode == 0 and Path(norm).exists() and Path(norm).stat().st_size > 1000:
                return norm
        except Exception:
            pass
        return None

    def _probe_size(self, mp4: str):
        """ffprobe 读取视频实际分辨率（竖屏适配用）。"""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "default=noprint_wrappers=1", mp4],
                capture_output=True, text=True, timeout=15).stdout
            w = h = 0
            for line in out.splitlines():
                if line.startswith("width="):
                    w = int(line.split("=")[1])
                elif line.startswith("height="):
                    h = int(line.split("=")[1])
            self._v_w, self._v_h = w, h
        except Exception:
            self._v_w = self._v_h = 0

    def _fit_size(self):
        """在预览容器内等比缩放：保持宽高比，竖屏视频不再被压扁。"""
        vw, vh = self._v_w, self._v_h
        if vw <= 0 or vh <= 0:
            self._disp_w, self._disp_h = PREVIEW_W, PREVIEW_H
            return PREVIEW_W, PREVIEW_H
        scale = min(PREVIEW_W / vw, PREVIEW_H / vh)
        dw = max(2, int(round(vw * scale)))
        dh = max(2, int(round(vh * scale)))
        self._disp_w, self._disp_h = dw, dh
        return dw, dh

    def _probe_mp4(self, mp4: str):
        # m2v 直播：用容器头权威帧率/时长。
        # ffprobe 对 P5R 的 MPEG-1 电视画面流会报 2 倍帧率（59.94=29.97×2），
        # 必须从 IVF 头 / MPEG-1 序列头读真实值，否则播放快 2 倍。
        if mp4 and mp4.lower().endswith(".m2v"):
            try:
                fps_m, dur_m = engine.probe_m2v_fps(mp4)
                if fps_m:
                    self._fps = max(1.0, min(fps_m, 60.0))
                    self._duration = dur_m if dur_m else 0.0
                    self._total_frames = int(self._duration * self._fps) if self._duration else 0
                    return
            except Exception:
                pass
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate,duration,nb_frames,avg_frame_rate",
                 "-of", "default=noprint_wrappers=1", mp4],
                capture_output=True, text=True, timeout=30).stdout
            fps = 30.0
            dur = 0.0
            nb_frames = 0
            avg_fps = 0.0
            for line in out.splitlines():
                if line.startswith("r_frame_rate="):
                    num, _, den = line.split("=")[1].partition("/")
                    try:
                        n, d = float(num), float(den or "1")
                        if n > 0 and d > 0:
                            fps = n / d
                    except ValueError:
                        pass
                elif line.startswith("avg_frame_rate="):
                    num, _, den = line.split("=")[1].partition("/")
                    try:
                        n, d = float(num), float(den or "1")
                        if n > 0 and d > 0:
                            avg_fps = n / d
                    except ValueError:
                        pass
                elif line.startswith("duration="):
                    try:
                        dur = float(line.split("=")[1])
                    except ValueError:
                        pass
                elif line.startswith("nb_frames="):
                    try:
                        nb_frames = int(line.split("=")[1])
                    except ValueError:
                        pass
            # 用 nb_frames/duration 计算更准确的帧率（如果可用）
            if nb_frames > 0 and dur > 0:
                calc_fps = nb_frames / dur
                # 如果计算帧率与 r_frame_rate 差异大，优先用计算帧率
                if abs(calc_fps - fps) > 1.0:
                    fps = calc_fps
            # 帧率 sanity check：P5R 过场动画应为 29.97fps，异常范围自动校正
            if fps < 20.0 or fps > 60.0:
                self.preview_status.set(
                    "警告：检测到异常帧率 %.1f fps，已自动校正为 29.97fps。"
                    "建议删除旧 MP4 后重新导入 CPK 转换。" % fps)
                fps = 30000.0 / 1001.0  # 29.97 NTSC
            self._fps = max(1.0, min(fps, 60.0))
            self._duration = dur
            self._total_frames = nb_frames
        except Exception:
            self._fps = 30000.0 / 1001.0
            self._duration = 0.0
            self._total_frames = 0

    def _show_first_frame(self, mp4: str):
        """显示 MP4 第一帧（预览图）。"""
        try:
            self._probe_size(mp4)
            dw, dh = self._fit_size()
            from p5r_tool.ffmpeg import FFMPEG as _FF
            proc = subprocess.Popen(
                [_FF, "-v", "error", "-i", mp4,
                 "-frames:v", "1", "-vf", "scale=%d:%d" % (dw, dh),
                 "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=0x08000000)
            data = proc.stdout.read()
            proc.wait(timeout=30)
            if data:
                from PIL import Image, ImageTk
                img = Image.open(io.BytesIO(data)).convert("RGB")
                self._photo = ImageTk.PhotoImage(img)
                self._show_frame_photo(self._photo)
        except Exception:
            pass

    def _start_playback(self):
        self.stop()  # 先清理旧播放
        mp4 = self.mp4_path
        self._probe_mp4(mp4)

        # 准备音轨（wav 放 ASCII 工作区，cricodecs/ffmpeg 均需 ASCII 路径）
        import pygame
        self._wav_work = None
        self._wav_path = None
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            aud = getattr(self, "_audio_wav", None)
            if aud and Path(aud).exists():
                # pygame/SDL2 在 Windows 上不支持中文路径 → 复制到 ASCII 临时目录
                if all(ord(ch) < 128 for ch in str(aud)):
                    self._wav_path = aud
                else:
                    from p5r_tool.paths import WorkDir
                    self._wav_work = WorkDir()
                    import shutil as _sh
                    self._wav_path = str(self._wav_work.path / ("preview_%d.wav" % time.time_ns()))
                    _sh.copyfile(aud, self._wav_path)
            else:
                from p5r_tool.paths import WorkDir
                self._wav_work = WorkDir()
                self._wav_path = str(self._wav_work.path / ("preview_%d.wav" % time.time_ns()))
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", mp4,
                     "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", self._wav_path],
                    capture_output=True, timeout=120)
                if not Path(self._wav_path).exists() or Path(self._wav_path).stat().st_size == 0:
                    self._wav_path = None
        except Exception:
            self._wav_path = None

        # 启动帧解码管道（d3d11va 硬解优先，GPU 解码对坏帧区容错更好且解码极快；
        # rawvideo rgb24 避免 MJPEG 编解码开销；大队列 2 秒缓冲）
        self._probe_size(mp4)
        dw, dh = self._fit_size()
        self._frame_w, self._frame_h = dw, dh  # 锁定本段播放的管道帧尺寸
        self._frames = queue.Queue(maxsize=60)
        frame_size = dw * dh * 3
        self._ffproc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-threads", "auto"]
            + self._hwaccel_args()
            + ["-i", mp4,
               "-vf", "scale=%d:%d" % (dw, dh),
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=frame_size * 4)
        self._reader = FrameReader(self._ffproc, self._frames, frame_size)
        self._reader.start()
        self._frames_displayed = 0
        self._last_shown = None
        self._dup_run = 0
        self._dup_waiting = False
        self._play_start_time = time.time()

        # 播放音频
        self._audio_seek = 0.0
        try:
            if self._wav_path and Path(self._wav_path).exists():
                # 高响度/削波音轨 → 响度归一化缓存（loudnorm），失败回退降音量
                _vol = 1.0
                try:
                    if _wav_peak(self._wav_path) > 30000:
                        _norm = self._ensure_normalized_wav(self._wav_path)
                        if _norm and Path(_norm).exists():
                            self._wav_path = _norm
                        else:
                            _vol = 0.35  # 归一化失败：降音量播放兜底
                except Exception:
                    _vol = 0.35
                pygame.mixer.music.set_volume(_vol)
                pygame.mixer.music.load(self._wav_path)
                pygame.mixer.music.play()
        except Exception:
            pass

        self._playing = True
        self._paused = False
        self._set_play_btn("重播", tk.NORMAL)
        self._set_pause_btn("暂停", tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL)
        self.preview_status.set("播放中…（%s）" % Path(mp4).name)
        self._schedule_tick()

    def _schedule_tick(self):
        self._cancel_tick()
        # 高频调度 15ms：音频时钟驱动，每次检查是否需要显示下一帧
        self._tick_after = self.after(15, self._tick)

    def _cancel_tick(self):
        if self._tick_after is not None:
            try:
                self.after_cancel(self._tick_after)
            except Exception:
                pass
            self._tick_after = None

    def _tick(self):
        self._tick_after = None
        if not self._playing or self._paused:
            return

        # 获取音频时钟位置（优先 pygame，fallback 墙钟）；
        # _audio_seek 用于坏帧段恢复后音频重新 seek 的偏移补偿
        try:
            import pygame
            audio_pos_ms = pygame.mixer.music.get_pos()
            if audio_pos_ms < 0:
                raise ValueError
            audio_pos = self._audio_seek + audio_pos_ms / 1000.0
        except Exception:
            audio_pos = time.time() - self._play_start_time

        # 启动同步：音频时钟 < 50ms 时不显示视频帧，等音频真正开始播放
        if audio_pos < 0.05:
            self.time_var.set("%s / %s" % (_fmt_ts(audio_pos), _fmt_ts(self._duration)))
            self._schedule_tick()
            return

        # 计算目标帧数（+1 消除 off-by-one：audio_pos=0 时应显示第 0 帧）
        target_frame = int(audio_pos * self._fps) + 1

        # 从队列取帧，直到已显示帧数 >= 目标帧数（跳过落后的帧，保证音画同步）
        last_frame = None
        while self._frames_displayed < target_frame:
            try:
                frame = self._frames.get_nowait()
            except queue.Empty:
                break  # 队列空，等待 ffmpeg 解码
            if frame is None:
                self._on_playback_end()
                return
            self._frames_displayed += 1
            # 同步暂停：ffmpeg VP9 解码器对 CRI 源数据中末尾字节疑似 superframe
            # marker 的帧会误判，输出前帧副本（约 1-2 秒），导致"画面卡住而
            # 声音继续"。检测到连续 3+ 帧完全相同 → 判定为坏帧副本段 →
            # 暂停音频同步等待（画面保持当前帧），避免音画错位。
            if self._last_shown is not None and frame == self._last_shown:
                self._dup_run += 1
                if self._dup_run >= 3 and not self._dup_waiting:
                    # 视频末尾的自然静止段（最后几帧相同）不触发同步暂停
                    remaining = (self._total_frames - self._frames_displayed
                                 ) if self._total_frames > 0 else 999999
                    if remaining > 15:
                        self._dup_waiting = True
                        try:
                            import pygame
                            pygame.mixer.music.pause()
                        except Exception:
                            pass
                        self.preview_status.set("检测到视频解码异常，已同步暂停…")
                        break
                    self._dup_run = 0
            else:
                self._dup_run = 0
                self._last_shown = frame
                last_frame = frame

        # 同步暂停等待：消费坏帧副本直到视频恢复；恢复帧立即显示
        # （画面跳变，音频同步），随后按音频时钟继续，音画保持同步。
        if self._dup_waiting:
            try:
                nxt = self._frames.get_nowait()
            except queue.Empty:
                pass
            else:
                if nxt is None:
                    self._dup_waiting = False
                    self._on_playback_end()
                    return
                self._frames_displayed += 1
                if nxt != self._last_shown:
                    self._dup_waiting = False
                    self._dup_run = 0
                    self._last_shown = nxt
                    last_frame = nxt
                    try:
                        import pygame
                        # 音频重新 seek 到恢复帧对应时间，与画面完全同步
                        seek = self._frames_displayed / max(1.0, self._fps)
                        self._audio_seek = seek
                        self._play_start_time = time.time() - seek
                        pygame.mixer.music.play(start=seek)
                    except Exception:
                        pass
                    self.preview_status.set("播放中…（%s）" % Path(self.mp4_path).name)
            self._schedule_tick()
            return

        # 只显示最后取到的帧（中间跳过的帧不显示，减少 UI 更新开销）
        if last_frame is not None:
            from PIL import Image, ImageTk
            # 用管道锁定尺寸重建；长度不符（换视频瞬间的旧帧）直接丢弃，避免崩溃
            if len(last_frame) == self._frame_w * self._frame_h * 3:
                img = Image.frombytes("RGB", (self._frame_w, self._frame_h), last_frame)
                self._photo = ImageTk.PhotoImage(img)
                self._show_frame_photo(self._photo)

        # 进度显示
        self.time_var.set("%s / %s" % (_fmt_ts(audio_pos), _fmt_ts(self._duration)))

        # 结束检测：视频帧已取完；有音轨时等音频自然播完再结束（避免截断）
        if self._reader and not self._reader.is_alive() and self._frames.empty():
            if self._wav_path and Path(self._wav_path).exists():
                try:
                    import pygame
                    if pygame.mixer.music.get_busy():
                        self._schedule_tick()
                        return
                except Exception:
                    pass
            self._on_playback_end()
            return
        self._schedule_tick()

    def _on_playback_end(self):
        self._cancel_tick()
        self._teardown_playback()
        self._playing = False
        self._paused = False
        self._set_play_btn("重播", tk.NORMAL)
        self._set_pause_btn("暂停", tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)
        self.time_var.set("%s / %s" % (_fmt_ts(self._duration), _fmt_ts(self._duration)))
        self.preview_status.set("播放结束。可再次点击「播放」重播。")

    def _teardown_playback(self):
        try:
            import pygame
            pygame.mixer.music.stop()
            pygame.mixer.music.set_volume(1.0)  # 恢复削波降量
        except Exception:
            pass
        if self._ffproc:
            try:
                self._ffproc.kill()
            except Exception:
                pass
            self._ffproc = None
        if self._reader:
            self._reader = None
        # 清空帧队列
        try:
            while True:
                self._frames.get_nowait()
        except queue.Empty:
            pass
        # 清理临时 wav 工作区
        if self._wav_work:
            try:
                self._wav_work.cleanup()
            except Exception:
                pass
            self._wav_work = None
        self._wav_path = None

    def on_close(self):
        """窗口关闭时清理播放资源。"""
        self._cancel_tick()
        self._teardown_playback()


# ======================================================================
# 替换面板
# ======================================================================
class ReplacePanel(tk.Frame):
    def __init__(self, master, app: App):
        super().__init__(master)
        self.app = app

        # ---- 输出设置（UI 已精简：名称/简介/输出目录均在生成时弹窗输入）----
        self.mod_name_var = tk.StringVar(value="我的过场替换")
        self.mod_desc_var = tk.StringVar(value="")
        self.mod_root_var = tk.StringVar(value=str(Path.cwd() / "P5R_Mods"))
        self.use_orig_br = tk.BooleanVar(value=False)
        self.open_mod_after = tk.BooleanVar(value=True)
        self.pack(fill=tk.X, pady=(0, 8))

    def build(self):
        # 源过场 = 所有已标记替换（replaced_with）的过场，一次性全部打包进同一 Mod。
        # 用户标记几个过场，就一次生成包含几个替换的 Mod。
        marked = [m for m in self.app.movies
                  if m.replaced_with and Path(m.replaced_with).exists()]
        if not marked:
            messagebox.showwarning(APP_TITLE,
                                   "还没有标记任何替换。\n"
                                   "请先选中过场 → 导入视频 → 点「替换所选视频」标记要替换的过场（可标记多个）。")
            return
        mod_root = self.mod_root_var.get().strip()
        mod_name = self.mod_name_var.get().strip()
        if not mod_root:
            messagebox.showwarning(APP_TITLE, "请填写 Mod 输出目录（② 步骤）。")
            return
        if not mod_name:
            mod_name = "P5R_Cutscene_Mod"
        # 记住本次 mod 名称，下次启动自动恢复
        _cfg_tmp = _load_cfg()
        if _cfg_tmp.get("last_mod_name") != mod_name:
            _cfg_tmp["last_mod_name"] = mod_name
            _save_cfg(_cfg_tmp)

        Path(mod_root).mkdir(parents=True, exist_ok=True)
        ivf_cache: dict = {}
        cache_dir = Path(mod_root) / ".p5r_cache"
        n = len(marked)
        TOTAL = n * 6 + 1

        # Tk 变量只能在主线程读取，这里先取好值传给后台线程
        use_orig = self.use_orig_br.get()

        self.app.show_progress("生成 Mod", "正在准备…（转码 + 加密 + 打包）")

        def fn(q):
            import concurrent.futures as _cf
            import threading as _th
            def prog(stage_n, total, msg):
                q.put(("progress", (stage_n, total, msg)))
            # 多视频并行转码：最多 2 个同时跑，每个视频分片数减半，避免 CPU 超载
            n_workers = max(1, min(len(marked), 2))
            jobs_per = max(2, ((os.cpu_count() or 8) // 2) // n_workers) if n_workers > 1 else 0
            built_list: list[tuple] = []
            _lock = _th.Lock()

            def one(it, i):
                video = Path(it.replaced_with)
                work_usm = Path(mod_root) / ("_build_" + Path(it.abs_path).stem + ".usm")
                if not video.exists():
                    q.put(("progress", (-1, TOTAL, "[%d/%d] 跳过 %s：视频不存在 %s"
                                     % (i + 1, n, it.name, video))))
                    return

                def p2(stage_n, total, msg):
                    if stage_n < 0:
                        prog(-1, TOTAL, "[%d/%d] %s | %s" % (i + 1, n, it.name, msg))
                    else:
                        prog(i * 6 + stage_n, TOTAL, "[%d/%d] %s | %s" % (i + 1, n, it.name, msg))

                opts = engine.BuildOptions(use_original_bitrate=use_orig,
                                           transcode_jobs=jobs_per)
                built, _info = engine.build_usm(it.abs_path, video, work_usm, opts,
                                                on_progress=p2, ivf_cache=ivf_cache,
                                                cache_dir=cache_dir)
                with _lock:
                    built_list.append((built, it.name))

            with _cf.ThreadPoolExecutor(max_workers=n_workers) as ex:
                futs = [ex.submit(one, it, i) for i, it in enumerate(marked)]
                for f in futs:
                    f.result()
            q.put(("progress", (n * 6, TOTAL, "组装 Mod 目录…")))
            # CPK 目录名必须与游戏实际加载的过场包一致（P5R 为 MOVIE_JE.CPK），
            # 否则 Reloaded/P5REssentials 覆盖不到游戏文件，替换不生效。
            _cpk_name = Path(self._cpk_path).name if getattr(self, "_cpk_path", None) else "MOVIE_JE.CPK"
            mod_dir = engine.build_mod(built_list, mod_root, mod_name,
                                       cpk_name=_cpk_name,
                                       description=self.mod_desc_var.get().strip() or
                                       "P5R 过场动画替换：共 %d 个过场" % len(built_list))
            return mod_dir

        def done(mod_dir):
            try:
                for m in marked:
                    wu = Path(mod_root) / ("_build_" + Path(m.abs_path).stem + ".usm")
                    wu.unlink(missing_ok=True)
            except OSError:
                pass
            for it in marked:
                it.mod_made = True
            self.app.refresh_list()
            names = "、".join(Path(m.name).name for m in marked)
            self.app.set_status("Mod 已生成（%d 个过场）：%s" % (len(marked), mod_dir))
            r2dir = _find_reloaded_mods_dir()
            installed_here = r2dir and Path(mod_dir).parent.resolve() == Path(r2dir).resolve()
            # 自动启用：把本 mod 的 ModId 写入游戏目录 mod.json 的 EnabledMods
            enabled_note = ""
            try:
                cfg = json.loads((Path(mod_dir) / "ModConfig.json").read_text(encoding="utf-8"))
                mod_id = cfg.get("ModId", "")
            except Exception:
                mod_id = ""
            game = engine.find_p5r_game_dir()
            auto_enabled = False
            if mod_id and game:
                try:
                    engine.enable_mod_in_reloaded(game, mod_id)
                    auto_enabled = True
                except Exception:
                    auto_enabled = False
            if installed_here:
                msg = ("Mod 已直接安装到 Reloaded-II：\n%s\n\n包含 %d 个过场替换：\n%s\n\n"
                       "%s\n\n"
                       "重启 Reloaded-II（或点左侧「刷新」按钮）即可在 Mods 列表看到本 Mod。\n"
                       "完成后可点工具顶部「启动游戏（带 Mod）」直接开玩。") % (
                           mod_dir, len(marked), names,
                           ("已自动启用（写入 %s\\mod.json）" % game) if auto_enabled else
                           ("未检测到游戏目录，请在 Reloaded-II 中勾选启用本 Mod。"))
            else:
                msg = ("Mod 已生成：\n%s\n\n包含 %d 个过场替换：\n%s\n\n"
                       "安装方法：\n"
                       "1. 打开 Reloaded-II，点击 P5R 的「Open Mods Folder」\n"
                       "2. 把 %s 文件夹复制进 Mods 目录\n"
                       "%s\n"
                       "3. 重启 Reloaded-II 勾选本 Mod，点工具「启动游戏（带 Mod）」开玩") % (
                           mod_dir, len(marked), names, Path(mod_dir).name,
                           ("（已自动启用：%s\\mod.json）" % game) if auto_enabled else
                           "（未检测到游戏目录，需在 Reloaded-II 中手动勾选）")
            messagebox.showinfo(APP_TITLE, msg)
            if self.open_mod_after.get():
                try:
                    os.startfile(str(mod_dir))  # type: ignore[attr-defined]
                except OSError:
                    subprocess.Popen(["explorer", str(mod_dir)])

        self.app.run_task(fn, done)


def main():
    import sys as _sys, os as _os
    try:
        _ldir = _os.path.dirname(_sys.executable) if getattr(_sys, 'frozen', False) else '.'
        with open(_os.path.join(_ldir, 'diag.log'), 'w', encoding='utf-8') as _lf:
            _lf.write(f"frozen: {getattr(_sys, 'frozen', False)}\n")
            _lf.write(f"executable: {_sys.executable}\n")
            _lf.write(f"cwd: {_os.getcwd()}\n")
            try:
                import cricodecs
                _lf.write(f"cricodecs: {cricodecs.__file__}\n")
                from cricodecs import usm
                _lf.write("cricodecs.usm import: OK\n")
            except Exception as e:
                _lf.write(f"cricodecs FAILED: {e}\n")
    except Exception:
        pass

    app = App()
    # 注意：必须在 App()（根窗口）创建之后再建 Style，否则 ttk.Style()
    # 会自动新建一个可见的默认根窗口（标题 "tk"），导致启动时弹出空白窗口。
    try:
        style = ttk.Style(app)
        # default 主题：TScrollbar 为可配色的细条（clam 26px 过宽、vista 忽略自定义色）
        style.theme_use("default")
        # 主题切换后再配置 P5 滚动条样式（App 构造时配置会被 vista 覆盖）
        App._configure_p5_scrollbar_style()
    except Exception:
        pass

    # 初始化 pygame 音频（用于内嵌视频预览的音轨播放）
    try:
        import pygame
        pygame.mixer.init()
    except Exception:
        pass

    def on_close():
        try:
            app.panel_preview.on_close()
        except Exception:
            pass
        try:
            app.panel_replace_video.on_close()
        except Exception:
            pass
        app.destroy()

    app.protocol("WM_DELETE_WINDOW", on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
