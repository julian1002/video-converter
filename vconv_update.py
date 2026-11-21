# -*- coding: utf-8 -*-
"""
更新模組：檢查 / 下載 / 安裝 FFmpeg 與本工具的新版本
（只有按「檢查更新」時才連網，轉檔本身完全離線）
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

import vconv_core as core

UA = {"User-Agent": f"VideoConverter/{core.APP_VERSION}"}
DEFAULT_CONFIG = {
    "ffmpeg_source": "btbn",
    "btbn_api": "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest",
    "gyan_version_url": "https://www.gyan.dev/ffmpeg/builds/release-version",
    "gyan_zip_url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    # 本工具的更新清單網址（JSON），例：放在自己的 GitHub / 公司內網
    "app_manifest_url": "https://raw.githubusercontent.com/julian1002/video-converter/main/manifest.json",
}
SOURCES = {"btbn": "BtbN（GitHub，GPL 完整編碼器）", "gyan": "Gyan.dev（Essentials）"}


# ------------------------------------------------------------ 設定檔
def config_path():
    return os.path.join(core.root_dir(), "config.json")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), encoding="utf-8-sig") as f:      # 相容 PowerShell 的 BOM
            for k, v in json.load(f).items():
                if v not in ("", None) or k not in DEFAULT_CONFIG:
                    cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def ffmpeg_meta_path():
    return os.path.join(core.bin_dir(), "ffmpeg_version.json")


def load_ffmpeg_meta():
    try:
        with open(ffmpeg_meta_path(), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------ 版本解析
def parse_version(text):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def fmt_ver(t):
    return ".".join(map(str, t)) if t else "?"


def local_ffmpeg(ffmpeg):
    """回傳 dict(raw, ver, meta)"""
    if not ffmpeg or not os.path.isfile(ffmpeg) and not shutil.which(ffmpeg):
        return {"raw": "未安裝", "ver": None, "meta": {}}
    out = core.run([ffmpeg, "-version"]).stdout
    m = re.search(r"ffmpeg version (\S+)", out)
    raw = m.group(1) if m else "未知"
    ver = None
    vm = re.match(r"n?(\d+\.\d+(?:\.\d+)?)", raw)
    if vm:
        ver = parse_version(vm.group(1))
    return {"raw": raw, "ver": ver, "meta": load_ffmpeg_meta(), "path": ffmpeg}


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _head_size(url):
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def remote_ffmpeg(cfg, source):
    """回傳 dict(ver, label, url, size, date, name, source)"""
    if source == "gyan":
        txt = _get(cfg["gyan_version_url"]).decode("utf-8", "replace").strip()
        ver = parse_version(txt)
        url = cfg["gyan_zip_url"]
        return {"ver": ver, "label": txt, "url": url, "size": _head_size(url),
                "date": "", "name": os.path.basename(url), "source": "gyan"}
    data = json.loads(_get(cfg["btbn_api"]).decode("utf-8"))
    best = None
    for a in data.get("assets", []):
        m = re.match(r"^ffmpeg-n(\d+)\.(\d+)-latest-win64-gpl-\d+\.\d+\.zip$", a.get("name", ""))
        if m:
            v = (int(m.group(1)), int(m.group(2)))
            if not best or v > best[0]:
                best = (v, a)
    if not best:
        raise RuntimeError("在 BtbN 發行頁找不到 Windows 64 位元 GPL 穩定版 .zip")
    v, a = best
    date = (a.get("updated_at") or data.get("published_at") or "")[:10]
    return {"ver": v + (0,), "label": f"{v[0]}.{v[1]} 穩定分支（{date} 建置）",
            "url": a["browser_download_url"], "size": int(a.get("size") or 0),
            "date": date, "name": a["name"], "source": "btbn"}


def compare_ffmpeg(local, remote):
    """回傳 (是否建議更新, 狀態文字)"""
    lv, rv = local.get("ver"), remote.get("ver")
    if local.get("raw") == "未安裝":
        return True, "尚未安裝，可下載"
    if not lv:
        return True, "目前為開發版或無法辨識，可改裝穩定版"
    if remote["source"] == "btbn":
        if rv[:2] > lv[:2]:
            return True, f"有新版本 {rv[0]}.{rv[1]}"
        if rv[:2] == lv[:2]:
            meta = local.get("meta") or {}
            if meta.get("source") == "btbn" and remote["date"] and meta.get("date", "") < remote["date"]:
                return True, "同版本有較新的建置（錯誤修正）"
            return False, "已是最新"
        return False, "已是最新（目前版本較新）"
    if rv and rv > lv:
        return True, f"有新版本 {fmt_ver(rv)}"
    return False, "已是最新"


# ------------------------------------------------------------ 下載
def download(url, dest, progress=None, cancel=None, chunk=256 * 1024):
    """progress(done, total, speed_Bps)；cancel() 回傳 True 時中止。回傳是否完成"""
    part = dest + ".part"
    req = urllib.request.Request(url, headers=UA)
    t0 = time.time()
    last = 0
    with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel and cancel():
                break
            buf = r.read(chunk)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            now = time.time()
            if progress and (now - last > 0.2 or done == total):
                last = now
                progress(done, total, done / max(now - t0, 1e-3))
    if cancel and cancel():
        os.remove(part)
        return False
    if total and done < total:
        os.remove(part)
        raise RuntimeError(f"下載不完整（{done}/{total} bytes），請重試")
    os.replace(part, dest)
    if progress:
        progress(done, total or done, done / max(time.time() - t0, 1e-3))
    return True


def downloads_dir():
    d = os.path.join(core.root_dir(), "downloads")
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------ 安裝 FFmpeg
def install_ffmpeg_zip(zip_path, remote=None, log=print):
    """從 zip 取出 ffmpeg / ffprobe（/ffplay）到 bin\\，舊版先備份，失敗自動還原"""
    names = ["ffmpeg" + core.EXE, "ffprobe" + core.EXE, "ffplay" + core.EXE]
    bdir = core.bin_dir()
    os.makedirs(bdir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        members = {}
        for info in z.infolist():
            base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if base in names and "/bin/" in "/" + info.filename.replace("\\", "/"):
                members[base] = info
        if names[0] not in members or names[1] not in members:
            raise RuntimeError("壓縮檔內找不到 bin/ffmpeg 與 bin/ffprobe")
        tmp = tempfile.mkdtemp(prefix="vconv_ff_", dir=downloads_dir())
        for base, info in members.items():
            with z.open(info) as src, open(os.path.join(tmp, base), "wb") as dst:
                shutil.copyfileobj(src, dst)
            if sys.platform != "win32":
                os.chmod(os.path.join(tmp, base), 0o755)
    # 先驗證新版能執行
    new_ff = os.path.join(tmp, names[0])
    r = core.run([new_ff, "-version"])
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("新版 ffmpeg 無法執行：" + r.stderr[-300:])
    # 備份舊版
    old = local_ffmpeg(os.path.join(bdir, names[0]))
    backup = None
    if os.path.isfile(os.path.join(bdir, names[0])):
        backup = os.path.join(core.root_dir(), "backup",
                              "ffmpeg-" + re.sub(r"[^\w.\-]", "_", old["raw"]) + time.strftime("-%Y%m%d%H%M%S"))
        os.makedirs(backup, exist_ok=True)
        for n in names + ["ffmpeg_version.json"]:
            p = os.path.join(bdir, n)
            if os.path.isfile(p):
                shutil.copy2(p, backup)
        log(f"已備份舊版到 {backup}")
    try:
        for n in os.listdir(tmp):
            shutil.copy2(os.path.join(tmp, n), os.path.join(bdir, n))
        meta = {"installed": time.strftime("%Y-%m-%d %H:%M:%S")}
        if remote:
            meta.update({k: remote.get(k) for k in ("source", "name", "url", "date", "label")})
        meta["version"] = local_ffmpeg(os.path.join(bdir, names[0]))["raw"]
        with open(ffmpeg_meta_path(), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        if backup:                      # 還原
            for n in os.listdir(backup):
                shutil.copy2(os.path.join(backup, n), os.path.join(bdir, n))
            log("安裝失敗，已還原舊版")
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return os.path.join(bdir, names[0])


# ------------------------------------------------------------ 工具本身更新
"""
更新清單格式 (app_manifest_url 指向的 JSON)：
{
  "version": "1.2.0",
  "notes": "更新內容說明",
  "files": {
    "vconv_gui.py":    {"url": "https://.../vconv_gui.py",    "sha256": "選填"},
    "vconv_core.py":   {"url": "https://.../vconv_core.py"},
    "vconv_update.py": {"url": "https://.../vconv_update.py"}
  }
}
"""


def remote_app(cfg):
    url = (cfg.get("app_manifest_url") or "").strip()
    if not url:
        return None
    m = json.loads(_get(url).decode("utf-8-sig"))
    files = {}
    for name, v in (m.get("files") or {}).items():
        if os.path.basename(name) != name or not name.endswith(".py"):
            raise RuntimeError(f"更新清單中的檔名不合法：{name}")
        files[name] = v if isinstance(v, dict) else {"url": v}
    return {"ver": parse_version(m.get("version", "")), "label": m.get("version", "?"),
            "notes": m.get("notes", ""), "files": files, "url": url}


def install_app_update(remote, progress=None, cancel=None, log=print):
    if getattr(sys, "frozen", False):
        raise RuntimeError("exe 打包版無法自動更新程式碼，請重新打包或改用安裝版")
    app_dir = core._app_dir()
    stage = tempfile.mkdtemp(prefix="vconv_app_", dir=downloads_dir())
    try:
        items = list(remote["files"].items())
        for i, (name, info) in enumerate(items):
            log(f"下載 {name}：{info['url']}")
            dest = os.path.join(stage, name)
            ok = download(info["url"], dest, cancel=cancel,
                          progress=(lambda d, t, s, i=i: progress and progress(
                              i + (d / t if t else 0), len(items), s)))
            if not ok:
                return False
            if info.get("sha256"):
                h = hashlib.sha256(open(dest, "rb").read()).hexdigest()
                if h.lower() != info["sha256"].lower():
                    raise RuntimeError(f"{name} 檢查碼不符，已中止更新")
            compile(open(dest, encoding="utf-8").read(), name, "exec")   # 語法檢查
        backup = os.path.join(core.root_dir(), "backup",
                              f"app-{core.APP_VERSION}" + time.strftime("-%Y%m%d%H%M%S"))
        os.makedirs(backup, exist_ok=True)
        for name, _ in items:
            p = os.path.join(app_dir, name)
            if os.path.isfile(p):
                shutil.copy2(p, backup)
        for name, _ in items:
            shutil.copy2(os.path.join(stage, name), os.path.join(app_dir, name))
        log(f"程式已更新，舊版備份於 {backup}")
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)
