"""Frisbo sync paging guards. Runs without pytest: `venv/bin/python test_sync_paging.py`."""

from datetime import datetime, timedelta

from app.services.sync_service import _created_floor, _newest_created, _one_worker


def test_floor_per_tier():
    now = datetime.utcnow()
    assert abs((now - _created_floor("incremental", None)) - timedelta(days=3)) < timedelta(seconds=5)
    assert abs((now - _created_floor("recheck_90d", None)) - timedelta(days=90)) < timedelta(seconds=5)
    assert _created_floor("full", None) is None
    assert _created_floor("custom", "2026-09-01T00:00:00Z") == datetime(2026, 9, 1)


def test_newest_created():
    page = [{"created_at": "2026-10-11T20:51:22"}, {"created_at": "2026-10-11T18:58:45.123"}]
    assert _newest_created(page) == datetime(2026, 10, 11, 20, 51, 22)
    assert _newest_created([{"created_at": None}]) is None


def test_one_worker_lock():
    with _one_worker("test-tier") as first:
        assert first is True
        with _one_worker("test-tier") as second:
            assert second is False
    with _one_worker("test-tier") as again:
        assert again is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
