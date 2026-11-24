# -*- coding: utf-8 -*-
"""TheCompressor -- 批量视频压缩
用显卡硬件编码器的恒定质量模式压缩视频：
NVIDIA 走 NVENC（vbr + cq），Intel 走 QSV（global_quality）。
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import subprocess
import threading
import tempfile

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
except Exception:  # 拖放库缺失时降级为纯按钮
    TkinterDnD = None
    DND_FILES = None

APP_NAME = "TheCompressor"
DEMO_SCALE = None
FORCE_VENDOR = None      # 调试用：强制按某个厂商构建命令
CONFIG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
# 旧版配置目录，首次读取时迁移
LEGACY_CONFIG_PATH = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                  "NVCompressor", "config.json")

VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".ts", ".m2ts", ".mts",
    ".m4v", ".webm", ".mpg", ".mpeg", ".3gp", ".rmvb", ".rm", ".vob", ".asf",
    ".f4v", ".ogv", ".divx", ".mxf", ".dat", ".trp", ".tp", ".m2t", ".h264",
    ".h265", ".hevc", ".264", ".av1", ".ivf",
}

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

AUDIO_MP4_OK = {"aac", "mp3", "ac3", "eac3", "alac", "mp2"}


# ---------------------------------------------------------------- ffmpeg 定位

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg():
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(getattr(sys, "_MEIPASS", ""), "ffmpeg.exe"))
    cands.append(os.path.join(app_dir(), "ffmpeg.exe"))
    cands.append(os.path.join(app_dir(), "bin", "ffmpeg.exe"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG = find_ffmpeg()


def human_size(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit in ("B", "KB") else ("%.2f %s" % (n, unit))
        n /= 1024.0


def human_time(sec):
    if sec is None or sec <= 0:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


# ---------------------------------------------------------------- 探测

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\d+:\d+.*?: Video: (.*)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+)")
_SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_PIX_RE = re.compile(r"(yuv\w+|p0?\d+le|nv12|rgb\w*)")
_COLOR_RE = re.compile(r"\((?:tv|pc|limited|full)?,?\s*([a-z0-9]+)/([a-z0-9]+)/([a-z0-9]+)\)")


def probe(path):
    """用 ffmpeg -i 的输出解析时长/分辨率/像素格式/音频编码。"""
    info = {"duration": 0.0, "width": 0, "height": 0, "pix_fmt": "",
            "tenbit": False, "audio": "", "vcodec": "", "color": None}

    err = b""
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        err = p.stderr or b""
    except Exception:
        return info
    text = err.decode("utf-8", "ignore")

    m = _DUR_RE.search(text)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    vm = _VIDEO_RE.search(text)
    if vm:
        line = vm.group(1)
        info["vcodec"] = line.split()[0].strip(",")
        sm = _SIZE_RE.search(line)
        if sm:
            info["width"], info["height"] = int(sm.group(1)), int(sm.group(2))
        pm = _PIX_RE.search(line)
        if pm:
            info["pix_fmt"] = pm.group(1)
            info["tenbit"] = bool(re.search(r"10le|10be|p010|p016", line))

    cm = _COLOR_RE.search(text)
    if cm:
        info["color"] = (cm.group(1), cm.group(2), cm.group(3))

    am = _AUDIO_RE.search(text)
    if am:
        info["audio"] = am.group(1)
    return info


# ---------------------------------------------------------------- 显卡检测

VENDOR_LABEL = {"nvidia": "NVENC", "intel": "QSV", "amd": "AMF", "cpu": "CPU"}
# 同一台机器并存时的优先顺序：独显优先，Intel 核显兜底
VENDOR_ORDER = ("nvidia", "intel", "amd")

_GPU_CACHE = {}


def _list_gpu_names():
    """用 wmic / PowerShell 列出显卡名，失败返回空表。"""
    cmds = [
        ["wmic", "path", "win32_VideoController", "get", "name"],
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
    ]
    for c in cmds:
        try:
            p = subprocess.run(c, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
            out = (p.stdout or b"").decode("utf-8", "ignore")
            names = [x.strip() for x in out.splitlines() if x.strip()]
            names = [n for n in names if n.lower() != "name"]
            if names:
                return names
        except Exception:
            continue
    return []


def detect_vendors(force=None):
    """返回 (可用的厂商列表, 选中的厂商, 显卡显示名)。"""
    if _GPU_CACHE.get("key") == force and "val" in _GPU_CACHE:
        return _GPU_CACHE["val"]
    names = _list_gpu_names()
    found = {"nvidia": [], "intel": [], "amd": []}
    for n in names:
        low = n.lower()
        if "nvidia" in low or "geforce" in low or "quadro" in low or "rtx" in low:
            found["nvidia"].append(n)
        elif "intel" in low or "arc" in low or "iris" in low or "uhd" in low:
            found["intel"].append(n)
        elif "amd" in low or "radeon" in low:
            found["amd"].append(n)

    avail = [v for v in VENDOR_ORDER if found[v]]
    if force in VENDOR_LABEL:
        chosen = force
    else:
        chosen = avail[0] if avail else "nvidia"
    disp = (found.get(chosen) or [""])[0] or "未检测到支持的显卡"
    val = (avail, chosen, disp, names)
    _GPU_CACHE["key"] = force
    _GPU_CACHE["val"] = val
    return val


def encoder_selftest(vendor):
    """用 lavfi 生成一帧画面做试编码，判断该厂商的编码器能否真的用起来。"""
    codec = "h264_qsv" if vendor == "intel" else "h264_nvenc"
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=size=320x240:rate=30", "-t", "0.4",
           "-c:v", codec]
    if vendor == "intel":
        cmd += ["-global_quality", "21", "-pix_fmt", "nv12"]
    else:
        cmd += ["-preset", "p4", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"]
    cmd += ["-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=25)
        return p.returncode == 0, (p.stderr or b"").decode("utf-8", "ignore")[-400:]
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- 命令组装

# 输出分辨率选项：(显示名, 目标高度)
RES_OPTIONS = [("4K（2160p）", 2160), ("2K（1440p）", 1440),
               ("1080p", 1080), ("720p", 720), ("540p", 540),
               ("360p", 360), ("270p", 270)]
RES_LABELS = [x[0] for x in RES_OPTIONS]
RES_HEIGHT = dict(RES_OPTIONS)

# 编码速度档位：配置里只存抽象等级，具体参数按显卡厂商映射
PRESET_LEVELS = ("best", "rec", "bal", "fast")
PRESET_NVENC = {"best": "p7", "rec": "p6", "bal": "p5", "fast": "p4"}
PRESET_QSV = {"best": "veryslow", "rec": "slow", "bal": "medium", "fast": "fast"}
PRESET_LABELS = {
    "nvidia": ("p7 最慢·质量最好", "p6 推荐", "p5 平衡", "p4 快速"),
    "intel": ("最慢·质量最好", "推荐", "平衡", "快速"),
    "amd": ("最慢·质量最好", "推荐", "平衡", "快速"),
}


def target_size(info, target_h):
    """按原视频长宽比计算目标宽高，宽度取偶数。"""
    ow, oh = int(info["width"]), int(info["height"])
    tw = int(round(ow * target_h / oh))
    tw = max(2, tw - tw % 2)
    return tw, target_h


def build_cmd(src, dst, opt, info, use_hwaccel, safe=False):
    vendor = opt.get("vendor") or "nvidia"
    level = opt.get("preset") if opt.get("preset") in PRESET_LEVELS else "rec"

    if vendor == "intel":
        codec = "hevc_qsv" if opt["codec"] == "hevc" else "h264_qsv"
        hw_args = ["-hwaccel", "qsv"]
    else:
        codec = "hevc_nvenc" if opt["codec"] == "hevc" else "h264_nvenc"
        hw_args = ["-hwaccel", "cuda"]

    cmd = [FFMPEG, "-hide_banner", "-y"]
    if use_hwaccel:
        cmd += hw_args
    cmd += ["-i", src, "-map", "0:v:0", "-map", "0:a?"]
    if dst.lower().endswith(".mkv"):
        cmd += ["-map", "0:s?", "-c:s", "copy"]

    if vendor == "intel":
        # QSV 的恒定质量：ICQ 模式，用 global_quality 表达「质量等级」
        cmd += ["-c:v", codec,
                "-preset", PRESET_QSV[level],
                "-global_quality", str(opt["cq"])]
        if not safe:
            cmd += ["-look_ahead", "1"]
    else:
        cmd += ["-c:v", codec,
                "-preset", (PRESET_NVENC["fast"] if safe else PRESET_NVENC[level]),
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", str(opt["cq"]),
                "-b:v", "0",
                "-spatial_aq", "1"]
        if not safe:
            cmd += ["-temporal_aq", "1", "-multipass", "fullres", "-rc-lookahead", "32"]
        if codec == "hevc_nvenc" and not safe:
            cmd += ["-b_ref_mode", "middle"]

    qsv_no10 = (vendor == "intel" and codec == "h264_qsv")
    if info.get("tenbit") and not qsv_no10:
        if vendor == "intel":
            cmd += ["-pix_fmt", "p010le", "-profile:v", "main10"]
        else:
            cmd += ["-pix_fmt", "p010le", "-profile:v", "main10"]
    else:
        cmd += ["-pix_fmt", "nv12" if vendor == "intel" else "yuv420p"]
        if vendor == "intel":
            if codec == "hevc_qsv":
                cmd += ["-profile:v", "main"]
        else:
            cmd += ["-profile:v", ("main" if codec == "hevc_nvenc" else "high")]

    if info.get("color"):
        p, t, s = info["color"]
        cmd += ["-color_primaries", p, "-color_trc", t, "-colorspace", s]

    audio = info.get("audio", "")
    if opt["audio"] == "copy" and audio and (dst.lower().endswith(".mkv") or audio in AUDIO_MP4_OK) and audio not in ("pcm_s16le", "pcm_s24le"):
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]

    # 输出分辨率：不保持原分辨率时按长宽比缩放
    vf = []
    if not opt.get("keep_res", True) and info.get("width") and info.get("height"):
        tw, th = target_size(info, int(opt.get("res_height", 1080)))
        vf.append("scale=%d:%d:flags=lanczos" % (tw, th))
    if vf:
        cmd += ["-vf", ",".join(vf)]

    cmd += ["-map_metadata", "-1", "-map_chapters", "0"]
    if dst.lower().endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]

    cmd += ["-loglevel", "warning", "-progress", "pipe:1", "-nostats", dst]
    return cmd


def unique_path(dst):
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 2
    while True:
        cand = "%s (%d)%s" % (base, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1


def friendly_error(err, vendor="nvidia"):
    """把 ffmpeg 的原始 stderr 归纳成一句能看懂的话。"""
    low = (err or "").lower()
    tag = VENDOR_LABEL.get(vendor, "显卡")
    if any(k in low for k in ("could not open encoder", "open encode session",
                              "no capable devices", "cannot load",
                              "error initializing", "mfx", "d3d11",
                              "hardware device", "-1313558101")):
        return "%s 编码器初始化失败（多为显卡驱动过旧或显卡被禁用），可先更新驱动再试" % tag
    if "invalid argument" in low and ("qsv" in low or "nvenc" in low):
        return "%s 不支持当前这组编码参数，请更新显卡驱动或换 H.264 再试" % tag
    if "no space" in low:
        return "磁盘空间不足"
    if "permission" in low:
        return "输出目录没有写入权限"
    # 兜底：只取第一条有内容的报错行
    for ln in (err or "").splitlines():
        s = ln.strip()
        if s and "http" not in s.lower():
            return s[:160]
    return "未知错误"


# ---------------------------------------------------------------- 单个转码任务

class FileTask(object):
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.size = os.path.getsize(path) if os.path.exists(path) else 0
        self.duration = 0.0
        self.res = ""
        self.status = "待处理"
        self.out_path = ""
        self.out_size = 0

    def probe(self):
        info = probe(self.path)
        self.duration = info["duration"]
        if info["width"]:
            self.res = "%dx%d%s" % (info["width"], info["height"], " 10bit" if info["tenbit"] else "")
        self.info = info
        return info


class Transcoder(object):
    """在后台线程里按顺序处理文件列表。items 为 [(全局索引, FileTask), ...]"""

    def __init__(self, items, opt, on_event, on_progress):
        self.items = items
        self.opt = opt
        self.on_event = on_event
        self.on_progress = on_progress
        self.cancelled = False
        self.running = True
        self.proc = None
        self.start_ts = 0

    # -- 对外
    def cancel(self):
        self.cancelled = True
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    # -- 内部
    def _run(self):
        self.start_ts = time.time()
        ok_count = 0
        fail_count = 0
        total_in = 0
        total_out = 0
        total_video = 0.0

        for pos, (gi, task) in enumerate(self.items):
            if self.cancelled:
                self.on_event(("log", "已取消。"))
                break
            if not os.path.exists(task.path):
                task.status = "文件不存在"
                self.on_event(("row", gi))
                fail_count += 1
                continue

            self.on_event(("row", ("start", gi)))
            total_in += task.size
            out_path, ok, msg, used = self._encode_one(gi, task, len(self.items))
            task.status = "完成" if ok else "失败"
            if ok:
                ok_count += 1
                task.out_path = out_path
                task.out_size = os.path.getsize(out_path)
                total_out += task.out_size
                total_video += task.duration
                ratio = (1 - task.out_size / float(task.size)) * 100 if task.size else 0
                task.status = ("完成 省%.0f%%" % ratio) if ratio >= 0 else ("完成 增%.0f%%" % -ratio)
            else:
                fail_count += 1
                task.status = "失败" if msg != "已取消" else "已取消"
                self.on_event(("log", "✗ %s 失败：%s" % (task.name, msg), "err"))
            self.on_event(("row", gi))

        elapsed = time.time() - self.start_ts
        summary = {
            "ok": ok_count, "fail": fail_count, "elapsed": elapsed,
            "total_in": total_in, "total_out": total_out,
            "video_sec": total_video, "cancelled": self.cancelled,
        }
        self.on_event(("done", summary))

    def _encode_one(self, idx, task, n_total):
        info = getattr(task, "info", None)
        if not info:
            info = task.probe()

        codec_name = "H.265/HEVC" if self.opt["codec"] == "hevc" else "H.264"
        ext = ".mkv" if self.opt["container"] == "mkv" else ".mp4"
        stem = os.path.splitext(task.path)[0]
        dst = stem + "_cq%d%s" % (self.opt["cq"], ext)
        dst = unique_path(dst)

        res_txt = ""
        if not self.opt.get("keep_res", True) and info.get("width"):
            tw, th = target_size(info, int(self.opt.get("res_height", 1080)))
            res_txt = ", 缩放至 %dx%d" % (tw, th)
        self.on_event(("log", "→ %s  [%s, CQ%d%s, %s]" % (
            task.name, codec_name, self.opt["cq"], res_txt,
            os.path.basename(FFMPEG))))

        # 依次尝试：GPU 解码 → CPU 解码 → 兼容参数(老显卡)
        hw_on = bool(self.opt.get("hwaccel"))
        plan = [(hw_on, False), (False, False), (False, True)]
        if not hw_on:
            plan = [(False, False), (False, True)]

        SAFE_HINT = ("b_ref_mode", "not supported", "No capable devices",
                     "rate control", "multipass", "lookahead", "Invalid argument",
                     "unsupported")

        last_err = ""
        for attempt_no, (use_hw, safe) in enumerate(plan):
            if self.cancelled:
                return dst, False, "已取消", use_hw
            cmd = build_cmd(task.path, dst, self.opt, info, use_hw, safe)
            ok, err = self._run_ffmpeg(idx, task, n_total, cmd, info)
            if ok:
                if os.path.exists(dst) and os.path.getsize(dst) > 0:
                    if safe:
                        self.on_event(("log", "   （已使用兼容参数完成）", "dim"))
                    return dst, True, "", use_hw
                last_err = "输出文件为空"
            else:
                last_err = err
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except Exception:
                    pass
            if self.cancelled:
                break
            low = last_err.lower()
            if attempt_no == 0 and use_hw:
                self.on_event(("log", "   GPU 解码不适用，回退 CPU 解码重试…", "dim"))
                continue
            if attempt_no + 1 < len(plan) and any(k.lower() in low for k in SAFE_HINT):
                self.on_event(("log", "   当前显卡不支持部分高级参数，改用兼容参数重试…", "dim"))
                continue
            break
        if self.cancelled and os.path.exists(dst):
            try:
                os.remove(dst)
            except Exception:
                pass
        return dst, False, friendly_error(last_err, self.opt.get("vendor")), False

    def _run_ffmpeg(self, idx, task, n_total, cmd, info):
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
                bufsize=1,
            )
        except Exception as e:
            return False, "启动 ffmpeg 失败：%s" % e

        err_lines = []

        def drain():
            try:
                for line in self.proc.stderr:
                    line = line.rstrip()
                    if line:
                        err_lines.append(line)
                        if len(err_lines) > 200:
                            del err_lines[:-200]
            except Exception:
                pass

        threading.Thread(target=drain, daemon=True).start()

        dur = info.get("duration") or 0
        st = {"out_us": 0, "speed": 0.0, "fps": 0.0}
        last_log = 0.0
        last_pct = -1.0

        for raw in self.proc.stdout:
            if self.cancelled:
                break
            line = raw.strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key == "out_time_us" and val.isdigit():
                st["out_us"] = int(val)
            elif key == "out_time_ms" and val.isdigit():
                st["out_us"] = int(val)  # ffmpeg 的 out_time_ms 实际是微秒
            elif key == "speed":
                try:
                    st["speed"] = float(val.rstrip("x"))
                except Exception:
                    pass
            elif key == "fps":
                try:
                    st["fps"] = float(val)
                except Exception:
                    pass
            elif key == "progress" and val == "end":
                pass

            if dur > 0:
                t = st["out_us"] / 1000000.0
                pct = min(100.0, max(0.0, t / dur * 100.0))
                eta = (dur - t) / st["speed"] if st["speed"] > 0.05 else -1
                msg = "%.1f%%  %s / %s  %.1fx" % (pct, human_time(t), human_time(dur), st["speed"])
                if eta > 0:
                    msg += "  剩 %s" % human_time(eta)
                if pct - last_pct >= 0.0 and time.time() - last_log > 0.35:
                    last_pct = pct
                    last_log = time.time()
                    self.on_progress(idx, pct, msg, eta or -1, n_total)

        rc = self.proc.wait()
        if self.cancelled:
            return False, "已取消"
        if rc != 0:
            tail = "\n".join(err_lines[-6:]) if err_lines else "ffmpeg 返回码 %d" % rc
            return False, tail
        return True, ""


# ---------------------------------------------------------------- GUI

def make_icon(path):
    from PIL import Image, ImageDraw
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 背景圆角方块 + 渐变
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    c1, c2 = (32, 197, 168), (76, 118, 255)
    for y in range(S):
        k = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=(int(c1[0] + (c2[0] - c1[0]) * k),
                                        int(c1[1] + (c2[1] - c1[1]) * k),
                                        int(c1[2] + (c2[2] - c1[2]) * k), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([8, 8, S - 8, S - 8], radius=52, fill=255)
    img.paste(grad, (0, 0), mask)
    # 胶片条
    d.rounded_rectangle([44, 74, 212, 140], radius=10, fill=(20, 24, 34, 235))
    for i in range(4):
        x = 58 + i * 40
        d.rectangle([x, 86, x + 20, 128], fill=(255, 255, 255, 60))
    # 向下箭头
    d.rounded_rectangle([114, 146, 142, 196], radius=6, fill=(255, 255, 255, 240))
    d.polygon([(96, 188), (160, 188), (128, 222)], fill=(255, 255, 255, 240))
    img.save(path, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


class BigSlider(tk.Canvas):
    """画布自制水平滑块：像素坐标精确映射，DPI 缩放，圆点握柄。"""

    def __init__(self, parent, from_, to, variable, command=None,
                 ui_scale=1.0, bg_color="#15171c", trough_color="#23272f",
                 accent="#3ec8a6"):
        h = int(14 * ui_scale) + 2
        super().__init__(parent, height=h, bg=bg_color, highlightthickness=0,
                         bd=0, cursor="hand2")
        self._flex_canvas = True   # 自检标记：画布随宽度重绘，不算裁切
        self.lo, self.hi = from_, to
        self.variable = variable
        self.command = command
        self.trough_color = trough_color
        self.accent = accent
        self.r = int(9 * ui_scale)              # 握柄半径
        self.track_t = max(3, int(6 * ui_scale))  # 轨道厚度
        self._dragging = False
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Configure>", lambda _e: self.redraw())
        try:
            self.variable.trace_add("write", lambda *_a: self.redraw())
        except Exception:
            pass
        self.redraw()

    def _value_x(self):
        w = self.winfo_width()
        if w <= 2 * self.r:
            return self.r
        try:
            v = float(self.variable.get())
        except Exception:
            v = self.lo
        v = max(self.lo, min(self.hi, v))
        return self.r + (v - self.lo) / (self.hi - self.lo) * (w - 2 * self.r)

    def redraw(self):
        self.delete("all")
        w = self.winfo_width() or (2 * self.r + 40)
        cy = self.winfo_height() // 2
        # 轨道
        self.create_rectangle(self.r, cy - self.track_t // 2,
                              w - self.r, cy + self.track_t // 2,
                              fill=self.trough_color, outline="", tags="track")
        # 已选段（左侧高亮）
        x = self._value_x()
        self.create_rectangle(self.r, cy - self.track_t // 2, x,
                              cy + self.track_t // 2,
                              fill=self.accent, outline="", tags="done")
        # 握柄
        self.create_oval(x - self.r, cy - self.r, x + self.r, cy + self.r,
                         fill=self.accent, outline="#15171c", width=1)

    def _set_from_x(self, px):
        w = self.winfo_width()
        if w <= 2 * self.r:
            return
        ratio = (px - self.r) / (w - 2 * self.r)
        ratio = max(0.0, min(1.0, ratio))
        v = int(round(self.lo + ratio * (self.hi - self.lo)))
        v = max(self.lo, min(self.hi, v))
        if int(self.variable.get()) != v:
            self.variable.set(v)
        self.redraw()
        if self.command:
            self.command(v)

    def _on_press(self, e):
        self._dragging = True
        self._set_from_x(e.x)

    def _on_motion(self, e):
        if self._dragging:
            self._set_from_x(e.x)

    def _on_release(self, _e):
        self._dragging = False

    def _on_wheel(self, e):
        try:
            v = int(self.variable.get())
        except Exception:
            v = self.lo
        v += -1 if e.delta > 0 else 1
        v = max(self.lo, min(self.hi, v))
        self.variable.set(v)
        self.redraw()
        if self.command:
            self.command(v)


class App(object):
    BG = "#15171c"
    CARD = "#1d2027"
    CARD2 = "#23272f"
    TEXT = "#e7e9ee"
    MUTED = "#9aa2b1"
    ACCENT = "#3ec8a6"
    ACCENT2 = "#4c76ff"
    WARN = "#ff8a5c"

    def __init__(self, root):
        self.root = root
        self.tasks = []
        self.tr = None
        self.queue = queue.Queue()
        self.output_dirs = []
        self.cfg = self._load_cfg()
        self.vendor = self.cfg.get("vendor") or "auto"
        _avail, _chosen, _name, _names = detect_vendors(FORCE_VENDOR or (
            None if self.vendor == "auto" else self.vendor))
        self.vendor_eff = _chosen
        self.gpu_name = _name
        self.vendors_avail = _avail

        root.title(APP_NAME + " · 视频压缩器")
        root.geometry(self._initial_geometry())
        root.configure(bg=self.BG)
        ico = os.path.join(getattr(sys, "_MEIPASS", app_dir()), "app.ico")
        try:
            if os.path.exists(ico):
                root.iconbitmap(ico)
        except Exception:
            pass

        self._build_style()
        self._build_ui()
        self._apply_minsize()
        self.root.after(100, self._poll)
        self.log("就绪。内置 ffmpeg 已就绪。")
        if not self._ffmpeg_ok():
            self.log("⚠ 未找到可用的 ffmpeg，请确认程序完整性。")
        self.root.after(600, self._verify_encoder)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _verify_encoder(self):
        """启动后异步试编码一次，QSV/NVENC 不可用时自动换到另一个厂商。"""
        def work():
            ok, err = encoder_selftest(self.vendor_eff)
            self.queue.put(("enc_check", (self.vendor_eff, ok, err)))
        threading.Thread(target=work, daemon=True).start()

    def _apply_vendor(self, vendor):
        self.vendor_eff = vendor
        self.preset_labels = PRESET_LABELS.get(vendor, PRESET_LABELS["nvidia"])
        try:
            self.cb_preset.configure(values=self.preset_labels)
            self._sync_preset_label()
        except Exception:
            pass
        try:
            self.sub_label.configure(text=self._subtitle_text())
        except Exception:
            pass

    # ---------- 窗口尺寸 ----------
    # 设计基准：2000x1200 是在 225% 缩放的机器上定的物理像素。
    # 先折回 100% 缩放下的逻辑尺寸，再按目标机器实际缩放倍数换算成物理像素，
    # 这样在 100% 的 1080p 屏上窗口会自动变小，不会接近全屏。
    REF_SCALE = 2.25
    REF_W = 2000 / REF_SCALE
    REF_H = 1200 / REF_SCALE

    def _system_scale(self):
        if DEMO_SCALE:
            return DEMO_SCALE   # 演示/截图时强制缩放
        try:
            import ctypes
            dpi = ctypes.windll.user32.GetDpiForSystem()
            if dpi and dpi > 0:
                return dpi / 96.0
        except Exception:
            pass
        return 1.0

    def _ui_scale(self):
        """当前 DPI 相对 96 DPI 的缩放倍数（225% 缩放 → 2.25）。"""
        try:
            return self.root.winfo_fpixels("1p") / (96.0 / 72.0)
        except Exception:
            return 1.0

    def _work_area(self):
        try:
            import ctypes
            import ctypes.wintypes
            rc = ctypes.wintypes.RECT()
            SPI_GETWORKAREA = 0x0030
            ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                                       ctypes.byref(rc), 0)
            return rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top
        except Exception:
            return 0, 0, 1920, 1080

    def _initial_geometry(self, w=None, h=None):
        """按当前 DPI 缩放换算初始窗口尺寸，限制在工作区内并居中。"""
        if w is None:
            w = self.REF_W * self._system_scale()
        if h is None:
            h = self.REF_H * self._system_scale()
        x0, y0, aw, ah = self._work_area()
        w = min(w, max(720, aw - 40))
        h = min(h, max(600, ah - 40))
        x = x0 + max(0, (aw - w) // 2)
        y = y0 + max(0, (ah - h) // 2)
        return "%dx%d+%d+%d" % (int(w), int(h), x, y)

    # 参考 225% 下的最小窗口（四行布局，1060 为加高后的值），折回逻辑尺寸
    REF_MIN_W = 1270 / REF_SCALE
    REF_MIN_H = 1060 / REF_SCALE

    def _apply_minsize(self):
        """最小窗口按参考 DPI 下的尺寸随当前缩放换算，保证控件不被裁切。"""
        _, _, aw, ah = self._work_area()
        scale = self._system_scale()
        mw = min(self.REF_MIN_W * scale, aw - 20)
        mh = min(self.REF_MIN_H * scale, ah - 20)
        self.root.minsize(int(mw), int(mh))

    # ---------- 配置 ----------
    def _load_cfg(self):
        default = {"cq": 21, "codec": "h264", "preset": "rec", "container": "mp4",
                   "audio": "copy", "hwaccel": True, "keep_res": True,
                   "res_height": 1080, "last_dir": "", "vendor": "auto"}
        try:
            path = CONFIG_PATH
            if not os.path.exists(path) and os.path.exists(LEGACY_CONFIG_PATH):
                path = LEGACY_CONFIG_PATH   # 从旧版目录迁移设置
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            default.update({k: v for k, v in d.items() if k in default})
        except Exception:
            pass
        # 旧配置里 preset 存的是 NVENC 的 p1..p7，换算成抽象档位
        legacy = {"p7": "best", "p6": "rec", "p5": "bal", "p4": "fast",
                  "p3": "fast", "p2": "fast", "p1": "fast"}
        if default["preset"] not in PRESET_LEVELS:
            default["preset"] = legacy.get(str(default["preset"]), "rec")
        if default["vendor"] not in ("auto", "nvidia", "intel", "amd"):
            default["vendor"] = "auto"
        return default

    def _save_cfg(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _ffmpeg_ok(self):
        try:
            p = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                               capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)
            out = p.stdout or b""
            key = b"hevc_qsv" if self.vendor_eff == "intel" else b"hevc_nvenc"
            return key in out
        except Exception:
            return False

    # ---------- 样式 ----------
    def _build_style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=self.BG, foreground=self.TEXT,
                     fieldbackground=self.CARD2, bordercolor=self.CARD2,
                     lightcolor=self.CARD2, darkcolor=self.BG)
        st.configure("TLabel", background=self.BG, foreground=self.TEXT)
        st.configure("Card.TFrame", background=self.CARD)
        st.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED)
        st.configure("CardMuted.TLabel", background=self.CARD, foreground=self.MUTED)
        st.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Microsoft YaHei UI", 16, "bold"))
        st.configure("Sub.TLabel", background=self.BG, foreground=self.MUTED, font=("Microsoft YaHei UI", 9))
        st.configure("TButton", background=self.CARD2, foreground=self.TEXT,
                     bordercolor=self.CARD2, focusthickness=0, padding=(12, 6),
                     font=("Microsoft YaHei UI", 9))
        st.map("TButton", background=[("active", "#2c313b"), ("pressed", "#343a46")],
               foreground=[("disabled", "#5c636f")])
        st.configure("Accent.TButton", background=self.ACCENT, foreground="#06231c",
                     font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 9))
        st.map("Accent.TButton", background=[("active", "#55d8b8"), ("pressed", "#2eb593")],
               foreground=[("disabled", "#7b8a86")])
        st.configure("Stop.TButton", background="#3a2226", foreground="#ff8a94",
                     font=("Microsoft YaHei UI", 9), padding=(12, 8))
        st.map("Stop.TButton", background=[("active", "#4d2a30")])
        st.configure("TCombobox", fieldbackground=self.CARD2, background=self.CARD2,
                     foreground=self.TEXT, arrowcolor=self.TEXT, padding=(6, 4))
        st.map("TCombobox", fieldbackground=[("readonly", self.CARD2)],
               foreground=[("readonly", self.TEXT)], selectbackground=[("readonly", self.CARD2)],
               selectforeground=[("readonly", self.TEXT)])
        st.configure("TSpinbox", fieldbackground=self.CARD2, background=self.CARD2,
                     foreground=self.TEXT, arrowcolor=self.TEXT, bordercolor=self.CARD2)
        st.configure("Treeview", background=self.CARD, fieldbackground=self.CARD,
                     foreground=self.TEXT, rowheight=int(17 * self._ui_scale()), borderwidth=0,
                     font=("Microsoft YaHei UI", 9))
        st.configure("Treeview.Heading", background=self.CARD2, foreground=self.MUTED,
                     relief="flat", font=("Microsoft YaHei UI", 9, "bold"), padding=(6, 7))
        st.map("Treeview.Heading", background=[("active", "#2b303a")])
        st.map("Treeview", background=[("selected", "#2f3b52")], foreground=[("selected", self.TEXT)])
        # clam 的进度条元素有固定高度，改用 alt 元素以支持厚度
        try:
            st.element_create("Accent.trough", "from", "alt")
            st.element_create("Accent.pbar", "from", "alt")
            st.layout("Accent.Horizontal.TProgressbar", [
                ("Accent.trough", {"sticky": "nswe", "children": [
                    ("Accent.pbar", {"sticky": "nswe"})]})])
        except Exception:
            pass
        st.configure("Accent.Horizontal.TProgressbar", background=self.ACCENT,
                     troughcolor=self.CARD2, bordercolor=self.CARD2,
                     lightcolor=self.ACCENT, darkcolor=self.ACCENT,
                     thickness=int(12 * self._ui_scale()))
        st.configure("TCheckbutton", background=self.BG, foreground=self.TEXT)
        st.map("TCheckbutton", background=[("active", self.BG)])
        self._style = st

    # ---------- 自定义大对勾复选框 ----------
    def _make_checkbox(self, parent, text, variable, on_cmd):
        sc = self._ui_scale()
        box_size = int(16 * sc)
        f = tk.Frame(parent, bg=self.BG)
        wrap = tk.Frame(f, width=box_size, height=box_size, bg=self.CARD2)
        wrap.pack(side="left", anchor="w")
        wrap.pack_propagate(False)
        box = tk.Label(wrap, bg=self.CARD2, text="", cursor="hand2",
                       font=("Microsoft YaHei UI", int(9 * sc), "bold"))
        box._fixed_box = True   # 自检标记：定形容器内的对勾标签
        box.pack(fill="both", expand=True)
        lbl = tk.Label(f, text=text, bg=self.BG, fg=self.TEXT, cursor="hand2",
                       font=("Microsoft YaHei UI", 9))
        lbl.pack(side="left", padx=(int(8 * sc), 0))

        def refresh(*_):
            if variable.get():
                wrap.configure(bg=self.ACCENT)
                box.configure(bg=self.ACCENT, fg="#15171c", text="\u2713")
            else:
                wrap.configure(bg=self.CARD2)
                box.configure(bg=self.CARD2, fg=self.CARD2, text="")

        def toggle(_e=None):
            variable.set(not variable.get())
            refresh()
            on_cmd()

        for wgt in (wrap, box, lbl, f):
            wgt.bind("<Button-1>", toggle)
        refresh()
        return f

    # ---------- 界面 ----------
    def _build_ui(self):
        outer = tk.Frame(self.root, bg=self.BG)
        outer.pack(fill="both", expand=True, padx=18, pady=(16, 14))
        outer.columnconfigure(0, weight=1)

        # 头部
        head = tk.Frame(outer, bg=self.BG)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.sub_label = ttk.Label(head, text=self._subtitle_text(),
                                   style="Sub.TLabel")
        self.sub_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.gpu_label = ttk.Label(head, text="", style="Sub.TLabel")
        self.gpu_label.grid(row=0, column=1, rowspan=2, sticky="e")

        # 拖放区
        drop_h = int(64 * self._ui_scale()) + 12
        self.drop_canvas = tk.Canvas(outer, height=drop_h, bg=self.CARD, highlightthickness=0,
                                     bd=0, cursor="hand2")
        self.drop_canvas.grid(row=1, column=0, sticky="ew", pady=(14, 12))
        self.drop_canvas.bind("<Configure>", self._draw_drop_zone)
        self.drop_canvas.bind("<Button-1>", lambda e: self._pick_files())
        self.drop_canvas.bind("<Enter>", lambda e: self.drop_canvas.configure(bg=self.CARD2))
        self.drop_canvas.bind("<Leave>", lambda e: self.drop_canvas.configure(bg=self.CARD))
        self._drop_hot = False

        # 文件列表
        tree_wrap = tk.Frame(outer, bg=self.CARD)
        tree_wrap.grid(row=2, column=0, sticky="nsew")
        outer.rowconfigure(2, weight=3)
        self.tree = ttk.Treeview(tree_wrap, columns=("f", "dur", "size", "status"),
                                 show="headings", selectmode="extended")
        self.tree.heading("f", text="文件")
        self.tree.heading("dur", text="时长")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        sc = self._ui_scale()
        self.tree.column("f", width=int(360 * sc), anchor="w", stretch=True)
        self.tree.column("dur", width=int(80 * sc), anchor="center", stretch=False)
        self.tree.column("size", width=int(100 * sc), anchor="e", stretch=False)
        self.tree.column("status", width=int(150 * sc), anchor="w", stretch=False)
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground="#5fe0a8")
        self.tree.tag_configure("bad", foreground="#ff7b86")
        self.tree.tag_configure("run", foreground=self.ACCENT2)
        self.tree.bind("<Delete>", lambda e: self._remove_selected())

        # 设置区（四行：CQ、编码器/速度/容器、音频/选项、输出分辨率）
        opt = tk.Frame(outer, bg=self.BG)
        opt.grid(row=3, column=0, sticky="ew", pady=(12, 4))
        opt.columnconfigure(0, weight=1)
        self.opt_row1 = tk.Frame(opt, bg=self.BG)
        self.opt_row1.pack(fill="x")
        self.opt_row2 = tk.Frame(opt, bg=self.BG)
        self.opt_row2.pack(fill="x", pady=(10, 0))
        self.opt_row3 = tk.Frame(opt, bg=self.BG)
        self.opt_row3.pack(fill="x", pady=(10, 0))
        self.opt_row4 = tk.Frame(opt, bg=self.BG)
        self.opt_row4.pack(fill="x", pady=(10, 0))

        def _group(parent, label_text, factory):
            g = tk.Frame(parent, bg=self.BG)
            g.pack(side="left", anchor="w", padx=(0, 18))
            ttk.Label(g, text=label_text, style="Muted.TLabel").pack(anchor="w")
            wgt = factory(g)
            wgt.pack(anchor="w", fill="x")
            return wgt

        self.var_cq = tk.IntVar(value=self.cfg["cq"])
        self.cq_left = tk.Frame(self.opt_row1, bg=self.BG)
        self.cq_left.pack(side="left", anchor="w")
        cq_head = tk.Frame(self.cq_left, bg=self.BG)
        cq_head.pack(anchor="w")
        ttk.Label(cq_head, text="质量 CQ", style="Muted.TLabel").pack(side="left")
        self.lbl_cq = ttk.Label(cq_head, text=str(self.cfg["cq"]),
                                style="Muted.TLabel", font=("Consolas", 10, "bold"))
        self.lbl_cq.pack(side="left", padx=(8, 0))
        self.lbl_cq_tag = ttk.Label(cq_head, text="", style="Muted.TLabel")
        self.lbl_cq_tag.pack(side="left", padx=(10, 0))
        cq_cap = tk.Frame(self.cq_left, bg=self.BG)
        cq_cap.pack(anchor="w", pady=(3, 0))
        self.lbl_cq_q = ttk.Label(cq_cap, text="", style="Muted.TLabel")
        self.lbl_cq_q.pack(side="left")
        ttk.Label(cq_cap, text="·", style="Muted.TLabel").pack(side="left", padx=8)
        self.lbl_cq_s = ttk.Label(cq_cap, text="", style="Muted.TLabel")
        self.lbl_cq_s.pack(side="left")
        self.scale_cq = BigSlider(self.opt_row1, 16, 30, self.var_cq,
                                  command=self._on_cq_slide,
                                  ui_scale=self._ui_scale(), bg_color=self.BG,
                                  trough_color=self.CARD2, accent=self.ACCENT)
        self.scale_cq.pack(side="right", anchor="e", fill="x", expand=True,
                           padx=(int(16 * self._ui_scale()), 0))
        # 布局完成后锁定左侧文字区宽度，避免文案长短变化导致滑块长度跳动
        self.root.after(0, self._lock_cq_left)

        self.var_codec = tk.StringVar(value=self.cfg["codec"])
        self.cb_codec = _group(self.opt_row2, "编码器", lambda g: ttk.Combobox(
            g, textvariable=self.var_codec, state="readonly", width=12,
            values=("hevc", "h264")))
        self.cb_codec.bind("<<ComboboxSelected>>", lambda e: self._on_opt_change())

        self.var_preset = tk.StringVar(value=self.cfg["preset"])
        self.preset_labels = PRESET_LABELS.get(self.vendor_eff, PRESET_LABELS["nvidia"])
        self.cb_preset = _group(self.opt_row2, "编码速度", lambda g: ttk.Combobox(
            g, textvariable=self.var_preset, state="readonly", width=15,
            values=self.preset_labels))
        self._sync_preset_label()
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self._on_opt_change())

        self.var_container = tk.StringVar(value=self.cfg["container"])
        self.cb_container = _group(self.opt_row2, "输出容器", lambda g: ttk.Combobox(
            g, textvariable=self.var_container, state="readonly", width=8,
            values=("mp4", "mkv")))
        self.cb_container.bind("<<ComboboxSelected>>", lambda e: self._on_opt_change())

        self.var_audio = tk.StringVar(value=self.cfg["audio"])
        self.cb_audio = _group(self.opt_row3, "音频", lambda g: ttk.Combobox(
            g, textvariable=self.var_audio, state="readonly", width=14,
            values=("copy 直接复制", "aac 192k 重编码")))
        self.cb_audio.set("copy 直接复制" if self.cfg["audio"] == "copy" else "aac 192k 重编码")
        self.cb_audio.bind("<<ComboboxSelected>>", lambda e: self._on_opt_change())

        chk_frame = tk.Frame(self.opt_row3, bg=self.BG)
        chk_frame.pack(side="left", anchor="w", padx=(0, 18))
        ttk.Label(chk_frame, text=" ", style="Muted.TLabel").pack(anchor="w")
        self.var_hw = tk.BooleanVar(value=self.cfg["hwaccel"])
        self.chk_hw = self._make_checkbox(chk_frame, "GPU 解码加速",
                                          self.var_hw, self._on_opt_change)
        self.chk_hw.pack(anchor="w")

        # 第四行：输出分辨率
        res_chk_frame = tk.Frame(self.opt_row4, bg=self.BG)
        res_chk_frame.pack(side="left", anchor="w", padx=(0, 18))
        ttk.Label(res_chk_frame, text=" ", style="Muted.TLabel").pack(anchor="w")
        self.var_keep_res = tk.BooleanVar(value=self.cfg["keep_res"])
        self.chk_keep_res = self._make_checkbox(res_chk_frame, "保持原分辨率",
                                                self.var_keep_res, self._on_keep_res)
        self.chk_keep_res.pack(anchor="w")

        self.cb_res = _group(self.opt_row4, "输出分辨率", lambda g: ttk.Combobox(
            g, state="readonly", width=14, values=RES_LABELS))
        cur_label = next((lbl for lbl, hh in RES_OPTIONS
                          if hh == self.cfg["res_height"]), "1080p")
        self.cb_res.set(cur_label)
        self.cb_res.bind("<<ComboboxSelected>>", lambda e: self._on_opt_change())
        self._sync_res_state()

        # 操作区
        act = tk.Frame(outer, bg=self.BG)
        act.grid(row=4, column=0, sticky="ew", pady=(10, 6))
        act.columnconfigure(4, weight=1)

        self.btn_start = ttk.Button(act, text="开始转码", style="Accent.TButton", command=self._start)
        self.btn_start.grid(row=0, column=0, sticky="w")
        self.btn_stop = ttk.Button(act, text="停止", style="Stop.TButton", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(act, text="添加文件", command=self._pick_files).grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Button(act, text="清空列表", command=self._clear).grid(row=0, column=3, sticky="w", padx=(8, 0))

        self.lbl_sum = ttk.Label(act, text="", style="Muted.TLabel", anchor="e")
        self.lbl_sum.grid(row=0, column=4, columnspan=2, sticky="e")

        # 进度
        pbar_wrap = tk.Frame(outer, bg=self.BG)
        pbar_wrap.grid(row=5, column=0, sticky="ew", pady=(2, 6))
        pbar_wrap.columnconfigure(0, weight=1)
        self.pbar = ttk.Progressbar(pbar_wrap, style="Accent.Horizontal.TProgressbar",
                                    mode="determinate", maximum=100)
        self.pbar.grid(row=0, column=0, sticky="ew")
        self.lbl_prog = ttk.Label(pbar_wrap, text="空闲", style="Muted.TLabel", width=42, anchor="e")
        self.lbl_prog.grid(row=0, column=1, sticky="e", padx=(10, 0))

        # 日志
        log_wrap = tk.Frame(outer, bg=self.CARD)
        log_wrap.grid(row=6, column=0, sticky="nsew", pady=(0, 0))
        outer.rowconfigure(6, weight=2)
        self.log_text = tk.Text(log_wrap, bg="#101216", fg="#b9c2d0", insertbackground="#b9c2d0",
                                relief="flat", bd=0, wrap="word", height=8,
                                font=("Consolas", 9), padx=10, pady=8)
        lsb = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.log_text.tag_configure("err", foreground="#ff8f99")
        self.log_text.tag_configure("ok", foreground="#64e0ab")
        self.log_text.tag_configure("dim", foreground="#7b8494")

        # 拖放注册
        try:
            self.drop_canvas.drop_target_register(DND_FILES)
            self.drop_canvas.dnd_bind("<<Drop>>", self._on_drop)
            self.drop_canvas.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            self.drop_canvas.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._on_drop)
            self.tree.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            self.tree.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            self._dnd_ok = True
        except Exception as e:
            self._dnd_ok = False
            self.log("⚠ 拖放功能初始化失败：%s" % e)

        self._refresh_cq_rating()
        self._refresh_gpu_info()
        self._update_summary()

    def _draw_drop_zone(self, event=None):
        c = self.drop_canvas
        w = c.winfo_width()
        h = c.winfo_height()
        c.delete("all")
        col = self.ACCENT if self._drop_hot else "#39404d"
        c.create_rectangle(6, 6, w - 6, h - 6, outline=col, width=2, dash=(7, 5))
        c.create_text(w // 2, int(h * 0.30), text="把视频拖到这里",
                      fill=self.TEXT, font=("Microsoft YaHei UI", 13, "bold"))
        c.create_text(w // 2, int(h * 0.58), text="支持多文件 / 文件夹 · 或点击选择文件",
                      fill=self.MUTED, font=("Microsoft YaHei UI", 9))
        try:
            cq = int(self.var_cq.get())
        except Exception:
            cq = 21
        c.create_text(w // 2, int(h * 0.84), text="输出到原视频所在目录，文件名追加 _cq%d" % cq,
                      fill="#6c7585", font=("Microsoft YaHei UI", 8))

    def _on_drop_enter(self, event):
        self._drop_hot = True
        self._draw_drop_zone()

    def _on_drop_leave(self, event):
        self._drop_hot = False
        self._draw_drop_zone()

    def _on_drop(self, event):
        self._drop_hot = False
        self._draw_drop_zone()
        try:
            items = self.root.tk.splitlist(event.data)
        except Exception:
            items = [event.data]
        self._add_paths(items)

    # ---------- 文件列表 ----------
    def _pick_files(self):
        paths = filedialog.askopenfilenames(
            title="选择视频文件",
            initialdir=self.cfg.get("last_dir") or os.getcwd(),
            filetypes=[("视频文件", "*.mp4 *.mkv *.mov *.avi *.flv *.wmv *.ts *.m4v *.webm *.rmvb *.mpg *.mpeg *.m2ts *.mts *.3gp *.vob"),
                       ("所有文件", "*.*")])
        if paths:
            self._add_paths(list(paths))

    def _add_paths(self, paths):
        if self.tr and getattr(self.tr, "running", False):
            self.log("⚠ 正在转码，请先停止再添加文件。")
            return
        added = 0
        existing = {os.path.normcase(os.path.abspath(t.path)) for t in self.tasks}
        for p in paths:
            p = p.strip().strip('"')
            if not p or not os.path.exists(p):
                continue
            if os.path.isdir(p):
                files = []
                for root_, _, fs in os.walk(p):
                    for f in fs:
                        if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                            files.append(os.path.join(root_, f))
                for f in sorted(files):
                    if self._insert(f, existing):
                        added += 1
            elif os.path.isfile(p):
                if self._insert(p, existing):
                    added += 1
        if added:
            self.cfg["last_dir"] = os.path.dirname(os.path.abspath(paths[0]))
            self.log("已添加 %d 个文件，共 %d 个待处理。" % (added, len(self.tasks)))
            self._update_summary()
            self._probe_async()

    def _insert(self, path, existing):
        key = os.path.normcase(os.path.abspath(path))
        if key in existing:
            return False
        existing.add(key)
        t = FileTask(path)
        self.tasks.append(t)
        self.tree.insert("", "end", iid=str(len(self.tasks) - 1),
                         values=(t.name, "-", human_size(t.size), t.status))
        return True

    def _probe_async(self):
        pending = [i for i, t in enumerate(self.tasks) if not getattr(t, "info", None)]
        if not pending:
            return

        def work():
            for i in pending:
                t = self.tasks[i]
                try:
                    t.probe()
                except Exception:
                    pass
                self.queue.put(("probe", i))
        threading.Thread(target=work, daemon=True).start()

    def _refresh_row(self, i):
        t = self.tasks[i]
        tag = ()
        if t.status.startswith("完成"):
            tag = ("ok",)
        elif t.status.startswith("失败") or "不存在" in t.status:
            tag = ("bad",)
        elif t.status.startswith("转码") or t.status.startswith("取消"):
            tag = ("run",)
        self.tree.item(str(i), values=(t.name, human_time(t.duration) if t.duration else "-",
                                       human_size(t.size) if t.size else "-", t.status),
                       tags=tag)

    def _remove_selected(self):
        if self.tr and getattr(self.tr, "running", False):
            return
        sel = sorted((int(x) for x in self.tree.selection()), reverse=True)
        for i in sel:
            self.tree.delete(str(i))
            del self.tasks[i]
        # 重建 iid
        self.tree.delete(*self.tree.get_children())
        for i, t in enumerate(self.tasks):
            self.tree.insert("", "end", iid=str(i),
                             values=(t.name, human_time(t.duration) if t.duration else "-",
                                     human_size(t.size), t.status))
        self._update_summary()

    def _clear(self):
        if self.tr and getattr(self.tr, "running", False):
            return
        self.tasks = []
        self.tree.delete(*self.tree.get_children())
        self.pbar["value"] = 0
        self.lbl_prog.configure(text="空闲")
        self._update_summary()

    def _update_summary(self):
        n = len(self.tasks)
        total = sum(t.size for t in self.tasks)
        finished = [t for t in self.tasks if t.out_size]
        if finished:
            outs = sum(t.out_size for t in finished)
            ins = sum(t.size for t in finished)
            saved = (1 - outs / float(ins)) * 100 if ins else 0
        else:
            saved = 0
        txt = "%d 个文件 · %s" % (n, human_size(total))
        if finished:
            if saved >= 0:
                txt += " · 已压缩 %d 个，节省 %.0f%%" % (len(finished), saved)
            else:
                txt += " · 已压缩 %d 个，体积增大 %.0f%%" % (len(finished), -saved)
        self.lbl_sum.configure(text=txt)

    # ---------- 选项 ----------
    def _subtitle_text(self):
        tag = VENDOR_LABEL.get(self.vendor_eff, "NVENC")
        if self.cfg.get("codec") == "hevc":
            return "HEVC %s · 恒定质量模式" % tag
        return "H.264 %s · 恒定质量模式" % tag

    def _cq_rating(self, v):
        quality = {16: "极致", 17: "极高", 18: "超高", 19: "次高", 20: "标高",
                   21: "高", 22: "较高", 23: "中等", 24: "中等偏下", 25: "一般",
                   26: "偏低", 27: "较低", 28: "低", 29: "很低", 30: "最低"}
        size = {16: "最大", 17: "很大", 18: "偏大", 19: "稍大", 20: "中等偏大",
                21: "适中", 22: "适中", 23: "适中", 24: "偏小", 25: "较小",
                26: "略小", 27: "小", 28: "很小", 29: "极小", 30: "最小"}
        if v <= 16:
            tag = "极致画质"
        elif v <= 18:
            tag = "高画质"
        elif v <= 20:
            tag = "偏画质"
        elif v <= 23:
            tag = "均衡推荐"
        elif v <= 25:
            tag = "偏体积"
        elif v <= 27:
            tag = "小体积"
        else:
            tag = "最小体积"
        return quality.get(v, "中等"), size.get(v, "适中"), tag

    def _lock_cq_left(self):
        # 用最长文案测量所需宽度
        self.lbl_cq_tag.configure(text="均衡推荐")
        self.lbl_cq_q.configure(text="画质 中等偏下")
        self.lbl_cq_s.configure(text="体积 中等偏大")
        self.root.update_idletasks()
        w = self.cq_left.winfo_reqwidth() + int(6 * self._ui_scale())
        h = self.cq_left.winfo_reqheight()
        # 宽高都显式固定，否则禁用传播后框架高度会塌缩为 0
        self.cq_left.configure(width=w, height=h)
        self.cq_left.pack_propagate(False)
        self._refresh_cq_rating()

    def _refresh_cq_rating(self):
        try:
            v = int(self.var_cq.get())
        except Exception:
            v = 21
        q, s, tag = self._cq_rating(v)
        self.lbl_cq.configure(text=str(v))
        self.lbl_cq_tag.configure(text=tag)
        self.lbl_cq_q.configure(text="画质 " + q)
        self.lbl_cq_s.configure(text="体积 " + s)
        self.lbl_cq_tag.configure(
            foreground=self.ACCENT if tag == "均衡推荐" else self.MUTED)

    def _on_cq_slide(self, *_):
        try:
            v = int(round(float(self.var_cq.get())))
        except Exception:
            v = 21
        v = max(16, min(30, v))
        self.var_cq.set(v)
        self._on_opt_change()

    def _on_keep_res(self):
        self._sync_res_state()
        self._on_opt_change()

    def _sync_res_state(self):
        self.cb_res.configure(state="disabled" if self.var_keep_res.get()
                              else "readonly")

    def _sync_preset_label(self):
        """把配置里的抽象档位显示成当前显卡对应的文案。"""
        level = self.cfg.get("preset")
        if level not in PRESET_LEVELS:
            level = "rec"
        idx = PRESET_LEVELS.index(level)
        try:
            self.cb_preset.set(self.preset_labels[idx])
        except Exception:
            pass

    def _preset_level(self):
        cur = self.var_preset.get()
        try:
            return PRESET_LEVELS[self.preset_labels.index(cur)]
        except ValueError:
            return "rec"

    def _on_opt_change(self, *_):
        try:
            self.cfg["cq"] = max(16, min(30, int(self.var_cq.get())))
        except Exception:
            self.cfg["cq"] = 21
            self.var_cq.set(21)
        self._refresh_cq_rating()
        self.cfg["codec"] = self.var_codec.get() or "h264"
        self.cfg["preset"] = self._preset_level()
        self.cfg["container"] = self.var_container.get() or "mp4"
        self.cfg["audio"] = "copy" if self.var_audio.get().startswith("copy") else "aac"
        self.cfg["hwaccel"] = bool(self.var_hw.get())
        self.cfg["keep_res"] = bool(self.var_keep_res.get())
        self.cfg["res_height"] = RES_HEIGHT.get(self.cb_res.get(), 1080)
        self.sub_label.configure(text=self._subtitle_text())
        self._draw_drop_zone()
        self._save_cfg()

    def _refresh_gpu_info(self):
        def work():
            try:
                _avail, chosen, name, names = detect_vendors(FORCE_VENDOR or (
                    None if self.vendor == "auto" else self.vendor))
            except Exception:
                chosen, name = self.vendor_eff, self.gpu_name
            txt = name
            tag = VENDOR_LABEL.get(chosen, "")
            if tag:
                txt = "%s · %s" % (name, tag)
            if len(txt) > 32:
                txt = txt[:30] + "…"   # 长显卡名会顶宽标题栏
            self.queue.put(("gpu", txt))
        threading.Thread(target=work, daemon=True).start()

    # ---------- 运行 ----------
    def _start(self):
        if self.tr and getattr(self.tr, "running", False):
            return
        todo = [(i, t) for i, t in enumerate(self.tasks) if not t.out_size]
        if not todo:
            self.log("没有待处理的文件。")
            return
        self._on_opt_change()
        self.output_dirs = []
        self.pbar["value"] = 0
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._running = True

        opt = dict(self.cfg)
        opt["vendor"] = self.vendor_eff
        self.tr = Transcoder(todo, opt, self._on_event, self._on_progress)
        self.tr.running = True
        started = time.time()
        self.log("开始转码 %d 个文件 · %s · %s · CQ%d · %s" % (
            len(todo), ("HEVC" if self.cfg["codec"] == "hevc" else "H.264"),
            VENDOR_LABEL.get(self.vendor_eff, ""), self.cfg["cq"],
            (PRESET_QSV if self.vendor_eff == "intel" else PRESET_NVENC)[
                self.cfg["preset"]]))
        self.tr.start()
        self._run_started = started

    def _stop(self):
        if self.tr:
            self.tr.cancel()
            self.log("正在停止…")
            self.btn_stop.configure(state="disabled")

    def _on_event(self, ev):
        self.queue.put(ev)

    def _on_progress(self, idx, pct, msg, eta, n_total):
        self.queue.put(("progress", (idx, pct, msg, eta, n_total)))

    def _poll(self):
        try:
            while True:
                ev = self.queue.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self.log(ev[1], ev[2] if len(ev) > 2 else None)
                elif kind == "row":
                    arg = ev[1]
                    if isinstance(arg, tuple):
                        i = arg[1]
                        self.tasks[i].status = "转码中"
                        self._refresh_row(i)
                    else:
                        self._refresh_row(arg)
                        self._update_summary()
                elif kind == "probe":
                    self._refresh_row(ev[1])
                elif kind == "progress":
                    idx, pct, msg, eta, n_total = ev[1]
                    self.pbar["value"] = pct
                    pos = "%d/%d" % (idx + 1, n_total)
                    self.lbl_prog.configure(text="%s · %s" % (pos, msg))
                    if idx < len(self.tasks):
                        self.tasks[idx].status = "转码中 %.0f%%" % pct
                        self._refresh_row(idx)
                elif kind == "gpu":
                    self.gpu_label.configure(text=ev[1])
                elif kind == "enc_check":
                    vendor, ok, err = ev[1]
                    self._on_enc_check(vendor, ok, err)
                elif kind == "done":
                    self._finish(ev[1])
        except queue.Empty:
            pass
        self.root.after(90, self._poll)

    def _on_enc_check(self, vendor, ok, err):
        if ok:
            self.log("编码器自检通过：%s · %s。" % (self.gpu_name,
                                              VENDOR_LABEL.get(vendor, vendor)), "dim")
            return
        tag = VENDOR_LABEL.get(vendor, vendor)
        others = [v for v in self.vendors_avail if v != vendor]
        if self.cfg.get("vendor") and self.cfg.get("vendor") != "auto":
            self.log("⚠ %s 编码器不可用，请检查显卡驱动，或把配置里的 vendor 改回 auto。" % tag, "err")
            return
        if self._running:
            # 正在转码，别中途换厂商，等这批跑完再说
            self.log("⚠ %s 编码器不可用，本次转码仍按原设置执行。" % tag, "err")
            return
        if others:
            new = others[0]
            self._apply_vendor(new)
            self.log("⚠ %s 编码器不可用（驱动不支持或显卡被禁用），已自动切换到 %s。"
                     % (tag, VENDOR_LABEL.get(new, new)), "dim")
        else:
            self.log("⚠ 没有可用的硬件编码器，%s 起不来。转码会回退到兼容参数，速度会明显变慢。"
                     % tag, "err")

    def _finish(self, s):
        self._running = False
        if self.tr:
            self.tr.running = False
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.pbar["value"] = 0
        self.lbl_prog.configure(text="空闲")
        el = s["elapsed"]
        speed = (s["video_sec"] / el) if el > 0 else 0
        msg = "完成 %d 个，失败 %d 个，用时 %s" % (s["ok"], s["fail"], human_time(el))
        if s["total_in"] and s["ok"] and s["total_out"] > 0:
            saved = (1 - s["total_out"] / float(s["total_in"])) * 100
            msg += "；输入 %s → 输出 %s（省 %.0f%%）" % (
                human_size(s["total_in"]), human_size(s["total_out"]), saved)
        elif s["fail"] and not s["ok"]:
            msg += "；没有成功的文件，检查日志里的报错"
        if speed:
            msg += "；平均 %.1fx" % speed
        self.log(msg, "ok" if not s["fail"] else "err")
        self._update_summary()
        if s["ok"]:
            self.log("输出目录与原视频相同（文件名带 _cq%d 后缀）。" % self.cfg["cq"], "dim")

    def log(self, text, tag=None):
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "[%s] %s\n" % (ts, text), tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self):
        if self.tr:
            self.tr.cancel()
        self._save_cfg()
        self.root.destroy()


# ---------------------------------------------------------------- 无界面自检

def selftest(src, out_log):
    lines = []

    def w(s):
        lines.append(s)
        print(s)

    w("ffmpeg: %s" % FFMPEG)
    w("exists: %s" % os.path.exists(FFMPEG))
    try:
        p = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW, timeout=20)
        w((p.stdout or b"").decode("utf-8", "ignore").splitlines()[0])
    except Exception as e:
        w("version fail: %s" % e)
    info = probe(src)
    w("probe: %s" % info)

    opt = {"cq": 21, "codec": "h264", "preset": "p6", "container": "mp4",
           "audio": "copy", "hwaccel": True, "keep_res": True,
           "res_height": 1080}
    dst = os.path.splitext(src)[0] + "_selftest.mp4"
    if os.path.exists(dst):
        os.remove(dst)
    cmd = build_cmd(src, dst, opt, info, True)
    w("cmd: %s" % " ".join('"%s"' % c if " " in c else c for c in cmd))
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
    rc = p.returncode
    w("hwaccel rc=%d elapsed=%.1fs" % (rc, time.time() - t0))
    if rc != 0:
        w("err: %s" % (p.stderr or b"").decode("utf-8", "ignore")[-1200:])
        cmd = build_cmd(src, dst, opt, info, False)
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)
        w("swdecode rc=%d elapsed=%.1fs" % (p.returncode, time.time() - t0))
        if p.returncode != 0:
            w("err: %s" % (p.stderr or b"").decode("utf-8", "ignore")[-1200:])
    if os.path.exists(dst):
        w("output: %s  size=%.2f MB  (in=%.2f MB)" % (
            dst, os.path.getsize(dst) / 1048576.0, os.path.getsize(src) / 1048576.0))
        pr = subprocess.run([FFMPEG, "-hide_banner", "-i", dst], capture_output=True,
                            creationflags=CREATE_NO_WINDOW)
        for ln in (pr.stderr or b"").decode("utf-8", "ignore").splitlines():
            if "Stream #" in ln or "Duration" in ln:
                w("  " + ln.strip())
    else:
        w("output missing")
    with open(out_log, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def selftest_ui(log_path):
    import traceback
    out = []

    def w(s):
        out.append(str(s))

    try:
        root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        root.withdraw()
        app = App(root)
        root.update_idletasks()
        w("dnd_module=%s" % bool(TkinterDnD))
        w("dnd_ok=%s" % getattr(app, "_dnd_ok", None))
        w("ffmpeg_ok=%s" % app._ffmpeg_ok())
        w("ffmpeg=%s" % FFMPEG)
        w("children=%d" % len(root.winfo_children()))
        w("tree_rows=%d" % len(app.tree.get_children()))
        w("OK")
        root.destroy()
    except Exception:
        w("FAIL\n" + traceback.format_exc())
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def selftest_layout(log_path):
    """隐藏窗口下按最小尺寸排版，检查各控件是否被裁切。"""
    import traceback
    out = []

    def w(s):
        out.append(str(s))

    try:
        root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        app = App(root)
        root.update_idletasks()
        w("default=%dx%d geometry=%s" % (root.winfo_width(), root.winfo_height(), root.geometry()))
        mw, mh = root.minsize()
        root.geometry("%dx%d" % (mw, mh))
        root.update_idletasks()
        root.update()

        content_w = root.winfo_width() - 36
        w("minsize=%dx%d  content_w=%d" % (mw, mh, content_w))
        w("root req=%dx%d  actual=%dx%d" % (
            root.winfo_reqwidth(), root.winfo_reqheight(), root.winfo_width(), root.winfo_height()))
        w("row1 reqwidth=%d  row2 reqwidth=%d" % (
            app.opt_row1.winfo_reqwidth(), app.opt_row2.winfo_reqwidth()))

        issues = []
        # 滑块画布会随宽度重绘，只检查固定文字区 + 滑块最小可用宽度
        if app.cq_left.winfo_reqwidth() + 200 > content_w:
            issues.append("row1 横向溢出")
        if app.opt_row2.winfo_reqwidth() > content_w:
            issues.append("row2 横向溢出")

        def walk(wgt):
            for c in wgt.winfo_children():
                try:
                    if getattr(c, "_fixed_box", False) or getattr(c, "_flex_canvas", False):
                        walk(c)
                        continue
                    if c.winfo_manager():
                        rw, rh = c.winfo_reqwidth(), c.winfo_reqheight()
                        aw, ah = c.winfo_width(), c.winfo_height()
                        kids = c.winfo_children()
                        scrollable = c.winfo_class() in ("Treeview", "Text") or \
                            any(k.winfo_class() in ("Treeview", "Text") for k in kids)
                        # 纯容器不单独判定，依赖子控件的裁切结果
                        is_container = c.winfo_class() in ("Frame", "TFrame")
                        if not is_container and (aw < rw - 6 or (not scrollable and ah < rh - 6)):
                            issues.append("%s req=%dx%d actual=%dx%d" % (
                                c.winfo_class(), rw, rh, aw, ah))
                except Exception:
                    pass
                walk(c)

        walk(root)

        for nm, wgt in [("tree", app.tree), ("drop", app.drop_canvas), ("log", app.log_text),
                        ("pbar", app.pbar), ("start", app.btn_start), ("stop", app.btn_stop)]:
            w("%s: %dx%d" % (nm, wgt.winfo_width(), wgt.winfo_height()))

        if issues:
            w("ISSUES:")
            for i in issues:
                w("  " + i)
        else:
            w("all widgets fit at minimum size")
        w("OK")
        root.destroy()
    except Exception:
        w("FAIL\n" + traceback.format_exc())
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def selftest_run(video, log_path, timeout=300):
    """隐藏窗口跑完整流程：加文件 → 开始转码 → 轮询 → 完成。"""
    import traceback
    out = []

    def w(s):
        out.append(str(s))

    try:
        root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        root.withdraw()
        app = App(root)
        app._add_paths([video])
        root.update()
        t0 = time.time()
        app._start()
        while time.time() - t0 < timeout:
            root.update()
            time.sleep(0.05)
            if not getattr(app, "_running", False) and time.time() - t0 > 1:
                break
        root.update()
        w("running=%s" % app._running)
        for i, t in enumerate(app.tasks):
            w("row%d: %s | %s | %s" % (i, t.name, t.status, human_size(t.out_size)))
        w("progress_label=%s" % app.lbl_prog.cget("text"))
        w("summary_label=%s" % app.lbl_sum.cget("text"))
        w("--- log ---")
        w(app.log_text.get("1.0", "end"))
        w("OK")
        root.destroy()
    except Exception:
        w("FAIL\n" + traceback.format_exc())
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    args = sys.argv[1:]
    global DEMO_SCALE, FORCE_VENDOR
    if "--force-vendor" in args:
        i = args.index("--force-vendor")
        try:
            FORCE_VENDOR = args[i + 1]
        except Exception:
            FORCE_VENDOR = None
    if "--demo-scale" in args:
        i = args.index("--demo-scale")
        try:
            DEMO_SCALE = float(args[i + 1])
        except Exception:
            DEMO_SCALE = None
    if os.environ.get("TP_DEBUG"):
        try:
            with open(os.path.join(tempfile.gettempdir(), "tp_debug.log"), "a",
                      encoding="utf-8") as f:
                f.write("args=%r DEMO_SCALE=%r\n" % (args, DEMO_SCALE))
        except Exception:
            pass
    if args and args[0] == "--selftest-layout":
        selftest_layout(args[1] if len(args) > 1 else os.path.join(tempfile.gettempdir(), "compressor_layout.log"))
        return
    if args and args[0] == "--selftest-run":
        selftest_run(args[1], args[2] if len(args) > 2 else os.path.join(tempfile.gettempdir(), "nvcomp_run.log"))
        return
    if args and args[0] == "--selftest-ui":
        selftest_ui(args[1] if len(args) > 1 else os.path.join(tempfile.gettempdir(), "nvcomp_ui.log"))
        return
    if args and args[0] == "--selftest":
        src = args[1]
        log_path = args[2] if len(args) > 2 else os.path.join(tempfile.gettempdir(), "nvcomp_selftest.log")
        selftest(src, log_path)
        return

    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    # 演示/截图：按指定缩放模拟 DPI（Tk 的 scaling 单位是点→像素）
    if DEMO_SCALE:
        root.tk.call("tk", "scaling", DEMO_SCALE * 96.0 / 72.0)
    app = App(root)
    if os.environ.get("TP_DEBUG"):
        def _dbg():
            try:
                with open(os.path.join(tempfile.gettempdir(), "tp_debug.log"), "a",
                          encoding="utf-8") as f:
                    f.write("geom=%s ui=%r sys=%r fpix=%r\n" % (
                        root.geometry(), app._ui_scale(), app._system_scale(),
                        root.winfo_fpixels("1p")))
            except Exception:
                pass
        root.after(600, _dbg)
    # 演示/截图辅助：预先载入素材，可选自动开始转码
    if args and args[0] == "--demo":
        demo_paths = [args[1]] if len(args) > 1 else []
        auto_run = "--demo-run" in args
        root.after(400, lambda: app._add_paths(demo_paths))
        if auto_run:
            root.after(2000, app._start)
    root.mainloop()


if __name__ == "__main__":
    main()
