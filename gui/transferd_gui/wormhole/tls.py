"""TLS for the mailbox: the certificate a mailbox makes for itself, and the pin a client holds it to.

A mailbox on the network serves wss://. Its certificate is made once, on the computer that hosts the mailbox, and kept in the
wormhole folder. Its SHA-256 fingerprint is the pin. A client given that fingerprint (copied from the hosting computer into
Settings) accepts that certificate and no other. A mailbox run by someone else with a certificate from a public authority
needs no pin: the system's certificates are checked as usual.

The pin does not depend on names. The TLS handshake still proves that the server holds the private key of the pinned
certificate, and the fingerprint is compared before anything is sent. Message content is sealed by the two clients whatever the
transport does. So TLS here keeps the nameplates, timing and sizes from the network, and keeps a stranger from standing in
for the mailbox.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

CERT_FILE = "mailbox-cert.pem"
KEY_FILE = "mailbox-key.pem"
VALID_DAYS = 3650
#: A certificate this close to its end is replaced the next time the mailbox starts.
RENEW_BEFORE_DAYS = 30
COMMON_NAME = "Transferd wormhole mailbox"
MIN_TLS = ssl.TLSVersion.TLSv1_2


@dataclass(frozen=True)
class Identity:
    """The mailbox's certificate and key, on disk, and the fingerprint of the certificate."""

    cert_path: Path
    key_path: Path
    fingerprint: str  # lower-case hex SHA-256 of the DER certificate


def fingerprint_of(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def load_or_create(directory: Path) -> Identity:
    """The identity in `directory`. Made on first use, and again when the files are missing, do not match, or are near expiry."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / CERT_FILE
    key_path = directory / KEY_FILE
    if not _usable(cert_path, key_path):
        cert_pem, key_pem = _make()
        # The key is written first. A crash between the two writes leaves a pair that does not match, which is made again.
        _write_private(key_path, key_pem)
        _write_atomic(cert_path, cert_pem)
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
    return Identity(cert_path, key_path, fingerprint_of(der))


def server_context(identity: Identity) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = MIN_TLS
    context.load_cert_chain(str(identity.cert_path), str(identity.key_path))
    return context


def pinned_context() -> ssl.SSLContext:
    """For a mailbox held to a pin. The chain is not checked, since a self-made certificate has no chain to check. The client
    compares the fingerprint after the handshake and before it sends anything; see `peer_fingerprint`."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = MIN_TLS
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def system_context() -> ssl.SSLContext:
    """For a mailbox with no pin: the system's certificates, and the host name, are checked."""
    context = ssl.create_default_context()
    context.minimum_version = MIN_TLS
    return context


def peer_fingerprint(ws) -> str:
    """The fingerprint of the certificate a connected websocket's server presented, or "" if there is none (a plain connection)."""
    transport = getattr(ws, "transport", None)
    ssl_object = transport.get_extra_info("ssl_object") if transport is not None else None
    if ssl_object is None:
        return ""
    der = ssl_object.getpeercert(binary_form=True)
    return fingerprint_of(der) if der else ""


# ----- making and keeping the identity -------------------------------------------------------------------


def _usable(cert_path: Path, key_path: Path) -> bool:
    if not (cert_path.is_file() and key_path.is_file()):
        return False
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (ValueError, TypeError, OSError):
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    if cert.not_valid_after_utc - now < datetime.timedelta(days=RENEW_BEFORE_DAYS):
        return False
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    der = serialization.Encoding.DER
    return cert.public_key().public_bytes(der, spki) == key.public_key().public_bytes(der, spki)


def _make() -> "tuple[bytes, bytes]":
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert_pem, key_pem


def _write_private(path: Path, data: bytes) -> None:
    """Write a secret file: owner-only where the platform allows it. On Windows the folder's own permissions apply."""
    _write_atomic(path, data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
