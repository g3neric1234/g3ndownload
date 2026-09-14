import os
import tempfile
import shutil
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Media Downloader")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
FFMPEG_DIR = os.getenv("FFMPEG_DIR")
try:
    import imageio_ffmpeg
    if not FFMPEG_DIR:
        FFMPEG_DIR = str(Path(imageio_ffmpeg.get_ffmpeg_exe()).parent)
except Exception:
    pass

class SearchRequest(BaseModel):
    query: str

class DownloadRequest(BaseModel):
    url: str
    mode: str

def ydl_base():
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "writethumbnail": True,
        "embedthumbnail": True,
        "addmetadata": True,
        "embedmetadata": True,
        "writeinfojson": True,
    }
    if FFMPEG_DIR:
        options["ffmpeg_location"] = FFMPEG_DIR
    return options

def item_data(info):
    return {
        "id": info.get("id"),
        "title": info.get("title") or "Sin título",
        "url": info.get("webpage_url") or info.get("original_url"),
        "artist": info.get("artists") or info.get("artist") or info.get("uploader"),
        "creator": info.get("creator") or info.get("channel") or info.get("uploader"),
        "producer": info.get("producer"),
        "album": info.get("album"),
        "year": info.get("release_year") or info.get("upload_date", "")[:4],
        "thumbnail": info.get("thumbnail"),
        "description": info.get("description"),
        "duration": info.get("duration"),
        "channel": info.get("channel"),
        "uploader": info.get("uploader"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "categories": info.get("categories") or [],
        "tags": info.get("tags") or [],
        "webpage_url": info.get("webpage_url"),
    }

@app.get("/api/health")
def health():
    return {"ok": True}

@app.post("/api/search")
def search(request: SearchRequest):
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Escribe una búsqueda o URL.")

    options = ydl_base()
    options["extract_flat"] = True

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            if query.startswith(("http://", "https://")):
                info = ydl.extract_info(query, download=False)
                entries = info.get("entries") if info.get("_type") == "playlist" else [info]
            else:
                info = ydl.extract_info(f"ytsearch8:{query}", download=False)
                entries = info.get("entries") or []

        return {"results": [item_data(item) for item in entries if item]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@app.post("/api/download")
def download(request: DownloadRequest):
    if request.mode not in {"mp4", "mp3"}:
        raise HTTPException(status_code=400, detail="Formato no válido.")

    temp_dir = Path(tempfile.mkdtemp(prefix="media-downloader-"))

    options = ydl_base()
    options["outtmpl"] = str(temp_dir / "%(title)s.%(ext)s")

    if request.mode == "mp4":
        options.update({
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
        })
    else:
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                },
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
        })

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(request.url, download=True)
            prepared = Path(ydl.prepare_filename(info))

        if request.mode == "mp3":
            target = prepared.with_suffix(".mp3")
        else:
            target = prepared.with_suffix(".mp4")
            if not target.exists():
                candidates = list(temp_dir.glob("*.mp4"))
                if candidates:
                    target = candidates[0]

        if not target.exists():
            files = [p for p in temp_dir.iterdir() if p.is_file() and p.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp"}]
            if files:
                target = files[0]

        if not target.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail="No se pudo generar el archivo.")

        from starlette.background import BackgroundTask
        cleanup = BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True)

        return FileResponse(
            target,
            filename=target.name,
            media_type="audio/mpeg" if request.mode == "mp3" else "video/mp4",
            background=cleanup,
        )
    except HTTPException:
        raise
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
