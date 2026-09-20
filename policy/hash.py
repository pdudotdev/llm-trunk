import hashlib
import re

_FRONTMATTER_RE = re.compile(rb"\A---\n.*?\n---\n\n?", re.DOTALL)


def sha256_hex(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def strip_frontmatter(contents: bytes) -> bytes:
    # Claude Code strips a skill's YAML frontmatter client-side before
    # sending its content to the model -- only the body ever appears on the
    # wire, so the body is what gets hashed and verified against the catalog.
    match = _FRONTMATTER_RE.match(contents)
    return contents[match.end() :] if match else contents
