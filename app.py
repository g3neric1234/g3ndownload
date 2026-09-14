import os
import tempfile
import shutil
import json
import re
import urllib.parse
import urllib.request
import queue
import threading
import zipfile
from pathlib import Path
from difflib import SequenceMatcher

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="G3nDownload")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
FFMPEG_DIR = os.getenv("FFMPEG_DIR")

try:
    import imageio_ffmpeg
    if not FFMPEG_DIR:
        FFMPEG_DIR = str(Path(imageio_ffmpeg.get_ffmpeg_exe()).parent)
except Exception:
    pass

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_AGENT = "G3nDownload/1.0 (https://g3ndownload.vercel.app)"

class SearchRequest(BaseModel):
    query: str

class DownloadRequest(BaseModel):
    url: str
    mode: str

class MetadataRequest(BaseModel):
    url: str
    title: str = ""
    artist: str = ""
    duration: float | None = None

class CollectionRequest(BaseModel):
    query: str

class ResolveTrackRequest(BaseModel):
    title: str
    artist: str = ""

class CollectionDownloadRequest(BaseModel):
    collection_type: str
    title: str
    artist: str = ""
    mode: str
    items: list[dict]


def ydl_base():
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "embedthumbnail": True,
        "addmetadata": True,
        "embedmetadata": True,
    }
    if FFMPEG_DIR:
        options["ffmpeg_location"] = FFMPEG_DIR
    return options


def item_data(info):
    video_id = info.get("id")
    thumbnail = info.get("thumbnail")
    if not thumbnail and video_id:
        thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    webpage_url = info.get("webpage_url") or info.get("original_url") or info.get("url")
    if not webpage_url and video_id:
        webpage_url = f"https://www.youtube.com/watch?v={video_id}"
    return {
        "id": video_id,
        "title": info.get("title") or "Sin título",
        "url": webpage_url,
        "artist": info.get("artists") or info.get("artist") or info.get("uploader"),
        "creator": info.get("creator") or info.get("channel") or info.get("uploader"),
        "producer": info.get("producer"),
        "album": info.get("album"),
        "year": info.get("release_year") or info.get("upload_date", "")[:4],
        "thumbnail": thumbnail,
        "description": info.get("description"),
        "duration": info.get("duration"),
        "channel": info.get("channel"),
        "uploader": info.get("uploader"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "categories": info.get("categories") or [],
        "tags": info.get("tags") or [],
        "webpage_url": webpage_url,
    }


def clean_music_title(title):
    value = re.sub(r"\s+", " ", title or "").strip()
    value = re.sub(r"\[[^\]]*(official|video|audio|lyrics?|visualizer|music)[^\]]*\]", "", value, flags=re.I)
    value = re.sub(r"\(([^)]*(official|video|audio|lyrics?|visualizer|music)[^)]*)\)", "", value, flags=re.I)
    value = re.sub(r"\b(official\s+music\s+video|official\s+video|official\s+audio|lyrics?|lyric\s+video|music\s+video|visualizer)\b", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -–—")
    return value


def text_value(value):
    if isinstance(value, list):
        return ", ".join(text_value(item) for item in value if item)
    if isinstance(value, dict):
        return value.get("name") or value.get("title") or ""
    return str(value or "")


def normalize(value):
    value = text_value(value).lower()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def musicbrainz_request(path, params):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{MUSICBRAINZ_URL}/{path}?{query}",
        headers={"User-Agent": MUSICBRAINZ_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def find_musicbrainz_metadata(title, artist, duration):
    cleaned_title = clean_music_title(title)
    if not cleaned_title:
        return {}
    artist_value = text_value(artist)
    query_parts = [f'recording:"{cleaned_title}"']
    if artist_value:
        query_parts.append(f'artist:"{artist_value}"')
    try:
        data = musicbrainz_request("recording", {"query": " AND ".join(query_parts), "fmt": "json", "limit": "8", "inc": "artist-credits+releases+isrcs"})
    except Exception:
        return {}
    recordings = data.get("recordings") or []
    best = None
    best_score = 0.0
    for recording in recordings:
        recording_title = recording.get("title") or ""
        title_score = similarity(cleaned_title, recording_title)
        artist_score = 0.0
        credits = recording.get("artist-credit") or []
        mb_artists = []
        for credit in credits:
            artist_obj = credit.get("artist") or {}
            if artist_obj.get("name"):
                mb_artists.append(artist_obj["name"])
        if artist_value and mb_artists:
            artist_score = max(similarity(artist_value, name) for name in mb_artists)
        elif not artist_value:
            artist_score = 0.5
        duration_score = 0.5
        mb_duration = recording.get("length")
        if duration and mb_duration:
            difference = abs(float(duration) - (float(mb_duration) / 1000))
            duration_score = max(0.0, 1.0 - min(difference / 20.0, 1.0))
        score = title_score * 0.6 + artist_score * 0.25 + duration_score * 0.15
        if score > best_score:
            best_score = score
            best = recording
    if not best or best_score < 0.55:
        return {}
    credits = best.get("artist-credit") or []
    artists = []
    for credit in credits:
        artist_obj = credit.get("artist") or {}
        name = artist_obj.get("name")
        if name:
            artists.append(name)
    releases = best.get("releases") or []
    release = releases[0] if releases else {}
    release_id = release.get("id")
    release_title = release.get("title")
    release_date = release.get("date") or ""
    result = {
        "source": "MusicBrainz",
        "match_score": round(best_score, 3),
        "artist": ", ".join(artists) if artists else None,
        "album": release_title,
        "year": release_date[:4] if release_date else None,
        "isrc": (best.get("isrcs") or [None])[0],
        "musicbrainz_recording_id": best.get("id"),
        "musicbrainz_release_id": release_id,
    }
    if release_id:
        try:
            cover_request = urllib.request.Request(f"https://coverartarchive.org/release/{release_id}", headers={"User-Agent": MUSICBRAINZ_AGENT})
            with urllib.request.urlopen(cover_request, timeout=10) as response:
                cover_data = json.loads(response.read().decode("utf-8"))
            images = cover_data.get("images") or []
            front = next((image for image in images if image.get("front")), images[0] if images else None)
            if front:
                thumbnails = front.get("thumbnails") or {}
                result["cover"] = thumbnails.get("large") or thumbnails.get("500") or front.get("image")
        except Exception:
            result["cover"] = f"https://coverartarchive.org/release/{release_id}/front-500"
    return result


def search_musicbrainz_release(query):
    try:
        data = musicbrainz_request("release", {"query": f'release:"{query}"', "fmt": "json", "limit": "8", "inc": "artist-credits"})
    except Exception:
        return []
    releases = data.get("releases") or []
    scored = []
    for release in releases:
        title = release.get("title") or ""
        score = similarity(query, title) * 0.75 + float(release.get("score") or 0) / 100 * 0.25
        scored.append((score, release))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [release for _, release in scored]


def get_release_details(release_id):
    return musicbrainz_request(f"release/{release_id}", {"fmt": "json", "inc": "recordings+artist-credits+release-groups"})


def release_to_collection(release):
    artist = release.get("artist-credit-phrase") or text_value(release.get("artist-credit"))
    tracks = []
    position = 0
    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            position += 1
            recording = track.get("recording") or {}
            track_artist = recording.get("artist-credit-phrase") or artist
            length = recording.get("length")
            tracks.append({
                "position": position,
                "title": recording.get("title") or track.get("title") or "Sin título",
                "artist": track_artist,
                "duration": round(float(length) / 1000, 2) if length else None,
                "url": None,
            })
    cover = None
    release_id = release.get("id")
    if release_id:
        cover = f"https://coverartarchive.org/release/{release_id}/front-500"
    return {
        "type": "album",
        "title": release.get("title") or "Álbum",
        "artist": artist or "Artista desconocido",
        "year": (release.get("date") or "")[:4] or None,
        "thumbnail": cover,
        "release_id": release_id,
        "tracks": tracks,
    }


def youtube_playlist(query):
    options = ydl_base()
    options["noplaylist"] = False
    options["extract_flat"] = True
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=False)
    if info.get("_type") != "playlist" and not info.get("entries"):
        raise RuntimeError("La URL no contiene una playlist reconocible.")
    entries = [entry for entry in (info.get("entries") or []) if entry]
    items = [item_data(entry) for entry in entries]
    return {
        "type": "playlist",
        "title": info.get("title") or "Playlist",
        "artist": info.get("uploader") or info.get("channel") or "",
        "year": None,
        "thumbnail": info.get("thumbnail") or (items[0].get("thumbnail") if items else None),
        "tracks": items,
    }


def resolve_youtube_track(title, artist=""):
    search = f"{artist} - {title}" if artist else title
    options = ydl_base()
    options["extract_flat"] = True
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch5:{search}", download=False)
    entries = info.get("entries") or []
    if not entries:
        raise RuntimeError(f"No se encontró un video para: {title}")
    best = None
    best_score = 0
    for entry in entries:
        entry_title = entry.get("title") or ""
        score = similarity(title, entry_title)
        if artist:
            score = score * 0.7 + similarity(artist, entry.get("uploader") or entry.get("channel") or "") * 0.3
        if score > best_score:
            best_score = score
            best = entry
    if not best:
        best = entries[0]
    return item_data(best)


def find_collection(query):
    value = query.strip()
    if value.startswith(("http://", "https://")):
        if "list=" in value or "/playlist" in value:
            return youtube_playlist(value)
        raise RuntimeError("Para descargar un contenido individual usa la búsqueda normal. Aquí puedes pegar un enlace de playlist de YouTube.")
    releases = search_musicbrainz_release(value)
    if not releases:
        raise RuntimeError("No se encontró ningún álbum en MusicBrainz.")
    for release in releases[:3]:
        try:
            details = get_release_details(release.get("id"))
            collection = release_to_collection(details)
            if collection["tracks"]:
                return collection
        except Exception:
            continue
    raise RuntimeError("Se encontró el álbum, pero no se pudieron obtener sus canciones.")


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
                if info.get("_type") == "playlist":
                    entries = [entry for entry in (info.get("entries") or []) if entry]
                else:
                    entries = [info]
            else:
                info = ydl.extract_info(f"ytsearch8:{query}", download=False)
                entries = info.get("entries") or []
        return {"results": [item_data(item) for item in entries if item]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/metadata")
def metadata(request: MetadataRequest):
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Falta la URL.")
    title = request.title.strip()
    artist = request.artist.strip()
    duration = request.duration
    try:
        options = ydl_base()
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        base = item_data(info)
        title = base.get("title") or title
        artist = base.get("artist") or artist
        duration = base.get("duration") or duration
    except Exception:
        base = {"title": title, "artist": artist, "duration": duration}
    external = find_musicbrainz_metadata(title, artist, duration)
    result = dict(base)
    for field in ("artist", "album", "year", "producer"):
        if not result.get(field) and external.get(field):
            result[field] = external[field]
    if external.get("cover"):
        result["thumbnail"] = external["cover"]
    result["metadata_source"] = external.get("source")
    result["metadata_match_score"] = external.get("match_score")
    result["isrc"] = external.get("isrc")
    return result


@app.post("/api/collection")
def collection(request: CollectionRequest):
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Escribe el nombre de un álbum o pega una playlist.")
    try:
        return find_collection(query)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/resolve-track")
def resolve_track(request: ResolveTrackRequest):
    if not request.title.strip():
        raise HTTPException(status_code=400, detail="Falta el título de la canción.")
    try:
        return resolve_youtube_track(request.title.strip(), request.artist.strip())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def download_stream(url, mode):
    if mode not in {"mp4", "mp3"}:
        raise HTTPException(status_code=400, detail="Formato no válido.")
    temp_dir = Path(tempfile.mkdtemp(prefix="g3ndownload-"))
    events = queue.Queue()
    progress_files = {}

    def push(event):
        events.put(event)

    def progress_hook(data):
        status = data.get("status")
        if status not in {"downloading", "finished"}:
            return
        filename = str(data.get("filename") or "download")
        downloaded = int(data.get("downloaded_bytes") or 0)
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        total = int(total) if total else None
        if status == "finished":
            downloaded = total or downloaded
        progress_files[filename] = {"downloaded": downloaded, "total": total}
        downloaded_total = sum(item["downloaded"] for item in progress_files.values())
        known_totals = [item["total"] for item in progress_files.values() if item["total"]]
        total_total = sum(known_totals) if known_totals else None
        push({"status": "downloading" if status == "downloading" else "processing", "downloaded": downloaded_total, "total": total_total, "speed": data.get("speed"), "eta": data.get("eta"), "filename": Path(filename).name})

    def worker():
        try:
            options = ydl_base()
            options["outtmpl"] = str(temp_dir / "%(title)s.%(ext)s")
            options["progress_hooks"] = [progress_hook]
            if mode == "mp4":
                options.update({"format": "bv*+ba/b", "merge_output_format": "mp4", "postprocessors": [{"key": "FFmpegMetadata"}, {"key": "EmbedThumbnail"}]})
            else:
                options.update({"format": "bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}, {"key": "FFmpegMetadata"}, {"key": "EmbedThumbnail"}]})
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                prepared = Path(ydl.prepare_filename(info))
            target = prepared.with_suffix(".mp3" if mode == "mp3" else ".mp4")
            if not target.exists():
                candidates = list(temp_dir.glob("*.mp4" if mode == "mp4" else "*.mp3"))
                if candidates:
                    target = candidates[0]
            if not target.exists():
                files = [p for p in temp_dir.iterdir() if p.is_file() and p.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp"}]
                if files:
                    target = files[0]
            if not target.exists():
                raise RuntimeError("No se pudo generar el archivo.")
            total = target.stat().st_size
            push({"status": "ready", "downloaded": total, "total": total, "filename": target.name, "file_size": total})
            with target.open("rb") as file:
                while True:
                    chunk = file.read(1024 * 1024)
                    if not chunk:
                        break
                    events.put(("chunk", chunk))
        except Exception as exc:
            events.put({"status": "error", "message": str(exc)})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        marker = b"\n---G3NDOWNLOAD-FILE---\n"
        yield b"G3NDOWNLOAD-PROGRESS\n"
        while True:
            event = events.get()
            if event is None:
                break
            if isinstance(event, tuple) and event[0] == "chunk":
                yield event[1]
                continue
            yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            if event.get("status") == "ready":
                yield marker
    return StreamingResponse(stream(), media_type="application/octet-stream", headers={"X-G3nDownload-Protocol": "1", "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.post("/api/download")
def download(request: DownloadRequest):
    return download_stream(request.url.strip(), request.mode)


def collection_download_stream(request):
    if request.mode not in {"mp3", "mp4"}:
        raise HTTPException(status_code=400, detail="Formato no válido.")
    if not request.items:
        raise HTTPException(status_code=400, detail="La colección no contiene canciones.")
    temp_dir = Path(tempfile.mkdtemp(prefix="g3ndownload-album-"))
    events = queue.Queue()
    progress_files = {}
    total_items = len(request.items)

    def push(event):
        events.put(event)

    def progress_hook(data):
        status = data.get("status")
        if status not in {"downloading", "finished"}:
            return
        filename = str(data.get("filename") or "download")
        downloaded = int(data.get("downloaded_bytes") or 0)
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        total = int(total) if total else None
        if status == "finished":
            downloaded = total or downloaded
        progress_files[filename] = {"downloaded": downloaded, "total": total}
        downloaded_total = sum(item["downloaded"] for item in progress_files.values())
        known_totals = [item["total"] for item in progress_files.values() if item["total"]]
        total_total = sum(known_totals) if known_totals else None
        completed = sum(1 for item in progress_files.values() if item.get("total") and item["downloaded"] >= item["total"])
        push({"status": "downloading" if status == "downloading" else "processing", "downloaded": downloaded_total, "total": total_total, "speed": data.get("speed"), "eta": data.get("eta"), "filename": Path(filename).name, "completed": min(completed, total_items), "items": total_items})

    def worker():
        try:
            options = ydl_base()
            options["noplaylist"] = True
            options["outtmpl"] = str(temp_dir / "%(title)s.%(ext)s")
            options["progress_hooks"] = [progress_hook]
            if request.mode == "mp4":
                options.update({"format": "bv*+ba/b", "merge_output_format": "mp4", "postprocessors": [{"key": "FFmpegMetadata"}, {"key": "EmbedThumbnail"}]})
            else:
                options.update({"format": "bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}, {"key": "FFmpegMetadata"}, {"key": "EmbedThumbnail"}]})
            for index, item in enumerate(request.items, 1):
                url = item.get("url")
                if not url:
                    resolved = resolve_youtube_track(item.get("title") or "", item.get("artist") or request.artist)
                    url = resolved.get("webpage_url") or resolved.get("url")
                if not url:
                    raise RuntimeError(f"No se pudo encontrar: {item.get('title') or 'tema'}")
                push({"status": "track", "index": index, "items": total_items, "title": item.get("title") or "Tema"})
                track_dir = temp_dir / f"track-{index:03d}"
                track_dir.mkdir(parents=True, exist_ok=True)
                track_options = dict(options)
                track_options["outtmpl"] = str(track_dir / f"{index:03d} - %(title)s.%(ext)s")
                with yt_dlp.YoutubeDL(track_options) as ydl:
                    ydl.extract_info(url, download=True)
                generated = [p for p in track_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp3", ".mp4"}]
                if not generated:
                    raise RuntimeError(f"No se pudo generar: {item.get('title') or 'tema'}")
                for generated_file in generated:
                    target_file = temp_dir / generated_file.name
                    shutil.move(str(generated_file), str(target_file))
                shutil.rmtree(track_dir, ignore_errors=True)
            files = [p for p in temp_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp3", ".mp4"}]
            if not files:
                raise RuntimeError("No se generaron archivos para el ZIP.")
            zip_path = temp_dir / f"{safe_filename(request.title)}.zip"
            push({"status": "zipping", "downloaded": 0, "total": len(files), "filename": zip_path.name, "items": len(files)})
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for index, file in enumerate(sorted(files), 1):
                    archive.write(file, arcname=file.name)
                    push({"status": "zipping", "downloaded": index, "total": len(files), "filename": file.name, "items": len(files)})
            total = zip_path.stat().st_size
            push({"status": "ready", "downloaded": total, "total": total, "filename": zip_path.name, "file_size": total, "file_mime": "application/zip"})
            with zip_path.open("rb") as file:
                while True:
                    chunk = file.read(1024 * 1024)
                    if not chunk:
                        break
                    events.put(("chunk", chunk))
        except Exception as exc:
            events.put({"status": "error", "message": str(exc)})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        marker = b"\n---G3NDOWNLOAD-FILE---\n"
        yield b"G3NDOWNLOAD-PROGRESS\n"
        while True:
            event = events.get()
            if event is None:
                break
            if isinstance(event, tuple) and event[0] == "chunk":
                yield event[1]
                continue
            yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            if event.get("status") == "ready":
                yield marker
    return StreamingResponse(stream(), media_type="application/octet-stream", headers={"X-G3nDownload-Protocol": "1", "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


def safe_filename(value):
    return re.sub(r"[\\/:*?\"<>|]", "_", value or "G3nDownload").strip()[:120] or "G3nDownload"


@app.post("/api/collection-download")
def collection_download(request: CollectionDownloadRequest):
    return collection_download_stream(request)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
