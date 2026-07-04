"""Unit tests for api/app/grounding.py — pure function, no fakes.

Special codepoints are built via chr() so this file stays pure ASCII and states
exactly which character it exercises (soft hyphen, NBSP, curly quotes, em-dash).
"""

import time

from app.grounding import _normalize, grounding

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


# --- regression: bugs found in the post-build code review --------------------

def test_fuzzy_match_survives_repeated_common_words():
    # A quote's common words repeat 1000x before its real location; the fuzzy
    # path must still find the match (rarest-anchor seeding), not miss it because
    # an in-document-order cap filled up on the earlier repetitions.
    common = "The widget shall comply with the widget standard. " * 500
    doc = common + " the thermal cutoff engages at 55 degrees celsius precisely. end."
    result = grounding("thermal cutoff engages at 55 degrees celcius", doc)  # 1-char drift -> fuzzy
    assert result["method"] == "fuzzy"
    assert result["grounded"] is True
    assert result["match_ratio"] >= 0.9


def test_soft_hyphen_not_at_linebreak_keeps_adjacent_space():
    # A soft hyphen that is NOT wrapping a line must be dropped WITHOUT eating a
    # following real space (else two words glue together and a quote corrupts).
    shy = chr(0x00ad)
    content = f"They agreed to co{shy} operate on the project."
    # "co operate" must stay two words; a quote for "co operate" grounds exact.
    result = grounding("co operate", content)
    assert result["method"] == "exact"
    assert _normalize(f"co{shy} operate") == "co operate"
