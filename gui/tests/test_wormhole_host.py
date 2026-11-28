"""The mailbox list and the hosted mailbox: the settings that hold them, the hosted mailbox's start, stop and status, what it
serves on this computer and on the network, and a wormhole on this computer that meets on its own mailbox when no list is set.

Hosting binds to 127.0.0.1 in these tests. The app binds to all interfaces when the network setting is on; the test injects the
address so that nothing listens beyond this computer."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from support import free_port, make_settings
from transferd_gui.backend.mailbox_host import MailboxHost
from transferd_gui.backend.wormhole import NO_MAILBOX, WormholeService
from transferd_gui.model.settings import SettingsError, validate
from transferd_gui.wormhole.errors import WormholeError
from transferd_gui.wormhole.keys import new_side
from transferd_gui.wormhole.mailbox import MailboxClient

PIN = "9f2c" * 16


def base(tmp_path: Path, **changes) -> dict:
    data = tmp_path / "data"
    return {"data_dir": str(data), "download_dir": str(data / "received"), **changes}


class FakeService:
    """The part of the app service that the wormhole service reads."""

    def __init__(self, settings, host: MailboxHost) -> None:
        self.settings = settings
        self.mailbox_host = host


async def settle(*tasks) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ----- settings --------------------------------------------------------------------------------------


def test_the_hosting_defaults_are_off_and_the_standard_port(tmp_path):
    settings = validate(base(tmp_path))
    assert settings.wormhole_host is False
    assert settings.wormhole_host_network is False
    assert settings.wormhole_host_port == 4470
    assert settings.wormhole_mailbox == ""


def test_a_list_of_mailboxes_with_pins_is_accepted(tmp_path):
    settings = validate(base(tmp_path, wormhole_mailbox=f"wss://a.example.net:4470#{PIN}, ws://127.0.0.1:4470"))
    assert settings.wormhole_mailbox.startswith("wss://a.example.net")


@pytest.mark.parametrize(
    "changes, problem",
    [
        ({"wormhole_mailbox": "ws://a.example.net:4470"}, "reaches only this computer"),
        ({"wormhole_mailbox": "wss://a.example.net:4470,,wss://b.example.net:4470"}, "entry 2"),
        ({"wormhole_mailbox": f"wss://a.example.net:4470#{'zz' * 32}"}, "64 hexadecimal"),
        ({"wormhole_mailbox": "wss://a.example.net:4470 " + "x" * 10}, "spaces"),
        ({"wormhole_mailbox": "x" * 700}, "longer than 600"),
        ({"wormhole_host_port": 0}, "wormhole_host_port must be between 1 and 65535"),
        ({"wormhole_host": "yes"}, "wormhole_host must be on or off"),
        ({"wormhole_host_network": 1}, "wormhole_host_network must be on or off"),
    ],
)
def test_bad_mailbox_and_hosting_settings_are_named_and_refused(tmp_path, changes, problem):
    with pytest.raises(SettingsError) as refused:
        validate(base(tmp_path, **changes))
    assert any(problem in message for message in refused.value.problems)


def test_a_wormhole_with_no_list_and_no_hosting_is_told_to_set_one(tmp_path):
    settings = make_settings(tmp_path)
    service = WormholeService(FakeService(settings, MailboxHost(tmp_path / "wormhole")))
    assert service.mailbox() == ""
    assert service.problem() == NO_MAILBOX
    with pytest.raises(WormholeError, match="host one on this computer"):
        asyncio.run(service.start_send("text", "hello"))


def test_a_listed_mailbox_is_used_in_preference_to_the_hosted_one(tmp_path):
    settings = make_settings(tmp_path, wormhole_mailbox="ws://127.0.0.1:9999")
    service = WormholeService(FakeService(settings, MailboxHost(tmp_path / "wormhole")))
    assert service.mailbox() == "ws://127.0.0.1:9999"
    assert service.problem() == ""


# ----- the hosted mailbox ----------------------------------------------------------------------------


def test_hosting_off_reports_nothing_and_offers_no_address(tmp_path):
    async def main():
        host = MailboxHost(tmp_path / "wormhole", network_bind="127.0.0.1")
        await host.apply(make_settings(tmp_path, wormhole_host=False))
        status = host.status()
        assert not status.enabled and not status.running
        assert host.address_here() == ""
        assert host.share_line() == ""

    asyncio.run(main())


def test_hosting_on_this_computer_listens_on_loopback_without_tls(tmp_path):
    port = free_port()

    async def main():
        host = MailboxHost(tmp_path / "wormhole", network_bind="127.0.0.1")
        await host.apply(make_settings(tmp_path, wormhole_host=True, wormhole_host_port=port))
        status = host.status()
        assert status.running and status.port == port and not status.network and status.fingerprint == ""
        assert host.address_here() == f"ws://127.0.0.1:{port}"
        assert host.share_line() == "", "a mailbox on this computer only has no share line"
        c = MailboxClient(host.address_here(), new_side(), timeout=3)
        await c.connect()
        assert await c.allocate() > 0
        await c.disconnect()
        await host.stop()
        assert not host.status().running

    asyncio.run(main())


def test_hosting_on_the_network_serves_wss_pinned_to_its_own_certificate(tmp_path):
    port = free_port()

    async def main():
        host = MailboxHost(tmp_path / "wormhole", network_bind="127.0.0.1")
        await host.apply(make_settings(tmp_path, wormhole_host=True, wormhole_host_port=port, wormhole_host_network=True))
        status = host.status()
        assert status.running and status.network
        assert len(status.fingerprint) == 64
        assert host.address_here() == f"wss://127.0.0.1:{port}#{status.fingerprint}"
        assert status.share_line("192.168.1.20") == f"wss://192.168.1.20:{port}#{status.fingerprint}"
        assert status.share_line("") == ""
        c = MailboxClient(host.address_here(), new_side(), timeout=3, attempts=1)
        await c.connect()
        assert await c.allocate() > 0
        await c.disconnect()
        await host.stop()

    asyncio.run(main())


def test_the_identity_of_a_network_mailbox_is_kept_across_restarts(tmp_path):
    port = free_port()

    async def main():
        settings = make_settings(tmp_path, wormhole_host=True, wormhole_host_port=port, wormhole_host_network=True)
        host = MailboxHost(tmp_path / "wormhole", network_bind="127.0.0.1")
        await host.apply(settings)
        first = host.status().fingerprint
        await host.stop()
        await host.apply(settings)
        assert host.status().fingerprint == first, "the other computers' pins stay valid across a restart"
        await host.stop()

    asyncio.run(main())


def test_a_port_already_in_use_is_reported_and_nothing_is_raised(tmp_path):
    port = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", port))
    blocker.listen()
    try:

        async def main():
            host = MailboxHost(tmp_path / "wormhole", network_bind="127.0.0.1")
            await host.apply(make_settings(tmp_path, wormhole_host=True, wormhole_host_port=port))
            status = host.status()
            assert not status.running
            assert f"could not listen on port {port}" in status.error

        asyncio.run(main())
    finally:
        blocker.close()


def test_a_certificate_that_cannot_be_written_is_reported(tmp_path):
    folder_is_a_file = tmp_path / "not-a-folder"
    folder_is_a_file.write_text("in the way")

    async def main():
        host = MailboxHost(folder_is_a_file, network_bind="127.0.0.1")
        await host.apply(make_settings(tmp_path, wormhole_host=True, wormhole_host_port=free_port(), wormhole_host_network=True))
        status = host.status()
        assert not status.running
        assert "could not make the mailbox's certificate" in status.error

    asyncio.run(main())


def test_applying_the_same_settings_again_keeps_the_running_server(tmp_path):
    port = free_port()

    async def main():
        settings = make_settings(tmp_path, wormhole_host=True, wormhole_host_port=port)
        host = MailboxHost(tmp_path / "wormhole", network_bind="127.0.0.1")
        await host.apply(settings)
        first = host._server
        await host.apply(settings)
        assert host._server is first
        await host.stop()

    asyncio.run(main())


def test_changing_the_port_moves_the_mailbox_and_turning_it_off_stops_it(tmp_path):
    first_port, second_port = free_port(), free_port()

    async def main():
        host = MailboxHost(tmp_path / "wormhole", network_bind="127.0.0.1")
        await host.apply(make_settings(tmp_path, wormhole_host=True, wormhole_host_port=first_port))
        assert host.status().port == first_port
        await host.apply(make_settings(tmp_path, wormhole_host=True, wormhole_host_port=second_port))
        assert host.status().port == second_port
        await host.apply(make_settings(tmp_path, wormhole_host=False, wormhole_host_port=second_port))
        assert not host.status().running and host.address_here() == ""
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", second_port), timeout=0.5)  # nothing listens any more

    asyncio.run(main())


# ----- a wormhole on this computer, on its own mailbox ----------------------------------------------


def test_a_wormhole_with_no_list_meets_on_this_computers_own_mailbox(tmp_path):
    port = free_port()

    async def main():
        settings = make_settings(tmp_path, wormhole_host=True, wormhole_host_port=port)
        host = MailboxHost(tmp_path / "wormhole", network_bind="127.0.0.1")
        await host.apply(settings)
        service = WormholeService(FakeService(settings, host))
        assert service.mailbox() == f"ws://127.0.0.1:{port}"
        assert service.problem() == ""
        sender = await service.start_send("text", "meeting on the hosted mailbox")
        sent = asyncio.ensure_future(service.run_send(sender))
        try:
            receiver = await service.open_receive(str(sender.code))
            received = await service.finish_receive(receiver, True)
            assert received.kind == "text"
            assert received.text == "meeting on the hosted mailbox"
            assert (await sent).ok
        finally:
            await settle(sent)
            await host.stop()

    asyncio.run(main())
