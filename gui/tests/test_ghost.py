"""Ghost keys through the library itself: create, request, finish with an issuer's answer, and check the certificate."""

from transferd.ghostkey import Issuer

from transferd_gui.backend import ghost as ghost_module
from transferd_gui.backend.ghost import GhostError, GhostService, GhostUnavailable


def test_full_certificate_flow_with_a_local_issuer(home):
    service = GhostService(home / "data")
    status = service.status()
    assert status.available and not status.has_key and status.certificate is None

    public = service.create_key()
    assert len(public) == 64  # the 32-byte Ed25519 public key, in hex
    issuer = Issuer.generate("test-issuer", "member", 2048)
    assert service.save_issuer(issuer.public.to_text()) == "test-issuer"

    blinded_hex = service.request_certificate()
    assert service.status().pending
    answer = issuer.sign_blinded(bytes.fromhex(blinded_hex)).hex()

    certificate = service.finish_certificate(answer)
    assert certificate.valid, certificate.detail
    assert not service.status().pending
    assert service.status().certificate.valid


def test_a_wrong_answer_is_refused_and_the_request_stays_pending(home):
    service = GhostService(home / "data")
    service.create_key()
    issuer = Issuer.generate("test-issuer", "member", 2048)
    service.save_issuer(issuer.public.to_text())
    service.request_certificate()
    try:
        service.finish_certificate("00" * 256)
    except GhostError as exc:
        assert "does not finish" in str(exc)
    else:
        raise AssertionError("a wrong answer must not finish a certificate")
    assert service.status().pending


def test_a_second_key_is_refused_until_reset(home):
    service = GhostService(home / "data")
    service.create_key()
    try:
        service.create_key()
    except GhostError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("a second key must not silently replace the first")
    service.reset()
    assert not service.status().has_key


def test_a_request_needs_a_key_and_an_issuer_first(home):
    service = GhostService(home / "data")
    try:
        service.request_certificate()
    except GhostError as exc:
        assert "create a ghost key first" in str(exc)
    service.create_key()
    try:
        service.request_certificate()
    except GhostError as exc:
        assert "load the issuer's public key first" in str(exc)


def test_missing_library_module_is_reported_not_crashed(home, monkeypatch):
    def broken(name):
        raise ImportError("simulated missing module")

    monkeypatch.setattr(ghost_module.importlib, "import_module", broken)
    status = GhostService(home / "data").status()
    assert not status.available
    assert "does not import" in status.reason
    try:
        ghost_module.load()
    except GhostUnavailable:
        pass
    else:
        raise AssertionError("load must refuse when the module will not import")
