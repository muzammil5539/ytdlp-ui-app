# Domain Glossary

- **Download Job**: One row in `jobs`; a single video (or, for a scheduled run, a whole URL). Tracked by UUID with structured columns: `status`, `percent`, `speed`, `eta`, `title`, `error`.
- **Parent Job** (`status='playlist'`): Container for a playlist; downloads nothing itself. Pause/resume/cancel/delete on it fan out to its children.
- **Child Job**: A Download Job with a `parent_id`; `file_prefix` keeps playlist order in filenames.
- **Schedule**: A saved URL + options + timing (`once` / `every` N min / `daily` at HH:MM). Each firing creates a **Scheduled Run** job (`schedule_id` set).
- **Scheduler**: An asyncio loop in the app, ticking every 20 s; fires schedules whose `next_run` has passed (so runs missed while off fire once on startup) and skips a firing if the previous run is still active.
- **Archive**: Per-schedule yt-dlp `download_archive` file in `archives/`; makes recurring runs download only new videos. A run that downloads nothing deletes its own job row.
- **Job State**: The SQLite `jobs` table is the single source of truth. All access goes through one connection + lock; progress writes are throttled to 1/s and are UPDATE-only so a deleted job can't be resurrected.
- **Cancel flag** (`CANCEL`): In-memory `pause|cancel|delete` request, checked by the yt-dlp progress hook, which raises to stop the download.
