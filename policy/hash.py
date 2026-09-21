import hashlib
import re

_BOM = b"\xef\xbb\xbf"
_FRONTMATTER_RE = re.compile(rb"\A---\r?\n.*?\r?\n---\r?\n(?:\r?\n)?", re.DOTALL)

# What Claude Code rewrites inside a skill body at invocation time (checked
# on the wire): $ARGUMENTS, positional $0/$1... -- even in prose like
# "$1,000" -- ${CLAUDE_SKILL_DIR}, ${CLAUDE_SESSION_ID}, and !`cmd` shell
# injection. A body containing any of them never arrives as the file's bytes.
SUBSTITUTION_RE = re.compile(rb"\$ARGUMENTS|\$\d|\$\{CLAUDE_(?:SKILL_DIR|SESSION_ID)\}|!`")


def sha256_hex(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def strip_frontmatter(contents: bytes) -> bytes:
    # Claude Code strips a skill's YAML frontmatter client-side before
    # sending its content to the model -- only the body ever appears on the
    # wire, so the body is what gets hashed and verified against the catalog.
    # It strips a UTF-8 BOM and CRLF frontmatter too, but sends CRLF line
    # endings inside the body as-is.
    contents = contents.removeprefix(_BOM)
    match = _FRONTMATTER_RE.match(contents)
    return contents[match.end() :] if match else contents
