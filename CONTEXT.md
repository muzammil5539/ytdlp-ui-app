# Domain Glossary

The words this codebase uses, and what each one means in it.

## Jobs

- **Download Job**: One row in `jobs`; a single video, or for a scheduled run, a whole URL.
  Tracked by UUID with structured columns: `status`, `percent`, `speed`, `eta`, `title`,
  `error`.
- **Parent Job** (`status='playlist'`): Container for a playlist; downloads nothing itself.
  Pause, resume, cancel and delete on it fan out to its children.
- **Child Job**: A Download Job with a `parent_id`; `file_prefix` keeps playlist order in
  filenames.
- **Convert Job** (`kind='convert'`): The same row shape used for an ffmpeg conversion. The
  source path sits in `url`, the target container in `format`, the target height in
  `quality`. It shares the queue and the concurrency limit with downloads.
- **Job State**: The SQLite `jobs` table is the single source of truth. All access goes
  through one connection and one lock; progress writes are throttled to one per second and
  are `UPDATE`-only, so a late write cannot resurrect a deleted job.
- **Active**: `queued`, `starting`, `downloading` or `processing`. Rows left active when the
  app stops are requeued on the next start. Note that a conversion in flight also reads as
  `downloading`; the UI shows it as *Converting*.
- **Finished**: `completed` or `error`. *Clear finished* removes these. **`paused` is not
  finished** and is kept.
- **Cancel flag** (`CANCEL`): In-memory job id → `pause`, `cancel`, `delete` or `shutdown`,
  checked by the yt-dlp progress hook and by the converter's watcher thread, which stop the
  work when they see one. Flags for deleted jobs are dropped after 30 seconds.

## Scheduling

- **Schedule**: A saved URL plus options and timing — `once`, `every` N minutes, or `daily`
  at HH:MM. Each firing creates a **Scheduled Run** job (`schedule_id` set).
- **Scheduler**: An asyncio loop inside the app, ticking every 20 seconds. It fires
  schedules whose `next_run` has passed, so runs missed while the app was off fire once on
  startup, and it skips a firing while the previous run is still active.
- **Archive**: A per-schedule yt-dlp `download_archive` file in `archives/`, which makes
  recurring runs fetch only new videos. A run that downloads nothing deletes its own job
  row rather than filling the history.
- **Run now**: A manual firing. It starts a run but leaves `next_run` where it was, so it
  does not shift the plan.

## Environment

- **Home** (`YTDLP_UI_HOME`): The data directory holding `jobs.db`, `downloads/` and
  `archives/`. Relative save folders resolve against it; absolute ones are left alone.
- **JS runtime**: `deno`, `node` or `bun`. YouTube extraction needs one and yt-dlp enables
  only `deno` by default, so the app finds whichever is on `PATH` and passes it as
  `{runtime: {}}` — the Python API's shape, not the CLI's list. Without one, some formats
  are missing and some videos fall back to a player client whose URLs get 403'd.
- **Stale yt-dlp**: A release more than 90 days old. Because YouTube changes faster than
  yt-dlp ships, this is the usual cause of 403s and extraction failures. The app reports
  the age from `/api/config`, warns in the UI, and adds the fix to the error text.
- **Allowed hosts** (`YTDLP_UI_ALLOWED_HOSTS`): Host names the API answers to. Loopback
  only by default, which stops a page you visit from reaching the API by DNS rebinding.
  Binding to a real interface switches the check off.
