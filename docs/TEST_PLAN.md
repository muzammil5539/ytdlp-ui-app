# Test plan

74 automated tests — 29 unit, 45 through the HTTP API — plus a manual pass for the parts a
test cannot reach. Run them all:

```bash
uv run pytest
```

## Two rules the suite holds to

**Nothing touches the network and nothing downloads.** `yt_dlp.YoutubeDL` is stubbed
wherever a test would reach it, and `main.spawn` records the coroutine instead of running
it. A suite that quietly depends on YouTube being up tells you about YouTube, not about
your code.

**Nothing touches the real data directory.** The `app_env` fixture repoints `HOME`,
`ARCHIVES` and `DB_FILE` at a per-test `tmp_path` and rebuilds the schema, so tests cannot
contaminate each other or the repo. `init_db` is patched rather than called directly
because the app's lifespan calls it on startup.

## What is covered

### Scheduling (`tests/test_core.py`)

| Area | Checks |
|---|---|
| Next-run maths | `every` N minutes; `daily` before, after and exactly at the slot; `once` from an ISO timestamp; a malformed time raises |
| The tick | Due schedules fire and advance; disabled and future ones do not; a one-off disarms itself |
| Overlap | A run still going blocks the next firing and records why; a finished one does not |
| Manual runs | *Run now* creates a job without moving `next_run` |

The "exactly at the slot" case is there because `daily` at the current minute has to count
as passed, or a schedule can fire twice.

### The job table (`tests/test_core.py`)

| Area | Checks |
|---|---|
| Delete race | A progress write arriving after a delete does not resurrect the row |
| Playlist fan-out | A parent's actions target its children; a child targets itself; an unknown id yields nothing |
| Paths | Relative save folders land under `HOME`, absolute ones are untouched |

### Converting (`tests/test_core.py`)

| Area | Checks |
|---|---|
| Resolution | Scaling only ever shrinks; keeping the original adds no filter; a height on an audio target is ignored |
| Formats | All six targets build a command; audio targets drop the video stream |
| Overwrites | `unique_path` steps around existing files; the source is never an output |
| Probing | A file ffprobe cannot read gives 0.0 rather than raising |

### Errors and environment (`tests/test_core.py`)

| Area | Checks |
|---|---|
| Stale yt-dlp | A 403 on a copy older than 90 days gains the age and the fix command, and keeps the original message |
| Current yt-dlp | The same 403 is passed through untouched |
| JS runtime | A missing runtime is explained; `js_runtime()` finds one when present and returns `None` when not |
| Option shape | The real option dict is handed to yt-dlp, which rejects anything malformed |

That last one exists because `js_runtimes` was first passed as the CLI's list when the
Python API wants `{runtime: {}}`. Every job failed, and no test on our own helpers could
see it — only handing the dict to yt-dlp catches that class of mistake.

### The HTTP API (`tests/test_api.py`)

| Endpoint | Checks |
|---|---|
| `GET /`, `/ui/`, `/api/config` | Redirect to the UI; the page is served; config reports save dir, slots, yt-dlp version and age, JS runtime, ffmpeg |
| `POST /api/download` | Queued and listed; format, quality and save folder stored as chosen |
| `POST /api/download_batch` | One parent and ordered children; children land in the playlist folder; a `../` title cannot escape it |
| `POST /api/resolve` | Single video; playlist with bare ids repaired and dead entries dropped; the failure reason is returned; a `None` result is handled |
| `POST /api/convert` | One job per file; blank save folder means beside the source; missing files, an empty list, an unsupported format and a missing ffmpeg are each refused with a reason |
| `pause`/`resume`/`cancel` | Status and stop-flag transitions; a cancelled job can be retried; a playlist pauses every child; acting on a missing job is not an error |
| `DELETE /api/jobs/{id}` | A playlist takes its children with it and leaves other jobs alone |
| `POST /api/clear_finished` | Removes completed and failed; **keeps paused, running and queued**; drops empty playlist parents but keeps busy ones |
| `GET`/`POST`/`DELETE /api/schedules` | Created with a next run; an unnamed one falls back to its URL; *run now* makes it due; five invalid shapes are refused; toggling pauses and re-arms; a spent one-off re-runs; deleting removes the archive; unknown ids are 404 |
| `POST /api/open/{id}` | The save folder is passed to the desktop; a headless failure is reported; an unknown job is 404 |
| Host guard | A rebound hostname gets 421; a loopback host passes; binding to a real interface switches the check off |

The `clear_finished` row is a regression test. The endpoint used to delete paused jobs
along with finished ones, which silently threw away a part-finished download someone had
deliberately stopped.

## What is not covered, and why

- **Real downloads.** Verified by hand (below). Automating it would make the suite depend
  on YouTube, a network and several hundred megabytes.
- **Real ffmpeg runs.** The command builder is tested; running it is not. The commands are
  exercised by hand on a generated clip.
- **The browser UI.** No JavaScript test runner, deliberately — there is no build step to
  hang one off. Checked by hand.
- **`GET /api/browse`.** Opens a native file dialog and blocks until someone answers it.
- **Windows and macOS.** `open_path` branches on `sys.platform`; only the Linux branch has
  been run.

## Manual pass

Needed after changing anything in the download or convert path. Takes about five minutes.

| # | Step | Expect |
|---|---|---|
| 1 | `uv run python main.py`, open `http://127.0.0.1:8000/` | Queue view, sidebar says Connected |
| 2 | Paste a single video URL, press Download | Row appears, title fills in, percentage and speed climb, ends *Done* |
| 3 | Check the file | Plays, full length, in the folder shown in **Save to** |
| 4 | Paste a playlist URL | Picker lists entries; select a few; they become one collapsible group with `01`, `02` … prefixes |
| 5 | Pause mid-download, then resume | Stops at once; resuming continues from the same percentage, does not restart |
| 6 | Cancel a download | Stops at once, row shows *Failed · Canceled* |
| 7 | Press *Clear finished* | Completed and failed rows go; **a paused row stays** |
| 8 | Convert page: a local file to MP3 | Progress and ETA shown; MP3 appears beside the source; source unchanged |
| 9 | Convert the same file again | Output is `name (1).mp3`, the first is not overwritten |
| 10 | Add a schedule, *Every 1 minute*, *Also run now* | Fires within 20 s; the 24-hour rail shows its ticks |
| 11 | Let it fire twice on a channel with nothing new | Second run records *Nothing new* and leaves no job row |
| 12 | Stop the app mid-download, start it again | The job is back in the queue and resumes from the partial file |
| 13 | Restart and look at Schedules | A run that was due while the app was off has fired once, not repeatedly |

### Checking the failure paths

| Step | Expect |
|---|---|
| Point **Save to** at a full disk | Refuses to start, names the free-space threshold |
| Rename `ffmpeg` off `PATH`, open Convert | Refused with a message naming ffmpeg |
| Pin an old yt-dlp, download a YouTube URL | 403 carries the version age and `uv sync --upgrade-package yt-dlp` |
| `curl -H 'Host: evil.test' http://127.0.0.1:8000/api/jobs` | `421` |

## Before committing

```bash
uv run pytest          # all 74 green
uv sync                # lock file and environment agree
```

Then the manual pass if the change touched downloading, converting or scheduling.
