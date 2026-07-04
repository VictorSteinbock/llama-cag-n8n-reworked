"""Tests for the framework-agnostic grounding gate.

No network and no cag-api needed: the gate takes a ``verify`` callable, so every
branch is driven by a scripted fake that returns exactly what ``POST /verify``
would. The final test is the headline demonstration — a stream of candidate
memory writes, showing the poisoned fact is quarantined instead of consolidated.
"""

from cag_gate import Action, GroundingGate, Policy, Verdict


# --- scripted oracles (mirror real POST /verify responses) -------------------

def v_supported(claim, document_id=None):
    return {
        "verdict": "supported",
        "quote": "the peak current limit is 12 A",
        "quote_grounded": True,
        "match_ratio": 1.0,
        "grounding_method": "exact",
    }


def v_fabricated_quote(claim, document_id=None):
    # The model claims support but the quote is NOT in the source (quote_grounded False).
    return {
        "verdict": "supported",
        "quote": "a passage that does not exist in the document",
        "quote_grounded": False,
        "match_ratio": 0.31,
        "grounding_method": "fuzzy",
    }


def v_contradicted(claim, document_id=None):
    return {"verdict": "contradicted", "quote": "requests are capped at 100/s", "quote_grounded": True}


def v_absent(claim, document_id=None):
    return {"verdict": "absent", "quote": "", "quote_grounded": None}


def v_raises(claim, document_id=None):
    raise ConnectionError("connection refused")


# --- the fail-safe matrix ----------------------------------------------------

def test_supported_with_grounded_quote_is_trusted():
    d = GroundingGate(verify=v_supported).gate_memory_write("The peak current limit is 12 A")
    assert d.action is Action.ALLOW
    assert d.trusted is True
    assert "supported by the canon" in d.reason


def test_fabricated_quote_is_never_trusted():
    gate = GroundingGate(verify=v_fabricated_quote)
    # A "supported" verdict whose quote is fabricated must not pass — caught mechanically.
    assert gate.gate_memory_write("anything").action is Action.QUARANTINE
    assert gate.gate_action("anything").action is Action.BLOCK
    assert gate.gate_memory_write("anything").tag == "fabricated-quote"
    assert gate.gate_memory_write("anything").trusted is False


def test_contradicted_blocks_action_and_quarantines_memory():
    gate = GroundingGate(verify=v_contradicted)
    assert gate.gate_action("The rate limit is 1000/s").action is Action.BLOCK
    mem = gate.gate_memory_write("The rate limit is 1000/s")
    assert mem.action is Action.QUARANTINE
    assert mem.tag == "contradicts-canon"
    assert mem.trusted is False


def test_absent_quarantines_memory_but_escalates_action():
    gate = GroundingGate(verify=v_absent)
    assert gate.gate_memory_write("Undocumented claim").action is Action.QUARANTINE
    assert gate.gate_action("Undocumented claim").action is Action.ESCALATE


def test_strict_policy_drops_absent_memory():
    gate = GroundingGate(verify=v_absent, policy=Policy.strict())
    assert gate.gate_memory_write("Undocumented claim").action is Action.BLOCK


def test_oracle_unavailable_fails_closed():
    gate = GroundingGate(verify=v_raises)
    mem = gate.gate_memory_write("The rate limit is 100/s")
    act = gate.gate_action("The rate limit is 100/s")
    assert mem.action is Action.QUARANTINE and mem.tag == "unverified"
    assert act.action is Action.ESCALATE
    assert "oracle unavailable" in mem.reason
    assert mem.trusted is False  # never silently trusted when the oracle is down


def test_empty_claim_is_blocked_without_calling_oracle():
    called = []

    def spy(claim, document_id=None):
        called.append(claim)
        return v_supported(claim)

    assert GroundingGate(verify=spy).gate_memory_write("   ").action is Action.BLOCK
    assert called == []  # short-circuited; no oracle call for an empty claim


def test_unknown_verdict_fails_closed():
    gate = GroundingGate(verify=lambda c, document_id=None: {"verdict": "maybe"})
    assert gate.gate_memory_write("x").action is Action.QUARANTINE
    assert gate.gate_action("x").action is Action.ESCALATE


# --- the evidence floor: existence is not sufficiency -------------------------

def v_generic_quote(claim, document_id=None):
    # External-audit scenario: a false claim "supported" by a fragment so generic
    # it exists in ANY document — the byte-check grounds it (exact, 1.0), so only
    # the evidence floor stands between this and an auto-pass.
    return {
        "verdict": "supported",
        "quote": "is the",
        "quote_grounded": True,
        "match_ratio": 1.0,
        "grounding_method": "exact",
    }


def test_generic_grounded_quote_is_not_evidence():
    gate = GroundingGate(verify=v_generic_quote)
    mem = gate.gate_memory_write("The rate limit is 10,000 req/s")
    assert mem.action is Action.QUARANTINE and mem.tag == "quote-too-generic"
    assert mem.trusted is False
    assert gate.gate_action("The rate limit is 10,000 req/s").action is Action.ESCALATE


def test_supported_with_empty_quote_is_not_trusted():
    # Empty quote -> grounding() reports quote_grounded None (not False), so the
    # fabricated-quote branch never fires — the floor must close this hole.
    gate = GroundingGate(
        verify=lambda c, document_id=None: {
            "verdict": "supported", "quote": "", "quote_grounded": None,
        }
    )
    d = gate.gate_memory_write("anything")
    assert d.action is Action.QUARANTINE and d.tag == "quote-too-generic"
    assert d.trusted is False


def test_zero_width_padding_does_not_beat_the_floor():
    padded = "i\u200bs\u200b \u200b\u200bt\u200bh\u200be\ufeff\ufeff"
    gate = GroundingGate(
        verify=lambda c, document_id=None: {
            "verdict": "supported", "quote": padded, "quote_grounded": True,
        }
    )
    assert gate.gate_memory_write("x").tag == "quote-too-generic"


def test_evidence_floor_can_be_disabled():
    gate = GroundingGate(verify=v_generic_quote, policy=Policy(min_grounded_quote_chars=0))
    assert gate.gate_memory_write("x").action is Action.ALLOW


# --- "absent" corroboration: the oracle's recall probe feeds the gate ---------

def v_absent_with_recall(overlap):
    def oracle(claim, document_id=None):
        return {
            "verdict": "absent", "quote": "", "quote_grounded": None,
            "recall": {
                "max_overlap": overlap,
                "checked_tokens": 4,
                "excerpt": "the warranty period is 24 months" if overlap else None,
            },
        }
    return oracle


def test_absent_but_topic_present_escalates_and_never_stores():
    # The canon clearly discusses this vocabulary, yet the oracle said absent:
    # a possible missed passage or twisted claim. Escalate memory AND action —
    # this must never ride the quarantine/episodic path.
    gate = GroundingGate(verify=v_absent_with_recall(0.8))
    mem = gate.gate_memory_write("The warranty period is 36 months")
    assert mem.action is Action.ESCALATE
    assert mem.tag == "absent-but-topic-present"
    assert mem.trusted is False
    assert gate.gate_action("The warranty period is 36 months").action is Action.ESCALATE


def test_absent_with_near_zero_recall_is_corroborated():
    gate = GroundingGate(verify=v_absent_with_recall(0.0))
    mem = gate.gate_memory_write("Undocumented claim")
    assert mem.action is Action.QUARANTINE and mem.tag == "unverified"
    assert "corroborates" in mem.reason  # the number is in the audit trail


def test_absent_without_recall_field_behaves_as_before():
    # Older oracle (no recall field): decisions and reasons stay exactly as they were.
    mem = GroundingGate(verify=v_absent).gate_memory_write("Undocumented claim")
    assert mem.action is Action.QUARANTINE and mem.tag == "unverified"
    assert "corroborates" not in mem.reason


def test_absent_recall_check_can_be_disabled():
    gate = GroundingGate(
        verify=v_absent_with_recall(0.9), policy=Policy(absent_recall_overlap=None)
    )
    assert gate.gate_memory_write("x").action is Action.QUARANTINE


def test_document_id_is_forwarded():
    seen = {}

    def spy(claim, document_id=None):
        seen["doc"] = document_id
        return v_supported(claim)

    GroundingGate(verify=spy, document_id=7).gate_memory_write("hi")
    assert seen["doc"] == 7


def test_verdict_from_non_dict_is_unavailable():
    v = Verdict.from_response("claim", ["not", "a", "dict"])
    assert v.error is not None and v.verdict is None


# --- headline: the compounding loop is broken at the write interface ---------

def _canon(claim, document_id=None):
    """A tiny canon: the true rate limit is 100/s; websockets are not mentioned."""
    c = claim.lower()
    # Order matters: "1000" contains "100", so test the contradiction first.
    if "rate limit" in c and "1000" in c:
        return {"verdict": "contradicted", "quote": "requests are capped at 100/s (Sec 4.2)",
                "quote_grounded": True}
    if "rate limit" in c and "100" in c:
        return {"verdict": "supported", "quote": "requests are capped at 100/s (Sec 4.2)",
                "quote_grounded": True}
    return {"verdict": "absent", "quote": "", "quote_grounded": None}


def _curate(gate, candidate_facts):
    """Split a stream of would-be memory writes into clean memory vs quarantine."""
    memory, quarantine = [], []
    for fact in candidate_facts:
        decision = gate.gate_memory_write(fact)
        (memory if decision.trusted else quarantine).append(fact)
    return memory, quarantine


def test_memory_write_gate_quarantines_the_poison():
    gate = GroundingGate(verify=_canon)
    stream = [
        "The API rate limit is 100 req/s",     # supported  -> enters memory
        "The API rate limit is 1000 req/s",    # contradicted (the hallucination) -> quarantined
        "The API supports websockets",         # absent -> quarantined (cannot be grounded)
    ]
    memory, quarantine = _curate(gate, stream)

    # Only the grounded fact is consolidated; the poison never enters memory, so it
    # can never be retrieved-and-reinforced on a later turn. Drift loop broken.
    assert memory == ["The API rate limit is 100 req/s"]
    assert "The API rate limit is 1000 req/s" in quarantine
    assert "The API supports websockets" in quarantine
