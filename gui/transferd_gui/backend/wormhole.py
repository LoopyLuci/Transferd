"""The wormhole as the app runs it: what is offered, the settings it uses, and the calls the screen makes.

Every method here is a coroutine. The screen runs them through the service (`TransferdService.run`) so they execute on the
backend loop, the same as the node's actions. Nothing here touches Qt.

Where a wormhole meets: the mailbox list in Settings (`wormhole_mailbox`), tried in order. When no list is set, a wormhole
meets on this computer's own hosted mailbox, if hosting is on (`backend.mailbox_host`). The relay and the download folder come
from Settings too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Union

from transferd_gui.backend.service import TransferdService
from transferd_gui.model.settings import Settings
from transferd_gui.wormhole import protocol as P
from transferd_gui.wormhole.codes import parse as parse_code
from transferd_gui.wormhole.endpoint import OverlayTransit
from transferd_gui.wormhole.errors import WormholeError
from transferd_gui.wormhole.session import ReceiveResult, Receiver, SendResult, Sender
from transferd_gui.wormhole.transfer import prepare_file, prepare_folder, remove_quietly

OnState = Callable[[str], None]
NO_MAILBOX = "Set a mailbox in Settings (Wormhole), or host one on this computer, before using a wormhole."


def mailbox_problem(settings: Settings, hosted: str = "") -> str:
    """Why a wormhole cannot start, or the empty string when it can. `hosted` is this computer's own mailbox address, or ""."""
    if not settings.wormhole_mailbox.strip() and not hosted:
        return NO_MAILBOX
    return ""


class WormholeService:
    """Builds and runs one wormhole attempt at a time, from the settings the app holds."""

    def __init__(self, service: TransferdService) -> None:
        self._service = service

    def _settings(self) -> Settings:
        return self._service.settings

    def scratch_dir(self) -> Path:
        """Where a folder is zipped before it is offered, and where a received folder is staged. Removed after use."""
        path = Path(self._settings().data_dir) / "wormhole"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def mailbox(self) -> str:
        """The mailbox list a wormhole meets on: the one in Settings, or this computer's own when Settings names none."""
        return self._settings().wormhole_mailbox.strip() or self._service.mailbox_host.address_here()

    def problem(self) -> str:
        """Why a wormhole cannot start now, or the empty string."""
        return mailbox_problem(self._settings(), self._service.mailbox_host.address_here())

    def _transit(self, on_state: Optional[OnState]) -> OverlayTransit:
        settings = self._settings()
        return OverlayTransit(
            name=settings.node_name or "Transferd",
            relay=settings.relay_address,
            streams=settings.wormhole_streams,
            on_state=on_state,
        )

    async def start_send(self, kind: str, value: str, on_state: Optional[OnState] = None) -> Sender:
        """Prepare what is offered and reserve a code. Returns the sender, whose `code` the screen shows."""
        problem = self.problem()
        if problem:
            raise WormholeError(problem)
        if kind == "text":
            offer, source = P.Offer.for_text(value), None
        elif kind == "file":
            offer, source = prepare_file(Path(value))
        elif kind == "folder":
            offer, source = prepare_folder(Path(value), self.scratch_dir())
        else:
            raise WormholeError("send a file, a folder or a text")
        sender = Sender(self.mailbox(), transit=self._transit(on_state), offer=offer, source=source, on_state=on_state)
        await sender.start()
        return sender

    async def run_send(self, sender: Sender) -> SendResult:
        """Carry the sender through to the end. A folder's archive is removed whatever the outcome."""
        try:
            return await sender.run()
        finally:
            if sender.offer.kind == "directory" and sender.source is not None:
                remove_quietly(sender.source)

    async def open_receive(self, code_text: str, on_state: Optional[OnState] = None) -> Receiver:
        """Read the code, join the mailbox, and read what is offered. The screen then asks the user to accept or decline."""
        problem = self.problem()
        if problem:
            raise WormholeError(problem)
        code = parse_code(code_text)
        receiver = Receiver(self.mailbox(), code, transit=self._transit(on_state), on_state=on_state)
        await receiver.connect()
        await receiver.offer()
        return receiver

    async def finish_receive(self, receiver: Receiver, accept: bool, reason: str = "") -> ReceiveResult:
        """Answer the offer and, when accepted, receive it into the download folder."""
        await receiver.answer(accept, reason)
        folder = Path(self._settings().download_dir)
        return await receiver.finish(folder)

    async def abandon(self, exchange: Union[Sender, Receiver]) -> None:
        """Stop an attempt that is waiting on the user: a sender's code stops working, a receiver declines its offer."""
        await exchange.abandon()
