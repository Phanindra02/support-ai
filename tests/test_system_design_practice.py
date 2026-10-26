"""The system design practice write-ups (docs/system-design/practice/) stay complete."""

import re

import pytest

from app.config import PROJECT_ROOT

PRACTICE = PROJECT_ROOT / "docs" / "system-design" / "practice"
DESIGNS = [
    "01-url-shortener.md",
    "02-rate-limiter.md",
    "03-notification-system.md",
    "04-chat-app.md",
    "05-news-feed.md",
    "06-file-storage.md",
    "07-support-ai-at-10-million-users.md",
]
# The steps of the 45-minute flow in 00-interview-flow-and-estimates.md.
FLOW = [
    "Requirements",
    "Estimates",
    "API",
    "Data model",
    "High-level design",
    "Deep dives",
    "Wrap-up",
]


@pytest.mark.parametrize("name", DESIGNS)
def test_each_design_follows_the_interview_flow_in_order(name):
    headings = re.findall(r"^## (.+)$", (PRACTICE / name).read_text(), flags=re.MULTILINE)
    assert headings == FLOW


@pytest.mark.parametrize("name", DESIGNS)
def test_each_design_has_a_diagram(name):
    assert "```mermaid\nflowchart" in (PRACTICE / name).read_text()


@pytest.mark.parametrize("path", sorted(PRACTICE.glob("*.md")), ids=lambda p: p.name)
def test_links_point_to_files_that_exist(path):
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text()):
        if not target.startswith("http"):
            assert (path.parent / target).exists(), target


def test_the_redesign_compares_itself_with_what_was_built():
    redesign = (PRACTICE / "07-support-ai-at-10-million-users.md").read_text()
    assert "| Part | Built | At 10 million | Why |" in redesign
