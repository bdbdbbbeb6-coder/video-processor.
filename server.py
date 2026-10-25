"""
Instagram promo video service for the Telegram Post Tracker automation.

What it does, in one sentence:
    Takes an Instagram reel link plus the price/text you want burned on it,
    downloads the reel, burns the Arabic text on the video, and returns a
    public link to the finished MP4 that Telegram can fetch and send.

Endpoints:
    GET  /health          -> quick check that ffmpeg and yt-dlp are installed
    POST /process         -> {"url": "...", "text": "...", "chat_id": "..."}
    GET  /media/<file>    -> serves the finished MP4 (supports Range requests)
"""

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./media")).resolve()
PUBLIC_BASE = os.getenv("PUBLIC_BASE", "").rstrip("/")
FONT_NAME = os.getenv("FONT_NAME", "Noto Naskh Arabic")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
YTDLP_BIN = os.getenv("YTDLP_BIN", "yt-dlp")
COOKIES_FILE = os.getenv("COOKIES_FILE", "./cookies.txt")
MAX_CLIP_SECONDS = int(os.getenv("MAX_CLIP_SECONDS", "60"))
FILE_TTL_SECONDS = int(os.getenv("FILE_TTL_SECONDS", str(60 * 60 * 6)))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
FONT_SIZE = int(os.getenv("FONT_SIZE", "64"))

MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Instagram Promo Renderer", version="1.0.0")
_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


class ProcessRequest(BaseModel):
    url: str
    text: str = ""
    chat_id: str = ""


def _run(cmd, timeout=300, cwd=None):
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        cwd=cwd,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "ignore").strip().splitlines()[-8:]
        raise RuntimeError("command failed: " + " | ".join(tail))
    return proc.stdout.decode("utf-8", "ignore")


def _download(url, job_id):
    cmd = [
        YTDLP_BIN,
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--socket-timeout", "30",
        "--retries", "3",
        "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "--print", "after_move:filepath",
        "-o", str(MEDIA_DIR / ("src_" + job_id + ".%(ext)s")),
    ]
    if Path(COOKIES_FILE).is_file():
        cmd[1:1] = ["--cookies", COOKIES_FILE]
    cmd.append(url)
    out = _run(cmd, timeout=300).strip().splitlines()
    if not out:
        raise RuntimeError("download produced no file")
    src = Path(out[-1].strip())
    if not src.is_file():
        raise RuntimeError("downloaded file not found: " + str(src))
    return src


def _escape_ass(text):
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        lines = ["عرض خاص"]
    wrapped = []
    for line in lines:
        while len(line) > 26:
            cut = line.rfind(" ", 0, 26)
            if cut < 8:
                cut = 26
            wrapped.append(line[:cut].strip())
            line = line[cut:].strip()
        if line:
            wrapped.append(line)
    return "\\N".join(wrapped[:4])


def _build_ass(text, path):
    safe = _escape_ass(text)
    content = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Promo," + FONT_NAME + "," + str(FONT_SIZE) + ",&H00FFFFFF,&H000000FF,"
        "&H00101010,&HA0000000,-1,0,0,0,100,100,0,0,3,10,0,2,70,70,260,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:10:00.00,Promo,,0,0,0,,{\\pos(540,1380)}" + safe + "\n"
    )
    path.write_text(content, encoding="utf-8")


def _render(src, ass_path, out):
    cmd = [
        FFMPEG_BIN, "-y", "-nostdin",
        "-i", str(src),
        "-vf", "ass=" + ass_path.name,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-t", str(MAX_CLIP_SECONDS),
        str(out),
    ]
    _run(cmd, timeout=600, cwd=str(MEDIA_DIR))
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError("render produced no video")


def _process_sync(job_id, req):
    src = _download(req.url, job_id)
    ass_path = MEDIA_DIR / ("overlay_" + job_id + ".ass")
    out = MEDIA_DIR / ("promo_" + job_id + ".mp4")
    try:
        _build_ass(req.text, ass_path)
        _render(src, ass_path, out)
    finally:
        for leftover in (src, ass_path):
            try:
                leftover.unlink()
            except OSError:
                pass
    return {"file": out.name, "status": "ready"}


@app.get("/health")
def health():
    return {
        "ok": True,
        "ffmpeg": bool(shutil.which(FFMPEG_BIN)),
        "yt_dlp": bool(shutil.which(YTDLP_BIN)),
        "font": FONT_NAME,
        "public_base": PUBLIC_BASE,
    }


@app.post("/process")
async def process(req: ProcessRequest, request: Request):
    url = (req.url or "").strip()
    if not url.lower().startswith("http"):
        raise HTTPException(status_code=400, detail="url is required")
    base = PUBLIC_BASE or str(request.base_url).rstrip("/")
    job_id = uuid.uuid4().hex[:12]
    async with _slots:
        try:
            result = await asyncio.to_thread(_process_sync, job_id, req)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)[:500])
    result["video_url"] = base + "/media/" + result["file"]
    return result


@app.get("/media/{name}")
def media(name: str, request: Request):
    safe = os.path.basename(name)
    path = (MEDIA_DIR / safe).resolve()
    if not str(path).startswith(str(MEDIA_DIR)) or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    size = path.stat().st_size
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        spec = range_header.split("=", 1)[1]
        start_raw, _, end_raw = spec.partition("-")
        try:
            start = int(start_raw) if start_raw else 0
            end = int(end_raw) if end_raw else size - 1
        except ValueError:
            raise HTTPException(status_code=416, detail="bad range")
        end = min(end, size - 1)
        if start > end:
            raise HTTPException(status_code=416, detail="range not satisfiable")
        length = end - start + 1

        def chunks():
            with path.open("rb") as handle:
                handle.seek(start)
                left = length
                while left > 0:
                    chunk = handle.read(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk

        return StreamingResponse(
            chunks(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": "bytes " + str(start) + "-" + str(end) + "/" + str(size),
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            },
        )
    return FileResponse(path, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})


async def _cleanup_loop():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for item in MEDIA_DIR.glob("*"):
            try:
                if item.is_file() and now - item.stat().st_mtime > FILE_TTL_SECONDS:
                    item.unlink()
            except OSError:
                pass


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_cleanup_loop())
