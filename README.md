# Antigravity Downloader

A local yt-dlp web app with a persistent download queue and a built-in **scheduler**.

## Features

- **Queue** – paste a video or playlist link; pick Video/Audio, quality and folder. Playlists open a picker and become one collapsible group.
- **Pause / resume / cancel / retry** per video or per playlist. Downloads run as in-process yt-dlp threads (no child processes), so they stop instantly.
- **Survives restarts** – every job lives in SQLite (`jobs.db`); anything mid-flight goes back in the queue on start and yt-dlp continues from the partial file.
- **Schedules** – check a channel/playlist **daily at a time**, **every N minutes/hours**, or **once**. Each schedule keeps its own yt-dlp archive, so only *new* videos are downloaded; runs with nothing new leave no clutter. Runs missed while the app was off fire once on startup. A "Next 24 hours" timetable shows what will fire when.
- **Convert** – change a local file's format (MP4, MKV, WebM, MP3, M4A, Opus) and/or lower its resolution (never upscaled) with ffmpeg. Runs in the same queue, shows progress and ETA, cancels instantly, and never touches the original. Finished downloads have a one-click Convert shortcut.
- **Runs as a service** – see [docs/RUN_AS_A_SERVICE.md](docs/RUN_AS_A_SERVICE.md) for systemd (Linux) and Task Scheduler (Windows).

## Run

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), and `ffmpeg` on PATH.

```bash
uv sync
uv run python main.py        # http://127.0.0.1:8000/
```

Settings are environment variables (`YTDLP_UI_HOME`, `_HOST`, `_PORT`, `_MAX_CONCURRENT`, `_MIN_FREE_GB`, `_SUB_LANGS`); defaults and meanings are in the service doc. The app has **no login** and listens on localhost only by default.

## Layout

| Path | What |
|---|---|
| `main.py` | FastAPI app: API, download worker, scheduler loop, SQLite |
| `static/index.html` | The whole UI (Queue + Schedules), no build step |
| `test_main.py` | `uv run python test_main.py` – schedule math, scheduler tick, delete race, ffmpeg commands |
| `docs/RUN_AS_A_SERVICE.md` | systemd + Windows Task Scheduler setup |

## License

[MIT](LICENSE)
