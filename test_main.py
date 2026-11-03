"""Run: .venv/bin/python test_main.py  — checks schedule math + one scheduler tick on a temp DB."""
import tempfile
import time
from datetime import datetime
from pathlib import Path

import main

now = datetime(2026, 11, 3, 10, 0)
ts = lambda *a: datetime(*a).timestamp()
assert main.compute_next_run("every", "", 30, now) == ts(2026, 11, 3, 10, 30)
assert main.compute_next_run("daily", "09:00", 0, now) == ts(2026, 11, 4, 9, 0)   # already passed -> tomorrow
assert main.compute_next_run("daily", "23:15", 0, now) == ts(2026, 11, 3, 23, 15)  # later today
assert main.compute_next_run("once", "2026-12-01T08:00", 0, now) == ts(2026, 12, 1, 8, 0)

main.init_db(Path(tempfile.mkdtemp()) / "t.db")
spawned = []
main.spawn = lambda coro: (spawned.append(1), coro.close())  # don't actually download

def add(kind, **kw):
    sid = f"s-{kind}"
    main.insert("schedules", id=sid, name=kind, url="http://x", format="video", quality="best", save_dir="d",
                kind=kind, at=kw.get("at", ""), every_min=kw.get("every_min", 0), next_run=time.time() - 1,
                enabled=1, created_at=0)
    return sid

every, once = add("every", every_min=60), add("once", at="2026-01-01T00:00")
main.tick()
sched = {s["id"]: s for s in main.q("SELECT * FROM schedules")}
assert len(spawned) == 2 and len(main.q("SELECT * FROM jobs")) == 2
assert sched[every]["next_run"] > time.time() + 3000      # moved an hour ahead
assert sched[once]["enabled"] == 0                        # one-off disarms itself

# overlap guard: a run still in flight blocks the next one
main.update("schedules", every, next_run=time.time() - 1)
main.tick()
assert len(spawned) == 2 and main.q("SELECT last_result FROM schedules WHERE id=?", (every,))[0]["last_result"].startswith("Skipped")

# deleted jobs stay deleted even if a late progress write arrives
jid = main.q("SELECT id FROM jobs")[0]["id"]
main.q("DELETE FROM jobs WHERE id=?", (jid,))
main.update("jobs", jid, percent=50)
assert main.get_job(jid) is None
# convert: resolution only ever shrinks, audio targets drop video, existing outputs are never overwritten
src, out = Path("a.webm"), Path("a.mp4")
v = main.build_convert_cmd(src, out, "mp4", 720)
assert "scale=-2:'min(720,ih)'" in v and "libx264" in v and "-vn" not in v
assert "-vf" not in main.build_convert_cmd(src, out, "mkv", 0)
a = main.build_convert_cmd(src, Path("a.mp3"), "mp3", 0)
assert "-vn" in a and "libmp3lame" in a and "-vf" not in a
tmp = Path(tempfile.mkdtemp()); (tmp / "x.mp4").touch(); (tmp / "x (1).mp4").touch()
assert main.unique_path(tmp / "x.mp4").name == "x (2).mp4"
print("ok")
