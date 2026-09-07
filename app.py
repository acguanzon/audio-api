"""Self-hosted, authenticated audio conversion API.

Use only with media you own or are authorised to download. The server converts
one approved YouTube URL at a time to an expiring MP3 download.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_file

load_dotenv()
APP_KEY = os.environ.get("AUDIO_API_KEY", "")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "./downloads")).resolve()
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "300"))
RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "10"))
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", "1800"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))
COOKIE_FILE = Path(os.environ.get("YTDLP_COOKIE_FILE", "/tmp/youtube-cookies.txt"))

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}

app = Flask(__name__)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def configure_ytdlp_cookies() -> None:
    """Write a Netscape cookie file supplied only through a server secret."""
    encoded_cookies = os.environ.get("YTDLP_COOKIES_B64", "").strip()
    if not encoded_cookies:
        return
    try:
        cookie_data = base64.b64decode(encoded_cookies, validate=True)
        if not cookie_data.startswith(b"# Netscape HTTP Cookie File"):
            raise ValueError("not a Netscape cookie file")
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_bytes(cookie_data)
        COOKIE_FILE.chmod(0o600)
        app.logger.info("yt-dlp cookie authentication is configured")
    except (ValueError, OSError) as error:
        app.logger.error("Invalid YTDLP_COOKIES_B64 secret: %s", error)


configure_ytdlp_cookies()
_jobs = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)
_tokens: dict[str, tuple[Path, float]] = {}
_token_lock = threading.Lock()
_requests: dict[str, deque[float]] = defaultdict(deque)
_request_lock = threading.Lock()


def require_api_key() -> None:
    if not APP_KEY:
        abort(503, description="Server is missing AUDIO_API_KEY configuration.")
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not secrets.compare_digest(supplied, APP_KEY):
        abort(401, description="Invalid API key.")


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        abort(400, description="Only HTTPS YouTube URLs are accepted.")


def enforce_rate_limit() -> None:
    client = request.remote_addr or "unknown"
    now = time.time()
    cutoff = now - 3600
    with _request_lock:
        timestamps = _requests[client]
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        if len(timestamps) >= RATE_LIMIT_PER_HOUR:
            abort(429, description="Hourly conversion limit reached.")
        timestamps.append(now)


def cleanup_expired_files() -> None:
    now = time.time()
    expired: list[Path] = []
    with _token_lock:
        for token, (path, expires_at) in list(_tokens.items()):
            if expires_at <= now:
                _tokens.pop(token, None)
                expired.append(path)
    for path in expired:
        path.unlink(missing_ok=True)


def audio_filename(video_id: str | None) -> Path:
    safe_id = video_id if video_id and video_id.replace("-", "").replace("_", "").isalnum() else secrets.token_hex(12)
    return DOWNLOAD_DIR / f"{safe_id}-{secrets.token_hex(6)}.mp3"


@app.get("/health")
def health():
    return jsonify(status="ok", ffmpeg_available=shutil.which("ffmpeg") is not None)


@app.post("/v1/conversions")
def create_conversion():
    require_api_key()
    enforce_rate_limit()
    cleanup_expired_files()

    payload = request.get_json(silent=True) or {}
    url = payload.get("url", "")
    if not isinstance(url, str) or not url.strip():
        abort(400, description="JSON field 'url' is required.")
    validate_url(url)

    if not _jobs.acquire(blocking=False):
        return jsonify(error="A conversion is already in progress. Try again shortly."), 429

    output_path: Path | None = None
    try:
        probe_options = {"quiet": True, "no_warnings": True, "skip_download": True}
        if COOKIE_FILE.exists():
            probe_options["cookiefile"] = str(COOKIE_FILE)
        with yt_dlp.YoutubeDL(probe_options) as ydl:
            info = ydl.extract_info(url, download=False)
        duration = info.get("duration") or 0
        if duration > MAX_DURATION_SECONDS:
            abort(400, description=f"Videos must be {MAX_DURATION_SECONDS // 60} minutes or shorter.")

        output_path = audio_filename(info.get("id"))
        options = {
            "format": "bestaudio/best",
            "outtmpl": str(output_path.with_suffix(".%(ext)s")),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
        if COOKIE_FILE.exists():
            options["cookiefile"] = str(COOKIE_FILE)
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        if not output_path.exists() or output_path.stat().st_size < 32_000:
            raise RuntimeError("Conversion did not produce a valid MP3 file.")

        token = secrets.token_urlsafe(32)
        expires_at = time.time() + TOKEN_TTL_SECONDS
        with _token_lock:
            _tokens[token] = (output_path, expires_at)
        return jsonify(
            token=token,
            expires_in_seconds=TOKEN_TTL_SECONDS,
            title=info.get("title"),
            duration_seconds=duration,
        ), 201
    except yt_dlp.utils.DownloadError as error:
        if output_path:
            output_path.unlink(missing_ok=True)
        app.logger.warning("yt-dlp conversion failed for %s: %s", url, error)
        return jsonify(error="The video could not be downloaded or converted."), 422
    except Exception as error:
        if output_path:
            output_path.unlink(missing_ok=True)
        app.logger.exception("Unexpected conversion failure for %s", url)
        return jsonify(error="The video could not be converted on the server."), 500
    finally:
        _jobs.release()


@app.get("/v1/downloads/<token>")
def download(token: str):
    require_api_key()
    cleanup_expired_files()
    with _token_lock:
        entry = _tokens.pop(token, None)  # one-time download token
    if entry is None:
        abort(404, description="Download token is invalid or expired.")

    path, _ = entry
    if not path.exists():
        abort(404, description="Converted file is no longer available.")
    response = send_file(path, mimetype="audio/mpeg", as_attachment=True, download_name="audio.mp3")

    @response.call_on_close
    def remove_file() -> None:
        path.unlink(missing_ok=True)

    return response


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(404)
@app.errorhandler(429)
@app.errorhandler(503)
def client_error(error):
    return jsonify(error=error.description), error.code


if __name__ == "__main__":
    if not APP_KEY:
        raise SystemExit("Set AUDIO_API_KEY before starting the server.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("FFmpeg must be installed and available on PATH.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
