"""LlamaClient wire-shape tests over an httpx MockTransport.

The one thing worth pinning at this layer is the exact JSON the client puts on
the wire for llama-server's /v1/chat/completions endpoint — in particular the
``response_format`` shape that turns a JSON Schema into grammar-constrained
sampling, which llama-server's server README documents as
``response_format: {"type": "json_schema", "schema": {...}}``.
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
    # The verified llama-server shape: response_format wraps the schema.
    assert payload["response_format"] == {"type": "json_schema", "schema": schema}
    # The rest of the payload is unchanged.
    assert payload["cache_prompt"] is True
