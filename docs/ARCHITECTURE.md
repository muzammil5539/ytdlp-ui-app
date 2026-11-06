# How it is put together

One Python file, one HTML file, one SQLite database. No build step, no framework on the
front end, no message broker. The whole app is about 780 lines of Python and 550 of HTML,
and it is meant to stay that size.

```
browser  ──HTTP/JSON──>  main.py  ──>  jobs.db      (queue + schedules, single source of truth)
                            │
                            ├── yt-dlp, in a worker thread      → downloads/
                            ├── ffmpeg, as a child process      → converted files
                            └── scheduler loop, every 20 s      → archives/<schedule>.txt
```

## The pieces

| Path | What it is |
|---|---|
| `main.py` | Everything server-side: HTTP API, download worker, converter, scheduler, database |
| `static/index.html` | The whole UI — markup, styles and script in one file, served as a static asset |
| `jobs.db` | SQLite, WAL mode. Two tables: `jobs` and `schedules` |
| `archives/<id>.txt` | One yt-dlp download archive per schedule, so recurring runs fetch only new videos |

`YTDLP_UI_HOME` decides where the last three live; it defaults to the folder holding
`main.py`. Relative save folders resolve against it, absolute ones are left alone.

## State lives in SQLite, not in memory

Every job is a row. Progress, speed, ETA, title and error are columns on that row, and the
UI polls `GET /api/jobs` for the list. Nothing about a job is held only in the process, so
killing the app loses nothing: on the next start, anything still marked active is set back
to `queued` and restarted, and yt-dlp resumes from the partial file.

Three rules keep that honest under threads:

**One connection, one lock.** All database access goes through `q()`, which holds a
`threading.Lock` for the duration. Writes are small and progress is throttled to one per
second per job, so a lock costs less than a writer queue and has no stale-read window.

**Writes are `UPDATE`, never `INSERT OR REPLACE`.** A worker thread can report progress
after the user has deleted the job. An `UPDATE` against a missing row does nothing; an
upsert would resurrect the row the user just removed.

**Schema changes are additive.** `init_db()` compares the columns it wants against
`PRAGMA table_info` and adds what is missing, so an older `jobs.db` keeps working.

## Downloads run as threads, not child processes

`run_job()` takes a slot from an `asyncio.Semaphore` (`YTDLP_UI_MAX_CONCURRENT`, 3 by
default) and calls yt-dlp through `asyncio.to_thread`. yt-dlp is used as a library.

Stopping is cooperative. `CANCEL` is a dict of job id → `pause`, `cancel`, `delete` or
`shutdown`. yt-dlp's progress hook runs many times a second and checks it, raising
`DownloadCancelled` when a flag is set, which unwinds the download immediately. That is why
pause and cancel feel instant: there is no process to signal and no poll interval to wait
out. Flags for deleted jobs are dropped 30 seconds later so the dict cannot grow forever.

Pause and resume are stop and start. yt-dlp writes `.part` files and continues from them,
so resuming needs no state beyond the row.

Progress for playlists is scaled across entries (`n_entries`, `playlist_index`) and kept
monotonic, because video and audio are fetched as separate streams and a naive percentage
walks backwards when the second one starts.

## Converting shells out to ffmpeg

A conversion is a job like any other — same table, same queue, same slot — with
`kind='convert'`, the source path in `url` and the target in `format`. `execute_convert()`
spawns ffmpeg with `-progress pipe:1` and parses `out_time_us` and `speed` to drive the bar.

A second thread watches `CANCEL` and kills the process, because ffmpeg can go seconds
between progress lines and a cancel should not wait for one. It kills rather than
terminates: the output is discarded anyway, and VP9 flushes for several seconds on SIGTERM.

Two guarantees worth knowing: the source file is never written to, and the output path goes
through `unique_path()`, which steps around anything already there (`clip.mp4`,
`clip (1).mp4`, …). A failed or cancelled conversion deletes its half-written output.
Resolution only ever shrinks — the filter is `scale=-2:'min(H,ih)'`, so a 480p source asked
for 1080p stays 480p.

## The scheduler is a loop inside the app

`scheduler_loop()` ticks every 20 seconds and fires any enabled schedule whose `next_run`
has passed, then advances it. There is no cron, no external timer, and no separate process.

Consequences, all intended:

- **Missed runs fire once.** A `next_run` in the past is due, so a schedule missed
  while the machine was off runs once at startup rather than once per slot missed.
- **Runs do not stack.** Before firing, the tick looks for an active job carrying the same
  `schedule_id`; if one is there it records `Skipped: previous run still going`.
- **Empty runs leave nothing.** Each schedule has its own yt-dlp archive, so a run that
  finds nothing new deletes its own job row instead of filling the history with blanks.
- **The app has to be running.** See [RUN_AS_A_SERVICE.md](RUN_AS_A_SERVICE.md).

A manual *Run now* calls the same code path with `advance=False`, so it does not move the
plan.

## Startup and shutdown

`lifespan()` resolves the JavaScript runtime, opens the database, requeues whatever was
active, and starts the scheduler. On the way out it cancels the loop, sets the `shutdown`
flag on every active job and kills any ffmpeg child, then waits half a second for worker
threads to clean up their partial output. Those rows stay active on disk, which is exactly
what makes them requeue on the next start.

## The front end

`static/index.html` is served by `StaticFiles` and talks to the API with `fetch`. It polls
`GET /api/jobs` every 1.5 seconds on the Queue view and every 5 on Schedules. There is no
WebSocket: a queue of a few dozen rows is cheap to re-send, and polling survives the server
restarting under it without reconnection logic.

Three views — Queue, Schedules, Convert — are sections in the one document, shown and
hidden by `setView()`.

## Two external dependencies, and what breaks without them

**yt-dlp** does all extraction and downloading. Its releases are dated, and YouTube changes
faster than they come out, so a stale copy is the single commonest cause of failures. The
app reads the release date at startup, reports it from `GET /api/config`, warns in the UI
past 90 days, and rewrites 403 errors from a stale copy to say so. Fix:
`uv sync --upgrade-package yt-dlp`.

**ffmpeg** merges separate video and audio streams, extracts audio, embeds subtitles and
runs every conversion. Downloads of already-muxed formats work without it; nothing on the
Convert page does.

yt-dlp also needs a JavaScript runtime for YouTube, and enables only `deno` by default.
`js_runtime()` picks the first of `deno`, `node` or `bun` on `PATH` and passes it as
`{runtime: {}}` — the Python API's shape, not the CLI's list.

## Security model

There is no login, and the API reads, writes and converts arbitrary local paths. It is
meant for one person on their own machine, bound to loopback.

Browsers already refuse cross-origin JSON posts, but DNS rebinding gets around that by
pointing an attacker's hostname at `127.0.0.1`. A middleware answers only to loopback host
names and returns `421` otherwise. Binding to a real interface — the documented "network
you trust" case — switches the check off, and `YTDLP_UI_ALLOWED_HOSTS` sets the list by
hand.

Path handling is deliberately permissive, since the point of the app is to save files where
you want them. The one exception is playlist titles, which are stripped to alphanumerics,
spaces, hyphens and underscores before becoming a folder name.

## Deliberate limits

- `GET /api/jobs` returns every row with no pagination. *Clear finished* keeps the list
  short; add paging if a history ever gets large enough to notice.
- One SQLite connection behind one lock caps throughput. For three concurrent downloads
  writing once a second, that is not the bottleneck.
- Quality is three choices (best, 1080p, 720p) rather than yt-dlp's full format syntax.
- `Browse…` and `Open folder` drive the *server's* desktop, so they do nothing headless.
