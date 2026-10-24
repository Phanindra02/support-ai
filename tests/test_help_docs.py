"""The sample help docs and the 25 questions that measure the bot (docs/help-docs/)."""

import json

from pypdf import PdfReader

from app.bot import DONT_KNOW
from app.config import PROJECT_ROOT

HELP_DOCS = PROJECT_ROOT / "docs" / "help-docs"
QUESTIONS = json.loads((HELP_DOCS / "questions.json").read_text())


def test_there_are_25_questions_each_asked_once():
    assert len(QUESTIONS) == 25
    assert len({q["question"] for q in QUESTIONS}) == 25


def test_each_known_answer_is_in_the_doc_it_names():
    # So a wrong answer on a question is the bot's fault, not the question's.
    for q in QUESTIONS:
        if q["source"] is None:
            assert q["answer"] == DONT_KNOW, q
            continue
        text = (HELP_DOCS / q["source"]).with_suffix(".txt").read_text()
        assert q["answer"] in text, q


def test_each_pdf_holds_its_docs_text():
    # The PDFs are what gets uploaded. Lines are wrapped between words, so the words survive.
    for pdf in HELP_DOCS.glob("*.pdf"):
        extracted = " ".join(page.extract_text() for page in PdfReader(pdf).pages).split()
        assert extracted == pdf.with_suffix(".txt").read_text().split(), pdf.name
