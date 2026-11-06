# When something goes wrong

## Downloads fail with `HTTP Error 403: Forbidden`

**This is almost always a stale yt-dlp.** Update it and restart:

```bash
uv sync --upgrade-package yt-dlp
```

Then restart the app (`uv run python main.py`, or `systemctl --user restart ytdlp-ui`).

Why it happens: yt-dlp releases are dated, and YouTube changes faster than they come out.
When the copy you have can no longer read the modern player, it falls back to an older
player client whose media URLs YouTube then refuses with a 403. Nothing about your account or the
file permissions is involved.

The app checks this for you. The Queue page warns when yt-dlp is more than 90 days old,
`GET /api/config` reports `ytdlp_age_days` and `ytdlp_stale`, and a 403 raised by a stale
copy carries the age and the fix in the error text instead of just the status code.

Still 403 after updating? Check **Some formats are missing** below — a missing JavaScript
runtime causes the same fallback.

## `No supported JavaScript runtime could be found`

YouTube extraction needs a JS runtime, and yt-dlp enables only `deno` by default. The app
looks for `deno`, `node` and `bun`, in that order, and uses the first one it finds — so
installing any of them is enough:

```bash
# any one of these
curl -fsSL https://deno.land/install.sh | sh
sudo apt install nodejs
curl -fsSL https://bun.sh/install | bash
```

Restart the app afterwards; the runtime is resolved once at startup and shown as
`js_runtime` in `GET /api/config`. Without one, some formats are unavailable and some
videos fall back to a client that 403s.

## A download refuses to start and says there is no space

The app will not begin a download when the target drive has less than `YTDLP_UI_MIN_FREE_GB`
free (5 GB by default), because a part-written file that fills the disk is worse than a
download that never started. Free some space, point **Save to** at another drive, or lower
the threshold:

```bash
YTDLP_UI_MIN_FREE_GB=1 uv run python main.py
```

Check what you have with `df -h <folder>`.

## `Browse…` and `Open folder` do nothing

Both ask the **server's** desktop to open a window, so they only work when the app runs in
a desktop session. Under systemd, in a container, or over SSH there is no session to open
one in. Type the path into the field instead — it is an ordinary text box.

On Linux the picker is `zenity` when installed and falls back to Python's `tkinter`.

## The app answers `421 Misdirected Request`

The API reads and converts local files and has no login, so it only answers to loopback
hostnames. You get a 421 when reaching it under another name — a LAN hostname, a reverse
proxy, or a rebound domain.

Binding to a real interface switches the check off automatically. To keep a loopback bind
but allow specific names:

```bash
YTDLP_UI_ALLOWED_HOSTS=localhost,127.0.0.1,nas.local uv run python main.py
```

`YTDLP_UI_ALLOWED_HOSTS=*` disables it entirely. Only do that on a network you trust: any
page you visit in a browser could otherwise drive this API.

## Conversions fail or the Convert page refuses

Convert needs `ffmpeg` (and `ffprobe` for the progress bar) on `PATH`:

```bash
sudo apt install ffmpeg      # Debian/Ubuntu
brew install ffmpeg          # macOS
```

`GET /api/config` reports `ffmpeg: true` once it is visible to the app. Your original file
is never modified or replaced; a failed or cancelled conversion deletes its half-written
output and leaves the source alone.

## A schedule did not run

- **It was already running.** A schedule skips a firing while its previous run is still
  going, and says so in *Last*. A 5-minute timer on a 20-minute playlist behaves this way.
- **Nothing was new.** Each schedule keeps its own yt-dlp archive, so a run that finds no
  new videos downloads nothing, records "Nothing new" and leaves no job behind.
- **The app was off.** Runs missed while the app was down fire once on startup, not once
  per slot missed.
- **The app is not running at all.** The scheduler lives inside the app; see
  [RUN_AS_A_SERVICE.md](RUN_AS_A_SERVICE.md).

## Jobs came back as "queued" after a restart

That is deliberate. Anything mid-flight when the app stopped goes back in the queue on
start, and yt-dlp resumes from the partial file rather than starting over.

## Where are the logs?

In the foreground, on the console. Under Task Scheduler with `pythonw.exe` (no console),
the app writes `server.log` inside `YTDLP_UI_HOME`. Under systemd:

```bash
journalctl --user -u ytdlp-ui -f
```
