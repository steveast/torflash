"""TorFlash: проверка minisign-подписи (Ed25519) без внешних бинарников.

Формат minisign:
  .pub  строка-2 = base64( b"Ed" + key_id[8] + ed25519_pubkey[32] )           (42 байта)
  .minisig строка-2 = base64( algo[2] + key_id[8] + signature[64] )            (74 байта)
    algo == b"ED" — подпись над BLAKE2b-512(файл) (prehashed, по умолчанию)
    algo == b"Ed" — подпись над сырыми байтами файла (legacy)
"""

import base64
import hashlib


def _payload_line(text: str) -> bytes:
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.lower().startswith("untrusted comment:"):
            return base64.b64decode(line)
    raise ValueError("no base64 payload in minisig")


def verify_minisign(data: bytes, minisig_text: str, pubkey_b64: str) -> bool:
    """True, если подпись верна для data под публичным ключом pubkey_b64.

    Fail-closed: любая ошибка, неверная подпись или отсутствие бэкенда
    (cryptography) → False. Вызывающий обязан НЕ устанавливать при False."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:
        print("[update] cryptography недоступна — подпись проверить нельзя", flush=True)
        return False
    try:
        pk = base64.b64decode((pubkey_b64 or "").strip())
        if len(pk) != 42 or pk[:2] != b"Ed":
            return False
        key_id, pub = pk[2:10], pk[10:42]
        sig_blob = _payload_line(minisig_text)
        if len(sig_blob) != 74:
            return False
        algo, sig_key_id, sig = sig_blob[:2], sig_blob[2:10], sig_blob[10:74]
        if sig_key_id != key_id:
            return False
        if algo == b"ED":
            msg = hashlib.blake2b(data, digest_size=64).digest()
        elif algo == b"Ed":
            msg = data
        else:
            return False
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except InvalidSignature:
        return False
    except Exception as e:
        print(f"[update] ошибка проверки подписи: {e}", flush=True)
        return False
