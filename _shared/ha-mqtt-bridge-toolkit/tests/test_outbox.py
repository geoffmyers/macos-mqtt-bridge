from __future__ import annotations

from pathlib import Path

from ha_mqtt_bridge.outbox import Outbox


def test_enqueue_creates_file_and_tracks_count(tmp_path: Path):
    outbox = Outbox(str(tmp_path / "outbox.jsonl"))
    assert outbox.pending_count() == 0
    outbox.enqueue("topic/a", {"x": 1})
    outbox.enqueue("topic/b", {"y": 2})
    assert outbox.pending_count() == 2


def test_drain_publishes_in_fifo_order(tmp_path: Path):
    outbox = Outbox(str(tmp_path / "outbox.jsonl"))
    for i in range(5):
        outbox.enqueue("t", {"i": i})
    seen = []

    def publish(topic, payload, retain=None):
        seen.append(payload["i"])
        return True

    drained = outbox.drain(publish)
    assert drained == 5
    assert seen == [0, 1, 2, 3, 4]
    assert outbox.pending_count() == 0


def test_drain_stops_at_first_failure_preserving_remaining(tmp_path: Path):
    outbox = Outbox(str(tmp_path / "outbox.jsonl"))
    for i in range(5):
        outbox.enqueue("t", {"i": i})

    # Fail on i=2.
    def publish(topic, payload, retain=None):
        return payload["i"] != 2

    drained = outbox.drain(publish)
    assert drained == 2  # 0 and 1 succeeded
    # 2, 3, 4 remain (head-of-line block at 2)
    assert outbox.pending_count() == 3

    # Now succeed; should drain the remaining 3.
    drained = outbox.drain(lambda t, p, r: True)
    assert drained == 3
    assert outbox.pending_count() == 0


def test_drain_with_no_pending_returns_zero(tmp_path: Path):
    outbox = Outbox(str(tmp_path / "outbox.jsonl"))
    assert outbox.drain(lambda t, p, r: True) == 0


def test_corrupt_lines_are_dropped(tmp_path: Path):
    path = tmp_path / "outbox.jsonl"
    path.write_text(
        '{"topic":"a","payload":{"x":1}}\n'
        "not-json\n"
        '{"topic":"b","payload":{"y":2}}\n'
    )
    outbox = Outbox(str(path))
    seen = []
    drained = outbox.drain(lambda t, p, r: seen.append((t, p["x"] if "x" in p else p["y"])) or True)
    assert drained == 2
    assert seen == [("a", 1), ("b", 2)]


def test_max_pending_drops_overflow(tmp_path: Path):
    outbox = Outbox(str(tmp_path / "outbox.jsonl"), max_pending=3)
    for i in range(10):
        outbox.enqueue("t", {"i": i})
    assert outbox.pending_count() == 3


def test_survives_process_restart(tmp_path: Path):
    """Simulate process crash by abandoning the first Outbox and rebuilding."""
    path = str(tmp_path / "outbox.jsonl")
    o1 = Outbox(path)
    o1.enqueue("t", {"i": 1})
    o1.enqueue("t", {"i": 2})

    # Process restart — new Outbox sees the existing file.
    o2 = Outbox(path)
    assert o2.pending_count() == 2
    seen = []
    o2.drain(lambda t, p, r: seen.append(p["i"]) or True)
    assert seen == [1, 2]
