"""Unit tests for the logic that has no HTTP around it: schedule maths, the scheduler tick,
the write path that must not resurrect deleted rows, and the ffmpeg command builder."""
import time
from datetime import datetime
from pathlib import Path

import pytest

ts = lambda *a: datetime(*a).timestamp()
NOW = datetime(2026, 11, 3, 10, 0)


# ---------------------------------------------------------------- schedule maths
@pytest.mark.parametrize("kind, at, every, expected", [
    ("every", "", 30, ts(2026, 11, 3, 10, 30)),
    ("every", "", 360, ts(2026, 11, 3, 16, 0)),
    ("daily", "09:00", 0, ts(2026, 11, 4, 9, 0)),    # today's slot has passed -> tomorrow
    ("daily", "23:15", 0, ts(2026, 11, 3, 23, 15)),  # still ahead -> today
    ("daily", "10:00", 0, ts(2026, 11, 4, 10, 0)),   # exactly now counts as passed
    ("once", "2026-12-01T08:00", 0, ts(2026, 12, 1, 8, 0)),
])
def test_compute_next_run(app_env, kind, at, every, expected):
    assert app_env.compute_next_run(kind, at, every, NOW) == expected


def test_compute_next_run_rejects_a_bad_time(app_env):
    with pytest.raises(ValueError):
        app_env.compute_next_run("daily", "not-a-time", 0, NOW)


# ---------------------------------------------------------------- the scheduler tick
def _add_schedule(m, sid, kind, **kw):
    m.insert("schedules", id=sid, name=sid, url="https://example.invalid/x", format="video",
             quality="best", save_dir="d", kind=kind, at=kw.get("at", ""),
             every_min=kw.get("every_min", 0), next_run=kw.get("next_run", time.time() - 1),
             enabled=kw.get("enabled", 1), created_at=0)
    return sid


def test_tick_fires_due_schedules_and_advances_them(app_env):
    m = app_env
    _add_schedule(m, "every", "every", every_min=60)
    _add_schedule(m, "once", "once", at="2026-01-01T00:00")
    m.tick()

    assert len(m.spawned) == 2
    assert len(m.q("SELECT * FROM jobs")) == 2
    rows = {s["id"]: s for s in m.q("SELECT * FROM schedules")}
    assert rows["every"]["next_run"] > time.time() + 3000      # moved an hour on
    assert rows["once"]["enabled"] == 0                        # a one-off disarms itself
    assert rows["once"]["next_run"] is None


def test_tick_ignores_disabled_and_future_schedules(app_env):
    m = app_env
    _add_schedule(m, "paused", "every", every_min=60, enabled=0)
    _add_schedule(m, "later", "every", every_min=60, next_run=time.time() + 3600)
    m.tick()
    assert m.spawned == [] and m.q("SELECT * FROM jobs") == []


def test_a_run_still_going_blocks_the_next_one(app_env):
    """Otherwise a 5-minute schedule on a 20-minute video stacks runs on top of each other."""
    m = app_env
    _add_schedule(m, "every", "every", every_min=60)
    m.tick()
    m.update("schedules", "every", next_run=time.time() - 1)
    m.tick()

    assert len(m.spawned) == 1, "a second run was started while the first was active"
    assert m.q("SELECT last_result FROM schedules WHERE id='every'")[0]["last_result"].startswith("Skipped")


def test_a_finished_run_does_not_block_the_next_one(app_env):
    m = app_env
    _add_schedule(m, "every", "every", every_min=60)
    m.tick()
    m.update("jobs", m.q("SELECT id FROM jobs")[0]["id"], status="completed")
    m.update("schedules", "every", next_run=time.time() - 1)
    m.tick()
    assert len(m.spawned) == 2


def test_a_manual_run_does_not_move_the_plan(app_env):
    m = app_env
    _add_schedule(m, "daily", "daily", at="03:00", next_run=time.time() + 3600)
    before = m.q("SELECT next_run FROM schedules WHERE id='daily'")[0]["next_run"]
    m.launch_schedule(m.q("SELECT * FROM schedules")[0], advance=False)
    assert m.q("SELECT next_run FROM schedules WHERE id='daily'")[0]["next_run"] == before


# ---------------------------------------------------------------- the delete race
def test_a_late_progress_write_cannot_resurrect_a_deleted_job(app_env):
    """Writes are UPDATE-only; a worker thread that reports progress after the user deleted the
    row must not put it back."""
    m = app_env
    jid = m.new_job(url="u", status="downloading", percent=10)
    m.q("DELETE FROM jobs WHERE id=?", (jid,))
    m.update("jobs", jid, percent=50, status="downloading")
    assert m.get_job(jid) is None


def test_targets_fans_a_playlist_out_to_its_children(app_env):
    m = app_env
    parent = m.new_job(url="Album", status="playlist")
    kids = [m.new_job(url=f"u{i}", status="queued", parent_id=parent) for i in range(3)]
    assert sorted(t["id"] for t in m.targets(parent)) == sorted(kids)
    assert [t["id"] for t in m.targets(kids[0])] == [kids[0]]       # a child acts on itself
    assert m.targets("no-such-job") == []


# ---------------------------------------------------------------- ffmpeg commands
def test_video_convert_scales_down_only(app_env):
    cmd = app_env.build_convert_cmd(Path("a.webm"), Path("a.mp4"), "mp4", 720)
    assert "scale=-2:'min(720,ih)'" in cmd, "must never upscale a source below the target"
    assert "libx264" in cmd and "-vn" not in cmd


def test_keeping_the_original_resolution_adds_no_filter(app_env):
    assert "-vf" not in app_env.build_convert_cmd(Path("a.webm"), Path("a.mkv"), "mkv", 0)


def test_audio_targets_drop_the_video_stream(app_env):
    cmd = app_env.build_convert_cmd(Path("a.mp4"), Path("a.mp3"), "mp3", 0)
    assert "-vn" in cmd and "libmp3lame" in cmd
    assert "-vf" not in cmd, "a height on an audio target must be ignored, not applied"


def test_every_offered_target_builds_a_command(app_env):
    for target in ("mp4", "mkv", "webm", "mp3", "m4a", "opus"):
        cmd = app_env.build_convert_cmd(Path("in.mov"), Path(f"out.{target}"), target, 1080)
        assert cmd[0] == "ffmpeg" and cmd[-1] == f"out.{target}"


def test_the_source_file_is_never_an_output(app_env):
    cmd = app_env.build_convert_cmd(Path("clip.mp4"), Path("clip (1).mp4"), "mp4", 0)
    assert cmd.count("clip.mp4") == 1 and cmd[-1] == "clip (1).mp4"


def test_unique_path_steps_around_what_is_already_there(app_env, tmp_path):
    (tmp_path / "x.mp4").touch()
    (tmp_path / "x (1).mp4").touch()
    assert app_env.unique_path(tmp_path / "x.mp4").name == "x (2).mp4"
    assert app_env.unique_path(tmp_path / "fresh.mp4").name == "fresh.mp4"


def test_probe_duration_survives_a_file_ffprobe_cannot_read(app_env, tmp_path):
    junk = tmp_path / "not-a-video.mp4"
    junk.write_text("hello")
    assert app_env.probe_duration(junk) == 0.0


# ---------------------------------------------------------------- paths
def test_relative_save_dirs_land_under_home_and_absolute_ones_are_left_alone(app_env, tmp_path):
    assert app_env.resolve_dir("") == str(tmp_path / "downloads")
    assert app_env.resolve_dir("clips") == str(tmp_path / "clips")
    assert app_env.resolve_dir("/srv/media") == "/srv/media"
