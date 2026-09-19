import hashlib


def sha256_hex(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()
