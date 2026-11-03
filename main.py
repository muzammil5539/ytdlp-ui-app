"""yt-dlp UI: download queue + built-in scheduler.

Run:  python main.py            (or: uv run python main.py)
Env:  YTDLP_UI_HOME            data dir (jobs.db, downloads/, archives/). Default: next to main.py
      YTDLP_UI_HOST / _PORT    default 127.0.0.1 / 8000
      YTDLP_UI_MAX_CONCURRENT  parallel downloads, default 3
      YTDLP_UI_MIN_FREE_GB     refuse to start a download below this free space, default 5
"""
import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
HOME = Path(os.environ.get("YTDLP_UI_HOME") or APP_DIR).expanduser().resolve()
DB_FILE = HOME / "jobs.db"
ARCHIVES = HOME / "archives"  # per-schedule yt-dlp archives: "only download what's new"
HOST = os.environ.get("YTDLP_UI_HOST", "127.0.0.1")
PORT = int(os.environ.get("YTDLP_UI_PORT", "8000"))
MAX_CONCURRENT = int(os.environ.get("YTDLP_UI_MAX_CONCURRENT", "3"))
MIN_FREE_GB = float(os.environ.get("YTDLP_UI_MIN_FREE_GB", "5"))
SUB_LANGS = [x for x in os.environ.get("YTDLP_UI_SUB_LANGS", "").split(",") if x.strip()]  # e.g. "en" or "en,es"; empty = no subtitles
TICK_SECONDS = 20
ACTIVE = ("queued", "starting", "downloading", "processing")
ACTIVE_SQL = "(" + ",".join(repr(s) for s in ACTIVE) + ")"  # constants, never user input

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
CANCEL: dict[str, str] = {}  # job_id -> "pause" | "cancel" | "delete"
TASKS: set = set()
PROCS: set = set()  # running ffmpeg children, killed on shutdown so they are never orphaned

# ---------------------------------------------------------------- database
# One connection + one lock. Writes are tiny and progress is throttled to 1/s,
# so a lock is simpler and has no stale-read window (unlike a writer queue).
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

JOB_COLS = {
    "id": "TEXT PRIMARY KEY", "url": "TEXT", "status": "TEXT", "progress": "TEXT",  # progress: legacy
    "format": "TEXT", "save_dir": "TEXT", "parent_id": "TEXT", "file_prefix": "TEXT",
    "quality": "TEXT", "title": "TEXT", "percent": "REAL", "speed": "REAL", "eta": "INTEGER",
    "error": "TEXT", "schedule_id": "TEXT", "created_at": "REAL",
    "kind": "TEXT",  # NULL/"download" or "convert"
    "filepath": "TEXT",  # final output file, for the "Convert" shortcut on finished downloads
}
SCHEDULE_COLS = {
    "id": "TEXT PRIMARY KEY", "name": "TEXT", "url": "TEXT", "format": "TEXT", "quality": "TEXT",
    "save_dir": "TEXT", "kind": "TEXT", "at": "TEXT", "every_min": "INTEGER", "next_run": "REAL",
    "last_run": "REAL", "last_result": "TEXT", "enabled": "INTEGER", "created_at": "REAL",
}


def init_db(path: Path = DB_FILE):
    global _conn
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVES.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    for table, cols in (("jobs", JOB_COLS), ("schedules", SCHEDULE_COLS)):
        _conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY)")
        have = {r["name"] for r in _conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():  # additive migration: old jobs.db files keep working
            if col not in have:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ.replace(' PRIMARY KEY', '')}")
    _conn.commit()


def q(sql: str, args=()) -> list[dict]:
    with _lock:
        rows = [dict(r) for r in _conn.execute(sql, args).fetchall()]
        _conn.commit()
        return rows


def insert(table: str, **f):
    q(f"INSERT INTO {table} ({','.join(f)}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))


def update(table: str, row_id: str, **f):
    # UPDATE-only on purpose: a late progress write can never resurrect a deleted row.
    q(f"UPDATE {table} SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), row_id))


def get_job(job_id: str):
    rows = q("SELECT * FROM jobs WHERE id=?", (job_id,))
    return rows[0] if rows else None


def new_job(**f) -> str:
    job_id = str(uuid.uuid4())
    insert("jobs", id=job_id, created_at=time.time(), **f)
    return job_id


# ---------------------------------------------------------------- helpers
def resolve_dir(save_dir: str) -> str:
    p = Path((save_dir or "").strip() or "downloads").expanduser()
    return str(p if p.is_absolute() else HOME / p)


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)


def execute(job_id: str):
    job = get_job(job_id)
    if job and not _stopped(job_id):
        (execute_convert if job["kind"] == "convert" else execute_download)(job)


async def run_job(job_id: str):
    async with download_semaphore:
        await asyncio.to_thread(execute, job_id)


def open_path(path: str):
    Path(path).mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path])


def targets(job_id: str) -> list[dict]:
    """A playlist parent acts on its children; anything else acts on itself."""
    job = get_job(job_id)
    if not job:
        return []
    return q("SELECT * FROM jobs WHERE parent_id=?", (job_id,)) if job["status"] == "playlist" else [job]


# ---------------------------------------------------------------- download
def _stopped(job_id: str) -> bool:
    flag = CANCEL.get(job_id)
    if flag == "pause":
        update("jobs", job_id, status="paused", speed=None, eta=None)
    elif flag == "cancel":
        update("jobs", job_id, status="error", error="Canceled", speed=None, eta=None)
    return bool(flag)


def execute_download(job: dict):
    """yt-dlp resumes partial files itself, so pause/resume is just stop/start."""
    job_id = job["id"]
    url, fmt, prefix, sid = job["url"], job["format"] or "video", job["file_prefix"], job["schedule_id"]
    save_dir = resolve_dir(job["save_dir"])
    os.makedirs(save_dir, exist_ok=True)

    if shutil.disk_usage(save_dir).free < MIN_FREE_GB * 1024**3:
        update("jobs", job_id, status="error", error=f"Less than {MIN_FREE_GB:g} GB free in {save_dir}")
        return
    update("jobs", job_id, status="starting", error=None)

    opts = {
        "outtmpl": os.path.join(save_dir, (f"{prefix} - " if prefix else "") + "%(title)s.%(ext)s"),
        "concurrent_fragment_downloads": 10,
        "retries": 10,
        "fragment_retries": 10,
        "no_color": True,
        "subtitleslangs": [x.strip() for x in SUB_LANGS],
    }
    if sid:  # scheduled run: whole URL (maybe a playlist/channel), skip what an earlier run got
        opts["outtmpl"] = os.path.join(save_dir, "%(playlist_title,uploader|Misc)s", "%(title)s.%(ext)s")
        opts["download_archive"] = str(ARCHIVES / f"{sid}.txt")
        opts["ignoreerrors"] = "only_download"  # one dead video must not abort the run
    if fmt == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
            {"key": "FFmpegMetadata"},
            {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False},
        ]
    else:
        height = {"1080p": 1080, "720p": 720}.get(job["quality"] or "best")
        if height:
            opts["format"] = f"bestvideo[height<={height}]+bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False}]

    last_write, peak, got = 0.0, 0.0, 0

    def hook(d):
        nonlocal last_write, peak, got
        flag = CANCEL.get(job_id)
        if flag:
            raise yt_dlp.utils.DownloadCancelled(flag)
        info = d.get("info_dict") or {}
        if d["status"] == "downloading":
            now = time.time()
            if now - last_write < 1.0:
                return
            last_write = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = (d.get("downloaded_bytes") or 0) / total * 100 if total else 0.0
            n, i = info.get("n_entries"), info.get("playlist_index")
            if n and i:
                pct = ((i - 1) * 100 + pct) / n
            peak = max(peak, pct)  # video+audio are separate streams; never let the bar go backwards
            update("jobs", job_id, status="downloading", percent=round(peak, 1),
                   speed=d.get("speed"), eta=d.get("eta"), title=info.get("title"))
        elif d["status"] == "finished":
            got += 1
            update("jobs", job_id, status="processing", speed=None, eta=None, title=info.get("title"),
                   filepath=d.get("filename"))

    def pp_hook(d):  # post-processors can rename the file (e.g. audio extraction), so the last one wins
        if d["status"] == "finished" and (d.get("info_dict") or {}).get("filepath"):
            update("jobs", job_id, filepath=d["info_dict"]["filepath"])

    errors: list[str] = []

    class Log:  # keep yt-dlp's last error so a failed run says why
        def debug(self, m): pass
        def info(self, m): pass
        def warning(self, m): pass
        def error(self, m): errors.append(str(m))

    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]
    opts["logger"] = Log()

    for subs in ((True, False) if SUB_LANGS else (False,)):  # YouTube 429s subtitle fetches; a missing subtitle must not fail the video
        opts["writesubtitles"] = opts["writeautomaticsub"] = subs
        ok, err = True, None
        errors.clear()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                if ydl.download([url]) and not got:
                    ok, err = False, (errors[-1] if errors else "Download failed")
        except Exception as e:
            if _stopped(job_id):
                return
            ok, err = False, str(e)
        if ok or "subtitles" not in (err or "").lower():
            break

    if sid:
        nothing_new = ok and got == 0
        result = "Nothing new" if nothing_new else f"{got} file(s) downloaded" if ok else f"Failed: {err}"
        update("schedules", sid, last_result=result)
        if nothing_new:  # don't clutter history with an empty run every N minutes
            q("DELETE FROM jobs WHERE id=?", (job_id,))
            return
    if ok:
        update("jobs", job_id, status="completed", percent=100, speed=None, eta=None, error=None)
    else:
        update("jobs", job_id, status="error", error=err, speed=None, eta=None)


# ---------------------------------------------------------------- convert
VIDEO_CODECS = {  # container -> ffmpeg args
    "mp4": ["-c:v", "libx264", "-crf", "23", "-preset", "medium", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
    "mkv": ["-c:v", "libx264", "-crf", "23", "-preset", "medium", "-c:a", "aac", "-b:a", "160k"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-c:a", "libopus", "-b:a", "128k"],
}
AUDIO_CODECS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-c:a", "aac", "-b:a", "192k"],
    "opus": ["-c:a", "libopus", "-b:a", "128k"],
}


def unique_path(p: Path) -> Path:
    q, n = p, 1
    while q.exists():
        q, n = p.with_name(f"{p.stem} ({n}){p.suffix}"), n + 1
    return q


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
        return float(r.stdout.strip())
    except (OSError, ValueError):
        return 0.0  # unknown length: the bar just stays at 0 until done


def build_convert_cmd(src: Path, out: Path, target: str, height: int) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src), "-map_metadata", "0"]
    if target in AUDIO_CODECS:
        return cmd + ["-map", "0:a:0", "-vn", *AUDIO_CODECS[target], "-progress", "pipe:1", "-nostats", str(out)]
    cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-sn"]
    if height:  # never upscale; -2 keeps the width even and the aspect ratio
        cmd += ["-vf", f"scale=-2:'min({height},ih)'"]
    return cmd + [*VIDEO_CODECS[target], "-progress", "pipe:1", "-nostats", str(out)]


def execute_convert(job: dict):
    job_id, src, target = job["id"], Path(job["url"]), job["format"]
    height = int(job["quality"] or 0)
    if not src.is_file():
        return update("jobs", job_id, status="error", error=f"Source file not found: {src}")
    out_dir = Path(job["save_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f" [{height}p]" if height and target in VIDEO_CODECS else ""
    out = unique_path(out_dir / f"{src.stem}{suffix}.{target}")
    duration = probe_duration(src)
    update("jobs", job_id, status="downloading", percent=0, error=None)  # "downloading" = running; UI says Converting
    last, proc = 0.0, None
    try:
        # ponytail: stderr is read after exit; with -loglevel error it stays far below the pipe buffer.
        proc = subprocess.Popen(build_convert_cmd(src, out, target, height), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        PROCS.add(proc)
        done_us, rate = 0, 0.0

        def watch():  # separate thread: ffmpeg can go seconds between progress lines, cancel must not wait
            while proc.poll() is None:
                if CANCEL.get(job_id):
                    proc.kill()  # output is discarded anyway; SIGTERM makes libvpx flush for seconds
                    return
                time.sleep(0.3)

        threading.Thread(target=watch, daemon=True).start()
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key == "out_time_us" and val.lstrip("-").isdigit():
                done_us = int(val)
            elif key == "speed" and val.rstrip("x").replace(".", "", 1).isdigit():
                rate = float(val.rstrip("x"))
            elif key == "progress" and time.time() - last >= 1.0 and duration:
                last = time.time()
                pct = min(99.0, done_us / 1e6 / duration * 100)
                eta = int((duration - done_us / 1e6) / rate) if rate else None
                update("jobs", job_id, percent=round(pct, 1), eta=eta)
        err = proc.stderr.read().strip()
        rc = proc.wait()
    except Exception as e:
        out.unlink(missing_ok=True)
        return update("jobs", job_id, status="error", error=str(e))
    finally:
        PROCS.discard(proc)
    stopped = _stopped(job_id)
    if stopped or rc:
        out.unlink(missing_ok=True)  # a half-written file is useless; Resume starts over
    if stopped:
        return
    if rc:
        update("jobs", job_id, status="error", error=(err.splitlines() or [f"ffmpeg exited with {rc}"])[-1], eta=None)
    else:
        update("jobs", job_id, status="completed", percent=100, eta=None, filepath=str(out))


# ---------------------------------------------------------------- scheduler
def compute_next_run(kind: str, at: str, every_min: int, now: datetime | None = None) -> float:
    now = now or datetime.now()
    if kind == "every":
        return (now + timedelta(minutes=every_min)).timestamp()
    if kind == "daily":
        h, m = map(int, at.split(":"))
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return (t if t > now else t + timedelta(days=1)).timestamp()
    return datetime.fromisoformat(at).timestamp()  # once


def launch_schedule(s: dict, now: float | None = None, advance: bool = True):
    """Start one run of schedule `s` and (unless advance=False) move its next_run forward."""
    now = now or time.time()
    fields: dict = {"last_run": now}
    if q(f"SELECT 1 FROM jobs WHERE schedule_id=? AND status IN {ACTIVE_SQL} LIMIT 1", (s["id"],)):
        fields["last_result"] = "Skipped: previous run still going"
    else:
        job_id = new_job(url=s["url"], status="queued", format=s["format"], quality=s["quality"],
                         save_dir=s["save_dir"], title=s["name"], percent=0, schedule_id=s["id"])
        fields["last_result"] = "Running"
        spawn(run_job(job_id))
    if advance and s["kind"] == "once":
        fields.update(enabled=0, next_run=None)
    elif advance:
        fields["next_run"] = compute_next_run(s["kind"], s["at"], s["every_min"], datetime.fromtimestamp(now))
    update("schedules", s["id"], **fields)


def tick(now: float | None = None):
    now = now or time.time()
    # Runs missed while the server was off fire once on startup (next_run is in the past).
    for s in q("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?", (now,)):
        launch_schedule(s, now)


async def scheduler_loop():
    while True:
        try:
            tick()
        except Exception as e:
            print("scheduler error:", e, file=sys.stderr)
        await asyncio.sleep(TICK_SECONDS)


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Anything that was mid-flight when we stopped goes back in the queue (yt-dlp resumes partials).
    for job in q(f"SELECT id FROM jobs WHERE status IN {ACTIVE_SQL}"):
        update("jobs", job["id"], status="queued")
        spawn(run_job(job["id"]))
    loop = asyncio.create_task(scheduler_loop())
    yield
    loop.cancel()
    # Stop running jobs so shutdown doesn't hang or orphan ffmpeg. Their rows stay "active",
    # so the next start puts them back in the queue.
    for job in q(f"SELECT id FROM jobs WHERE status IN {ACTIVE_SQL}"):
        CANCEL[job["id"]] = "shutdown"
    for proc in list(PROCS):
        proc.kill()
    await asyncio.sleep(0.5)  # let worker threads delete their partial output before the process exits


app = FastAPI(title="yt-dlp UI", lifespan=lifespan)


class Options(BaseModel):  # what every download-ish request chooses
    format: Literal["video", "audio"] = "video"
    quality: str = "best"
    save_dir: str = ""


class DownloadRequest(Options):
    url: str


class BatchItem(BaseModel):
    url: str
    title: str


class BatchDownloadRequest(Options):
    parent_title: str
    items: list[BatchItem]


class ResolveRequest(BaseModel):
    url: str


class ConvertRequest(BaseModel):
    sources: list[str]
    format: Literal["mp4", "mkv", "webm", "mp3", "m4a", "opus"]
    height: Literal[0, 360, 480, 720, 1080, 1440, 2160] = 0  # 0 = keep original
    save_dir: str = ""  # blank = next to each source file


class ScheduleRequest(Options):
    name: str = ""
    url: str
    kind: Literal["once", "every", "daily"]
    at: str = ""  # once: local "YYYY-MM-DDTHH:MM"; daily: "HH:MM"
    every_min: int = 0
    run_now: bool = False


@app.get("/")
async def root():
    return RedirectResponse(url="/ui/")


@app.get("/api/config")
async def config():
    return {"default_save_dir": resolve_dir(""), "max_concurrent": MAX_CONCURRENT}


@app.post("/api/resolve")
async def resolve_url(req: ResolveRequest):
    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True}

    def extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(req.url, download=False)

    try:
        info = await asyncio.to_thread(extract)
    except Exception as e:
        return {"error": str(e)}
    if "entries" not in info:
        return {"type": "video", "title": info.get("title", "Video")}
    entries = []
    for e in info["entries"]:
        if not e:
            continue
        u = e.get("url") or ""
        if u and not u.startswith("http"):
            u = f"https://www.youtube.com/watch?v={u}"
        entries.append({"title": e.get("title") or u, "url": u})
    return {"type": "playlist", "title": info.get("title", "Playlist"), "entries": entries}


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    job_id = new_job(url=req.url, status="queued", format=req.format, quality=req.quality,
                     save_dir=resolve_dir(req.save_dir), percent=0)
    spawn(run_job(job_id))
    return {"job_id": job_id}


@app.post("/api/download_batch")
async def start_batch(req: BatchDownloadRequest):
    safe = "".join(c for c in req.parent_title if c.isalnum() or c in " -_").strip() or "Playlist"
    save_dir = os.path.join(resolve_dir(req.save_dir), safe)
    parent = new_job(url=req.parent_title, status="playlist", format=req.format, quality=req.quality, save_dir=save_dir)
    for idx, item in enumerate(req.items, 1):
        job_id = new_job(url=item.url, status="queued", format=req.format, quality=req.quality, save_dir=save_dir,
                         parent_id=parent, file_prefix=f"{idx:02d}", title=item.title, percent=0)
        spawn(run_job(job_id))
    return {"parent_id": parent}


@app.post("/api/convert")
async def start_convert(req: ConvertRequest):
    if not shutil.which("ffmpeg"):
        raise HTTPException(400, "ffmpeg was not found on PATH. Install it to convert files.")
    paths = [Path(p.strip().strip('"')).expanduser() for p in req.sources if p.strip()]
    missing = [str(p) for p in paths if not p.is_file()]
    if not paths or missing:
        raise HTTPException(400, "File not found: " + ", ".join(missing) if missing else "Add at least one file.")
    for src in paths:
        src = src.resolve()
        out_dir = resolve_dir(req.save_dir) if req.save_dir.strip() else str(src.parent)
        job_id = new_job(url=str(src), kind="convert", status="queued", format=req.format, quality=str(req.height),
                         save_dir=out_dir, title=src.name, percent=0)
        spawn(run_job(job_id))
    return {"queued": len(paths)}


@app.get("/api/jobs")
async def list_jobs():
    # ponytail: no pagination; "Clear finished" keeps the list short. Add paging if history gets huge.
    return q("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC")


def _halt(job_id: str, flag: str, status_if_queued: dict):
    for t in targets(job_id):
        if t["status"] in ACTIVE:
            CANCEL[t["id"]] = flag
            if t["status"] == "queued":  # not started yet, so no thread will write this for us
                update("jobs", t["id"], **status_if_queued)


@app.post("/api/pause/{job_id}")
async def pause(job_id: str):
    _halt(job_id, "pause", {"status": "paused"})
    return {"status": "ok"}


@app.post("/api/cancel/{job_id}")
async def cancel(job_id: str):
    _halt(job_id, "cancel", {"status": "error", "error": "Canceled"})
    return {"status": "ok"}


@app.post("/api/resume/{job_id}")
async def resume(job_id: str):
    for t in targets(job_id):
        if t["status"] in ("paused", "error"):
            CANCEL.pop(t["id"], None)
            update("jobs", t["id"], status="queued", error=None)
            spawn(run_job(t["id"]))
    return {"status": "ok"}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    for t in targets(job_id):
        CANCEL[t["id"]] = "delete"
    CANCEL[job_id] = "delete"
    q("DELETE FROM jobs WHERE id=? OR parent_id=?", (job_id, job_id))
    return {"status": "ok"}


@app.post("/api/clear_finished")
async def clear_finished():
    # Leaves playlist parents that still have children alone.
    q("DELETE FROM jobs WHERE status IN ('completed','error','paused') AND parent_id IS NULL")
    q("DELETE FROM jobs WHERE status IN ('completed','error') AND parent_id IS NOT NULL")
    q("DELETE FROM jobs WHERE status='playlist' AND id NOT IN (SELECT parent_id FROM jobs WHERE parent_id IS NOT NULL)")
    return {"status": "ok"}


@app.post("/api/open/{job_id}")
async def open_job_folder(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    try:
        await asyncio.to_thread(open_path, resolve_dir(job["save_dir"]))
    except Exception as e:  # headless server: nothing to open a folder on
        raise HTTPException(500, f"Could not open the folder: {e}")
    return {"status": "ok"}


@app.get("/api/browse")
async def browse(mode: Literal["dir", "files"] = "dir"):
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--title=Select " + ("files" if mode == "files" else "download folder")]
        cmd += ["--multiple", "--separator=\n"] if mode == "files" else ["--directory"]
    else:  # Windows / macOS / Linux without zenity
        call = "chr(10).join(f.askopenfilenames())" if mode == "files" else "f.askdirectory()"
        cmd = [sys.executable, "-c", f"import tkinter.filedialog as f;print({call})"]
    try:
        r = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        paths = [p for p in r.stdout.strip().splitlines() if p] if r.returncode == 0 else []
    except Exception:
        paths = []
    return {"path": paths[0] if paths else "", "paths": paths}


# ---- schedules
@app.get("/api/schedules")
async def list_schedules():
    return q("SELECT * FROM schedules ORDER BY created_at")


@app.post("/api/schedules")
async def create_schedule(req: ScheduleRequest):
    try:
        if req.kind == "every":
            if req.every_min < 1:
                raise ValueError("Repeat interval must be at least 1 minute")
        elif req.kind == "daily":
            datetime.strptime(req.at, "%H:%M")
        nxt = compute_next_run(req.kind, req.at, req.every_min)
    except ValueError as e:
        raise HTTPException(400, str(e))
    sid = str(uuid.uuid4())
    insert("schedules", id=sid, name=req.name.strip() or req.url[:60], url=req.url, format=req.format,
           quality=req.quality, save_dir=resolve_dir(req.save_dir), kind=req.kind, at=req.at,
           every_min=req.every_min, next_run=time.time() if req.run_now else nxt, enabled=1,
           created_at=time.time())
    return {"id": sid}


@app.post("/api/schedules/{sid}/run")
async def run_schedule_now(sid: str):
    rows = q("SELECT * FROM schedules WHERE id=?", (sid,))
    if not rows:
        raise HTTPException(404, "not found")
    launch_schedule(rows[0], advance=False)  # a manual run doesn't change the plan
    return {"status": "ok"}


@app.post("/api/schedules/{sid}/toggle")
async def toggle_schedule(sid: str):
    rows = q("SELECT * FROM schedules WHERE id=?", (sid,))
    if not rows:
        raise HTTPException(404, "not found")
    s = rows[0]
    if s["enabled"]:
        update("schedules", sid, enabled=0)
    else:  # re-arm: recurring schedules wait for their next slot, a spent one-off runs now
        nxt = time.time() if s["kind"] == "once" else compute_next_run(s["kind"], s["at"], s["every_min"])
        update("schedules", sid, enabled=1, next_run=nxt)
    return {"status": "ok"}


@app.delete("/api/schedules/{sid}")
async def delete_schedule(sid: str):
    q("DELETE FROM schedules WHERE id=?", (sid,))
    (ARCHIVES / f"{sid}.txt").unlink(missing_ok=True)
    return {"status": "ok"}


app.mount("/ui", StaticFiles(directory=APP_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    if sys.stdout is None or sys.stderr is None:  # pythonw.exe (Windows Task Scheduler) has no console
        sys.stdout = sys.stderr = open(HOME / "server.log", "a", buffering=1, encoding="utf-8")

    uvicorn.run(app, host=HOST, port=PORT)
