"""Integration tests for every HTTP endpoint, driven through FastAPI's TestClient.

Nothing here downloads or converts: `main.spawn` is stubbed by the `app_env` fixture, so these
assert what the API *records and answers*, which is what the UI reads.
"""
import time

import pytest


# ---------------------------------------------------------------- basics
def test_the_root_sends_a_browser_to_the_ui(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 302) and r.headers["location"] == "/ui/"


def test_the_ui_is_served(client):
    r = client.get("/ui/")
    assert r.status_code == 200 and "<title>" in r.text


def test_config_tells_the_ui_where_files_land(client, tmp_path):
    body = client.get("/api/config").json()
    assert body["default_save_dir"] == str(tmp_path / "downloads")
    assert body["max_concurrent"] >= 1


# ---------------------------------------------------------------- queueing
def test_a_download_is_queued_and_listed(client):
    jid = client.post("/api/download", json={"url": "https://example.invalid/v"}).json()["job_id"]
    jobs = client.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [jid]
    assert jobs[0]["status"] == "queued" and jobs[0]["percent"] == 0


def test_download_options_are_stored_as_chosen(client, tmp_path):
    client.post("/api/download", json={"url": "u", "format": "audio", "quality": "720p",
                                       "save_dir": "podcasts"})
    job = client.get("/api/jobs").json()[0]
    assert job["format"] == "audio" and job["quality"] == "720p"
    assert job["save_dir"] == str(tmp_path / "podcasts")


def test_a_batch_becomes_one_parent_with_ordered_children(client):
    r = client.post("/api/download_batch", json={
        "parent_title": "Season 1", "format": "video", "quality": "best",
        "items": [{"url": "u1", "title": "Pilot"}, {"url": "u2", "title": "Second"}]})
    parent = r.json()["parent_id"]

    jobs = client.get("/api/jobs").json()
    kids = sorted((j for j in jobs if j["parent_id"] == parent), key=lambda j: j["file_prefix"])
    assert len(jobs) == 3
    assert next(j for j in jobs if j["id"] == parent)["status"] == "playlist"
    assert [k["file_prefix"] for k in kids] == ["01", "02"]
    assert [k["title"] for k in kids] == ["Pilot", "Second"]
    assert all("Season 1" in k["save_dir"] for k in kids), "children go in the playlist's folder"


def test_a_playlist_title_cannot_escape_the_download_folder(client, tmp_path):
    client.post("/api/download_batch", json={"parent_title": "../../etc", "items": [{"url": "u", "title": "t"}]})
    job = next(j for j in client.get("/api/jobs").json() if j["parent_id"])
    assert ".." not in job["save_dir"] and str(tmp_path) in job["save_dir"]


# ---------------------------------------------------------------- pause / resume / cancel / delete
@pytest.fixture
def queued_job(client):
    return client.post("/api/download", json={"url": "https://example.invalid/v"}).json()["job_id"]


def test_pause_then_resume_puts_a_job_back_in_the_queue(client, app_env, queued_job):
    client.post(f"/api/pause/{queued_job}")
    assert app_env.get_job(queued_job)["status"] == "paused"
    assert app_env.CANCEL[queued_job] == "pause"

    client.post(f"/api/resume/{queued_job}")
    assert app_env.get_job(queued_job)["status"] == "queued"
    assert queued_job not in app_env.CANCEL, "the stop flag must be cleared or the job stops again"


def test_cancel_marks_the_job_failed_with_a_reason(client, app_env, queued_job):
    client.post(f"/api/cancel/{queued_job}")
    job = app_env.get_job(queued_job)
    assert job["status"] == "error" and job["error"] == "Canceled"


def test_a_canceled_job_can_be_retried(client, app_env, queued_job):
    client.post(f"/api/cancel/{queued_job}")
    client.post(f"/api/resume/{queued_job}")
    job = app_env.get_job(queued_job)
    assert job["status"] == "queued" and job["error"] is None


def test_pausing_a_playlist_pauses_every_child(client, app_env):
    parent = client.post("/api/download_batch", json={
        "parent_title": "P", "items": [{"url": "a", "title": "a"}, {"url": "b", "title": "b"}]}).json()["parent_id"]
    client.post(f"/api/pause/{parent}")
    kids = [j for j in client.get("/api/jobs").json() if j["parent_id"] == parent]
    assert [k["status"] for k in kids] == ["paused", "paused"]


def test_deleting_a_playlist_removes_its_children_too(client, queued_job):
    parent = client.post("/api/download_batch", json={
        "parent_title": "P", "items": [{"url": "a", "title": "a"}]}).json()["parent_id"]
    client.delete(f"/api/jobs/{parent}")
    left = client.get("/api/jobs").json()
    assert [j["id"] for j in left] == [queued_job], "only the unrelated job survives"


def test_acting_on_a_job_that_is_gone_is_not_an_error(client):
    for path, method in [("/api/pause/nope", "post"), ("/api/resume/nope", "post"),
                         ("/api/cancel/nope", "post"), ("/api/jobs/nope", "delete")]:
        assert getattr(client, method)(path).status_code == 200


# ---------------------------------------------------------------- clear finished
def test_clear_finished_keeps_paused_and_running_work(client, app_env):
    """Regression: 'Clear finished' used to delete paused jobs, silently throwing away a
    part-finished download the user had deliberately stopped."""
    ids = {}
    for status in ("completed", "error", "paused", "downloading", "queued"):
        ids[status] = app_env.new_job(url=status, status=status)

    client.post("/api/clear_finished")
    left = {j["url"] for j in client.get("/api/jobs").json()}
    assert left == {"paused", "downloading", "queued"}


def test_clear_finished_removes_an_empty_playlist_but_keeps_a_busy_one(client, app_env):
    empty = app_env.new_job(url="empty", status="playlist")
    busy = app_env.new_job(url="busy", status="playlist")
    app_env.new_job(url="child", status="downloading", parent_id=busy)
    app_env.new_job(url="done-child", status="completed", parent_id=busy)

    client.post("/api/clear_finished")
    left = {j["id"] for j in client.get("/api/jobs").json()}
    assert busy in left and empty not in left
    assert {j["url"] for j in client.get("/api/jobs").json() if j["parent_id"]} == {"child"}


# ---------------------------------------------------------------- convert
def test_convert_queues_one_job_per_file(client, app_env, tmp_path):
    srcs = []
    for name in ("one.mkv", "two.mkv"):
        p = tmp_path / name
        p.write_bytes(b"not really a video")
        srcs.append(str(p))

    r = client.post("/api/convert", json={"sources": srcs, "format": "mp4", "height": 720})
    assert r.json() == {"queued": 2}

    jobs = client.get("/api/jobs").json()
    assert {j["kind"] for j in jobs} == {"convert"}
    assert {j["format"] for j in jobs} == {"mp4"} and {j["quality"] for j in jobs} == {"720"}
    assert {j["save_dir"] for j in jobs} == {str(tmp_path)}, "blank save_dir means beside the source"


def test_convert_refuses_a_file_that_is_not_there(client, tmp_path):
    r = client.post("/api/convert", json={"sources": [str(tmp_path / "ghost.mkv")], "format": "mp4"})
    assert r.status_code == 400 and "ghost.mkv" in r.json()["detail"]


def test_convert_refuses_an_empty_list(client):
    r = client.post("/api/convert", json={"sources": ["", "  "], "format": "mp3"})
    assert r.status_code == 400 and "at least one file" in r.json()["detail"].lower()


def test_convert_rejects_a_format_it_cannot_produce(client, tmp_path):
    p = tmp_path / "a.mkv"; p.write_bytes(b"x")
    assert client.post("/api/convert", json={"sources": [str(p)], "format": "avi"}).status_code == 422


def test_convert_says_so_when_ffmpeg_is_missing(client, monkeypatch, tmp_path):
    p = tmp_path / "a.mkv"; p.write_bytes(b"x")
    monkeypatch.setattr("main.shutil.which", lambda name: None)
    r = client.post("/api/convert", json={"sources": [str(p)], "format": "mp4"})
    assert r.status_code == 400 and "ffmpeg" in r.json()["detail"].lower()


# ---------------------------------------------------------------- resolve
def _stub_ytdlp(monkeypatch, info):
    class Stub:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            if isinstance(info, Exception):
                raise info
            return info
    monkeypatch.setattr("main.yt_dlp.YoutubeDL", Stub)


def test_resolve_reports_a_single_video(client, monkeypatch):
    _stub_ytdlp(monkeypatch, {"title": "One clip"})
    assert client.post("/api/resolve", json={"url": "u"}).json() == {"type": "video", "title": "One clip"}


def test_resolve_expands_a_playlist_and_repairs_bare_ids(client, monkeypatch):
    _stub_ytdlp(monkeypatch, {"title": "Mix", "entries": [
        {"title": "A", "url": "https://full/a"},
        {"title": "B", "url": "abc123"},        # flat extraction gives a bare id
        None,                                    # yt-dlp yields None for a dead entry
    ]})
    body = client.post("/api/resolve", json={"url": "u"}).json()
    assert body["type"] == "playlist" and body["title"] == "Mix"
    assert [e["url"] for e in body["entries"]] == ["https://full/a", "https://www.youtube.com/watch?v=abc123"]


def test_resolve_hands_back_the_reason_it_failed(client, monkeypatch):
    _stub_ytdlp(monkeypatch, RuntimeError("Video unavailable"))
    assert client.post("/api/resolve", json={"url": "u"}).json() == {"error": "Video unavailable"}


def test_resolve_handles_extractors_that_return_nothing(client, monkeypatch):
    _stub_ytdlp(monkeypatch, None)
    assert "error" in client.post("/api/resolve", json={"url": "u"}).json()


# ---------------------------------------------------------------- schedules
def _make(client, **over):
    body = {"url": "https://example.invalid/@c", "kind": "daily", "at": "03:00", "run_now": False}
    body.update(over)
    return client.post("/api/schedules", json=body)


def test_a_daily_schedule_is_created_with_its_next_run(client):
    sid = _make(client, name="Morning").json()["id"]
    s = client.get("/api/schedules").json()[0]
    assert s["id"] == sid and s["name"] == "Morning" and s["enabled"] == 1
    assert s["next_run"] > time.time()


def test_an_unnamed_schedule_falls_back_to_its_url(client):
    _make(client, name="   ")
    assert client.get("/api/schedules").json()[0]["name"] == "https://example.invalid/@c"


def test_run_now_makes_the_schedule_due_immediately(client):
    _make(client, run_now=True)
    assert client.get("/api/schedules").json()[0]["next_run"] <= time.time()


@pytest.mark.parametrize("body, why", [
    ({"kind": "every", "every_min": 0}, "a zero-minute repeat would spin"),
    ({"kind": "every", "every_min": -5}, "a negative repeat is meaningless"),
    ({"kind": "daily", "at": "25:00"}, "not a real time"),
    ({"kind": "daily", "at": ""}, "no time given"),
    ({"kind": "once", "at": "whenever"}, "not a date"),
])
def test_a_schedule_that_could_not_work_is_refused(client, body, why):
    assert _make(client, **body).status_code == 400, why


def test_an_unknown_repeat_kind_is_refused(client):
    assert _make(client, kind="hourly").status_code == 422


def test_running_a_schedule_by_hand_creates_a_job_without_moving_the_plan(client):
    sid = _make(client).json()["id"]
    before = client.get("/api/schedules").json()[0]["next_run"]

    assert client.post(f"/api/schedules/{sid}/run").status_code == 200
    after = client.get("/api/schedules").json()[0]
    assert after["next_run"] == before
    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 1 and jobs[0]["schedule_id"] == sid


def test_toggling_pauses_and_re_arms_a_schedule(client):
    sid = _make(client).json()["id"]
    client.post(f"/api/schedules/{sid}/toggle")
    assert client.get("/api/schedules").json()[0]["enabled"] == 0

    client.post(f"/api/schedules/{sid}/toggle")
    s = client.get("/api/schedules").json()[0]
    assert s["enabled"] == 1 and s["next_run"] > time.time()


def test_re_arming_a_spent_one_off_runs_it_now(client):
    sid = _make(client, kind="once", at="2026-12-01T08:00").json()["id"]
    client.post(f"/api/schedules/{sid}/toggle")   # off
    client.post(f"/api/schedules/{sid}/toggle")   # on again
    assert client.get("/api/schedules").json()[0]["next_run"] <= time.time()


def test_deleting_a_schedule_takes_its_archive_with_it(client, app_env):
    sid = _make(client).json()["id"]
    archive = app_env.ARCHIVES / f"{sid}.txt"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("youtube abc123\n")

    client.delete(f"/api/schedules/{sid}")
    assert client.get("/api/schedules").json() == []
    assert not archive.exists(), "a stale archive would make a re-added schedule skip old videos"


def test_acting_on_a_schedule_that_is_gone_is_a_404(client):
    assert client.post("/api/schedules/nope/run").status_code == 404
    assert client.post("/api/schedules/nope/toggle").status_code == 404


# ---------------------------------------------------------------- open folder
def test_open_folder_asks_the_desktop_to_open_the_save_dir(client, app_env, monkeypatch):
    opened = []
    monkeypatch.setattr(app_env, "open_path", opened.append)
    jid = client.post("/api/download", json={"url": "u", "save_dir": "clips"}).json()["job_id"]

    assert client.post(f"/api/open/{jid}").status_code == 200
    assert opened == [str(app_env.HOME / "clips")]


def test_open_folder_on_a_headless_box_reports_the_failure(client, app_env, monkeypatch):
    def boom(_): raise OSError("no display")
    monkeypatch.setattr(app_env, "open_path", boom)
    jid = client.post("/api/download", json={"url": "u"}).json()["job_id"]
    assert client.post(f"/api/open/{jid}").status_code == 500


def test_open_folder_for_an_unknown_job_is_a_404(client):
    assert client.post("/api/open/nope").status_code == 404


# ---------------------------------------------------------------- host guard
def test_a_rebound_hostname_is_turned_away(client, app_env, monkeypatch):
    """A page on the open web can point its own hostname at 127.0.0.1; without this check it
    could then read and convert any file on the machine."""
    monkeypatch.setattr(app_env, "ALLOWED_HOSTS", {"localhost", "127.0.0.1", "::1"})
    assert client.get("/api/jobs", headers={"host": "evil.example.com"}).status_code == 421
    assert client.get("/api/jobs", headers={"host": "127.0.0.1:8000"}).status_code == 200


def test_binding_to_a_real_interface_switches_the_check_off(client, app_env, monkeypatch):
    monkeypatch.setattr(app_env, "ALLOWED_HOSTS", {"*"})
    assert client.get("/api/jobs", headers={"host": "nas.local:8000"}).status_code == 200


def test_config_reports_what_the_machine_can_actually_do(client, app_env):
    """The UI warns about a stale yt-dlp from this, so these keys are part of the contract."""
    body = client.get("/api/config").json()
    assert set(body) >= {"default_save_dir", "max_concurrent", "ytdlp_version",
                         "ytdlp_age_days", "ytdlp_stale", "js_runtime", "ffmpeg"}
    assert isinstance(body["ytdlp_stale"], bool)


def test_config_flags_a_stale_ytdlp(client, app_env, monkeypatch):
    monkeypatch.setattr(app_env, "ytdlp_age_days", lambda: 400)
    assert client.get("/api/config").json()["ytdlp_stale"] is True
