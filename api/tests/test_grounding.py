"""Unit tests for api/app/grounding.py — pure function, no fakes.

Special codepoints are built via chr() so this file stays pure ASCII and states
exactly which character it exercises (soft hyphen, NBSP, curly quotes, em-dash).
"""

import time

from app.grounding import _normalize, grounding, recall_probe

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


# --- F17: recall_probe (mechanical corroboration for "absent") ---------------

def test_recall_probe_zero_overlap_corroborates_absent():
    # A claim whose vocabulary the document never uses: absent is backed by 0.0.
    result = recall_probe("the software license forbids commercial redistribution", DOC)
    assert result["max_overlap"] == 0.0
    assert result["excerpt"] is None
    assert result["checked_tokens"] == 5


def test_recall_probe_high_overlap_flags_twisted_topic():
    # The claim is false (warranty is 24 months, not 36) so a good oracle says
    # absent/contradicted — but its VOCABULARY is right there. High overlap must
    # flag that "absent" deserves escalation, and the excerpt locates the region.
    result = recall_probe("the warranty period is 36 months", DOC)
    assert result["max_overlap"] == 0.75  # warranty, period, months hit; "36" does not
    assert "warranty" in result["excerpt"]


def test_recall_probe_numbers_count_as_signal():
    # Bare numbers are kept as tokens ("40" here); short words ("is", "to") are not.
    result = recall_probe("the maximum charge current is 40 amperes", DOC)
    assert result["checked_tokens"] == 5  # maximum, charge, current, amperes, 40
    assert result["max_overlap"] == 0.4   # charge + 40 occur; the rest do not


def test_recall_probe_too_little_signal_is_inconclusive():
    # One distinctive token (or none) measures nothing: None, not 0.0 — the gate
    # must treat this as inconclusive rather than as corroboration.
    assert recall_probe("Warranty?", DOC)["max_overlap"] is None
    assert recall_probe("is it ok", DOC)["max_overlap"] is None


def test_recall_probe_stopwords_do_not_inflate():
    result = recall_probe("this is about that which have been there", DOC)
    assert result["checked_tokens"] == 0
    assert result["max_overlap"] is None


def test_recall_probe_unsegmented_scripts_are_inconclusive_not_corroborated():
    # CJK has no spaces, so token equality across differently-phrased clauses
    # never fires; zero hits there must read as "cannot measure" (None), never
    # as a confident 0.0 the gate would treat as corroboration.
    cjk_claim = (
        "".join(chr(c) for c in (0x4FDD, 0x8A3C, 0x671F, 0x9593))
        + " "
        + "".join(chr(c) for c in (0x4E09, 0x5E74, 0x9593, 0x3067))
    )
    cjk_doc = "".join(chr(c) for c in (0x88FD, 0x54C1, 0x306F, 0x4E38, 0x5E74, 0x4FDD))
    result = recall_probe(cjk_claim, cjk_doc)
    assert result["max_overlap"] is None  # inconclusive, not corroborated
    # Latin claims with genuinely alien vocabulary still corroborate with 0.0.
    assert recall_probe("orbital launch trajectory telemetry", DOC)["max_overlap"] == 0.0


def test_recall_probe_scores_co_occurrence_not_bag_of_words():
    # Both topic words exist in the document but thousands of characters apart:
    # no single window holds them together, so the score reflects the best
    # window (2 of 3 tokens), not a document-wide bag-of-words match.
    filler = "pad " * 2000  # ~8k chars; "pad" is <4 chars, never a token
    doc = "alpha subsystem overview. " + filler + " beta subsystem details."
    result = recall_probe("alpha beta subsystem", doc)
    assert result["max_overlap"] == round(2 / 3, 4)
