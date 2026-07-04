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
``supported``/``contradicted`` (a passage exists to check). ``absent`` has no
quote to check at all — for that, :func:`recall_probe` runs the machinery in
reverse: it scans the *claim's* vocabulary against the document so an "absent"
verdict is at least corroborated (or challenged) by an auditable number instead
of resting on the model's say-so alone.
"""

import re
import unicodedata
from difflib import SequenceMatcher

# Escapes (not literal glyphs) for every non-visible codepoint so the source
# stays reviewable and can't be silently mangled: U+00AD soft hyphen,
# U+200B/C/D + U+FEFF zero-widths, U+00A0 NBSP, U+2018/19 curly single quotes,
# U+201C/D curly double quotes, U+2013/14 en/em dash.
_WS = re.compile(r"\s+")
# A soft hyphen (U+00AD) marks a hyphenation point. When it wraps a line it is
# followed by a newline (and maybe indentation): rejoin by deleting the whole
# run. A soft hyphen NOT at a line break is invisible and must be dropped
# WITHOUT eating an adjacent real space (else "co<shy> operate" -> "cooperate").
_SHY_WRAP = re.compile(r"\u00ad[ \t]*\r?\n[ \t]*")
_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))
_PUNCT = str.maketrans(
    {
        "\u2019": "'", "\u2018": "'",  # curly single quotes
        "\u201c": '"', "\u201d": '"',  # curly double quotes
        "\u2013": "-", "\u2014": "-",  # en / em dash
        "\u00a0": " ",                 # non-breaking space
    }
)

# Cost bound: seed windows from at most this many candidate offsets, and score at
# most this many distinct windows. Seeds are drawn from the RAREST anchor words
# first (see _best_fuzzy_ratio), so a quote whose common words repeat thousands of
# times before its real location is still found \u2014 the cap never skips the match by
# document position, only ever trims genuinely redundant windows.
_SEED_CAP = 800
_WINDOW_CAP = 800


def _normalize(text: str) -> str:
    """Fold typography so honest quotes match. Order is load-bearing: NFKC, then
    rejoin soft-hyphen line breaks and drop zero-widths, then the punctuation
    fold, and only then whitespace collapse + casefold."""
    text = unicodedata.normalize("NFKC", text)
    text = _SHY_WRAP.sub("", text)  # rejoin soft-hyphenated line breaks
    text = text.replace("\u00ad", "")  # drop any remaining bare soft hyphen, keep spacing
    text = text.translate(_ZW).translate(_PUNCT)
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

    # Index every occurrence of each anchor word in one linear pass.
    hits: dict[str, list[int]] = {}
    for match in re.finditer(r"\S+", nc):
        word = match.group()
        if word in anchors:
            hits.setdefault(word, []).append(match.start())
    if not hits:
        return 0.0

    # Seed from the RAREST anchor words first: a genuine quote's distinctive words
    # appear only a handful of times (once at the real match), so seeding on them
    # targets that location instead of drowning in a common word's occurrences.
    # This is the fix for the document-order cap that could skip a present quote
    # located after many earlier occurrences of its common words.
    starts: list[int] = []
    for word in sorted(hits, key=lambda w: len(hits[w])):
        starts.extend(hits[word])
        if len(starts) >= _SEED_CAP:
            break
    starts = [max(0, offset - qlen // 4) for offset in starts]

    # Thin near-identical windows: keep one per ~quarter-quote of advance so
    # clustered anchors don't re-score the same region repeatedly.
    step = max(1, qlen // 4)
    span = qlen + qlen // 2
    thinned: list[int] = []
    last = None
    for start in sorted(set(starts)):
        if last is None or start - last >= step:
            thinned.append(start)
            last = start
    # Backstop: if a pathologically repetitive doc still yields too many windows,
    # sample them evenly across the whole document (never just the first N) so the
    # real match region is still represented.
    if len(thinned) > _WINDOW_CAP:
        stride = len(thinned) / _WINDOW_CAP
        thinned = [thinned[int(i * stride)] for i in range(_WINDOW_CAP)]

    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq1(nq)
    best = 0.0
    for start in thinned:
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
      to check -- used for the ``absent`` verdict, which grounding cannot harden;
      :func:`recall_probe` corroborates it instead);
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


# Content tokens for the recall probe: words of >=4 chars carry the topic
# signal; bare numbers of ANY length are kept because quantities ("40", "1000")
# are often the very thing a claim gets wrong. A small English stopword list
# keeps common glue words from inflating overlap — a heuristic aid, harmless
# for other languages (their glue words are simply not filtered).
_CONTENT_TOKEN = re.compile(r"\w{4,}|\d+")
_PROBE_STOPWORDS = frozenset(
    "that this with from have been being will would should could these those "
    "when where what which while there their them they your yours than then "
    "does must about into over under between because after before other only "
    "also some such more most each every here very much many both against".split()
)


def recall_probe(claim: str, content: str, *, window_chars: int = 400) -> dict:
    """Mechanical topic-recall probe for the ``absent`` verdict.

    ``grounding()`` can prove a quote exists; nothing can prove absence. This
    probe *corroborates* an ``absent`` verdict instead: it measures how much of
    the claim's distinctive vocabulary co-occurs inside any ~``window_chars``
    stretch of the (normalized) document.

    - ``max_overlap`` near 0.0 — the document never mentions these things:
      "absent" is backed by an auditable number, not just the model's say-so.
    - ``max_overlap`` high — the document DOES discuss this vocabulary
      somewhere; an "absent" verdict deserves escalation (the oracle may have
      missed a passage, or the claim twists a topic that is really there).

    Returns ``{"max_overlap": float | None, "checked_tokens": int,
    "excerpt": str | None}``. ``max_overlap`` is ``None`` when the claim has
    fewer than two distinctive tokens (nothing to measure — treat as
    inconclusive, not as corroboration). ``excerpt`` is the best-matching
    region of the *normalized* (lowercased) document — a locator hint for
    humans, never a quotable passage.
    """
    n_claim = _normalize(claim)
    n_content = _normalize(content)
    tokens = {t for t in _CONTENT_TOKEN.findall(n_claim) if t not in _PROBE_STOPWORDS}
    if len(tokens) < 2:
        return {"max_overlap": None, "checked_tokens": len(tokens), "excerpt": None}

    # One linear pass collects only the positions where a claim token occurs;
    # the sliding window then walks those hits (not the whole document), so
    # cost scales with the number of matches.
    hits = [
        (m.start(), m.group())
        for m in _CONTENT_TOKEN.finditer(n_content)
        if m.group() in tokens
    ]
    if not hits:
        return {"max_overlap": 0.0, "checked_tokens": len(tokens), "excerpt": None}

    best = 0
    best_lo = best_hi = 0
    counts: dict[str, int] = {}
    distinct = 0
    lo = 0
    for hi_pos, word in hits:
        counts[word] = counts.get(word, 0) + 1
        if counts[word] == 1:
            distinct += 1
        while hi_pos - hits[lo][0] > window_chars:
            lo_word = hits[lo][1]
            counts[lo_word] -= 1
            if counts[lo_word] == 0:
                distinct -= 1
            lo += 1
        if distinct > best:
            best, best_lo, best_hi = distinct, hits[lo][0], hi_pos
    excerpt = n_content[max(0, best_lo - 40) : best_hi + 60]
    if len(excerpt) > 240:
        excerpt = excerpt[:240]
    return {
        "max_overlap": round(best / len(tokens), 4),
        "checked_tokens": len(tokens),
        "excerpt": excerpt or None,
    }
