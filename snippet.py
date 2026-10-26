# -*- coding: utf-8 -*-
"""
Instagram Promo Renderer - النسخة 2
تتعامل مع ثلاثة أنواع من المدخلات:
  1) رابط فيديو مباشر (mp4 / cdninstagram / fbcdn)  -> تنزيل مباشر بدون yt-dlp
  2) رابط منشور انستقرام                          -> استخراج رابط مباشر عبر خدمات مجانية
  3) رابط انستقرام والكوكيز غير متوفرة            -> yt-dlp مع كوكيز الجلسة

متغيرات البيئة:
  PORT                  منفذ الخدمة (يضبطه Render تلقائياً)
  PUBLIC_BASE           رابط الخدمة العامة، مثال: https://video-processor-5tu3.onrender.com
  YTDLP_COOKIES_B64     كوكيز انستقرام بصيغة Base64 (محتوى ملف cookies.txt بصيغة Netscape)
  YTDLP_COOKIES         بديل: نص الكوكيز الخام بدون Base64
  MEDIA_DIR             مجلد حفظ الفيديوهات (افتراضي /tmp/media)
  DL_TIMEOUT            مهلة التحميل بالثواني (افتراضي 110)
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

PORT = int(os.environ.get("PORT", "10000"))
PUBLIC_BASE = (os.environ.get("PUBLIC_BASE") or "").rstrip("/")
COOKIES_B64 = (os.environ.get("YTDLP_COOKIES_B64") or "").strip()
COOKIES_RAW = (os.environ.get("YTDLP_COOKIES") or "").strip()
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/tmp/media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MAX_FILES = 40
MAX_AGE = 3600
DL_TIMEOUT = int(os.environ.get("DL_TIMEOUT", "110"))

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
UA_INSTAGRAM_APP = (
    "Instagram 219.0.0.12.117 Android (30/11; 420dpi; 1080x1920; OnePlus; "
    "ONEPLUS A6013; OnePlus6T; qcom; en_US; 458229257)"
)

RESOLVERS = [
    ("reelsaver", "https://reelsaver.vercel.app/api/video?postUrl="),
    ("mu", "https://instagram-video-downloader-mu.vercel.app/api/video?postUrl="),
    ("esiig", "https://esiig.ehsanjs.ir/api/video?postUrl="),
    ("alldl", "https://ahm7xmakki.com/api/alldl?url="),
]

app = FastAPI(title="Instagram Promo Renderer")


class ProcessRequest(BaseModel):
    url: str = ""
    text: str = ""
    chat_id: str = ""


def write_cookies():
    """يفك كوكيز Base64 ويكتبها في ملف بصيغة Netscape. يعيد (المسار، أسماء الكوكيز)"""
    data = None
    if COOKIES_B64:
        try:
            data = base64.b64decode(COOKIES_B64 + "=" * (-len(COOKIES_B64) % 4))
        except Exception:
            data = None
        if not data or b"instagram.com" not in data:
            data = None
    if data is None and COOKIES_RAW:
        data = COOKIES_RAW.encode("utf-8")
    if not data or b"instagram.com" not in data:
        return None, []

    path = Path(tempfile.gettempdir()) / "instagram_cookies.txt"
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    names = []
    for line in data.decode("utf-8", "ignore").splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[0].strip().endswith("instagram.com"):
            names.append(parts[5].strip())
    return str(path), names


COOKIES_FILE, COOKIE_NAMES = write_cookies()


def cleanup():
    try:
        files = [f for f in MEDIA_DIR.glob("*") if f.is_file()]
    except OSError:
        return
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    now = time.time()
    for i, f in enumerate(files):
        try:
            if i >= MAX_FILES or (now - f.stat().st_mtime) > MAX_AGE:
                f.unlink()
        except OSError:
            pass


def is_direct_media(url):
    low = url.lower()
    if ".mp4" in low or ".mov" in low or ".webm" in low:
        return True
    for host in ("cdninstagram.com", "fbcdn.net", "scontent-", "rapidcdn.app", "cdninstagram"):
        if host in low:
            return True
    return False


def pick_by_key(obj, keys, depth=0):
    if not isinstance(obj, dict) or depth > 5:
        return None
    for k, v in obj.items():
        if isinstance(v, str) and k.lower() in keys and v.lower().startswith("http"):
            return v
    for v in obj.values():
        r = pick_by_key(v, keys, depth + 1) if isinstance(v, dict) else None
        if r:
            return r
    for v in obj.values():
        if isinstance(v, list):
            for item in v:
                r = pick_by_key(item, keys, depth + 1) if isinstance(item, dict) else None
                if r:
                    return r
    return None


def find_mp4(obj, depth=0):
    keys = {"videourl", "video_url", "videolink", "video_src", "playable_url", "hd_url", "url"}
    r = pick_by_key(obj, keys, depth)
    if r:
        return r
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for v in obj.values():
            r = find_mp4(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_mp4(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, str):
        if obj.lower().startswith("http") and ".mp4" in obj.lower():
            return obj
    return None


def resolve_direct(post_url):
    """يستخرج رابط الفيديو المباشر عبر خدمات مجانية. يعيد (الرابط، اسم الخدمة)"""
    for name, base in RESOLVERS:
        try:
            req = urllib.request.Request(
                base + urllib.parse.quote(post_url, safe=""),
                headers={"User-Agent": UA_MOBILE, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            video = find_mp4(data)
            if video:
                return video, name
        except Exception:
            continue
    return None, None


def download_direct(url):
    """ينزّل ملف فيديو مباشر من رابط CDN. يعيد المسار"""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    target = MEDIA_DIR / ("ig_" + digest + ".mp4")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA_MOBILE,
            "Referer": "https://www.instagram.com/",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = resp.read()
    if len(data) < 1024:
        raise RuntimeError("downloaded file is too small: " + str(len(data)))
    target.write_bytes(data)
    return target


def download_ytdlp(url):
    """ينزّل عبر yt-dlp مع كوكيز الجلسة. يعيد (المسار، رسالة الخطأ)"""
    tpl = str(MEDIA_DIR / "%(id)s.%(ext)s")
    common = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--no-progress",
        "--retries", "3", "--socket-timeout", "20",
        "--user-agent", UA_MOBILE,
        "--add-header", "Referer:https://www.instagram.com/",
        "-o", tpl,
    ]
    if COOKIES_FILE:
        common += ["--cookies", COOKIES_FILE]
    last = ""
    for fmt in ("bv*+ba/b", "best", "b"):
        cmd = common + ["-f", fmt, "--merge-output-format", "mp4", url]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=DL_TIMEOUT)
        except subprocess.TimeoutExpired:
            last = "download timed out"
            continue
        except FileNotFoundError:
            last = "yt-dlp executable not found"
            break
        if res.returncode == 0:
            for f in sorted(MEDIA_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv"):
                    return f, ""
        last = (res.stderr or res.stdout or "").strip()
    return None, last


def pick_video():
    cands = [f for f in MEDIA_DIR.glob("*") if f.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv")]
    if not cands:
        return None
    return max(cands, key=lambda f: f.stat().st_mtime)


def as_mp4(src):
    if src.suffix.lower() == ".mp4":
        return src
    dst = src.with_suffix(".mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c", "copy", str(dst)],
            capture_output=True, text=True, timeout=120,
        )
        if dst.exists() and dst.stat().st_size > 0:
            try:
                src.unlink()
            except OSError:
                pass
            return dst
    except Exception:
        pass
    return src


@app.get("/health")
def health():
    return {
        "ok": True,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "yt_dlp": bool(shutil.which("yt-dlp")),
        "font": "DejaVu Sans",
        "public_base": PUBLIC_BASE,
        "cookies": bool(COOKIES_FILE),
        "cookie_names": COOKIE_NAMES,
        "direct_download": True,
    }


@app.post("/process")
def process(req: ProcessRequest):
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    cleanup()
    source = ""

    if is_direct_media(url):
        try:
            f = download_direct(url)
            source = "direct"
        except Exception as e:
            f = None
            source = "direct_failed:" + str(e)[:120]
    else:
        f = None
        resolved, resolver_name = resolve_direct(url)
        if resolved:
            try:
                f = download_direct(resolved)
                source = "resolved:" + resolver_name
            except Exception as e:
                source = "resolved_failed:" + str(e)[:120]
        else:
            source = "resolve_none"

    if f is None:
        f, err = download_ytdlp(url)
        source = source + " | ytdlp:" + ("ok" if f is not None else "failed")
        if f is None:
            low = err.lower()
            hint = ""
            if ("login" in low) or ("rate-limit" in low) or ("cookies" in low):
                hint = " | instagram needs a logged-in session, check YTDLP_COOKIES_B64 in Render"
            raise HTTPException(
                status_code=500,
                detail="command failed: " + (err or "unknown error") + hint,
            )

    f = as_mp4(f)
    public = (PUBLIC_BASE + "/media/" + f.name) if PUBLIC_BASE else ("/media/" + f.name)
    return {
        "ok": True,
        "video_url": public,
        "file": f.name,
        "size": f.stat().st_size,
        "source": source,
        "chat_id": req.chat_id,
    }


@app.get("/media/{name}")
def media(name: str):
    safe = Path(name).name
    p = MEDIA_DIR / safe
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        str(p),
        media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=1800"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
