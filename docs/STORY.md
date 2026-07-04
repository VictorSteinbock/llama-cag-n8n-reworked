# A CAG-based validation protocol. Maybe. Help me find out.

Hi. I'm a hobbyist, not a professional in this field. I work remotely on a
dated laptop and build this in my spare time. A year ago I tried the same idea
and abandoned it half-built. This is the second attempt, and honestly I've
spent more time sharpening the axe than chopping with it. Before I sink another
year in, I want people who know more than me to tell me where the thinking is
wrong.

One thing to say upfront: I leaned on Claude Code heavily to build this. The
direction, the decisions and the mistakes are mine, but a lot of the code was
written with it and reviewed by me to the level I'm able to. Judge accordingly.

## The problems I think this addresses, on some level

I'm deliberately saying "I think" and "on some level". I'm not confident this
is even the right approach. But these are the three itches:

1. **Paying for the same read over and over.** One dense manual, a hundred
   questions. Every setup I tried re-reads the document per question, in time
   or in tokens. The document didn't change, so why does the work repeat?
2. **Invented citations.** A model will say "supported, see section 4.2" and
   section 4.2 says no such thing. Checking that quote is string matching, not
   intelligence. It felt like something code should do, not vibes.
3. **Agent memory drift.** Self-improving agents (OpenClaw, Hermes) write their
   own memory. One hallucinated fact gets stored, retrieved, reinforced, and
   there's no ground truth anywhere in that loop to catch it.

## What I built

The mechanism: llama.cpp reads your document once, and the model's internal
state (the KV cache) is saved to disk. Every later question restores that
state, so only the question and the answer are ever computed.

<p align="center">
  <img src="images/demo-60s.svg" alt="Three steps: pin the document once, your agent asks a tens-of-tokens question, a grounded answer arrives with the exact quoted line. Receipt: evaluated 38 of 41,772 tokens, 590 ms, from memory." width="100%">
</p>

The part I actually care about sits on top, the validation step. Send a claim,
get a verdict at temperature 0 with the supporting quote, and then plain code
checks byte-for-byte that the quote really occurs in the source. A fabricated
citation fails mechanically, no second model call. That feeds a fail-closed
gate: only "supported with a real, non-trivial quote" is ever trusted, and
everything else (contradicted, absent, fabricated, oracle down) gets
quarantined or escalated. The gate is what I bolt into agent loops:

<p align="center">
  <img src="images/grounding-gate.svg" alt="An agent's feedback loop with corruptible memory. A candidate fact passes a write-validation gate that consults a canon pinned outside the loop; supported facts enter memory, everything else is quarantined." width="100%">
</p>

Five concrete setups, one page each with their limits printed on the slide, are
in the [use-case deck](USE-CASES.md).

## Resource reality, plainly

The saved state lives on disk, one file per document. To answer, it has to be
active in RAM: the server allocates a fixed block at startup (sized by your
context setting) and holds the model weights next to it, always. A cold
document loads from disk into that block in seconds. More documents cost disk
only, never more RAM. The flip side: the default model (12B) plus a 64k
context wants more RAM than my laptop has, which is exactly why I need help
below.

## Tested vs not tested, honestly

Tested by me: the core loop (ingest, ask, restore after restart, self-heal when
cache files vanish), the quote-check mechanics, the model-switch guard, and the
gate logic. About 170 tests in CI, all against fakes.

Not tested: the lived experience with a serious model on serious hardware, the
Hermes and OpenClaw adapters against live agent installs, the GPU paths, and
calibration on a real large document. Built, reasoned about, reviewed, but not
run for real. That gap is the whole reason for this post.

## What I'm asking for

1. **Criticism of the logic, first and foremost.** Is a pinned whole-document
   KV cache a sane anchor for validating answers and agent memory, or am I
   over-engineering something retrieval already solves? Where do the evidence
   rules (byte-checked quotes, minimum quote length, fail-closed defaults)
   break?
2. **A big-RAM volunteer.** If you have a 32 to 128 GB machine and some
   curiosity, the [live verification script](IMPLEMENTATION_PLAN.md) is
   written. Run it, tell me what breaks.
3. **Prior art I missed.** If this already exists and I reinvented a worse
   wheel, point me at the better one. Genuinely.

## If you want to poke at it

Docker plus three commands: `python llamacag.py setup`, `start`, drop a file in
`./documents`. Bundled samples let you see it work in a minute. If you use
Claude Code, there's a [paste-one-prompt setup](SETUP.md#let-claude-code-do-it)
that does the whole install for you. The [README](../README.md) has a
60-second visual demo at the top.

Be blunt. I'd rather be embarrassed now than a year further in.
