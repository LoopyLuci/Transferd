"""The file convention (headers, safe names, unique names), the backend thread, and the capability list."""

import asyncio
import time

import pytest

from transferd_gui.backend import capabilities
from transferd_gui.backend.files import (
    FILE_SERVICE,
    FileHeaderError,
    HeaderReader,
    encode_header,
    safe_name,
    unique_path,
)
from transferd_gui.backend.worker import BackendWorker


def test_header_round_trips_across_chunk_boundaries():
    body = b"hello, the file body"
    stream = encode_header("report.pdf", len(body)) + body
    reader = HeaderReader()
    received = b""
    for index in range(0, len(stream), 3):  # three-byte chunks, so the header itself is split
        received += reader.feed(stream[index : index + 3])
    assert reader.header is not None
    assert (reader.header.name, reader.header.size) == ("report.pdf", len(body))
    assert received == body


def test_header_that_is_not_json_is_refused():
    reader = HeaderReader()
    with pytest.raises(FileHeaderError, match="not JSON"):
        reader.feed(b"this is not json\n")


def test_header_needs_name_and_non_negative_size():
    with pytest.raises(FileHeaderError, match="name and a size"):
        HeaderReader().feed(b'{"name": "x", "size": -1}\n')
    with pytest.raises(FileHeaderError, match="name and a size"):
        HeaderReader().feed(b'{"name": "x", "size": true}\n')


def test_endless_header_is_refused():
    with pytest.raises(FileHeaderError, match="longer than 4 KB"):
        HeaderReader().feed(b"x" * 5000)


def test_safe_name_keeps_only_the_base_name():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name(r"C:\Users\me\notes.txt") == "notes.txt"
    assert safe_name("bad:name?.txt") == "bad_name_.txt"
    assert safe_name("   ") == "file"
    assert len(safe_name("a" * 300)) == 120


def test_unique_path_adds_a_counter_instead_of_overwriting(tmp_path):
    first = unique_path(tmp_path, "photo.jpg")
    assert first == tmp_path / "photo.jpg"
    first.write_bytes(b"1")
    second = unique_path(tmp_path, "photo.jpg")
    assert second == tmp_path / "photo (2).jpg"
    second.write_bytes(b"2")
    assert unique_path(tmp_path, "photo.jpg") == tmp_path / "photo (3).jpg"


def test_file_service_name_is_the_documented_one():
    assert FILE_SERVICE == "file"


def test_worker_runs_coroutines_and_returns_results():
    worker = BackendWorker()
    worker.start()
    try:
        async def add(a, b):
            await asyncio.sleep(0)
            return a + b

        assert worker.submit(add(2, 3)).result(timeout=5) == 5
        assert worker.running
    finally:
        worker.stop()
    assert not worker.running


def test_worker_reports_exceptions_through_the_future():
    worker = BackendWorker()
    worker.start()
    try:
        async def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            worker.submit(boom()).result(timeout=5)
    finally:
        worker.stop()


def test_worker_refuses_work_when_stopped():
    worker = BackendWorker()
    worker.start()
    worker.stop()

    async def later():
        return 1

    coro = later()
    with pytest.raises(RuntimeError, match="not running"):
        worker.submit(coro)


def test_call_soon_runs_on_the_backend_thread():
    import threading

    worker = BackendWorker()
    worker.start()
    try:
        seen = []
        done = threading.Event()

        def record():
            seen.append(threading.current_thread().name)
            done.set()

        worker.call_soon(record)
        assert done.wait(5)
        assert seen == ["transferd-backend"]
    finally:
        worker.stop()
    assert time.monotonic() > 0


def test_every_unavailable_feature_has_a_reason():
    for name in (
        capabilities.HOLE_PUNCH_START,
        capabilities.MESH_DNS,
        capabilities.GHOST_REGISTER,
        capabilities.CUSTOM_RELAY_TOKEN,
        capabilities.RELAY_UDP_PORT,
        capabilities.SESSION_TIMEOUTS,
    ):
        assert capabilities.reason(name).strip()
    with pytest.raises(KeyError):
        capabilities.reason("teleport")
