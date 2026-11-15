from pathlib import Path
from runpy import run_path

import pytest

notes = run_path(str(Path(__file__).parents[1] / "scripts" / "release-notes.py"))["release_notes"]


def test_release_notes_select_exact_version_and_exclude_unreleased():
    changelog = """# Changelog
## [Unreleased]
Do not publish this future feature.
## [0.3.0]
- Current feature.
## [0.2.0]
- Old feature.
"""
    assert notes(changelog, "0.3.0") == "- Current feature.\n"


@pytest.mark.parametrize("body", ["", "\n\n", "[0.3.0]: https://example.com"])
def test_release_notes_refuse_empty_version_entry(body):
    with pytest.raises(ValueError, match="contain release notes"):
        notes(f"## [0.3.0]\n{body}\n## [0.2.0]\nOld", "0.3.0")


@pytest.mark.parametrize("headers", ["## [Unreleased]", "## [0.3.0]\na\n## [0.3.0]"])
def test_release_notes_refuse_missing_or_duplicate_version(headers):
    with pytest.raises(ValueError, match="exactly one"):
        notes(headers, "0.3.0")
