"""The wormhole mailbox, against the real server on 127.0.0.1: meeting on a nameplate, replay, acknowledgements, reconnection,
the limits, and expiry. Each test runs its own server and its own event loop."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from transferd_gui.wormhole.errors import MailboxError
from transferd_gui.wormhole.keys import new_side
from transferd_gui.wormhole.mailbox import MailboxClient
from transferd_gui.wormhole.server import APP_ID, MailboxServer


def run(scenario, timeout: float = 20.0, *, tls=None, **server_kwargs):
    """Start a server, run `scenario(server, url)`, stop the server. Returns the scenario's result. With `tls`, the server
    speaks wss:// with that SSL context, and `url` is a wss:// address."""

    async def main():
        server = MailboxServer(**server_kwargs)
        port = await server.start("127.0.0.1", 0, tls=tls)
        scheme = "wss" if tls is not None else "ws"
        try:
            return await asyncio.wait_for(scenario(server, f"{scheme}://127.0.0.1:{port}"), timeout)
        finally:
            await server.close()

    return asyncio.run(main())


async def client(url: str, timeout: float = 5.0) -> MailboxClient:
    c = MailboxClient(url, new_side(), timeout=timeout)
    await c.connect()
    return c


async def raw_session(url: str):
    """A connection that speaks the wire protocol by hand, for what the client never sends."""
    ws = await connect(url)
    welcome = json.loads(await ws.recv())
    return ws, welcome


async def next_of(ws, kind: str):
    while True:
        message = json.loads(await ws.recv())
        if message.get("type") == kind:
            return message


# ----- meeting and exchanging ------------------------------------------------------------------------


def test_two_sides_meet_on_a_nameplate_and_exchange_messages_both_ways():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        assert await b.claim(nameplate) == await a.claim(nameplate)
        await a.open()
        await b.open()
        await a.add("pake", b"from a")
        got = await b.receive(2)
        assert (got.side, got.phase, got.body) == (a.side, "pake", b"from a")
        await b.add("pake", b"from b")
        back = await a.receive(2)
        assert (back.side, back.body) == (b.side, b"from b")
        await a.disconnect()
        await b.disconnect()

    run(scenario)


def test_a_sender_never_receives_its_own_message_back():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await b.open()
        await a.add("pake", b"mine")
        assert await a.receive(0.3) is None
        await a.disconnect()
        await b.disconnect()

    run(scenario)


def test_messages_sent_before_the_other_side_opens_are_replayed_in_order():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await a.add("pake", b"first")
        await a.add("version", b"second")
        await b.open()
        assert (await b.receive(2)).body == b"first"
        assert (await b.receive(2)).body == b"second"
        await a.disconnect()
        await b.disconnect()

    run(scenario)


# ----- acknowledgements and reconnection ------------------------------------------------------------


def test_an_acknowledged_message_is_not_replayed_after_a_reconnect():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await b.open()
        await a.add("offer", b"offer")
        got = await b.receive(2)
        await b.ack(got.message_id)
        await b.reconnect()
        assert await b.receive(0.4) is None, "an acknowledged message is not sent again"
        await a.disconnect()
        await b.disconnect()

    run(scenario)


def test_a_message_delivered_before_a_reconnect_is_not_delivered_twice():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await b.open()
        await a.add("offer", b"offer")
        first = await b.receive(2)
        await b.reconnect()  # the mailbox still holds it, since nothing acknowledged it
        assert await b.receive(0.4) is None, "the caller already has it, so the replay is dropped"
        await b.ack(first.message_id)
        await b.reconnect()
        assert await b.receive(0.4) is None
        await a.disconnect()
        await b.disconnect()

    run(scenario)


def test_a_message_sent_while_disconnected_is_kept_and_sent_after_reconnect():
    async def scenario(server, url):
        a = await client(url, timeout=0.5)
        b = await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await b.open()
        await a.disconnect()
        with pytest.raises(MailboxError):
            await a.add("version", b"sent while down")
        await a.reconnect()
        await a.flush()
        got = await b.receive(2)
        assert got.body == b"sent while down"
        await a.disconnect()
        await b.disconnect()

    run(scenario)


def test_an_acknowledgement_sent_while_disconnected_is_sent_after_reconnect():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await b.open()
        await a.add("offer", b"offer")
        got = await b.receive(2)
        await b.disconnect()
        await b.ack(got.message_id)  # kept, since the connection is down
        await b.reconnect()
        await b.open()
        assert await b.receive(0.4) is None
        await a.disconnect()
        await b.disconnect()

    run(scenario)


# ----- the nameplate: crowding, release, close -------------------------------------------------------


def test_a_third_computer_on_a_nameplate_is_refused_as_crowded():
    async def scenario(server, url):
        a, b, c = await client(url), await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        with pytest.raises(MailboxError, match="crowded"):
            await c.claim(nameplate)
        for x in (a, b, c):
            await x.disconnect()

    run(scenario)


def test_a_bad_bind_is_refused_and_the_connection_can_try_again():
    async def scenario(server, url):
        ws, _welcome = await raw_session(url)
        await ws.send(json.dumps({"type": "bind", "appid": "someone-else/9", "side": new_side()}))
        error = await next_of(ws, "error")
        assert "this mailbox serves" in error["error"]
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": new_side()}))
        await ws.send(json.dumps({"type": "allocate"}))
        assert (await next_of(ws, "allocated"))["nameplate"].isdigit()
        await ws.close()

    run(scenario)


def test_a_request_before_bind_and_an_unknown_request_are_refused():
    async def scenario(server, url):
        ws, _welcome = await raw_session(url)
        await ws.send(json.dumps({"type": "allocate"}))
        assert "bind before" in (await next_of(ws, "error"))["error"]
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": new_side()}))
        await ws.send(json.dumps({"type": "frobnicate"}))
        assert "unknown message" in (await next_of(ws, "error"))["error"]
        await ws.send("not json at all")
        assert "JSON object" in (await next_of(ws, "error"))["error"]
        await ws.close()

    run(scenario)


def test_a_message_over_the_size_limit_is_refused_by_the_client_and_by_the_server():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        with pytest.raises(MailboxError, match="at most 65536 bytes"):
            await a.add("pake", b"x" * 65537)
        ws, _welcome = await raw_session(url)
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": a.side}))
        await ws.send(json.dumps({"type": "open", "mailbox": a.mailbox_id}))
        await next_of(ws, "opened")
        await ws.send(json.dumps({"type": "add", "mailbox": a.mailbox_id, "id": "beef", "phase": "pake", "body": "ab" * 65537}))
        error = await next_of(ws, "error")
        assert error["orig"] == "add" and error["id"] == "beef"
        await ws.close()
        await a.disconnect()
        await b.disconnect()

    run(scenario)


def test_the_mailbox_stops_taking_messages_at_its_limit():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await a.add("pake", b"1")
        await a.add("version", b"2")
        with pytest.raises(MailboxError, match="full"):
            await a.add("identity", b"3")
        await a.disconnect()
        await b.disconnect()

    run(scenario, max_messages=2)


def test_a_retry_of_a_stored_message_is_confirmed_and_stored_once():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await b.open()
        ws, _welcome = await raw_session(url)
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": a.side}))
        await ws.send(json.dumps({"type": "open", "mailbox": a.mailbox_id}))
        await next_of(ws, "opened")
        payload = {"type": "add", "mailbox": a.mailbox_id, "id": "c0de", "phase": "pake", "body": "00"}
        await ws.send(json.dumps(payload))
        assert (await next_of(ws, "added"))["id"] == "c0de"
        await ws.send(json.dumps(payload))
        assert (await next_of(ws, "added"))["id"] == "c0de"
        assert (await b.receive(2)).message_id == "c0de"
        assert await b.receive(0.3) is None, "the retry is not stored, so it is not delivered again"
        await ws.close()
        await a.disconnect()
        await b.disconnect()

    run(scenario)


def test_releasing_the_only_side_removes_the_nameplate():
    async def scenario(server, url):
        a = await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        assert server.open_mailboxes() == 1
        await a.release()
        assert server.open_nameplates() == 0
        assert server.open_mailboxes() == 0
        await a.disconnect()

    run(scenario)


def test_both_sides_closing_removes_the_mailbox():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.close("happy")
        assert server.open_mailboxes() == 1, "one side closing leaves the mailbox for the other"
        await b.close("happy")
        assert server.open_mailboxes() == 0

    run(scenario)


# ----- expiry ----------------------------------------------------------------------------------------


def test_an_unclaimed_nameplate_expires_and_a_claimed_one_does_not():
    now = [0.0]

    async def scenario(server, url):
        a = await client(url)
        await a.allocate()
        b = await client(url)
        nameplate = await b.allocate()
        await b.claim(nameplate)
        now[0] = 301.0
        server.reap()
        assert server.open_nameplates() == 1, "the claimed one stays; the unclaimed one has gone"
        await a.disconnect()
        await b.disconnect()

    run(scenario, clock=lambda: now[0])


def test_an_idle_mailbox_expires():
    now = [0.0]

    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        assert server.open_mailboxes() == 1
        now[0] = 3601.0
        server.reap()
        assert server.open_mailboxes() == 0
        assert server.open_nameplates() == 0
        await a.disconnect()
        await b.disconnect()

    run(scenario, clock=lambda: now[0])


def test_nameplates_are_in_range_and_not_repeated():
    async def scenario(server, url):
        c = await client(url)
        numbers = [await c.allocate() for _ in range(20)]
        assert len(set(numbers)) == 20
        assert all(1 <= n <= 65535 for n in numbers)
        await c.disconnect()

    run(scenario)
