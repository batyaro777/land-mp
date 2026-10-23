"""Подпись .sig выписки (CMS ГОСТ Р 34.10-2012) через OpenSSL + gost-engine (R13).

Без движка тесты пропускаются: в CI он ставится пакетом libengine-gost-openssl.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from landcheck import sig

pytestmark = pytest.mark.skipif(not sig.engine_available(), reason="нет gost-engine в OpenSSL")

CONTENT = b'<?xml version="1.0"?><extract><cad_number>50:09:0060202:101</cad_number></extract>'


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


@pytest.fixture
def signed(tmp_path: Path) -> tuple[bytes, bytes, Path]:
    key, cert, doc, out = (tmp_path / n for n in ("key.pem", "cert.pem", "doc.xml", "doc.sig"))
    doc.write_bytes(CONTENT)
    _openssl(
        "req", "-engine", "gost", "-newkey", "gost2012_256", "-pkeyopt", "paramset:A", "-nodes",
        "-keyout", str(key), "-x509", "-days", "2",
        "-subj", "/CN=Test Signer/O=Test Rosreestr", "-out", str(cert),
    )  # fmt: skip
    _openssl(
        "cms", "-sign", "-engine", "gost", "-binary", "-in", str(doc), "-signer", str(cert),
        "-inkey", str(key), "-outform", "DER", "-out", str(out),
    )  # fmt: skip
    return CONTENT, out.read_bytes(), cert


def test_verify_with_roots_ok(signed):
    content, der, cert = signed
    res = sig.verify_detached(content, der, roots=cert)
    assert res.status == "ok", res
    assert "Test Rosreestr" in res.signer and "Test Signer" in res.signer
    assert res.signed_at and res.signed_at.endswith("Z")
    assert res.to_dict()["status"] == "ok"


def test_verify_without_roots_is_chain_unverified(signed):
    content, der, _ = signed
    res = sig.verify_detached(content, der)
    assert res.status == "chain_unverified" and "корневые" in res.detail
    assert "Test Signer" in res.signer


def test_tampered_content_is_bad(signed):
    content, der, cert = signed
    res = sig.verify_detached(content + b"x", der, roots=cert)
    assert res.status == "bad" and res.detail


def test_base64_and_pem_sig_accepted(signed):
    content, der, cert = signed
    b64 = base64.b64encode(der)
    assert sig.verify_detached(content, b64, roots=cert).status == "ok"
    pem = b"-----BEGIN CMS-----\n" + b"\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
    pem += b"\n-----END CMS-----\n"
    assert sig.verify_detached(content, pem, roots=cert).status == "ok"
    assert sig.looks_like_cms(der) and sig.looks_like_cms(b64) and sig.looks_like_cms(pem)
    assert not sig.looks_like_cms(b"%PDF-1.4") and not sig.looks_like_cms(b"")


def test_wrong_roots_chain_unverified(signed, tmp_path: Path):
    content, der, _ = signed
    other = tmp_path / "other.pem"
    _openssl(
        "req", "-engine", "gost", "-newkey", "gost2012_256", "-pkeyopt", "paramset:A", "-nodes",
        "-keyout", str(tmp_path / "k2.pem"), "-x509", "-days", "2", "-subj", "/CN=Other CA",
        "-out", str(other),
    )  # fmt: skip
    res = sig.verify_detached(content, der, roots=other)
    assert res.status == "chain_unverified" and res.detail


def test_unavailable_without_engine(monkeypatch):
    monkeypatch.setattr(sig, "engine_available", lambda *a, **k: False)
    res = sig.verify_detached(
        CONTENT, b"\x30\x82\x00\x10" + b"\x06\x09" + b"\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02"
    )
    assert res.status == "unavailable" and "gost" in res.detail
    assert sig.STATUS_LABELS["unavailable"]
