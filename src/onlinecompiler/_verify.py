"""Checks that need no client and no network: webhook signatures, receipt signatures and the canonical JSON
receipts are signed over. Webhooks use only the standard library; receipts need the optional cryptography
package (pip install "onlinecompiler[receipts]")."""

from __future__ import annotations

import base64
import hmac
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from hashlib import sha256
from typing import Any, Protocol, Union

RECEIPTS_EXTRA = 'pip install "onlinecompiler[receipts]"'
_TIMESTAMP = re.compile(r"[0-9]{1,15}")
_SIGNATURE = re.compile(r"[0-9a-f]{64}")


class _SupportsItems(Protocol):
    def items(self) -> Iterable[tuple[Any, Any]]: ...


# Request headers: anything with items(), as dicts and the request headers of Django, Flask (werkzeug), FastAPI and
# Starlette have, or (name, value) pairs as an ASGI scope holds them.
HeadersLike = Union[_SupportsItems, Iterable[tuple[Any, Any]]]
MAX_SIGNATURES = 8  # X-OC-Signature entries looked at; the API sends one, or two during a secret rotation


# Canonical JSON (RFC 8785 style), written to match the API's own (backend/judge/canonical.py) byte for byte:
# object keys sorted by their UTF-16 code units, no whitespace, non-ASCII text left as it is, and numbers as
# ECMAScript writes them.

def _number(x: float) -> str:
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("NaN and Infinity cannot be canonicalized")
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    _sign, digits_t, exp = Decimal(repr(abs(x))).as_tuple()
    digits = "".join(map(str, digits_t)).rstrip("0") or "0"
    n = len(digits_t) + int(exp)  # where the decimal point falls, counted from the first digit
    k = len(digits)
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * -n + digits
    else:
        e = n - 1
        out = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("-" if e < 0 else "+") + str(abs(e))
    return sign + out


def _write(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_number(value))
    elif isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        out.append("{")
        for i, key in enumerate(sorted(value, key=lambda k: k.encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise TypeError(f"cannot canonicalize {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """The canonical JSON of value as UTF-8 bytes: what receipts are signed over, and what their tests and
    files hashes are taken of. The same bytes as the API's own canonical form for any value the API signs.
    Raises ValueError for NaN, Infinity or a string holding a lone surrogate (which the API never signs) and
    TypeError for anything JSON cannot hold. Python ints of any size are written exactly; JavaScript cannot hold
    integers beyond 2**53, so the Node client's canonical form differs from this one there."""
    out: list[str] = []
    _write(value, out)
    return "".join(out).encode("utf-8")


# Webhooks

def _text(value: Any) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("latin-1")
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return ",".join(value)
    return value if isinstance(value, str) else None


def _header(headers: Any, name: str) -> str | None:
    """The first value of the header called name (lower case) in a mapping, or in pairs as an ASGI scope holds them."""
    pairs: Iterable = headers.items() if hasattr(headers, "items") else headers
    for key, value in pairs:
        key = _text(key)
        if key is not None and key.lower() == name:
            return _text(value)
    return None


def verify_webhook(secret: str | Sequence[str], raw_body: bytes | str, headers: HeadersLike,
                   tolerance: float = 300) -> bool:
    """Whether a webhook delivery is genuine: True when X-OC-Signature holds a v1= HMAC-SHA256 of
    "{X-OC-Timestamp}.{raw_body}" made with secret (the whole whsec_ string), and the timestamp is within
    tolerance seconds (default 300) of now. Only the first 8 entries of X-OC-Signature are looked at.

    raw_body is the request body exactly as received, before any JSON parsing (bytes; a str is encoded as
    UTF-8). headers is the request's headers (Django's, Flask's or FastAPI's request.headers, a dict, or
    (name, value) pairs), matched case-insensitively. secret may be a list, for while you rotate it on your side; during a rotation on
    ours the header holds two signatures, and one match is enough. Compared in constant time. Any missing,
    malformed or wrong input returns False; it never raises."""
    try:
        given: list[object] = [secret] if isinstance(secret, str) else list(secret)
        secrets = [item for item in given if isinstance(item, str) and item]
        if not secrets or len(secrets) != len(given):
            return False
        if isinstance(raw_body, str):
            raw_body = raw_body.encode("utf-8")
        if not isinstance(raw_body, (bytes, bytearray, memoryview)):
            return False
        timestamp = _header(headers, "x-oc-timestamp")
        signature = _header(headers, "x-oc-signature")
        if timestamp is None or signature is None or not _TIMESTAMP.fullmatch(timestamp):
            return False
        if not abs(time.time() - int(timestamp)) <= tolerance:  # written so that a NaN tolerance fails
            return False
        entries = [value.strip() for value in signature.split(",", MAX_SIGNATURES)[:MAX_SIGNATURES]]
        values = [value[3:] for value in entries if value.startswith("v1=")]
        sent = [value.encode("ascii") for value in values if _SIGNATURE.fullmatch(value)]
        message = timestamp.encode("ascii") + b"." + bytes(raw_body)
        matched = False
        for item in secrets:
            expected = hmac.new(item.encode("utf-8"), message, sha256).hexdigest().encode("ascii")
            for value in sent:
                matched |= hmac.compare_digest(expected, value)
        return matched
    except Exception:  # bad input of any kind is a failed check, never an error
        return False


# Receipts

def _ed25519():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise ModuleNotFoundError(f"Verifying receipts needs the cryptography package: {RECEIPTS_EXTRA}",
                                  name="cryptography") from None
    return Ed25519PublicKey, InvalidSignature


def _b64(value: Any) -> bytes:
    if not isinstance(value, str):
        raise TypeError("expected base64 text")
    return base64.b64decode(value, validate=True)


def verify_receipt(receipt: Mapping[str, Any] | None,
                   keys: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None) -> bool:
    """Whether a receipt's Ed25519 signature is valid: over the canonical JSON of every field but signature, with
    the key the receipt names in key_id. keys is the published key list, as client.receipt_keys() returns it or the
    whole {"keys": [...]} document from https://api.onlinecompiler.io/.well-known/receipt-keys.json. No network.

    Returns False for a changed field, an unknown key_id, a malformed signature, None (a submission without a
    receipt) or any other bad input. Needs the cryptography package: without it this raises ModuleNotFoundError
    saying what to install."""
    public_key_class, _invalid_signature = _ed25519()  # outside the try, so a missing extra is never a False
    try:
        if isinstance(keys, Mapping):
            keys = keys["keys"]
        if not isinstance(receipt, Mapping) or keys is None or isinstance(keys, (str, bytes)):
            return False
        key_id = receipt["key_id"]
        signature = _b64(receipt["signature"])
        entry = next((key for key in keys if isinstance(key, Mapping) and key.get("key_id") == key_id), None)
        if entry is None or entry.get("algorithm", "Ed25519") != "Ed25519":
            return False
        message = canonical_json({name: value for name, value in receipt.items() if name != "signature"})
        public_key_class.from_public_bytes(_b64(entry["public_key"])).verify(signature, message)
    except Exception:  # InvalidSignature, and any bad input (hostile nesting deep enough for RecursionError too)
        return False
    return True  # only once verify() has passed
