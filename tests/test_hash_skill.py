"""scripts/hash_skill.py: the catalog row it prints, and the bodies it refuses."""
import subprocess
import sys

import pytest
from conftest import REPO

from policy.hash import sha256_hex

SCRIPT = REPO / "scripts" / "hash_skill.py"


def _run(path):
    return subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)


def test_prints_the_hash_and_length_of_the_body_without_frontmatter(tmp_path):
    body = "Review the design.\n"
    skill = tmp_path / "SKILL.md"
    skill.write_text(f"---\nname: design-review\ndescription: x\n---\n\n{body}")
    result = _run(skill)
    assert result.returncode == 0
    assert result.stdout == f'sha256: "{sha256_hex(body.encode())}"\nbytes: {len(body)}\n'


@pytest.mark.parametrize("placeholder", ["$ARGUMENTS", "$1", "${CLAUDE_SKILL_DIR}", "${CLAUDE_SESSION_ID}", "!`date`"])
def test_refuses_bodies_claude_code_would_rewrite(tmp_path, placeholder):
    skill = tmp_path / "SKILL.md"
    skill.write_text(f"---\nname: x\n---\nUse {placeholder} here.\n")
    result = _run(skill)
    assert result.returncode == 1
    assert "rewrites at invocation time" in result.stderr and result.stdout == ""


def test_missing_file_is_an_error(tmp_path):
    result = _run(tmp_path / "nope.md")
    assert result.returncode == 1 and result.stderr.startswith("error:")
