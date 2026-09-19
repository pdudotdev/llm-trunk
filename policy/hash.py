import hashlib


def hash_skill_md(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()
