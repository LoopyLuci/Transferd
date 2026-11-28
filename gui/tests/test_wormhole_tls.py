"""The mailbox over TLS, and the client's choices of mailbox: a pinned certificate meets and passes messages, a wrong pin is refused
before anything is sent, a plain connection to another computer is refused, a mailbox with no pin must have a certificate the
system trusts, the client keeps trying while a mailbox starts, it passes over a mailbox that is down, and after it has
connected it stays on that mailbox."""

from __future__ import annotations

import asyncio
import socket

import pytest

from transferd_gui.wormhole import tls
from transferd_gui.wormhole.addresses import MailboxAddress
from transferd_gui.wormhole.errors import MailboxError
from transferd_gui.wormhole.keys import new_side
from transferd_gui.wormhole.mailbox import MailboxClient
from transferd_gui.wormhole.server import MailboxServer
from test_wormhole_mailbox import run


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


@pytest.fixture
def identity(tmp_path):
    return tls.load_or_create(tmp_path / "mailbox")


# ----- the certificate --------------------------------------------------------------------------------


def test_the_identity_is_kept_between_runs(tmp_path):
    first = tls.load_or_create(tmp_path)
    again = tls.load_or_create(tmp_path)
    assert again.fingerprint == first.fingerprint
    assert len(first.fingerprint) == 64
    assert first.cert_path.exists() and first.key_path.exists()


def test_the_identity_is_made_again_when_its_key_no_longer_matches_it(tmp_path):
    first = tls.load_or_create(tmp_path)
    _cert, other_key = tls._make()
    (tmp_path / tls.KEY_FILE).write_bytes(other_key)
    fresh = tls.load_or_create(tmp_path)
    assert fresh.fingerprint != first.fingerprint


def test_the_identity_is_made_again_when_it_is_near_its_end(tmp_path, monkeypatch):
    monkeypatch.setattr(tls, "VALID_DAYS", 1)  # ends inside the renewal window, so the next start makes a new one
    short = tls.load_or_create(tmp_path)
    monkeypatch.undo()
    renewed = tls.load_or_create(tmp_path)
    assert renewed.fingerprint != short.fingerprint


# ----- meeting over TLS -------------------------------------------------------------------------------


def test_a_pinned_mailbox_meets_and_passes_messages_over_wss(identity):
    async def scenario(server, url):
        pinned = f"{url}#{identity.fingerprint}"
        a = MailboxClient(pinned, new_side(), timeout=5)
        b = MailboxClient(pinned, new_side(), timeout=5)
        await a.connect()
        await b.connect()
        assert a.url == url
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await b.open()
        await a.add("pake", b"over tls")
        got = await b.receive(2)
        assert got.body == b"over tls"
        await a.disconnect()
        await b.disconnect()

    run(scenario, tls=tls.server_context(identity))


def test_a_pin_that_is_not_the_servers_is_refused_before_anything_is_sent(identity):
    async def scenario(server, url):
        other = tls.fingerprint_of(b"a certificate that is not this one")
        c = MailboxClient(f"{url}#{other}", new_side(), timeout=5, attempts=1)
        with pytest.raises(MailboxError, match="not the one pinned"):
            await c.connect()
        assert server.open_nameplates() == 0
        assert server.open_mailboxes() == 0

    run(scenario, tls=tls.server_context(identity))


def test_a_mailbox_with_no_pin_must_present_a_certificate_the_system_trusts(identity):
    async def scenario(server, url):
        c = MailboxClient(url, new_side(), timeout=5, attempts=1)
        with pytest.raises(MailboxError, match="certificate this computer accepts"):
            await c.connect()

    run(scenario, tls=tls.server_context(identity))


def test_the_mailbox_command_will_not_serve_the_network_without_tls(capsys):
    from transferd_gui.wormhole.server import main

    assert main(["--host", "0.0.0.0", "--port", "0"]) == 2
    assert "must be served over TLS" in capsys.readouterr().err


def test_plain_ws_to_another_computer_is_refused_without_connecting():
    c = MailboxClient([MailboxAddress("ws", "mailbox.example.net", 4470)], new_side(), timeout=1, attempts=1)
    with pytest.raises(MailboxError, match="reaches only this computer"):
        asyncio.run(c.connect())


# ----- which mailbox, and when --------------------------------------------------------------------------


def test_the_client_keeps_trying_until_a_mailbox_that_starts_late_answers():
    port = _free_port()

    async def main():
        server = MailboxServer()

        async def start_late():
            await asyncio.sleep(0.4)
            await server.start("127.0.0.1", port)

        starter = asyncio.ensure_future(start_late())
        c = MailboxClient(
            f"ws://127.0.0.1:{port}", new_side(), timeout=1, attempts=60, backoff=0.02, backoff_cap=0.1
        )
        try:
            await c.connect()
            assert await c.allocate() > 0
            await c.disconnect()
        finally:
            await starter
            await server.close()

    asyncio.run(main())


def test_a_mailbox_that_is_down_is_passed_over_for_the_next_one_in_the_list():
    async def scenario(server, url):
        dead = f"ws://127.0.0.1:{_free_port()}"
        c = MailboxClient(f"{dead}, {url}", new_side(), timeout=1, attempts=1)
        await c.connect()
        assert c.url == url
        await c.disconnect()

    run(scenario)


def test_after_connecting_a_client_stays_on_its_mailbox_and_does_not_move_to_another():
    async def scenario(server, url):
        second = MailboxServer()
        second_port = await second.start("127.0.0.1", 0)
        try:
            c = MailboxClient(
                [
                    MailboxAddress("ws", "127.0.0.1", _port_of(url), ""),
                    MailboxAddress("ws", "127.0.0.1", second_port, ""),
                ],
                new_side(),
                timeout=1,
                attempts=2,
                backoff=0.01,
            )
            await c.connect()
            assert c.url == url
            await server.close()  # the mailbox the meeting is on goes away
            with pytest.raises(MailboxError):
                await c.reconnect()
            assert c.url == url, "a reconnect never moves to another mailbox: the meeting is on this one"
        finally:
            await second.close()

    run(scenario)
