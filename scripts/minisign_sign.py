#!/usr/bin/env python3
"""Подписать файлы в формате minisign, используя НЕзашифрованный (-W) секретный ключ.

Не требует бинаря minisign — только cryptography (Ed25519), как и верификатор
в torflash/update/signature.py. Производит <file>.minisig рядом с каждым файлом.

Формат секретного ключа minisign (после base64 второй строки .key):
  sig_alg[2]="Ed" kdf_alg[2] cksum_alg[2]="B2" kdf_salt[32]
  kdf_opslimit[8] kdf_memlimit[8] key_id[8] ed25519_sk[64] checksum[32]
  ed25519_sk = seed[32] + pubkey[32];  kdf_alg == \\x00\\x00 → без пароля.

Использование: minisign_sign.py <secret_key> <file> [<file> ...]
"""
import base64
import hashlib
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def load_secret(path: str):
    lines = [l.strip() for l in open(path, encoding="utf-8")
             if l.strip() and not l.lower().startswith("untrusted comment:")]
    blob = base64.b64decode(lines[0])
    if len(blob) != 158 or blob[:2] != b"Ed":
        sys.exit("not a minisign secret key")
    if blob[2:4] != b"\x00\x00":
        sys.exit("secret key is password-protected; regenerate with `minisign -G -W`")
    key_id = blob[54:62]
    seed = blob[62:94]
    return key_id, Ed25519PrivateKey.from_private_bytes(seed)


def sign_file(key_id: bytes, sk: Ed25519PrivateKey, path: str):
    data = open(path, "rb").read()
    prehash = hashlib.blake2b(data, digest_size=64).digest()
    sig = sk.sign(prehash)                       # подпись над BLAKE2b(файл) (алгоритм "ED")
    name = os.path.basename(path)                # кроссплатформенно (Windows: \\)
    trusted = f"timestamp:0\tfile:{name}\thashed"
    global_sig = sk.sign(sig + trusted.encode())  # глобальная подпись над sig+trusted comment
    out = (
        "untrusted comment: signature from torflash minisign\n"
        + base64.b64encode(b"ED" + key_id + sig).decode() + "\n"
        + "trusted comment: " + trusted + "\n"
        + base64.b64encode(global_sig).decode() + "\n"
    )
    open(path + ".minisig", "w", encoding="utf-8").write(out)
    print(f"signed {path} -> {path}.minisig")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    kid, key = load_secret(sys.argv[1])
    for f in sys.argv[2:]:
        sign_file(kid, key, f)
