"""
Server identity and per-response authentication (security spec §2/§3).

The release key (never present on the server) signs a small certificate that
delegates trust to a per-build server Ed25519 key. For authenticated requests
(``GET /dashboard.png``, ``GET /identity``) carrying a valid
``X-Tracker-Nonce``, the server signs a message that binds the nonce, path,
status, body hash and a fixed list of control headers, and returns it as
``X-Tracker-Auth`` together with the certificate in ``X-Tracker-Cert``.

If the ``cryptography`` package or the identity files are missing, signing is
disabled (loudly) and the server keeps serving unsigned responses, which signed
clients will refuse.
"""

if __name__ == "identity":
    __name__ = "server.identity"

import base64
import binascii
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from typing import Any, Optional

from paths import find_artifact

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when the dep is missing
    CRYPTO_AVAILABLE = False

KEY_NAME = "server_identity.key"
CERT_NAME = "server_identity.cert.json"
CERT_FORMAT = "transit-tracker-server-v1"
RESP_FORMAT_V1 = "transit-tracker-resp-v1"
RESP_FORMAT_V2 = "transit-tracker-resp-v2"
RESP_FORMAT = RESP_FORMAT_V2

NONCE_HEADER = "X-Tracker-Nonce"
CERT_HEADER = "X-Tracker-Cert"
AUTH_HEADER = "X-Tracker-Auth"

_NONCE_RE = re.compile(r"\A[0-9a-f]{32}\Z")

# Signed headers for v1 (legacy clients, e.g. v1.35.x)
SIGNED_HEADERS_V1 = (
    "etag",
    "x-kindle-poll-interval",
    "x-tracker-presentation",
    "x-tracker-mode",
    "x-tracker-action",
    "x-tracker-diag",
    "x-kindle-brightness",
    "x-kindle-warmth",
    "x-tracker-version",
    "x-tracker-sha256",
    "x-resolved-view",
    "x-tracker-view",
)

# Signed headers for v2 (mandated by spec §3 with policy header)
SIGNED_HEADERS_V2 = SIGNED_HEADERS_V1 + ("x-tracker-policy",)

SIGNED_HEADERS = SIGNED_HEADERS_V2


class IdentityError(Exception):
    """Raised when the identity key or certificate is unusable."""


def is_valid_nonce(nonce: Any) -> bool:
    """True only for exactly 32 lowercase hex characters."""
    return isinstance(nonce, str) and _NONCE_RE.match(nonce) is not None


def b64decode_strict(value: Any) -> bytes:
    """Standard base64 with padding; rejects anything else."""
    if not isinstance(value, str) or not value:
        raise IdentityError("expected a base64 string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as e:
        raise IdentityError(f"bad base64: {e}") from e


def build_response_message(
    nonce: str,
    path: str,
    status: int,
    body: bytes,
    headers: Mapping[str, str],
    resp_format: str = RESP_FORMAT_V2,
) -> bytes:
    """
    Builds the §3 response message: fixed preamble lines, then one
    `<name>=<value>` line per signed header (empty when the header is absent),
    joined by LF with no trailing newline.
    """
    if any("\r" in s or "\n" in s for s in (nonce, path)):
        raise IdentityError("CR or LF in nonce or path")
    lower: dict[str, str] = {}
    for name, value in headers.items():
        if any("\r" in s or "\n" in s for s in (name, value)):
            raise IdentityError(f"CR or LF in header {name}")
        lower[name.lower()] = value
    signed_hdrs = (
        SIGNED_HEADERS_V1 if resp_format == RESP_FORMAT_V1 else SIGNED_HEADERS_V2
    )
    fmt_str = RESP_FORMAT_V1 if resp_format == RESP_FORMAT_V1 else RESP_FORMAT_V2
    lines = [
        fmt_str,
        f"nonce={nonce}",
        f"path={path}",
        f"status={int(status)}",
        f"body-sha256={hashlib.sha256(body).hexdigest()}",
    ]
    for name in signed_hdrs:
        lines.append(f"{name}={lower.get(name, '')}")
    return "\n".join(lines).encode("utf-8")


def cert_message(public_key_b64: str, issued_at: int) -> bytes:
    """The §2 certificate message signed by the release key."""
    return "\n".join(
        [
            CERT_FORMAT,
            f"public_key={public_key_b64}",
            f"issued_at={int(issued_at)}",
        ]
    ).encode("utf-8")


def parse_cert(cert_bytes: bytes) -> dict[str, Any]:
    """Parses and structurally validates a server identity certificate."""
    try:
        cert = json.loads(cert_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise IdentityError(f"certificate is not valid JSON: {e}") from e
    if not isinstance(cert, dict):
        raise IdentityError("certificate is not a JSON object")
    if cert.get("format") != CERT_FORMAT:
        raise IdentityError(f"certificate format is not {CERT_FORMAT!r}")
    if len(b64decode_strict(cert.get("public_key"))) != 32:
        raise IdentityError("certificate public_key is not 32 bytes")
    issued_at = cert.get("issued_at")
    if not isinstance(issued_at, int) or isinstance(issued_at, bool) or issued_at < 0:
        raise IdentityError("certificate issued_at is not a non-negative integer")
    if len(b64decode_strict(cert.get("signature"))) != 64:
        raise IdentityError("certificate signature is not 64 bytes")
    return cert


def verify_cert(cert: dict[str, Any], release_public_key_b64: str) -> bool:
    """Verifies a parsed certificate against a release public key."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(
            b64decode_strict(release_public_key_b64)
        )
        pub.verify(
            b64decode_strict(cert["signature"]),
            cert_message(cert["public_key"], cert["issued_at"]),
        )
        return True
    except (IdentityError, InvalidSignature, ValueError, KeyError):
        return False


class ServerIdentity:
    """A loaded server identity: private key + the exact certificate bytes."""

    def __init__(self, private_key: Any, cert_bytes: bytes) -> None:
        self._key = private_key
        self.cert_bytes = cert_bytes.strip()
        self.cert = parse_cert(self.cert_bytes)
        raw_pub = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        if base64.b64encode(raw_pub).decode("ascii") != self.cert["public_key"]:
            raise IdentityError("server key does not match the certificate public_key")
        self.public_key_b64 = self.cert["public_key"]
        self.cert_header = base64.b64encode(self.cert_bytes).decode("ascii")

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self._key.sign(message)).decode("ascii")

    def sign_response(
        self,
        nonce: str,
        path: str,
        status: int,
        body: bytes,
        headers: Mapping[str, str],
        resp_format: str = RESP_FORMAT_V2,
    ) -> list[tuple[str, str]]:
        """Returns the X-Tracker-Cert / X-Tracker-Auth headers for a response."""
        msg = build_response_message(
            nonce, path, status, body, headers, resp_format=resp_format
        )
        return [(CERT_HEADER, self.cert_header), (AUTH_HEADER, self.sign(msg))]


def _resolve(env_name: str, artifact: str) -> Optional[str]:
    override = os.environ.get(env_name, "").strip()
    if override:
        return override
    return find_artifact(artifact)


def load_identity(
    key_path: Optional[str] = None,
    cert_path: Optional[str] = None,
) -> tuple[Optional[ServerIdentity], str]:
    """
    Loads the server identity. Returns (identity, human-readable status).
    identity is None when signing is unavailable; the status explains why.
    Never raises.
    """
    if not CRYPTO_AVAILABLE:
        return None, "the 'cryptography' package is not installed"
    key_path = key_path or _resolve("IDENTITY_KEY_PATH", KEY_NAME)
    cert_path = cert_path or _resolve("IDENTITY_CERT_PATH", CERT_NAME)
    if not key_path or not os.path.exists(key_path):
        return None, f"identity key {KEY_NAME} not found"
    if not cert_path or not os.path.exists(cert_path):
        return None, f"identity certificate {CERT_NAME} not found"
    try:
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise IdentityError("identity key is not an Ed25519 key")
        with open(cert_path, "rb") as f:
            cert_bytes = f.read(64 * 1024)
        identity = ServerIdentity(key, cert_bytes)
    except (IdentityError, ValueError, TypeError, OSError) as e:
        return None, f"identity unusable ({e})"

    release_pub = os.environ.get("OTA_PUBLIC_KEY", "").strip()
    if release_pub and not verify_cert(identity.cert, release_pub):
        return None, "identity certificate does not verify against OTA_PUBLIC_KEY"

    note = ""
    try:
        if stat.S_IMODE(os.stat(key_path).st_mode) & 0o077:
            note = f" (WARNING: {key_path} is readable by group/other; chmod 600 it)"
    except OSError:  # pragma: no cover - the file was just read
        pass
    return identity, f"signing with {key_path} / {cert_path}{note}"
