"""Instagram Promo Renderer - memory safe edition."""
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/tmp/media"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "").rstrip("/")
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "1920"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "120"))
MEM_LIMIT_MB = int(os.getenv("MEM_LIMIT_MB", "900"))
FONT_CANDIDATES = [
    os.getenv("FONT_FILE", ""),
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

app = FastAPI(title="Instagram Promo Renderer")


class ProcessRequest(BaseModel):
    url: str
    text: Optional[str] = ""
    chat_id: Optional[str] = ""


def font_file() -> str:
    for candidate in FONT_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def limits():
    import resource

    def apply():
        limit = MEM_LIMIT_MB * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:
            pass

    return apply


def download(url: str, workdir: Path) -> Path:
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings", "--no-progress",
        "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b",
        "--merge-output-format", "mp4",
        "--max-filesize", str(MAX_UPLOAD_MB) + "M",
        "--socket-timeout", "30", "--retries", "2",
        "-o", str(workdir / "src.%(ext)s"), url,
    ]
    result = run(cmd, timeout=420)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "download failed").strip()[-800:])
    files = [p for p in workdir.iterdir() if p.suffix.lower() == ".mp4" and p.stat().st_size > 0]
    if not files:
        raise RuntimeError("no media downloaded")
    return max(files, key=lambda p: p.stat().st_size)


def burn(src: Path, dst: Path, text: str, workdir: Path) -> None:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()][:3]
    if not lines:
        shutil.copyfile(src, dst)
        return
    overlay = workdir / "overlay.txt"
    overlay.write_text("\n".join(lines), encoding="utf-8")
    font = font_file()
    draw = (
        "drawtext=fontfile='" + font + "':textfile='" + str(overlay) + "'"
        ":fontcolor=white:fontsize=h/24:box=1:boxcolor=black@0.45:boxborderw=26"
        ":x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=14"
    )
    vf = "scale='min(" + str(MAX_HEIGHT) + ",iw)':-2," + draw
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-filter_threads", "1",
        "-i", str(src), "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(dst),
    ]
    result = run(cmd, timeout=600)
    if result.returncode != 0 or not dst.exists():
        raise RuntimeError((result.stderr or "ffmpeg failed").strip()[-800:])


@app.get("/health")
def health():
    return {
        "ok": True,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "yt_dlp": shutil.which("yt-dlp") is not None,
        "font": font_file(),
        "public_base": PUBLIC_BASE,
    }


@app.get("/resolve")
def resolve(url: str = Query(...)):
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "-f", "bv*[height<=1080][ext=mp4]+ba/b[height<=1080]/b",
        "-g", url,
    ]
    result = run(cmd, timeout=120)
    if result.returncode != 0:
        raise HTTPException(502, (result.stderr or "resolve failed").strip()[-500:])
    links = [line.strip() for line in result.stdout.split() if line.strip().startswith("http")]
    if not links:
        raise HTTPException(404, "no direct video url")
    return {"status": "ok", "video_url": links[0], "alternate": links[1] if len(links) > 1 else ""}


@app.post("/process")
def process(req: ProcessRequest, request: Request):
    workdir = Path(tempfile.mkdtemp(prefix="job_"))
    try:
        src = download(req.url, workdir)
        name = uuid.uuid4().hex + ".mp4"
        dst = MEDIA_DIR / name
        burned = False
        try:
            burn(src, dst, req.text or "", workdir)
            burned = True
        except Exception as exc:
            shutil.copyfile(src, dst)
            print("burn failed, serving original:", exc)
        base = PUBLIC_BASE or str(request.base_url).rstrip("/")
        return {
            "status": "ok",
            "media_url": base + "/media/" + name,
            "file": name,
            "text_applied": burned,
            "size": dst.stat().st_size,
        }
    except Exception as exc:
        raise HTTPException(500, type(exc).__name__ + ": " + str(exc)[:700])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.get("/media/{name}")
def media(name: str):
    target = MEDIA_DIR / Path(name).name
    if not target.exists():
        raise HTTPException(404, "not found")
    return FileResponse(target, media_type="video/mp4", filename=target.name)
