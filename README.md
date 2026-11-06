# Antigravity Downloader

A local yt-dlp web app with a persistent download queue, a built-in **scheduler** and an
ffmpeg **converter**. Runs on your own machine; there is no login and it listens on
localhost.

## Features

- **Queue** — paste a video or playlist link; pick Video/Audio, quality and folder.
  Playlists open a picker and become one collapsible group.
- **Pause / resume / cancel / retry**, per video or per playlist. Downloads run as
  in-process yt-dlp threads, so they stop the moment you ask.
- **Survives restarts** — every job lives in SQLite. Anything mid-flight when the app
  stops goes back in the queue on start, and yt-dlp continues from the partial file.
- **Schedules** — check a channel or playlist **daily at a time**, **every N minutes or
  hours**, or **once**. Each schedule keeps its own yt-dlp archive, so only *new* videos
  are fetched and runs with nothing new leave no clutter. Runs missed while the app was off
  fire once on startup. A "Next 24 hours" timetable shows what will fire when.
- **Convert** — change a local file's format (MP4, MKV, WebM, MP3, M4A, Opus) and/or lower
  its resolution (never upscaled) with ffmpeg. Runs in the same queue with progress and
  ETA, cancels instantly, and never touches the original.
- **Runs as a service** — see [docs/RUN_AS_A_SERVICE.md](docs/RUN_AS_A_SERVICE.md).

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- **ffmpeg** on `PATH` — merges video and audio, extracts audio, embeds subtitles, and runs
  every conversion
- **A JavaScript runtime** — `deno`, `node` or `bun`. YouTube extraction needs one, and
  yt-dlp enables only `deno` by default. The app uses whichever it finds.

## Run

```bash
uv sync
uv run python main.py        # http://127.0.0.1:8000/
```

## Keeping yt-dlp current

**Do this first whenever downloads start failing**, especially with `HTTP Error 403:
Forbidden`:

```bash
uv sync --upgrade-package yt-dlp
```

Restart the app afterwards.

yt-dlp releases are dated and YouTube changes faster than they come out, so a copy that is
a few months old is the commonest cause of failures — the symptom looks like a permissions
problem and is not one. The app warns when its yt-dlp is more than 90 days old, reports the
age from `GET /api/config`, and rewrites a 403 from a stale copy to say so.

Other symptoms and fixes are in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Settings

All optional, all environment variables. Defaults and the full table are in
[docs/RUN_AS_A_SERVICE.md](docs/RUN_AS_A_SERVICE.md).

| Variable | Default | Meaning |
|---|---|---|
| `YTDLP_UI_HOME` | folder holding `main.py` | Where `jobs.db`, `downloads/` and `archives/` live |
| `YTDLP_UI_HOST` / `_PORT` | `127.0.0.1` / `8000` | Interface and port |
| `YTDLP_UI_MAX_CONCURRENT` | `3` | Parallel downloads |
| `YTDLP_UI_MIN_FREE_GB` | `5` | Refuse to start a download below this much free space |
| `YTDLP_UI_SUB_LANGS` | *(empty)* | Subtitle languages, e.g. `en` or `en,es` |
| `YTDLP_UI_ALLOWED_HOSTS` | loopback names | Host names the API answers to |

## Tests

```bash
uv run pytest        # 74 tests, no network, no downloads
```

The suite and the manual checklist are described in [docs/TEST_PLAN.md](docs/TEST_PLAN.md).

## Layout

| Path | What |
|---|---|
| `main.py` | FastAPI app: API, download worker, converter, scheduler loop, SQLite |
| `static/index.html` | The whole UI (Queue, Schedules, Convert), no build step |
| `tests/` | `test_core.py` unit tests, `test_api.py` endpoint tests, `conftest.py` fixtures |
| `docs/ARCHITECTURE.md` | How the pieces fit and why they are shaped that way |
| `docs/TEST_PLAN.md` | What is covered, what is not, and the manual pass |
| `docs/TROUBLESHOOTING.md` | 403s, missing ffmpeg, dead file pickers, silent schedules |
| `docs/RUN_AS_A_SERVICE.md` | systemd and Windows Task Scheduler setup |
| `CONTEXT.md` | Domain glossary — the words this codebase uses |

## Security

No login, and the API reads, writes and converts arbitrary local paths — it is built for
one person on their own machine. To stop a web page you visit from driving it via DNS
rebinding, the API answers only to loopback host names and returns `421` otherwise.
Binding to a real interface switches that off; see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#security-model).

## License

[MIT](LICENSE)
