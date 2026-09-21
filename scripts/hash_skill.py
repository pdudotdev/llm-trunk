#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy.hash import SUBSTITUTION_RE, sha256_hex, strip_frontmatter


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path/to/SKILL.md>", file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    try:
        contents = path.read_bytes()
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # Hash the body only (frontmatter stripped) -- that's the only part
    # Claude Code actually sends verbatim when a skill is invoked. Printed as
    # catalog.yaml fields: routing needs both the digest and the canonical
    # byte length to isolate the body inside a larger payload.
    body = strip_frontmatter(contents)
    substitution = SUBSTITUTION_RE.search(body)
    if substitution:
        print(
            f"error: body contains {substitution.group().decode()!r}, which Claude Code "
            "rewrites at invocation time -- the body it sends will never match this "
            "hash, so every invocation would be denied as stale. Remove it to route "
            "this skill.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f'sha256: "{sha256_hex(body)}"')
    print(f"bytes: {len(body)}")


if __name__ == "__main__":
    main()
