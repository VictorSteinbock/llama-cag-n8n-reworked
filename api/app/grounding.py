"""Mechanical quote-grounding for POST /verify (stdlib only).

The oracle can answer ``supported`` while *fabricating* the citation. Grounding
closes that gap without a second LLM call: it checks whether the model's returned
quote actually occurs in the source document.

Two tiers:

1. **Exact** -- after normalization the quote is a substring of the document.
   Normalization folds the typography real documents carry (curly quotes,
   en/em-dashes, NBSP, soft-hyphenated line breaks, zero-width chars) so an
   *honest* verbatim quote is never flagged as fabricated over a stylistic
   apostrophe. O(n) fast path.
2. **Fuzzy** -- an honest paraphrase (a word swapped, whitespace reflowed) still
   grounds if its best difflib ratio against an anchored window clears the
   threshold. Anchored windows keep this near-linear on a 60k-token document
   instead of O(n^2) over every offset.

This verifies the quote's *existence*, not the claim's *entailment*: it hardens
``supported``/``contradicted`` (a passage exists to check) but cannot harden
``absent`` (there is nothing to look for).
"""

import re
import unicodedata
from difflib import SequenceMatcher

# Escapes (not literal glyphs) for every non-visible codepoint so the source
# stays reviewable and can't be silently mangled: U+00AD soft hyphen,
# U+200B/C/D + U+FEFF zero-widths, U+00A0 NBSP, U+2018/19 curly single quotes,
# U+201C/D curly double quotes, U+2013/14 en/em dash.
_WS = re.compile(r"\s+")
_SHY = re.compile(r"\u00ad\s*")  # soft hyphen + the line break it wraps
_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))
_PUNCT = str.maketrans(
    {
        "\u2019": "'", "\u2018": "'",  # curly single quotes
        "\u201c": '"', "\u201d": '"',  # curly double quotes
        "\u2013": "-", "\u2014": "-",  # en / em dash
        "\u00a0": " ",                 # non-breaking space
    }
)

# A quote sharing no >=4-char word with the document is not grounded, so anchor
# scanning is bounded: at most this many content matches are ever windowed.
_ANCHOR_HIT_CAP = 400


def _normalize(text: str) -> str:
    """Fold typography so honest quotes match. Order is load-bearing: NFKC, then
    rejoin soft-hyphen line breaks and drop zero-widths, then the punctuation
    fold, and only then whitespace collapse + casefold."""
    text = unicodedata.normalize("NFKC", text)
    text = _SHY.sub("", text).translate(_ZW).translate(_PUNCT)
    return _WS.sub(" ", text).strip().casefold()


def _best_fuzzy_ratio(nq: str, nc: str) -> float:
    """Best similarity of the (normalized) quote against any anchored window of
    the (normalized) content. Windows are seeded at content words that match a
    distinct >=4-char word of the quote, so cost scales with the number of anchor
    hits (capped), not with document length.

    Scoring trims to the matched span, not the raw window: a difflib ratio over
    the whole (deliberately over-sized) window would dilute even a tight
    paraphrase with the window's non-matching padding, capping it near 0.8. We
    take difflib's matching blocks and score ``2*matched / (len(quote) +
    matched_content_span)`` -- padding outside the aligned region never counts
    against a quote that is genuinely present."""
    qlen = len(nq)
    q_words = re.findall(r"\S+", nq)
    anchors = {w for w in q_words if len(w) >= 4} or set(q_words)
    if not anchors:
        return 0.0

    starts: list[int] = []
    for match in re.finditer(r"\S+", nc):
        if match.group() in anchors:
            starts.append(max(0, match.start() - qlen // 4))
            if len(starts) >= _ANCHOR_HIT_CAP:
                break
    if not starts:
        return 0.0

    # Thin near-identical windows: keep one per ~quarter-quote of advance so
    # clustered anchors don't re-score the same region repeatedly.
    step = max(1, qlen // 4)
    span = qlen + qlen // 2
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq1(nq)
    best = 0.0
    last = None
    for start in sorted(set(starts)):
        if last is not None and start - last < step:
            continue
        last = start
        matcher.set_seq2(nc[start : min(len(nc), start + span)])
        blocks = [b for b in matcher.get_matching_blocks() if b.size]
        if not blocks:
            continue
        matched = sum(b.size for b in blocks)
        content_span = blocks[-1].b + blocks[-1].size - blocks[0].b
        score = 2 * matched / (qlen + content_span)
        if score > best:
            best = score
            if best >= 0.999:
                break
    return best


def grounding(quote: str, content: str, *, threshold: float = 0.9) -> dict:
    """Does ``quote`` occur in ``content``?

    Returns ``{"grounded": bool | None, "match_ratio": float, "method": str}``:

    - empty/whitespace quote -> ``grounded=None``, ``method="absent"`` (nothing
      to check -- used for the ``absent`` verdict, which grounding cannot harden);
    - exact normalized substring -> ``grounded=True``, ``match_ratio=1.0``,
      ``method="exact"``;
    - otherwise the best anchored-window difflib ratio -> ``method="fuzzy"``,
      ``grounded`` iff that ratio ``>= threshold``.
    """
    if not quote or not quote.strip():
        return {"grounded": None, "match_ratio": 0.0, "method": "absent"}
    nq = _normalize(quote)
    nc = _normalize(content)
    if not nq:
        return {"grounded": None, "match_ratio": 0.0, "method": "absent"}
    if nq in nc:
        return {"grounded": True, "match_ratio": 1.0, "method": "exact"}
    best = _best_fuzzy_ratio(nq, nc)
    return {"grounded": best >= threshold, "match_ratio": round(best, 4), "method": "fuzzy"}
