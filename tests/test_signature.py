"""Тесты проверки minisign-подписи (verify_minisign).

Строим настоящую Ed25519-подпись в формате minisign и проверяем верификатор.
Пропускается, если cryptography недоступна.
"""

import base64
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

KEY_ID = b"\x01\x02\x03\x04\x05\x06\x07\x08"


def _make(data: bytes, prehash: bool = True, key_id: bytes = KEY_ID):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    pub_raw = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_line = base64.b64encode(b"Ed" + key_id + pub_raw).decode()
    algo = b"ED" if prehash else b"Ed"
    msg = hashlib.blake2b(data, digest_size=64).digest() if prehash else data
    sig = sk.sign(msg)
    minisig = "untrusted comment: signature\n" + base64.b64encode(algo + key_id + sig).decode() + "\n"
    return pub_line, minisig


class TestMinisign:
    def test_valid_prehashed(self):
        from torflash.update.signature import verify_minisign
        data = b"hello torflash"
        pub, sig = _make(data, prehash=True)
        assert verify_minisign(data, sig, pub) is True

    def test_valid_legacy(self):
        from torflash.update.signature import verify_minisign
        data = b"legacy mode"
        pub, sig = _make(data, prehash=False)
        assert verify_minisign(data, sig, pub) is True

    def test_tampered_data_rejected(self):
        from torflash.update.signature import verify_minisign
        pub, sig = _make(b"original payload")
        assert verify_minisign(b"tampered payload", sig, pub) is False

    def test_wrong_key_rejected(self):
        from torflash.update.signature import verify_minisign
        data = b"payload"
        pub1, _ = _make(data)
        _, sig2 = _make(data)  # подпись другим ключом, тот же key_id
        assert verify_minisign(data, sig2, pub1) is False

    def test_key_id_mismatch_rejected(self):
        from torflash.update.signature import verify_minisign
        data = b"payload"
        pub, _ = _make(data, key_id=b"\x01" * 8)
        _, sig = _make(data, key_id=b"\x02" * 8)
        assert verify_minisign(data, sig, pub) is False

    def test_garbage_inputs(self):
        from torflash.update.signature import verify_minisign
        assert verify_minisign(b"x", "", "") is False
        assert verify_minisign(b"x", "untrusted comment: only\n", "notbase64!!") is False
