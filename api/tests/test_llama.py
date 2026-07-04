"""LlamaClient wire-shape tests over an httpx MockTransport.

The one thing worth pinning at this layer is the exact JSON the client puts on
the wire for llama-server's /v1/chat/completions endpoint — in particular the
``response_format`` shape that turns a JSON Schema into grammar-constrained
sampling. llama-server's OpenAI-compat parser reads the schema from the NESTED
``json_schema`` object (``{"type": "json_schema", "json_schema": {"schema":
{...}}}``); a top-level ``schema`` key beside ``type: json_schema`` is silently
ignored and leaves sampling unconstrained — the exact bug this pin prevents.
"""

import httpx

from app.llama import LlamaClient

_COMPLETION = {
    "choices": [{"message": {"content": '{"verdict": "supported"}'}}],
    "timings": {"prompt_n": 5, "cache_n": 400, "predicted_n": 8},
    "usage": {"prompt_tokens": 405},
}


def _client_capturing(sink: list[httpx.Request]) -> LlamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(request)
        return httpx.Response(200, json=_COMPLETION)

    client = LlamaClient("http://llama-test:8080")
    client._client = httpx.Client(
        base_url="http://llama-test:8080", transport=httpx.MockTransport(handler)
    )
    return client


def test_chat_without_schema_omits_response_format():
    sink: list[httpx.Request] = []
    client = _client_capturing(sink)

    client.chat([{"role": "user", "content": "hi"}], max_tokens=8, temperature=0.0)

    import json

    payload = json.loads(sink[-1].content)
    assert "response_format" not in payload  # unchanged behaviour when no schema


def test_chat_with_schema_sends_json_schema_response_format():
    sink: list[httpx.Request] = []
    client = _client_capturing(sink)
    schema = {
        "type": "object",
        "properties": {
            "claim": {"type": "string"},
            "verdict": {"enum": ["supported", "absent", "contradicted"]},
            "quote": {"type": "string"},
        },
        "required": ["claim", "verdict", "quote"],
    }

    client.chat(
        [{"role": "user", "content": "verify"}],
        max_tokens=64,
        temperature=0.0,
        json_schema=schema,
    )

    import json

    payload = json.loads(sink[-1].content)
    # The OpenAI-wrapper shape llama-server actually parses: the schema rides
    # INSIDE the nested json_schema object, never as a top-level sibling key.
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"schema": schema},
    }
    # The rest of the payload is unchanged.
    assert payload["cache_prompt"] is True
