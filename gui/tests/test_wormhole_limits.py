"""The mailbox's limits and refusals, against the real server on 127.0.0.1: every refusal has a code, requests are rate-limited per
address and say when to retry, connections and nameplates are capped, and a connection that never binds is closed. Each test
runs its own server, the same as `test_wormhole_mailbox.py`."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from transferd_gui.wormhole.errors import MailboxError
from transferd_gui.wormhole.keys import new_side
from transferd_gui.wormhole.mailbox import MailboxClient
from transferd_gui.wormhole.server import APP_ID, PROTOCOL
from test_wormhole_mailbox import client, next_of, raw_session, run


def test_every_refusal_carries_a_machine_readable_code():
    async def scenario(server, url):
        ws, _welcome = await raw_session(url)
        await ws.send(json.dumps({"type": "allocate"}))
        assert (await next_of(ws, "error"))["code"] == "not_bound"
        await ws.send(json.dumps({"type": "bind", "appid": "someone-else/9", "side": new_side()}))
        assert (await next_of(ws, "error"))["code"] == "wrong_app"
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": "not-a-side"}))
        assert (await next_of(ws, "error"))["code"] == "bad_side"
        side = new_side()
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": side}))
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": side}))
        assert (await next_of(ws, "error"))["code"] == "already_bound"
        await ws.send(json.dumps({"type": "frobnicate"}))
        assert (await next_of(ws, "error"))["code"] == "unknown_message"
        await ws.send("not json at all")
        assert (await next_of(ws, "error"))["code"] == "bad_request"
        await ws.send(json.dumps({"type": "claim", "nameplate": "x"}))
        assert (await next_of(ws, "error"))["code"] == "bad_nameplate"
        await ws.send(json.dumps({"type": "open", "mailbox": "nope"}))
        assert (await next_of(ws, "error"))["code"] == "no_mailbox"
        await ws.send(json.dumps({"type": "allocate"}))
        nameplate = json.loads(await ws.recv())["nameplate"]
        await ws.send(json.dumps({"type": "claim", "nameplate": nameplate}))
        mailbox = (await next_of(ws, "claimed"))["mailbox"]
        await ws.send(json.dumps({"type": "add", "mailbox": mailbox, "id": "zz", "phase": "pake", "body": "00"}))
        assert (await next_of(ws, "error"))["code"] == "bad_id"
        await ws.send(json.dumps({"type": "add", "mailbox": mailbox, "id": "beef", "phase": "PAKE", "body": "00"}))
        assert (await next_of(ws, "error"))["code"] == "bad_phase"
        await ws.send(json.dumps({"type": "add", "mailbox": mailbox, "id": "beef", "phase": "pake", "body": "zz"}))
        assert (await next_of(ws, "error"))["code"] == "bad_body"
        await ws.send(json.dumps({"type": "add", "mailbox": mailbox, "id": "beef", "phase": "pake", "body": "ab" * 65537}))
        error = await next_of(ws, "error")
        assert error["code"] == "too_big" and error["id"] == "beef"
        await ws.close()

    run(scenario)


def test_the_welcome_names_the_protocol_and_the_limits():
    async def scenario(server, url):
        ws, welcome = await raw_session(url)
        assert welcome["protocol"] == PROTOCOL
        assert welcome["limits"]["bind_timeout_s"] > 0
        assert welcome["limits"]["max_message_bytes"] == 65536
        await ws.close()

    run(scenario)


def test_allocation_past_the_address_rate_is_refused_with_the_time_to_wait():
    async def scenario(server, url):
        ws, _welcome = await raw_session(url)
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": new_side()}))
        await ws.send(json.dumps({"type": "allocate"}))
        assert (await next_of(ws, "allocated"))["nameplate"].isdigit()
        await ws.send(json.dumps({"type": "allocate"}))
        refused = await next_of(ws, "error")
        assert refused["code"] == "rate_limited"
        assert refused["orig"] == "allocate"
        assert 0 < refused["retry_after"] <= 1.0
        await asyncio.sleep(refused["retry_after"] + 0.05)
        await ws.send(json.dumps({"type": "allocate"}))
        assert (await next_of(ws, "allocated"))["nameplate"].isdigit()
        await ws.close()

    run(scenario, rates={"allocate": (1.0, 20.0)})


def test_a_client_waits_out_a_rate_limit_and_carries_on():
    async def scenario(server, url):
        c = await client(url)
        first = await c.allocate()
        second = await c.allocate()  # refused once, then sent after the time the mailbox gave
        assert first != second
        await c.disconnect()

    run(scenario, rates={"allocate": (1.0, 20.0)})


def test_an_add_past_its_rate_is_sent_again_and_is_not_lost():
    async def scenario(server, url):
        a, b = await client(url), await client(url)
        nameplate = await a.allocate()
        await a.claim(nameplate)
        await b.claim(nameplate)
        await a.open()
        await b.open()
        await a.add("pake", b"one")
        await a.add("version", b"two")  # the second add is refused once, then sent again when allowed
        assert (await b.receive(2)).body == b"one"
        assert (await b.receive(2)).body == b"two"
        await a.disconnect()
        await b.disconnect()

    run(scenario, rates={"add": (1.0, 20.0)})


def test_a_connection_beyond_the_address_limit_is_refused_as_busy():
    async def scenario(server, url):
        held = [await connect(url) for _ in range(2)]
        for ws in held:
            await ws.recv()  # the welcome
        third = await connect(url)
        refusal = json.loads(await third.recv())
        assert refusal["type"] == "error" and refusal["code"] == "busy"
        assert refusal["retry_after"] > 0
        with pytest.raises(ConnectionClosed):
            await third.recv()
        for ws in held:
            await ws.close()

    run(scenario, max_connections_per_address=2)


def test_a_client_refused_as_busy_says_so_rather_than_failing_silently():
    async def scenario(server, url):
        held = [await connect(url) for _ in range(2)]
        for ws in held:
            await ws.recv()
        c = MailboxClient(url, new_side(), timeout=2.0, attempts=1)
        with pytest.raises(MailboxError) as refused:
            await c.connect()
        assert refused.value.code == "busy"
        for ws in held:
            await ws.close()

    run(scenario, max_connections_per_address=2)


def test_nameplates_are_capped_and_the_refusal_is_busy():
    async def scenario(server, url):
        ws, _welcome = await raw_session(url)
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": new_side()}))
        await ws.send(json.dumps({"type": "allocate"}))
        await next_of(ws, "allocated")
        await ws.send(json.dumps({"type": "allocate"}))
        await next_of(ws, "allocated")
        await ws.send(json.dumps({"type": "allocate"}))
        refused = await next_of(ws, "error")
        assert refused["code"] == "busy"
        assert server.open_nameplates() == 2
        await ws.close()

    run(scenario, max_nameplates=2)


def test_a_connection_that_never_binds_is_closed():
    async def scenario(server, url):
        ws, _welcome = await raw_session(url)
        await asyncio.sleep(0.6)
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert server.open_connections() == 0

    run(scenario, bind_timeout=0.2)


def test_a_bound_connection_is_not_closed_by_the_bind_timeout():
    async def scenario(server, url):
        ws, _welcome = await raw_session(url)
        await ws.send(json.dumps({"type": "bind", "appid": APP_ID, "side": new_side()}))
        await asyncio.sleep(0.6)
        await ws.send(json.dumps({"type": "allocate"}))
        assert (await next_of(ws, "allocated"))["nameplate"].isdigit()
        await ws.close()

    run(scenario, bind_timeout=0.2)


def test_a_bad_rate_is_refused_when_the_server_is_made():
    from transferd_gui.wormhole.server import MailboxServer

    with pytest.raises(ValueError):
        MailboxServer(rates={"allocate": (0.0, 1.0)})
    with pytest.raises(ValueError):
        MailboxServer(rates={"allocate": (1.0, 0.0)})
