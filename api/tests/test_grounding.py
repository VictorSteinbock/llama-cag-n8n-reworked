"""Unit tests for api/app/grounding.py — pure function, no fakes.

Special codepoints are built via chr() so this file stays pure ASCII and states
exactly which character it exercises (soft hyphen, NBSP, curly quotes, em-dash).
"""

import time

from app.grounding import grounding

DOC = (
    "The ACME Widget Pro battery lasts eighteen hours on a single full charge "
    "under normal use. Operating temperature range: 0 C to 40 C. The warranty "
    "period is 24 months from the date of purchase."
)

# One word changed ("normal" -> "typical"); scores ~0.935, inside (0.9, 0.99).
PARAPHRASE = "battery lasts eighteen hours on a single full charge under typical use"


def test_exact_substring_is_grounded_exact():
    # Verbatim quote with mixed case and collapsed-away extra spaces.
    result = grounding("  BATTERY   lasts EIGHTEEN hours ", DOC)
    assert result == {"grounded": True, "match_ratio": 1.0, "method": "exact"}


def test_honest_paraphrase_within_threshold_is_fuzzy():
    result = grounding(PARAPHRASE, DOC)
    assert result["grounded"] is True
    assert result["method"] == "fuzzy"
    assert result["match_ratio"] >= 0.9


def test_fabricated_quote_absent_is_not_grounded():
    result = grounding("the widget is waterproof to a depth of fifty meters", DOC)
    assert result["grounded"] is False
    assert result["method"] == "fuzzy"
    assert result["match_ratio"] < 0.9


def test_empty_quote_is_absent():
    absent = {"grounded": None, "match_ratio": 0.0, "method": "absent"}
    assert grounding("", DOC) == absent
    assert grounding("   ", DOC) == absent


def test_threshold_is_respected():
    assert grounding(PARAPHRASE, DOC, threshold=0.99)["grounded"] is False
    assert grounding(PARAPHRASE, DOC, threshold=0.5)["grounded"] is True


def test_unicode_punctuation_folds_to_exact():
    # Content carries curly quotes, an em-dash, and an NBSP; the claim quote is
    # plain ASCII. An honest quote must not read as fabricated over typography.
    lq, rq = chr(0x201c), chr(0x201d)  # curly double quotes
    dash, nbsp = chr(0x2014), chr(0x00a0)  # em-dash, non-breaking space
    content = f"Spec: {lq}peak current{dash}12{nbsp}A{rq} for 10 s."
    result = grounding('"peak current-12 A"', content)
    assert result["method"] == "exact"
    assert result["match_ratio"] == 1.0


def test_soft_hyphen_linebreak_rejoins_word():
    shy = chr(0x00ad)  # soft hyphen wrapping a line break in the source doc
    content = f"The war{shy}\nranty period is 24 months."
    result = grounding("warranty period", content)
    assert result["method"] == "exact"


def test_large_document_stays_fast():
    # 60k-word synthetic doc; the quote sits verbatim near the very end.
    filler = "lorem ipsum dolor sit amet consectetur " * 6000  # ~48k words
    needle = "the final clause pins the maximum torque at 42 newton meters"
    big = filler + " " + needle + " end."

    start = time.monotonic()
    exact = grounding(needle, big)
    assert exact["method"] == "exact" and exact["match_ratio"] == 1.0

    # A near-miss quote forces the fuzzy path; it must still return quickly
    # (anchored windows + the anchor-hit cap, never O(n^2) over the doc).
    fuzzy = grounding("the final clause pins peak torque at 42 newton meters", big)
    elapsed = time.monotonic() - start
    assert fuzzy["method"] in {"exact", "fuzzy"}
    assert elapsed < 2.0
