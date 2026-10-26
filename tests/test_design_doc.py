"""The platform's design doc (docs/design-doc.md) stays complete and in step with the code."""

import re

import app.models  # noqa: F401  Registers every table on Base.metadata.
from app.config import PROJECT_ROOT
from app.database import Base

DOCS = PROJECT_ROOT / "docs"
DOC = (DOCS / "design-doc.md").read_text()


def test_has_every_section_the_roadmap_asks_for():
    headings = set(re.findall(r"^## (.+)$", DOC, flags=re.MULTILINE))
    for section in [
        "Requirements",
        "Scale estimates",
        "Architecture",
        "Data model",
        "Bottlenecks and failure points",
        "Trade-offs",
    ]:
        assert section in headings


def test_has_an_architecture_diagram_and_a_data_model_diagram():
    assert "```mermaid\nflowchart" in DOC
    assert "```mermaid\nerDiagram" in DOC


def test_describes_every_table():
    # A new table without a row in the data model means the doc has fallen behind.
    for table in Base.metadata.tables:
        assert f"| `{table}` |" in DOC, table


def test_its_links_point_to_files_that_exist():
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", DOC):
        if not target.startswith("http"):
            assert (DOCS / target).exists(), target
