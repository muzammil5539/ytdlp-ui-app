# Keep the scheduler running

The scheduler lives **inside** the app: while `main.py` is running it checks your
schedules every 20 seconds. So "scheduling" the app just means *starting it at
login/boot and restarting it if it dies*. systemd (Linux) and Task Scheduler
(Windows) do that.

If the machine was off or asleep when a run was due, the run fires **once** as
soon as the app is back up (it won't replay every missed slot).

## Settings (environment variables, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `YTDLP_UI_HOME` | folder containing `main.py` | Where `jobs.db`, `downloads/`, `archives/` live. Relative save folders resolve against it. |
| `YTDLP_UI_HOST` | `127.0.0.1` | Interface to listen on. There is **no login**: only change this on a network you trust. |
| `YTDLP_UI_PORT` | `8000` | Port. |
| `YTDLP_UI_MAX_CONCURRENT` | `3` | Parallel downloads. |
| `YTDLP_UI_MIN_FREE_GB` | `5` | A download refuses to start with less free space than this. |
| `YTDLP_UI_SUB_LANGS` | *(empty)* | Subtitle languages to fetch, e.g. `en` or `en,es`. Empty = no subtitles. |

Install once: `uv sync` (needs `ffmpeg` on PATH for audio extraction and subtitle embedding).
Run by hand: `uv run python main.py`, then open <http://127.0.0.1:8000/>.

---

## Linux: systemd (user service, recommended)

A *user* service runs as you, so downloads are yours and no root is needed.

1. Create `~/.config/systemd/user/ytdlp-ui.service` (replace `/path/to/ytdlp_ui_app` with the real project folder):

   ```ini
   [Unit]
   Description=Antigravity Downloader (yt-dlp UI + scheduler)
   After=network-online.target
   Wants=network-online.target

   [Service]
   WorkingDirectory=/path/to/ytdlp_ui_app
   ExecStart=/path/to/ytdlp_ui_app/.venv/bin/python main.py
   # Environment=YTDLP_UI_HOME=/path/to/ytdlp-data
   # Environment=YTDLP_UI_MAX_CONCURRENT=2
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=default.target
   ```

2. Enable and start it:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now ytdlp-ui
   ```

3. Start at **boot** even when you're not logged in (otherwise it starts at your first login):

   ```bash
   sudo loginctl enable-linger "$USER"
   ```

Day to day:

```bash
systemctl --user status ytdlp-ui          # is it up?
journalctl --user -u ytdlp-ui -f          # live log
systemctl --user restart ytdlp-ui         # after editing the unit or updating code
```

Notes
- Under a service there is no desktop session, so **Browse…** and **Open folder** may do nothing; type the folder path instead.
- To keep yt-dlp current (sites change often): `uv lock --upgrade-package yt-dlp && uv sync`, then restart.
- System-wide service instead? Put the unit in `/etc/systemd/system/`, add `User=<you>` under `[Service]`, change `WantedBy=multi-user.target`, and use `sudo systemctl enable --now ytdlp-ui` (no `--user`, no linger).

---

## Windows: Task Scheduler

> Not tested on a Windows machine. The command shapes are standard; if a step fails, the
> log is `server.log` inside `YTDLP_UI_HOME` (or the project folder).

Assume the project is in `C:\Users\you\ytdlp_ui_app` and you ran `uv sync` there.
`pythonw.exe` runs Python without a console window; the app writes its log to `server.log`.

### Option A: PowerShell (one paste)

Run in a normal (non-admin) PowerShell:

```powershell
$dir = "C:\Users\you\ytdlp_ui_app"
$action   = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\pythonw.exe" -Argument "main.py" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ytdlp-ui" -Action $action -Trigger $trigger -Settings $settings `
            -Description "Antigravity Downloader + scheduler"
Start-ScheduledTask -TaskName "ytdlp-ui"
```

Remove it later: `Unregister-ScheduledTask -TaskName "ytdlp-ui" -Confirm:$false`

### Option B: the Task Scheduler window

1. Start menu → **Task Scheduler** → **Create Task…** (not "Basic Task").
2. **General**: name `ytdlp-ui`. Keep "Run only when user is logged on" (needed for the folder picker).
3. **Triggers** → New → **At log on** (your user).
4. **Actions** → New → *Start a program*:
   - Program: `C:\Users\you\ytdlp_ui_app\.venv\Scripts\pythonw.exe`
   - Arguments: `main.py`
   - Start in: `C:\Users\you\ytdlp_ui_app`
5. **Conditions**: untick "Start the task only if the computer is on AC power" on a laptop.
6. **Settings**: tick "Run task as soon as possible after a scheduled start is missed" and
   "If the task fails, restart every 1 minute"; untick "Stop the task if it runs longer than".
7. OK, then right-click the task → **Run**. Open <http://127.0.0.1:8000/>.

To change settings such as the port, add them before starting, e.g. in PowerShell once:
`[Environment]::SetEnvironmentVariable("YTDLP_UI_PORT","8080","User")`, then restart the task.

### Alternative: let the OS scheduler fire the downloads

You don't have to keep the app running all day. Two other shapes work, though the in-app
Schedules page is simpler:
- Start the app only at the time you want (Task Scheduler "Daily" trigger, or a systemd timer
  with `OnCalendar=`), and add a schedule with **Every… / Also run now** so it fetches immediately.
- Keep the app on and let the in-app scheduler do the timing (the setup above).
