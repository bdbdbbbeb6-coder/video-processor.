"""معالج الفيديو - نسخة مُصلَحة: /health يبقى سريعاً أثناء المعالجة، وتحميل بثقة."""
import base64, os, shutil, subprocess, uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

APP_DIR = Path(os.getenv("APP_DIR", "/app"))
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", str(APP_DIR / "media")))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

YTDLP_BIN = os.getenv("YTDLP_BIN", "yt-dlp")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "").rstrip("/")
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "90"))
BURN_TIMEOUT = int(os.getenv("BURN_TIMEOUT", "90"))
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "1280"))
UA = os.getenv("YTDLP_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")
COOKIES_PATH = Path(os.getenv("YTDLP_COOKIES", "/tmp/yt_cookies.txt"))


# الكوكيز تصل كمتغير بيئة بترميز base64 فلا تُرفع إلى GitHub
raw_cookies = os.getenv("YTDLP_COOKIES_B64", "").strip()
if raw_cookies:
    try:
        COOKIES_PATH.write_bytes(base64.b64decode(raw_cookies))
        COOKIES_PATH.chmod(0o600)
    except Exception:
        pass

app = FastAPI()

class ProcessRequest(BaseModel):
    url: str
    text: str = ""
    chat_id: str = ""

def run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"انتهت المهلة بعد {timeout} ثانية"
    except Exception as exc:
        return 1, "", str(exc)


# نقطة end-point عادية (def) لا async: يعمل FastAPI في خيوط منفصلة

# فيبقى /health مستجيباً أثناء تنفيذ yt-dlp بدل أن تتجمد الخدمة كلها
@app.get("/health")
def health():
    return {
        "ok": True,
        "ffmpeg": shutil.which(FFMPEG_BIN) is not None,
        "yt_dlp": shutil.which(YTDLP_BIN) is not None,
        "font": os.getenv("FONT_NAME", "DejaVu Sans"),
        "public_base": PUBLIC_BASE,
        "cookies": COOKIES_PATH.exists(),
    }

@app.post("/process")
def process(req: ProcessRequest, request: Request):
    url = (req.url or "").strip()
    if not url:
        return JSONResponse(status_code=400, content={"ok": False, "error": "الرابط مطلوب"})

    job_id = uuid.uuid4().hex[:12]
    raw_path = MEDIA_DIR / f"{job_id}.mp4"
    out_path = MEDIA_DIR / f"{job_id}_burned.mp4"

    cmd = [YTDLP_BIN, "--no-playlist", "--no-part", "--no-progress",
           "--socket-timeout", "20", "--retries", "2", "--extractor-retries", "2",
           "--fragment-retries", "2", "--user-agent", UA,
           "-o", str(raw_path), url]
    if COOKIES_PATH.exists():
        cmd += ["--cookies", str(COOKIES_PATH)]

    code, out, err = run(cmd, DOWNLOAD_TIMEOUT)
    if code != 0 or not raw_path.exists():
        return JSONResponse(status_code=502, content={"ok": False, "error": (err or out)[-1200:]})

    final = raw_path
    if req.text.strip():
        safe = req.text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "")
        vf = (f"scale=-2:'min({MAX_HEIGHT},ih)':force_original_aspect_ratio=decrease,"
              f"drawtext=text='{safe}':fontcolor=white:fontsize=h/18:"
              f"box=1:boxcolor=black@0.55:boxborderw=30:x=(w-text_w)/2:y=(h-text_h)/2")
        code, out, err = run([FFMPEG_BIN, "-y", "-i", str(raw_path), "-vf", vf,
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                              "-c:a", "aac", "-threads", "1", "-movflags", "+faststart",
                              str(out_path)], BURN_TIMEOUT)
        final = out_path if (code == 0 and out_path.exists()) else raw_path

    base = PUBLIC_BASE or str(request.base_url).rstrip("/")
    return {"ok": True, "media_url": f"{base}/media/{final.name}", "file": final.name, "source": url}

@app.get("/media/{name}")
def media(name: str):
    path = MEDIA_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="الملف غير موجود")
    return FileResponse(path, media_type="video/mp4")
