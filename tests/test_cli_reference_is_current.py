"""docs/cli-reference.md says it is generated from the parser; this keeps that
true. Regenerated here and compared byte for byte.

Skipped below Python 3.13, whose argparse lays help out differently: the page is
kept in the 3.13 layout (see scripts/gen-cli-reference.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(sys.version_info < (3, 13), reason="the page uses Python 3.13's argparse layout")
def test_the_cli_reference_matches_the_parser():
    page = ROOT / "docs" / "cli-reference.md"
    fresh = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen-cli-reference.py"), str(page)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "COLUMNS": "80", "PYTHONPATH": str(ROOT / "src")})
    assert fresh.returncode == 0, fresh.stderr
    assert fresh.stdout.rstrip("\n") == page.read_text().rstrip("\n"), (
        "docs/cli-reference.md is stale: COLUMNS=80 python3.13 scripts/gen-cli-reference.py "
        "docs/cli-reference.md > docs/cli-reference.md.new && mv docs/cli-reference.md.new docs/cli-reference.md")
