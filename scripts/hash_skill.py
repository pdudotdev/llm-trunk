#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy.hash import hash_skill_md


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

    print(hash_skill_md(contents))


if __name__ == "__main__":
    main()
