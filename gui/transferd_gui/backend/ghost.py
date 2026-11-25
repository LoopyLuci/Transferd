"""Ghost keys: this node's ghost key, its pending request, its certificate, and the issuer it was issued by.

The library's `ghostkey` module may be missing or changing in some builds. `load` imports the names this module needs, and
when that fails every operation refuses with the reason, which the Ghost keys screen shows. Nothing else is stubbed.

The operations are synchronous and small (a few RSA operations at most), so the UI calls them directly.

Files, in `<data_dir>/ghost/`:
    key.txt          the ghost key's 32-byte secret, in hex. Keep it private: it is the key.
    issuer.txt       the issuer's public key text, as the issuer publishes it.
    pending.txt      the request that is waiting for the issuer's answer (the library's own text, which holds secrets).
    certificate.txt  the finished certificate, as the library writes it.

Issuing (signing a blinded request) is the issuer's job and is not in this GUI. A certificate is requested here and
finished here, from the issuer's answer.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

GHOST_DIR = "ghost"
KEY_FILE = "key.txt"
ISSUER_FILE = "issuer.txt"
PENDING_FILE = "pending.txt"
CERTIFICATE_FILE = "certificate.txt"
REQUIRED_NAMES = ("GhostKey", "BlindState", "GhostCertificate", "IssuerPublic", "GhostKeyError")


class GhostUnavailable(RuntimeError):
    """The ghost key module cannot be used in this build. The message says why."""


class GhostError(ValueError):
    """A ghost key operation that the user needs to fix, such as a missing issuer or a bad answer."""


@dataclass(frozen=True)
class CertificateStatus:
    issuer_id: str
    tier: str
    valid: bool
    detail: str


@dataclass(frozen=True)
class GhostStatus:
    available: bool
    reason: str
    has_key: bool
    public_key: str
    issuer_name: str
    pending: bool
    certificate: Optional[CertificateStatus]


def load() -> Any:
    """The ghost key module, or GhostUnavailable with the reason it cannot be used."""
    try:
        module = importlib.import_module("transferd.ghostkey")
    except Exception as exc:  # any failure to import is a reason to report, not a crash
        raise GhostUnavailable(f"the transferd.ghostkey module does not import: {exc}") from exc
    missing = [name for name in REQUIRED_NAMES if not hasattr(module, name)]
    if missing:
        raise GhostUnavailable(f"transferd.ghostkey has no {', '.join(missing)} in this build")
    return module


class GhostService:
    """Reads and writes the ghost key files in one data directory."""

    def __init__(self, data_dir: Path) -> None:
        self._folder = Path(data_dir) / GHOST_DIR

    @property
    def folder(self) -> Path:
        return self._folder

    def _path(self, name: str) -> Path:
        return self._folder / name

    def status(self) -> GhostStatus:
        try:
            module = load()
        except GhostUnavailable as exc:
            return GhostStatus(False, str(exc), False, "", "", False, None)
        issuer = self._read_issuer(module)
        return GhostStatus(
            available=True,
            reason="",
            has_key=self._path(KEY_FILE).exists(),
            public_key=self._public_hex(module) or "",
            issuer_name=issuer.name if issuer is not None else "",
            pending=self._path(PENDING_FILE).exists(),
            certificate=self._certificate_status(module, issuer),
        )

    def create_key(self) -> str:
        """Make this node's ghost key and keep its secret in the key file. Refuses to replace an existing key."""
        module = load()
        if self._path(KEY_FILE).exists():
            raise GhostError("a ghost key already exists; reset the ghost key files first to make another")
        key = module.GhostKey.generate()
        self._write(KEY_FILE, key.secret().hex() + "\n")
        return key.public().hex()

    def save_issuer(self, text: str) -> str:
        """Keep an issuer's public key text. Returns the issuer's name."""
        module = load()
        try:
            issuer = module.IssuerPublic.from_text(text.strip())
        except Exception as exc:
            raise GhostError(f"that is not an issuer public key: {exc}") from exc
        self._write(ISSUER_FILE, issuer.to_text() + "\n")
        return issuer.name

    def request_certificate(self) -> str:
        """Blind this node's ghost key for the loaded issuer. Returns the blinded request in hex, which the user sends to the
        issuer. The matching state is kept in the pending file."""
        module = load()
        key = self._load_key(module)
        issuer = self._read_issuer(module)
        if issuer is None:
            raise GhostError("load the issuer's public key first")
        if self._path(PENDING_FILE).exists():
            raise GhostError("a request is already waiting for the issuer's answer; finish it or reset the ghost key files")
        blinded, state = key.blind_request(issuer)
        self._write(PENDING_FILE, state.to_text(key) + "\n")
        return blinded.hex()

    def finish_certificate(self, answer_hex: str) -> CertificateStatus:
        """Unblind the issuer's answer into a certificate, check it, and keep it. The pending request is then cleared."""
        module = load()
        issuer = self._read_issuer(module)
        if issuer is None:
            raise GhostError("load the issuer's public key first")
        pending_path = self._path(PENDING_FILE)
        if not pending_path.exists():
            raise GhostError("there is no pending request to finish")
        try:
            state, key = module.BlindState.from_text(pending_path.read_text(encoding="ascii").strip())
            answer = bytes.fromhex(answer_hex.strip())
            certificate = key.finish(issuer, state, answer)
        except Exception as exc:
            raise GhostError(f"the issuer's answer does not finish the request: {exc}") from exc
        self._write(CERTIFICATE_FILE, certificate.to_text() + "\n")
        pending_path.unlink()
        return self._certificate_status(module, issuer)  # type: ignore[return-value]

    def reset(self) -> None:
        """Delete the ghost key files. Only the user can ask for this, and the UI asks first."""
        for name in (KEY_FILE, ISSUER_FILE, PENDING_FILE, CERTIFICATE_FILE):
            self._path(name).unlink(missing_ok=True)

    # ----- helpers ------------------------------------------------------------------------------

    def _write(self, name: str, text: str) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        self._path(name).write_text(text, encoding="ascii")

    def _load_key(self, module: Any) -> Any:
        path = self._path(KEY_FILE)
        if not path.exists():
            raise GhostError("create a ghost key first")
        try:
            return module.GhostKey(bytes.fromhex(path.read_text(encoding="ascii").strip()))
        except Exception as exc:
            raise GhostError(f"the ghost key file is not readable: {exc}") from exc

    def _public_hex(self, module: Any) -> Optional[str]:
        path = self._path(KEY_FILE)
        if not path.exists():
            return None
        try:
            return module.GhostKey(bytes.fromhex(path.read_text(encoding="ascii").strip())).public().hex()
        except Exception:
            return None

    def _read_issuer(self, module: Any) -> Any:
        path = self._path(ISSUER_FILE)
        if not path.exists():
            return None
        try:
            return module.IssuerPublic.from_text(path.read_text(encoding="ascii").strip())
        except Exception:
            return None

    def _certificate_status(self, module: Any, issuer: Any) -> Optional[CertificateStatus]:
        path = self._path(CERTIFICATE_FILE)
        if not path.exists():
            return None
        try:
            certificate = module.GhostCertificate.from_text(path.read_text(encoding="ascii").strip())
        except Exception as exc:
            return CertificateStatus("", "", False, f"the certificate file does not read: {exc}")
        if issuer is None:
            return CertificateStatus(certificate.issuer_id, certificate.tier, False, "load the issuer's public key to check it")
        try:
            certificate.verify(issuer)
        except Exception as exc:
            return CertificateStatus(certificate.issuer_id, certificate.tier, False, f"does not verify against the loaded issuer: {exc}")
        return CertificateStatus(certificate.issuer_id, certificate.tier, True, "verifies against the loaded issuer")
