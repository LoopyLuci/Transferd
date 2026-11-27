"""The USB route: Android's `adb` carries a TCP connection over the cable with `adb forward`.

The bridge makes no protocol of its own. `adb forward tcp:L tcp:P` makes the device's Transferd listener (port P, on the
device) reachable at 127.0.0.1:L on the computer. The node dials that address with the ordinary handshake and adds it to
the device's session as a route. So a USB route is a loopback TCP route, and everything above it is unchanged.

Every failure of `adb` becomes an `AdbError` with a sentence a user can act on.
"""

from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from typing import List, Sequence, Union

#: How long one `adb` command may take. `adb` talks to its server, which normally answers at once.
ADB_TIMEOUT_S = 15.0

#: The words adb uses for a device's state, and what the user should do about each one that is not `device`.
STATE_HELP = {
    "device": "ready",
    "unauthorized": "allow USB debugging on the phone (accept the prompt that appears there)",
    "offline": "the phone is offline; unplug it, plug it in again, and accept the prompt if one appears",
    "bootloader": "the phone is in the bootloader; start Android to debug it",
    "recovery": "the phone is in recovery mode; start Android to debug it",
    "sideload": "the phone is in sideload mode; start Android to debug it",
}


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def help(self) -> str:
        return STATE_HELP.get(self.state, f"adb reports the state '{self.state}'")


class AdbError(Exception):
    """`adb` is missing, failed, or timed out. The message says what to do."""


def parse_devices(text: str) -> List[AdbDevice]:
    """The devices in `adb devices -l` output. Header and blank lines are skipped; the model comes from `model:<name>`."""
    devices: List[AdbDevice] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")
        devices.append(AdbDevice(serial=parts[0], state=parts[1], model=model.replace("_", " ")))
    return devices


def free_local_port() -> int:
    """A loopback port that is free now. The forward binds it a moment later; the caller retries if that fails."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Adb:
    """The `adb` program, run as a subprocess. `program` is the path or the command, as a string or a list."""

    def __init__(self, program: Union[str, Sequence[str]] = "adb") -> None:
        self._program = [program] if isinstance(program, str) else list(program)

    def _run(self, *args: str) -> str:
        try:
            done = subprocess.run(
                [*self._program, *args],
                capture_output=True,
                text=True,
                timeout=ADB_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(
                f"adb was not found at '{self._program[0]}'. Install Android platform-tools, or set the adb path in Settings."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError("adb did not answer in time; its server may be stuck (try 'adb kill-server')") from exc
        except OSError as exc:
            raise AdbError(f"adb could not be started: {exc.strerror or exc}") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            raise AdbError(detail[-1] if detail else f"adb exited with code {done.returncode}")
        return done.stdout

    def devices(self) -> List[AdbDevice]:
        return parse_devices(self._run("devices", "-l"))

    def forward(self, serial: str, local_port: int, device_port: int) -> None:
        self._run("-s", serial, "forward", f"tcp:{local_port}", f"tcp:{device_port}")

    def remove_forward(self, local_port: int) -> None:
        self._run("forward", "--remove", f"tcp:{local_port}")

    def version(self) -> str:
        out = self._run("version").strip()
        return out.splitlines()[0] if out else ""
