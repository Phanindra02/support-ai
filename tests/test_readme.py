"""The README, API reference, and resume bullets stay accurate as the project changes."""

import re

import pytest

from app.config import PROJECT_ROOT

DOCS = PROJECT_ROOT / "docs"
README = (PROJECT_ROOT / "README.md").read_text()
RESULTS = (DOCS / "results.md").read_text()
BULLETS = (DOCS / "resume-bullets.md").read_text()


def links(text):
    return [t for t in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text) if not t.startswith("http")]


@pytest.mark.parametrize(
    "path",
    [PROJECT_ROOT / "README.md", DOCS / "api.md", DOCS / "resume-bullets.md"],
    ids=lambda p: p.name,
)
def test_links_point_to_files_that_exist(path):
    for target in links(path.read_text()):
        assert (path.parent / target).exists(), target


def test_readme_has_the_architecture_diagram_and_links_the_design_doc():
    assert "```mermaid\nflowchart" in README
    assert "](docs/design-doc.md)" in README


@pytest.mark.parametrize(
    "number",
    ["11.07 ms", "0.02 ms", "0.7 to 1.0 ms", "0.709", "0.594", "0.743", "99.1%", "24/24", "25/25", "87,244", "182"],
)
def test_headline_numbers_come_from_the_results(number):
    # A number in the README must be one that was measured, so a rerun that changes it
    # shows up here instead of leaving the README behind.
    assert number in README
    assert number in RESULTS


def test_resume_bullets_use_only_measured_numbers():
    for number in ["0.709", "0.743", "0.594", "87,244", "182", "34 ms", "130 ms", "550x"]:
        assert number in BULLETS
        assert number in RESULTS
