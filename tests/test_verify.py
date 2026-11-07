"""verify_webhook, verify_receipt and canonical_json: no client and no network.
Run from packages/python with: PYTHONPATH=src python -m unittest discover tests
The receipt tests sign with a fresh Ed25519 key, so they need cryptography (onlinecompiler[receipts]);
without it they are skipped."""

import base64
import hashlib
import hmac
import importlib.util
import sys
import time
import unittest
from unittest import mock

import onlinecompiler as oc

HAS_CRYPTOGRAPHY = importlib.util.find_spec("cryptography") is not None

SECRET = "whsec_" + "a" * 43
OLD_SECRET = "whsec_" + "b" * 43
BODY = b'{"event":"submission.finished","submission":"sub_8f2Kq7LmT3xR9vWb1cYd4hNe","status":"finished"}'


def sign(secret: str, body: bytes, timestamp: int) -> str:
    return "v1=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def headers_for(*secrets: str, body: bytes = BODY, timestamp: int | None = None) -> dict:
    timestamp = int(time.time()) if timestamp is None else timestamp
    return {"X-OC-Event": "submission.finished", "X-OC-Delivery": "dlv_8f2Kq7LmT3xR9vWb1cYd4hNe",
            "X-OC-Timestamp": str(timestamp), "X-OC-Signature": ",".join(sign(s, body, timestamp) for s in secrets)}


class VerifyWebhookTests(unittest.TestCase):
    def test_a_correct_signature_passes(self):
        self.assertIs(oc.verify_webhook(SECRET, BODY, headers_for(SECRET)), True)

    def test_during_a_rotation_either_secret_passes(self):
        both = headers_for(SECRET, OLD_SECRET)
        self.assertIn(",", both["X-OC-Signature"])
        self.assertTrue(oc.verify_webhook(SECRET, BODY, both))
        self.assertTrue(oc.verify_webhook(OLD_SECRET, BODY, both))

    def test_the_receiver_may_hold_several_secrets(self):
        self.assertTrue(oc.verify_webhook([OLD_SECRET, SECRET], BODY, headers_for(SECRET)))
        self.assertTrue(oc.verify_webhook((SECRET,), BODY, headers_for(SECRET)))

    def test_header_names_match_in_any_case(self):
        sent = headers_for(SECRET)
        for convert in (str.lower, str.upper, lambda name: name.title()):
            with self.subTest(convert=convert):
                self.assertTrue(oc.verify_webhook(SECRET, BODY, {convert(k): v for k, v in sent.items()}))

    def test_bytes_header_pairs_and_other_body_types_work(self):
        sent = headers_for(SECRET)
        raw_pairs = [(k.lower().encode(), v.encode()) for k, v in sent.items()]  # as an ASGI scope holds them
        self.assertTrue(oc.verify_webhook(SECRET, BODY, raw_pairs))
        self.assertTrue(oc.verify_webhook(SECRET, bytearray(BODY), sent))
        self.assertTrue(oc.verify_webhook(SECRET, memoryview(BODY), sent))
        self.assertTrue(oc.verify_webhook(SECRET, BODY.decode(), sent))

    def test_only_the_first_8_signatures_count(self):
        now = int(time.time())
        decoys = [sign(f"whsec_decoy{i}", BODY, now) for i in range(8)]
        good = sign(SECRET, BODY, now)
        eighth = ",".join(decoys[:7] + [good])
        ninth = ",".join(decoys + [good])
        self.assertTrue(oc.verify_webhook(SECRET, BODY, {"X-OC-Timestamp": str(now), "X-OC-Signature": eighth}))
        self.assertFalse(oc.verify_webhook(SECRET, BODY, {"X-OC-Timestamp": str(now), "X-OC-Signature": ninth}))

    def test_a_wrong_secret_fails(self):
        self.assertIs(oc.verify_webhook(OLD_SECRET, BODY, headers_for(SECRET)), False)
        self.assertFalse(oc.verify_webhook(SECRET[len("whsec_"):], BODY, headers_for(SECRET)))  # prefix is keyed too

    def test_a_changed_body_fails(self):
        self.assertFalse(oc.verify_webhook(SECRET, BODY.replace(b"finished", b"failed"), headers_for(SECRET)))
        self.assertFalse(oc.verify_webhook(SECRET, BODY + b"\n", headers_for(SECRET)))

    def test_a_timestamp_outside_the_tolerance_fails(self):
        now = int(time.time())
        self.assertFalse(oc.verify_webhook(SECRET, BODY, headers_for(SECRET, timestamp=now - 301)))
        self.assertFalse(oc.verify_webhook(SECRET, BODY, headers_for(SECRET, timestamp=now + 301)))
        self.assertTrue(oc.verify_webhook(SECRET, BODY, headers_for(SECRET, timestamp=now - 3000), tolerance=3600))
        self.assertFalse(oc.verify_webhook(SECRET, BODY, headers_for(SECRET), tolerance=float("nan")))

    def test_a_missing_header_fails(self):
        for name in ("X-OC-Timestamp", "X-OC-Signature"):
            with self.subTest(name=name):
                sent = headers_for(SECRET)
                del sent[name]
                self.assertIs(oc.verify_webhook(SECRET, BODY, sent), False)

    def test_malformed_headers_fail_without_raising(self):
        now = int(time.time())
        good = sign(SECRET, BODY, now)
        cases = {
            "timestamp not digits": {"X-OC-Timestamp": f"{now}.0", "X-OC-Signature": good},
            "signed timestamp with a plus sign": {"X-OC-Timestamp": f"+{now}", "X-OC-Signature": good},
            "timestamp blank": {"X-OC-Timestamp": "", "X-OC-Signature": good},
            "no v1= prefix": {"X-OC-Timestamp": str(now), "X-OC-Signature": good[3:]},
            "other scheme": {"X-OC-Timestamp": str(now), "X-OC-Signature": "v0=" + good[3:]},
            "upper-case hex": {"X-OC-Timestamp": str(now), "X-OC-Signature": "v1=" + good[3:].upper()},
            "garbage": {"X-OC-Timestamp": str(now), "X-OC-Signature": "v1=éé, ,,=="},
            "empty": {"X-OC-Timestamp": str(now), "X-OC-Signature": ""},
        }
        for label, sent in cases.items():
            with self.subTest(label):
                self.assertIs(oc.verify_webhook(SECRET, BODY, sent), False)

    def test_bad_arguments_return_false(self):
        sent = headers_for(SECRET)
        for secret, body, headers in [(None, BODY, sent), ("", BODY, sent), ([], BODY, sent), ([SECRET, 5], BODY, sent),
                                      (SECRET, 7, sent), (SECRET, None, sent), (SECRET, BODY, None),
                                      (SECRET, BODY, "X-OC-Timestamp: 1")]:
            with self.subTest(secret=secret, body=body, headers=headers):
                self.assertIs(oc.verify_webhook(secret, body, headers), False)


# Hard-coded canonical forms, checked byte for byte against the API's own canonical_json
# (backend/judge/canonical.py) when they were written.
CANONICAL_CASES = [
    ({"b": 1, "a": {"d": [3, {"z": None, "y": True}], "c": False}}, b'{"a":{"c":false,"d":[3,{"y":true,"z":null}]},"b":1}'),
    ({"é": 1, "e": 2, "\U0001f600": 3, "｡": 4}, '{"e":2,"é":1,"\U0001f600":3,"｡":4}'.encode()),
    ("café \U0001f680 中文", '"café \U0001f680 中文"'.encode()),
    ('quote " backslash \\ slash / nl \n tab \t cr \r bs \b ff \f nul \x00 us \x1f del \x7f ls  ',
     b'"quote \\" backslash \\\\ slash / nl \\n tab \\t cr \\r bs \\b ff \\f nul \\u0000 us \\u001f del \x7f ls '
     + " ".encode() + b'"'),
    ([0, 1, -1, 42, 9007199254740991, -9007199254740991], b"[0,1,-1,42,9007199254740991,-9007199254740991]"),
    ([1.0, 0.1, 1e21, 1e-7, -0.0, 1e20, 1e-6, 100.0, 33.33, 1.5e300, 5e-324, 0.000123],
     b"[1,0.1,1e+21,1e-7,0,100000000000000000000,0.000001,100,33.33,1.5e+300,5e-324,0.000123]"),
    ({"score": 33.33, "tolerance": 1e-06, "time_limit": 2.5, "passed": 1}, b'{"passed":1,"score":33.33,"time_limit":2.5,"tolerance":0.000001}'),
]


class CanonicalJsonTests(unittest.TestCase):
    def test_canonical_forms(self):
        for value, expected in CANONICAL_CASES:
            with self.subTest(value=value):
                self.assertEqual(oc.canonical_json(value), expected)

    def test_values_json_cannot_hold_are_refused(self):
        for value in (float("nan"), float("inf"), {1: "x"}, {"a": object()}, b"bytes"):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    oc.canonical_json(value)


def receipt_fields() -> dict:
    return {
        "receipt": "rcpt_7Hq2Zp4Wn8Ks1Ab6Ct3Dv5Ex", "format": 1, "submission": "sub_8f2Kq7LmT3xR9vWb1cYd4hNe",
        "account": "acct_3k9Fj2Lq8Rt5Wm1Z", "external_ref": "attempt-4821 é\U0001f600", "issued": "2026-11-05T09:14:22Z",
        "compiler": {"id": "python-3.14", "version": "3.14", "image": None},
        "code_sha256": "0" * 64, "tests_sha256": "1" * 64, "files_sha256": None,
        "settings": {"checker": "trim", "tolerance": 1e-06, "scoring": "percent", "time_limit": 2,
                     "stop_on_failure": False},
        "verdict": "wrong_answer", "score": 33.33, "passed": 1, "total": 3,
        "tests": [{"verdict": "accepted", "hidden": False, "time": "0.0213"}, {"verdict": "wrong_answer", "hidden": True},
                  {"verdict": "wrong_answer", "hidden": True}],
    }


@unittest.skipUnless(HAS_CRYPTOGRAPHY, "verify_receipt needs cryptography: pip install 'onlinecompiler[receipts]'")
class VerifyReceiptTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()
        raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.keys = [{"key_id": "older-key", "algorithm": "Ed25519", "public_key": base64.b64encode(b"\x01" * 32).decode()},
                     {"key_id": "test-key", "algorithm": "Ed25519", "public_key": base64.b64encode(raw).decode()}]
        # Signed as the API signs: every field but the signature, key_id included, in canonical JSON.
        unsigned = {**receipt_fields(), "key_id": "test-key"}
        self.receipt = {**unsigned, "signature": base64.b64encode(private.sign(oc.canonical_json(unsigned))).decode()}

    def test_a_signed_receipt_passes(self):
        self.assertIs(oc.verify_receipt(self.receipt, self.keys), True)
        self.assertTrue(oc.verify_receipt(self.receipt, {"keys": self.keys}))  # the key list as published

    def test_key_order_does_not_matter(self):
        shuffled = dict(reversed(list(self.receipt.items())))
        self.assertTrue(oc.verify_receipt(shuffled, self.keys))

    def test_a_tampered_field_fails(self):
        for field, value in [("verdict", "accepted"), ("score", 100), ("passed", 3), ("external_ref", "attempt-4822"),
                             ("issued", "2026-11-05T09:14:23Z")]:
            with self.subTest(field=field):
                self.assertIs(oc.verify_receipt({**self.receipt, field: value}, self.keys), False)
        nested = {**self.receipt, "tests": [{"verdict": "accepted", "hidden": True}] + self.receipt["tests"][1:]}
        self.assertFalse(oc.verify_receipt(nested, self.keys))
        added = {**self.receipt, "extra": 1}
        self.assertFalse(oc.verify_receipt(added, self.keys))

    def test_an_unknown_key_id_fails(self):
        self.assertIs(oc.verify_receipt({**self.receipt, "key_id": "other-key"}, self.keys), False)
        self.assertFalse(oc.verify_receipt(self.receipt, self.keys[:1]))
        self.assertFalse(oc.verify_receipt(self.receipt, []))

    def test_a_bad_signature_fails(self):
        for signature in ("not base64!", self.receipt["signature"][:-2], "", "AAAA", None, 12):
            with self.subTest(signature=signature):
                self.assertIs(oc.verify_receipt({**self.receipt, "signature": signature}, self.keys), False)
        signature = bytearray(base64.b64decode(self.receipt["signature"]))
        signature[0] ^= 1
        self.assertFalse(oc.verify_receipt({**self.receipt, "signature": base64.b64encode(signature).decode()}, self.keys))

    def test_deeply_nested_hostile_json_fails_without_raising(self):
        deep: object = "x"
        for _ in range(50000):
            deep = [deep]
        self.assertIs(oc.verify_receipt({**self.receipt, "deep": deep}, self.keys), False)
        deep_keys: object = self.keys
        for _ in range(50000):
            deep_keys = {"keys": deep_keys}
        self.assertIs(oc.verify_receipt(self.receipt, deep_keys), False)

    def test_none_is_false(self):
        self.assertIs(oc.verify_receipt(None, self.keys), False)
        self.assertIs(oc.verify_receipt(self.receipt, None), False)

    def test_bad_input_fails(self):
        missing = dict(self.receipt)
        del missing["signature"]
        wrong_algorithm = [{**key, "algorithm": "RSA"} for key in self.keys]
        bad_key = [{**self.keys[1], "public_key": "%%%"}]
        for receipt, keys in [(missing, self.keys), (self.receipt, wrong_algorithm), (self.receipt, bad_key),
                              (None, self.keys), ("receipt", self.keys), (self.receipt, None), (self.receipt, "keys"),
                              ({**self.receipt, "score": float("nan")}, self.keys)]:
            with self.subTest(receipt=type(receipt).__name__, keys=keys):
                self.assertIs(oc.verify_receipt(receipt, keys), False)


class MissingExtraTests(unittest.TestCase):
    def test_without_cryptography_the_error_says_what_to_install(self):
        blocked = dict.fromkeys(["cryptography", "cryptography.exceptions", "cryptography.hazmat",
                                 "cryptography.hazmat.primitives", "cryptography.hazmat.primitives.asymmetric",
                                 "cryptography.hazmat.primitives.asymmetric.ed25519"])
        with mock.patch.dict(sys.modules, blocked):
            with self.assertRaises(ImportError) as caught:
                oc.verify_receipt({"key_id": "k", "signature": "AAAA"}, [])
        self.assertIn('pip install "onlinecompiler[receipts]"', str(caught.exception))


if __name__ == "__main__":
    unittest.main()
