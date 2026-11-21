"""Проверка открепленной подписи выписки ЕГРН (.sig: CMS, ГОСТ Р 34.10-2012) через OpenSSL
с движком gost-engine (пакет libengine-gost-openssl, open source, без КриптоПро).

Два уровня: математика подписи (файл не менялся и подписан ключом из вложенного сертификата)
и цепочка до корневого сертификата Минцифры (нужен файл корней `LANDCHECK_GOST_ROOTS`).
Проверено в песочнице 23.10.2026: sign/verify/tamper на самоподписанном ГОСТ-сертификате.
Исключений наружу нет: любой сбой — статус `unavailable` с причиной.
"""

from __future__ import annotations

import base64
import functools
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SIGNED_DATA_OID = b"\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02"  # 1.2.840.113549.1.7.2
_TIMEOUT = 30.0
STATUS_LABELS = {
    "ok": "подпись верна, цепочка до корня проверена",
    "chain_unverified": "подпись верна, цепочка до корня Минцифры не проверена",
    "bad": "подпись не сходится: файл изменён или подпись от другого файла",
    "unavailable": "проверка подписи недоступна на сервере",
}


@dataclass
class SigResult:
    status: str  # ok|chain_unverified|bad|unavailable
    signer: str = ""  # организация/CN из сертификата подписанта
    issuer: str = ""
    signed_at: str | None = None  # ISO 8601
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def looks_like_cms(raw: bytes) -> bool:
    """DER-контейнер CMS SignedData (или его base64/PEM-обёртка)."""
    der = normalize_sig(raw)
    return bool(der) and der[:1] == b"\x30" and _SIGNED_DATA_OID in der[:128]


def normalize_sig(raw: bytes) -> bytes:
    """Файл .sig бывает DER, base64 одной строкой или PEM; на выходе всегда DER."""
    if not raw:
        return b""
    if raw[:1] == b"\x30":
        return raw
    text = raw.decode("ascii", "ignore")
    text = re.sub(r"-----(BEGIN|END)[^-]*-----", "", text)
    text = re.sub(r"\s+", "", text)
    if not text or not re.fullmatch(r"[A-Za-z0-9+/=]+", text):
        return raw
    try:
        der = base64.b64decode(text, validate=True)
    except ValueError:
        return raw
    return der if der[:1] == b"\x30" else raw


@functools.lru_cache(maxsize=1)
def engine_available(openssl: str = "openssl") -> bool:
    if not shutil.which(openssl):
        return False
    rc, out, _ = _run([openssl, "engine", "gost"], timeout=10)
    return rc == 0 and "gost" in out.lower()


def _run(args: list[str], timeout: float = _TIMEOUT) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", f"{type(e).__name__}: {e}"
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _last_error(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip() and "Engine" not in ln]
    if not lines:
        return ""
    ln = lines[-1]
    m = re.search(r"error:[0-9A-F]+:([^:]+):([^:]+):([^:]+)", ln)
    return f"{m.group(2)}: {m.group(3)}" if m else ln[:200]


def _name_field(line: str, key: str) -> str:
    m = re.search(rf"(?:^|[,/])\s*{key}\s*=\s*([^,/]+)", line)
    return m.group(1).strip() if m else ""


def _display_name(line: str) -> str:
    org = _name_field(line, "O")
    cn = _name_field(line, "CN")
    if org and cn and org != cn:
        return f"{org} ({cn})"
    return org or cn or line.strip()[:120]


def verify_detached(
    content: bytes, sig: bytes, *, roots: Path | None = None, openssl: str = "openssl"
) -> SigResult:
    """Проверка файла против его открепленной подписи. roots — PEM с корневыми сертификатами."""
    if not engine_available(openssl):
        return SigResult("unavailable", detail="в OpenSSL нет движка gost (libengine-gost-openssl)")
    der = normalize_sig(sig)
    if not looks_like_cms(der):
        return SigResult("unavailable", detail="файл .sig не похож на подпись CMS")
    with tempfile.TemporaryDirectory(prefix="lc-sig-") as tmp:
        t = Path(tmp)
        (t / "content").write_bytes(content)
        (t / "sig.der").write_bytes(der)
        base = [
            openssl,
            "cms",
            "-verify",
            "-engine",
            "gost",
            "-binary",
            "-inform",
            "DER",
            "-in",
            str(t / "sig.der"),
            "-content",
            str(t / "content"),
            "-out",
            "/dev/null",
        ]
        rc, _, err = _run([*base, "-noverify", "-signer", str(t / "signer.pem")])
        if rc != 0:
            return SigResult("bad", detail=_last_error(err))
        signer, issuer = _signer_names(t / "signer.pem", openssl)
        signed_at = _signing_time(t / "sig.der", openssl)
        if roots and Path(roots).exists():
            rc, _, err = _run([*base, "-CAfile", str(roots)])
            if rc == 0:
                return SigResult("ok", signer, issuer, signed_at, "цепочка проверена")
            return SigResult("chain_unverified", signer, issuer, signed_at, _last_error(err))
        return SigResult(
            "chain_unverified", signer, issuer, signed_at, "корневые сертификаты не заданы"
        )


def _signer_names(pem: Path, openssl: str) -> tuple[str, str]:
    rc, out, _ = _run(
        [
            openssl,
            "x509",
            "-in",
            str(pem),
            "-noout",
            "-subject",
            "-issuer",
            "-nameopt",
            "oneline,-esc_msb,utf8,sep_comma_plus_space",
        ],
        timeout=10,
    )
    if rc != 0:
        return "", ""
    subject = issuer = ""
    for ln in out.splitlines():
        if ln.startswith("subject="):
            subject = _display_name(ln[len("subject=") :])
        elif ln.startswith("issuer="):
            issuer = _display_name(ln[len("issuer=") :])
    return subject, issuer


def _signing_time(der: Path, openssl: str) -> str | None:
    rc, out, _ = _run(
        [openssl, "cms", "-cmsout", "-print", "-inform", "DER", "-in", str(der)], timeout=10
    )
    if rc != 0:
        return None
    m = re.search(r"signingTime.*?(?:UTCTIME|GENERALIZEDTIME):([^\n]+)", out, re.S)
    if not m:
        return None
    raw = m.group(1).strip()
    for fmt in ("%b %d %H:%M:%S %Y GMT", "%b %d %H:%M:%S %Y %Z"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return raw
