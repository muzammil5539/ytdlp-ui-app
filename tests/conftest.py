"""Shared fixtures.

Two rules hold for the whole suite:

* **No test touches the network and no test downloads anything.** `yt_dlp.YoutubeDL` is replaced
  wherever it would be reached, and `main.spawn` is replaced so queued work is recorded instead of
  run. A suite that quietly depends on YouTube being up is not a suite.
* **No test touches the real data directory.** Every test gets its own SQLite file, downloads
  folder and archives folder under `tmp_path`, so tests cannot contaminate each other or the repo.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """main, re-pointed at a throwaway home. Returns the module with `.spawned` attached."""
    monkeypatch.setattr(main, "HOME", tmp_path)
    monkeypatch.setattr(main, "ARCHIVES", tmp_path / "archives")
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "jobs.db")

    real_init = main.init_db
    monkeypatch.setattr(main, "init_db", lambda path=tmp_path / "jobs.db": real_init(path))

    spawned = []

    def fake_spawn(coro):           # record the coroutine, never run it
        spawned.append(coro.cr_code.co_name if hasattr(coro, "cr_code") else "?")
        coro.close()

    monkeypatch.setattr(main, "spawn", fake_spawn)
    monkeypatch.setattr(main, "CANCEL", {})
    main.init_db()
    main.spawned = spawned
    yield main
    if main._conn is not None:
        main._conn.close()
        main._conn = None


@pytest.fixture
def client(app_env):
    """A TestClient whose lifespan runs against the throwaway home.

    base_url is a loopback name on purpose: TestClient otherwise sends `Host: testserver`,
    which the DNS-rebinding guard rejects. Tests go through that guard rather than round it."""
    with TestClient(app_env.app, base_url="http://127.0.0.1:8000") as c:
        yield c


@pytest.fixture
def no_ytdlp(monkeypatch):
    """Make any attempt to reach yt-dlp loud rather than silently hitting the network."""
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("a test reached yt_dlp.YoutubeDL; stub it instead")

    monkeypatch.setattr(main.yt_dlp, "YoutubeDL", Boom)
