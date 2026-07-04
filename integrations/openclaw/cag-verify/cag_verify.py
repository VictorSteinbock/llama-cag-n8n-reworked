#!/usr/bin/env python3
"""cag-verify: fail-safe fact check against a cag-api canon (standard library only).

Usage:  cag_verify.py "<claim>" [--document-id N]
Env:    CAG_API_URL   (default http://localhost:8000)
Prints one JSON line: {trusted, action, reason, verdict, quote, quote_grounded, conditions}
Exit:   0 = trusted (supported by the canon, with a grounded quote)
        1 = NOT trusted — do not store as a fact or send as true

This is a standalone port of cag_gate.GroundingGate (the unit-tested reference in
the llama-cag-n8n repo) so the skill can ship on ClawHub with no extra installs.
It is deliberately conservative: anything that is not 'supported' with a grounded
quote of meaningful length comes back trusted=false.
"""

import argparse
import json
import os
import sys
import urllib.request

# Existence is not sufficiency: a generic fragment ("is the") occurs in any
# document and grounds trivially, and an empty quote has quote_grounded null.
# A supported quote shorter than this — zero-width chars (U+200B/C/D, U+FEFF)
# stripped, whitespace collapsed — is never treated as evidence.
MIN_QUOTE_CHARS = 12
_ZERO_WIDTH = dict.fromkeys((0x200B, 0x200C, 0x200D, 0xFEFF))


def _evidence_len(quote):
    return len(" ".join((quote or "").translate(_ZERO_WIDTH).split()))


def verify(claim, document_id, base_url, timeout=60.0):
    payload = {"claim": claim}
    if document_id is not None:
        payload["document_id"] = document_id
    req = urllib.request.Request(
        base_url.rstrip("/") + "/verify",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def decide(data):
    """Map a /verify response to (action, reason). Fail closed."""
    verdict = data.get("verdict")
    quote = data.get("quote") or ""
    grounded = data.get("quote_grounded")
    if verdict == "supported" and grounded is False:
        return "block", "supported but the cited quote is not in the source (fabricated citation)"
    if verdict == "supported" and _evidence_len(quote) < MIN_QUOTE_CHARS:
        return "escalate", "supported but the cited quote is too short/generic to count as evidence"
    if verdict == "supported":
        return "allow", "supported by the canon" + (f': "{quote}"' if quote else "")
    if verdict == "contradicted":
        return "block", "contradicted by the canon" + (f': "{quote}"' if quote else "")
    if verdict == "absent":
        return "escalate", "not found in the canon (cannot be grounded)"
    return "escalate", f"unrecognized verdict: {verdict!r}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fact-check a claim against a cag-api canon.")
    parser.add_argument("claim", help="The factual claim to check.")
    parser.add_argument("--document-id", type=int, default=None, help="Which canon (default: latest).")
    args = parser.parse_args(argv)

    base_url = os.environ.get("CAG_API_URL", "http://localhost:8000")
    try:
        data = verify(args.claim, args.document_id, base_url)
    except Exception as exc:  # fail closed: unreachable oracle is never 'trusted'
        print(json.dumps({
            "trusted": False,
            "action": "escalate",
            "reason": f"oracle unavailable: {type(exc).__name__}: {exc}",
        }))
        return 1

    action, reason = decide(data)
    trusted = action == "allow"
    print(json.dumps({
        "trusted": trusted,
        "action": action,
        "reason": reason,
        "verdict": data.get("verdict"),
        "quote": data.get("quote", ""),
        "quote_grounded": data.get("quote_grounded"),
        "conditions": data.get("conditions", ""),
    }))
    return 0 if trusted else 1


if __name__ == "__main__":
    sys.exit(main())
