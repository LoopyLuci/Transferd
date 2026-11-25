"""The pure model: the event log buffer, ring buffers and rates, the snapshot helpers and the formatting."""

import math
import threading

import pytest

from transferd_gui.model.eventlog import EventLog, EventLogStore
from transferd_gui.model.fmt import NO_DATA, human_age, human_bytes, human_mbps, or_no_data, short_hex
from transferd_gui.model.snapshot import IdentitySnapshot, ListenerSnapshot, NodeSnapshot, RelaySnapshot, RouteSnapshot, SessionSnapshot
from transferd_gui.model.telemetry import LIBRARY_MBPS, RTT, RateTracker, RingBuffer, TelemetryStore, record_routes


def route(key="p/1", up=True, rtt=None, sent=0, received=0, mbps=None):
    return RouteSnapshot(key=key, peer_id="p", link_id=1, kind="direct", up=up, closed=False, rtt_ms=rtt, mbps=mbps,
                         sent_bytes=sent, received_bytes=received)


def test_event_log_drops_the_oldest_past_capacity():
    log = EventLog(3)
    for index in range(5):
        log.add("info", "t", f"m{index}", when=float(index))
    assert [e.message for e in log.entries()] == ["m2", "m3", "m4"]
    assert len(log) == 3


def test_event_log_filters_by_level_and_above():
    log = EventLog(10)
    log.add("info", "a", "i")
    log.add("warning", "a", "w")
    log.add("error", "a", "e")
    assert [e.level for e in log.entries("warning")] == ["warning", "error"]
    assert [e.level for e in log.entries("error")] == ["error"]


def test_event_log_rejects_unknown_levels_and_zero_capacity():
    log = EventLog(2)
    with pytest.raises(ValueError):
        log.add("debug", "a", "x")
    with pytest.raises(ValueError):
        EventLog(0)


def test_event_log_resize_keeps_the_newest_entries():
    log = EventLog(5)
    for index in range(5):
        log.add("info", "t", str(index))
    log.resize(2)
    assert [e.message for e in log.entries()] == ["3", "4"]
    assert log.capacity == 2


def test_event_log_store_is_safe_across_threads():
    store = EventLogStore(10_000)

    def writer():
        for index in range(500):
            store.add("info", "thread", str(index))

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.entries()) == 2000


def test_ring_buffer_keeps_only_the_window():
    buffer = RingBuffer(3)
    for index in range(10):
        buffer.append(float(index), float(index * 2))
    assert buffer.points() == [(7.0, 14.0), (8.0, 16.0), (9.0, 18.0)]
    assert buffer.latest() == 18.0
    with pytest.raises(ValueError):
        RingBuffer(0)


def test_store_never_records_missing_or_non_finite_values():
    store = TelemetryStore(10)
    store.record(("r", RTT), 1.0, None)
    store.record(("r", RTT), 2.0, math.nan)
    store.record(("r", RTT), 3.0, math.inf)
    assert store.points(("r", RTT)) == []
    assert store.latest(("r", RTT)) is None
    store.record(("r", RTT), 4.0, 12.5)
    assert store.points(("r", RTT)) == [(4.0, 12.5)]


def test_rate_tracker_needs_two_samples_and_handles_resets():
    tracker = RateTracker()
    assert tracker.observe(10.0, 1000) is None
    assert tracker.observe(12.0, 3000) == pytest.approx(1000.0)
    assert tracker.observe(12.0, 4000) is None  # no time passed
    assert tracker.observe(13.0, 100) is None  # counter went backwards


def test_record_routes_samples_up_routes_and_forgets_departed_ones():
    store = TelemetryStore(100)
    record_routes(store, 10.0, [route("a/1", rtt=3.0, mbps=2.0, sent=0, received=0)])
    record_routes(store, 11.0, [route("a/1", rtt=4.0, mbps=2.5, sent=125_000, received=0)])
    assert store.points(("a/1", RTT)) == [(10.0, 3.0), (11.0, 4.0)]
    assert store.latest(("a/1", LIBRARY_MBPS)) == 2.5
    # 125000 bytes in one second is one Mbit/s of sending.
    assert store.latest(("a/1", "sent_mbps")) == pytest.approx(1.0)
    record_routes(store, 12.0, [route("b/2", rtt=1.0)])
    assert ("a/1", RTT) not in store.keys()


def test_down_routes_record_nothing():
    store = TelemetryStore(100)
    record_routes(store, 1.0, [route("a/1", up=False, rtt=9.0)])
    assert store.keys() == []


def test_snapshot_helpers_count_routes_and_ready_peers():
    routes = (route("a/1", up=True), route("a/2", up=False))
    session = SessionSnapshot("a", "alpha", "ready", 3, ("file",), routes, established_at=0.0)
    idle = SessionSnapshot("b", "bravo", "negotiating", 0, (), (), established_at=0.0)
    snap = NodeSnapshot(
        taken_at=1.0,
        identity=IdentitySnapshot(ready=True, node_id="x"),
        listener=ListenerSnapshot(active=False),
        relay=RelaySnapshot(configured=False, address="", state="off"),
        sessions=(session, idle),
    )
    assert snap.routes_up == 1
    assert len(snap.all_routes) == 2
    assert [s.peer_id for s in snap.ready_peers()] == ["a"]
    assert session.routes_up == 1


def test_formatting_never_invents_a_value():
    assert or_no_data(None, human_mbps) == NO_DATA
    assert or_no_data(1.5, human_mbps) == "1.50 Mbit/s"
    assert human_bytes(512) == "512 B"
    assert human_bytes(3 * 1024 * 1024) == "3.0 MiB"
    assert human_age(65) == "1 min 5 s"
    assert human_age(3725) == "1 h 2 min"
    assert short_hex("abcdef0123456789", 4, 4) == "abcd...6789"
    assert short_hex("abc") == "abc"
