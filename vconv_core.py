# -*- coding: utf-8 -*-
"""
離線影片轉檔工具 - 核心邏輯（不依賴 GUI，可單獨測試）
需要 ffmpeg / ffprobe 可執行檔（放在程式同資料夾、bin/ 子資料夾，或系統 PATH）
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

SEQ_EXTS = {".jpg", ".jpeg", ".png", ".exr", ".tif", ".tiff", ".dpx", ".bmp", ".tga"}
VIDEO_EXTS = {".avi", ".mp4", ".mov", ".wmv", ".mkv", ".m4v", ".mxf", ".webm",
              ".flv", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp"}

# Windows 下執行 ffmpeg 時不跳出黑色主控台視窗
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---------------------------------------------------------------- ffmpeg 位置
APP_VERSION = "1.1.0"
EXE = ".exe" if sys.platform == "win32" else ""


def _app_dir():
    if getattr(sys, "frozen", False):          # PyInstaller 打包後
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def root_dir():
    """安裝根目錄：程式放在 <根>/app/ 時回傳 <根>，否則就是程式所在資料夾"""
    a = _app_dir()
    return os.path.dirname(a) if os.path.basename(a).lower() == "app" else a


def bin_dir():
    return os.path.join(root_dir(), "bin")


def find_tool(name):
    exe = name + EXE
    base = _app_dir()
    for p in (os.path.join(bin_dir(), exe), os.path.join(base, exe),
              os.path.join(base, "bin", exe), os.path.join(base, "ffmpeg", "bin", exe)):
        if os.path.isfile(p):
            return p
    return shutil.which(name)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                          errors="replace", creationflags=_NO_WINDOW, **kw)


def list_encoders(ffmpeg):
    out = run([ffmpeg, "-hide_banner", "-encoders"]).stdout
    encs = set()
    for line in out.splitlines():
        m = re.match(r"^\s*[VAS][\.A-Z]{5}\s+(\S+)", line)
        if m:
            encs.add(m.group(1))
    return encs


# ---------------------------------------------------------------- 來源資訊
@dataclass
class Source:
    kind: str                 # "sequence" | "video"
    path: str                 # 影片路徑，或序列的 ffmpeg 樣式路徑 (xxx_%04d.png)
    width: int = 0
    height: int = 0
    fps: float = 24.0
    frames: int = 0
    duration: float = 0.0
    start_number: int = 0
    ext: str = ""
    has_audio: bool = False
    audio_bitrate: int = 0    # bps
    codec: str = ""
    pix_fmt: str = ""
    size_bytes: int = 0
    first_file: str = ""
    missing_frames: list = field(default_factory=list)

    def describe(self):
        lines = []
        if self.kind == "sequence":
            lines.append(f"類型：序列圖 ({self.ext.upper().lstrip('.')})")
            lines.append(f"樣式：{os.path.basename(self.path)}")
            lines.append(f"影格：{self.start_number} ~ {self.start_number + self.frames - 1}"
                         f"（共 {self.frames} 張）")
            if self.missing_frames:
                mf = ", ".join(map(str, self.missing_frames[:10]))
                more = " …" if len(self.missing_frames) > 10 else ""
                lines.append(f"⚠ 缺少影格 {len(self.missing_frames)} 張：{mf}{more}")
        else:
            lines.append(f"類型：影片檔 ({self.codec})")
            lines.append(f"檔案：{os.path.basename(self.path)}")
            lines.append(f"長度：{fmt_time(self.duration)}（約 {self.frames} 格）")
            lines.append(f"音訊：{'有' if self.has_audio else '無'}")
        lines.append(f"解析度：{self.width} × {self.height}")
        lines.append(f"幀率：{self.fps:g} fps")
        if self.pix_fmt:
            lines.append(f"像素格式：{self.pix_fmt}")
        lines.append(f"來源總大小：{fmt_size(self.size_bytes)}")
        return "\n".join(lines)


_NUM_RE = re.compile(r"^(.*?)(\d+)(\.[A-Za-z0-9]+)$")


def scan_sequences(folder):
    """掃描資料夾內所有序列，回傳 [(key, info)]，依張數多寡排序"""
    groups = {}
    for fn in os.listdir(folder):
        m = _NUM_RE.match(fn)
        if not m:
            continue
        prefix, num, ext = m.groups()
        if ext.lower() not in SEQ_EXTS:
            continue
        key = (prefix, ext)
        groups.setdefault(key, []).append((int(num), num, fn))
    result = []
    for (prefix, ext), items in groups.items():
        if len(items) < 2:
            continue
        items.sort()
        widths = {len(s) for _, s, _ in items}
        # 若位數一致 → 補零格式；否則（1,2,...,10）→ %d
        pad = widths.pop() if len(widths) == 1 else 0
        result.append({
            "prefix": prefix, "ext": ext, "pad": pad,
            "numbers": [n for n, _, _ in items],
            "files": [f for _, _, f in items],
        })
    result.sort(key=lambda r: -len(r["numbers"]))
    return result


def ffprobe_json(ffprobe, args):
    r = run([ffprobe, "-v", "error", "-print_format", "json"] + args)
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def load_sequence(ffprobe, folder, seq, fps=24.0):
    prefix = seq["prefix"].replace("%", "%%")      # 檔名中的 % 要跳脫
    num_fmt = f"%0{seq['pad']}d" if seq["pad"] else "%d"
    pattern = os.path.join(folder, f"{prefix}{num_fmt}{seq['ext']}")
    nums = seq["numbers"]
    first = os.path.join(folder, seq["files"][0])
    expected = set(range(nums[0], nums[-1] + 1))
    missing = sorted(expected - set(nums))
    info = ffprobe_json(ffprobe, ["-show_streams", first])
    st = (info.get("streams") or [{}])[0]
    size = sum(os.path.getsize(os.path.join(folder, f)) for f in seq["files"])
    # ffmpeg 序列輸入遇到缺格會中斷，所以用連續段長度
    contiguous = 0
    for i, n in enumerate(nums):
        if n != nums[0] + i:
            break
        contiguous += 1
    return Source(kind="sequence", path=pattern, width=int(st.get("width", 0)),
                  height=int(st.get("height", 0)), fps=fps, frames=contiguous,
                  duration=contiguous / fps, start_number=nums[0], ext=seq["ext"].lower(),
                  pix_fmt=st.get("pix_fmt", ""), size_bytes=size, first_file=first,
                  missing_frames=missing)


def _parse_rate(s):
    try:
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 0.0


def load_video(ffprobe, path):
    info = ffprobe_json(ffprobe, ["-show_streams", "-show_format", path])
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise ValueError("找不到影像軌，可能不是影片檔或格式不支援")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = info.get("format", {})
    fps = _parse_rate(v.get("avg_frame_rate", "0/0")) or _parse_rate(v.get("r_frame_rate", "0/0")) or 30.0
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    frames = int(v.get("nb_frames") or 0) or int(round(dur * fps))
    return Source(kind="video", path=path, width=int(v.get("width", 0)),
                  height=int(v.get("height", 0)), fps=round(fps, 3), frames=frames,
                  duration=dur, has_audio=a is not None,
                  audio_bitrate=int(a.get("bit_rate") or 0) if a else 0,
                  codec=v.get("codec_name", ""), pix_fmt=v.get("pix_fmt", ""),
                  size_bytes=int(fmt.get("size") or os.path.getsize(path)), ext=os.path.splitext(path)[1].lower())


# ---------------------------------------------------------------- 編碼器定義
# mode: crf = 品質係數（數字越小畫質越好）  q = qscale  profile = 固定設定檔
# bpp_ref: 1080p 時在 ref 參數下的「每像素位元數」經驗值，用來粗估檔案大小
CODECS = {
    "H.264 / AVC": dict(enc="libx264", mode="crf", rng=(12, 35), default=20, ref=23,
                        bpp_ref=0.10, step=6, containers=["mp4", "mov", "mkv", "avi"],
                        presets=["ultrafast", "superfast", "veryfast", "faster", "fast",
                                 "medium", "slow", "slower", "veryslow"], preset="medium",
                        bits=[8, 10], note="相容性最好，幾乎所有裝置都能播放"),
    "H.265 / HEVC": dict(enc="libx265", mode="crf", rng=(14, 38), default=23, ref=28,
                         bpp_ref=0.05, step=6, containers=["mp4", "mov", "mkv"],
                         presets=["ultrafast", "superfast", "veryfast", "faster", "fast",
                                  "medium", "slow", "slower", "veryslow"], preset="medium",
                         bits=[8, 10], note="同畫質約為 H.264 的一半大小，編碼較慢"),
    "H.264 (NVIDIA GPU)": dict(enc="h264_nvenc", mode="cq", rng=(15, 40), default=21, ref=24,
                               bpp_ref=0.11, step=6, containers=["mp4", "mov", "mkv"],
                               presets=["p1", "p2", "p3", "p4", "p5", "p6", "p7"], preset="p5",
                               bits=[8], note="需 NVIDIA 顯示卡，速度極快"),
    "H.265 (NVIDIA GPU)": dict(enc="hevc_nvenc", mode="cq", rng=(15, 42), default=24, ref=28,
                               bpp_ref=0.06, step=6, containers=["mp4", "mov", "mkv"],
                               presets=["p1", "p2", "p3", "p4", "p5", "p6", "p7"], preset="p5",
                               bits=[8, 10], note="需 NVIDIA 顯示卡，速度極快"),
    "AV1 (SVT-AV1)": dict(enc="libsvtav1", mode="crf", rng=(18, 55), default=32, ref=35,
                          bpp_ref=0.045, step=7, containers=["mp4", "mkv", "webm"],
                          presets=[str(i) for i in range(13)], preset="8",
                          bits=[8, 10], note="新一代高壓縮率格式，較舊裝置可能無法播放"),
    "VP9": dict(enc="libvpx-vp9", mode="crf", rng=(15, 55), default=31, ref=31,
                bpp_ref=0.06, step=7, containers=["webm", "mkv"], presets=["0", "1", "2", "3", "4", "5"],
                preset="2", bits=[8, 10], note="網頁用格式 (WebM)"),
    "Apple ProRes": dict(enc="prores_ks", mode="profile", containers=["mov"],
                         profiles=[("Proxy", 0, 45), ("LT", 1, 102), ("422", 2, 147),
                                   ("422 HQ", 3, 220), ("4444", 4, 330), ("4444 XQ", 5, 500)],
                         default=3, note="後製剪輯用中間檔，檔案很大但畫質極佳"),
    "Avid DNxHR": dict(enc="dnxhd", mode="profile", containers=["mov"],
                       profiles=[("LB", "dnxhr_lb", 45), ("SQ", "dnxhr_sq", 145),
                                 ("HQ", "dnxhr_hq", 220), ("HQX (10-bit)", "dnxhr_hqx", 220),
                                 ("444", "dnxhr_444", 440)],
                       default=2, note="後製剪輯用中間檔（Avid / Premiere / Resolve）"),
    "MPEG-4 Part 2 (Xvid)": dict(enc="mpeg4", mode="q", rng=(2, 20), default=4, ref=4,
                                 bpp_ref=0.15, containers=["avi", "mp4", "mkv"], bits=[8],
                                 note="舊式 AVI 相容格式"),
    "WMV (Windows Media 8)": dict(enc="wmv2", mode="q", rng=(2, 20), default=4, ref=4,
                                  bpp_ref=0.17, containers=["wmv"], bits=[8],
                                  note="Windows 舊式播放器相容"),
    "Motion JPEG": dict(enc="mjpeg", mode="q", rng=(2, 20), default=3, ref=3,
                        bpp_ref=0.8, containers=["avi", "mov", "mkv"], bits=[8],
                        note="每格獨立壓縮，好剪輯但檔案大"),
}

AUDIO_FOR_CONTAINER = {"mp4": "aac", "mov": "aac", "mkv": "aac", "avi": "libmp3lame",
                       "wmv": "wmav2", "webm": "libopus", "mxf": "pcm_s16le"}


def available_codecs(encoders):
    return [name for name, c in CODECS.items() if c["enc"] in encoders]


# ---------------------------------------------------------------- 畫質等級
def quality_level(codec_name, value, width=1920, height=1080):
    """回傳 (等級文字, 0~100 分數)。value 為 CRF/q 值或 profile 索引"""
    c = CODECS[codec_name]
    if c["mode"] == "profile":
        n = len(c["profiles"])
        score = 40 + 60 * value / max(1, n - 1)
    elif c["mode"] == "q":
        # qscale 是倍率關係，用對數換算分數
        lo, hi = c["rng"]
        score = 100 * (1 - math.log(value / lo) / math.log(hi / lo))
    else:
        lo, hi = c["rng"]
        score = 100 * (hi - value) / (hi - lo)
    return level_text(score), score


def level_text(score):
    if score >= 85:
        return "極高畫質（近乎無損）"
    if score >= 62:
        return "高畫質"
    if score >= 42:
        return "標準畫質"
    if score >= 22:
        return "低畫質"
    return "極低畫質（明顯失真）"


def level_from_bpp(bpp, codec_name):
    """指定位元率模式時，用 bpp 反推畫質等級"""
    c = CODECS[codec_name]
    if c["mode"] == "profile":
        return level_text(80)
    # 換算回等效 CRF
    if c["mode"] in ("crf", "cq"):
        eq = c["ref"] - c["step"] * math.log2(max(bpp, 1e-6) / c["bpp_ref"])
    else:
        eq = c["ref"] * c["bpp_ref"] / max(bpp, 1e-6)
    lo, hi = c["rng"]
    eq = min(max(eq, lo), hi)
    return quality_level(codec_name, eq)[0]


# ---------------------------------------------------------------- 大小預估
def out_dims(src, scale_w):
    w, h = src.width, src.height
    if scale_w and w and scale_w != w:
        h = int(round(h * scale_w / w / 2) * 2)
        w = scale_w
    return (w // 2 * 2) or 2, (h // 2 * 2) or 2


def estimate_video_bitrate(codec_name, value, w, h, fps):
    """粗估影像位元率 (bps)"""
    c = CODECS[codec_name]
    px = w * h
    if c["mode"] == "profile":
        mbps_1080p30 = c["profiles"][value][2]
        return mbps_1080p30 * 1e6 * (px / 2073600) * (fps / 29.97)
    # 解析度越高，每像素需要的位元越少
    res_factor = (px / 2073600) ** -0.25
    if c["mode"] in ("crf", "cq"):
        bpp = c["bpp_ref"] * 2 ** ((c["ref"] - value) / c["step"])
    else:  # qscale：位元率約與 q 成反比
        bpp = c["bpp_ref"] * c["ref"] / value
    return bpp * res_factor * px * fps


@dataclass
class Settings:
    codec: str = "H.264 / AVC"
    container: str = "mp4"
    rate_mode: str = "quality"     # quality | bitrate
    value: float = 20              # CRF/q/profile 索引
    bitrate_kbps: int = 8000
    preset: str = ""
    bits: int = 8
    scale_w: int = 0               # 0 = 原始
    out_fps: float = 0             # 0 = 與來源相同
    audio: str = "encode"          # encode | copy | none
    audio_kbps: int = 192
    exr_srgb: bool = True
    output: str = ""


def output_duration(src, st):
    return src.frames / src.fps if src.fps else src.duration


def estimate_size(src, st):
    """回傳 (總位元組, 影像 bps, 音訊 bps)"""
    w, h = out_dims(src, st.scale_w)
    fps = st.out_fps or src.fps
    dur = output_duration(src, st)
    if st.rate_mode == "bitrate" and CODECS[st.codec]["mode"] != "profile":
        vbps = st.bitrate_kbps * 1000
    else:
        vbps = estimate_video_bitrate(st.codec, st.value, w, h, fps)
    abps = 0
    if src.kind == "video" and src.has_audio and st.audio != "none":
        if st.audio == "copy":
            abps = src.audio_bitrate or 192000
        elif AUDIO_FOR_CONTAINER.get(st.container) == "pcm_s16le":
            abps = 1536000
        else:
            abps = st.audio_kbps * 1000
    total = (vbps + abps) * dur / 8 * 1.01    # +1% 容器額外負擔
    return int(total), vbps, abps


# ---------------------------------------------------------------- 指令組合
def _pix_fmt(st):
    enc = CODECS[st.codec]["enc"]
    if enc == "prores_ks":
        return "yuva444p10le" if st.value >= 4 else "yuv422p10le"
    if enc == "dnxhd":
        prof = CODECS[st.codec]["profiles"][int(st.value)][1]
        return {"dnxhr_444": "yuv444p10le", "dnxhr_hqx": "yuv422p10le"}.get(prof, "yuv422p")
    if enc == "mjpeg":
        return "yuvj420p"
    if enc == "hevc_nvenc" and st.bits == 10:
        return "p010le"
    return "yuv420p10le" if st.bits == 10 else "yuv420p"


def build_command(ffmpeg, src, st, output=None, sample=None):
    """
    sample: (起始影格偏移, 影格數) → 用於取樣預估，只輸出片段且不含音訊
    """
    c = CODECS[st.codec]
    enc = c["enc"]
    cmd = [ffmpeg, "-hide_banner", "-y"]
    # ---- 輸入
    if src.kind == "sequence":
        if src.ext == ".exr" and st.exr_srgb:
            cmd += ["-apply_trc", "iec61966_2_1"]       # 線性 EXR → sRGB
        start = src.start_number + (sample[0] if sample else 0)
        cmd += ["-framerate", f"{src.fps:g}", "-start_number", str(start), "-i", src.path]
    else:
        if sample:
            cmd += ["-ss", f"{sample[0] / src.fps:.3f}"]
        cmd += ["-i", src.path]
    # ---- 濾鏡：縮放 / 偶數尺寸 / 幀率
    vf = []
    if st.scale_w and st.scale_w != src.width:
        vf.append(f"scale={st.scale_w}:-2:flags=lanczos")
    else:
        vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    if st.out_fps and abs(st.out_fps - src.fps) > 1e-3:
        vf.append(f"fps={st.out_fps:g}")
    vf.append(f"format={_pix_fmt(st)}")
    cmd += ["-vf", ",".join(vf), "-map", "0:v:0"]
    # ---- 影像編碼
    cmd += ["-c:v", enc]
    use_bitrate = st.rate_mode == "bitrate" and c["mode"] != "profile"
    if c["mode"] == "profile":
        prof = c["profiles"][int(st.value)][1]
        cmd += ["-profile:v", str(prof)]
        if enc == "prores_ks":
            cmd += ["-vendor", "apl0"]
    elif use_bitrate:
        b = f"{st.bitrate_kbps}k"
        cmd += ["-b:v", b, "-maxrate", f"{int(st.bitrate_kbps * 1.5)}k",
                "-bufsize", f"{st.bitrate_kbps * 2}k"]
    elif c["mode"] == "crf":
        cmd += ["-crf", str(int(st.value))]
        if enc == "libvpx-vp9":
            cmd += ["-b:v", "0"]
    elif c["mode"] == "cq":
        cmd += ["-rc", "vbr", "-cq", str(int(st.value)), "-b:v", "0"]
    else:
        cmd += ["-q:v", str(int(st.value))]
    if c.get("presets"):
        p = st.preset or c["preset"]
        if enc == "libvpx-vp9":
            cmd += ["-deadline", "good", "-cpu-used", p, "-row-mt", "1"]
        else:
            cmd += ["-preset", p]
    if enc in ("libx265", "hevc_nvenc") and st.container in ("mp4", "mov"):
        cmd += ["-tag:v", "hvc1"]       # 讓 Apple 裝置 / QuickTime 能播放 HEVC
    if enc == "libx265":
        cmd += ["-x265-params", "log-level=error"]
    if enc in ("mpeg4",) and st.container == "avi":
        cmd += ["-vtag", "XVID"]
    # ---- 音訊
    if sample or src.kind == "sequence" or not src.has_audio or st.audio == "none":
        cmd += ["-an"]
    else:
        cmd += ["-map", "0:a:0?"]
        if st.audio == "copy":
            cmd += ["-c:a", "copy"]
        else:
            ac = AUDIO_FOR_CONTAINER.get(st.container, "aac")
            cmd += ["-c:a", ac]
            if ac != "pcm_s16le":
                cmd += ["-b:a", f"{st.audio_kbps}k"]
    if sample:
        cmd += ["-frames:v", str(sample[1])]
    if st.container in ("mp4", "mov") and not sample:
        cmd += ["-movflags", "+faststart"]
    cmd += [output or st.output]
    return cmd


def sample_estimate(ffmpeg, src, st, seconds=2.0, points=3, cancel=None):
    """實際壓縮數個片段來推算大小，比經驗公式準確很多"""
    total = src.frames
    if total <= 0:
        raise ValueError("無法得知影格數")
    n = max(1, min(total, int(round(src.fps * seconds))))
    if total <= n * points:
        offsets = [0]
        n = total
    else:
        offsets = [int((total - n) * (i + 1) / (points + 1)) for i in range(points)]
    tmpdir = tempfile.mkdtemp(prefix="vconv_")
    try:
        enc_bytes = 0
        enc_frames = 0
        for i, off in enumerate(offsets):
            if cancel and cancel():
                return None
            out = os.path.join(tmpdir, f"s{i}.{st.container}")
            r = run(build_command(ffmpeg, src, st, out, sample=(off, n)))
            if r.returncode != 0 or not os.path.exists(out):
                raise RuntimeError("取樣失敗：\n" + r.stderr[-800:])
            enc_bytes += os.path.getsize(out)
            enc_frames += n
        fps_out = st.out_fps or src.fps
        # 片段頭有容器開銷，減去約 2KB/段
        per_frame = max(enc_bytes - 2048 * len(offsets), enc_bytes * 0.5) / enc_frames
        video_bytes = per_frame * total * (fps_out / src.fps)
        _, _, abps = estimate_size(src, st)
        audio_bytes = abps * output_duration(src, st) / 8
        vbps = per_frame * 8 * src.fps
        return int(video_bytes + audio_bytes), vbps
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 工具函式
def fmt_size(b):
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}" if unit != "B" else f"{int(b)} B"
        b /= 1024
    return f"{b:.2f} TB"


def fmt_rate(bps):
    return f"{bps / 1e6:.2f} Mbps" if bps >= 1e6 else f"{bps / 1e3:.0f} kbps"


def fmt_time(sec):
    sec = max(0, sec)
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{int((sec % 1) * 100):02d}"


def parse_progress(line, state):
    """解析 ffmpeg -progress 輸出；回傳是否有新進度"""
    if "=" not in line:
        return False
    k, v = line.strip().split("=", 1)
    state[k] = v
    return k == "progress"
