from types import SimpleNamespace

import pytest

from scripts.evaluate_claude import MODEL, CountedClient


def request():
    return {
        "modelId": MODEL,
        "messages": [{"role": "user", "content": [{"text": "test"}]}],
        "inferenceConfig": {"maxTokens": 1000},
    }


def test_reservation_precedes_single_inference():
    events = []
    client = SimpleNamespace(
        count_tokens=lambda **kw: {"inputTokens": 1500},
        converse=lambda **kw: events.append("call") or {},
    )
    wrapper = CountedClient(client, lambda cost, count: events.append((cost, count)))
    wrapper.converse(**request())
    assert events == [(0.040536, 1500), "call"]
    with pytest.raises(RuntimeError):
        wrapper.converse(**request())
    assert len(events) == 2


def test_failed_reservation_never_invokes():
    def reserve(*args):
        raise RuntimeError("Cap")

    client = SimpleNamespace(
        count_tokens=lambda **kw: {"inputTokens": 1500},
        converse=lambda **kw: pytest.fail("Must not bill"),
    )
    with pytest.raises(RuntimeError):
        CountedClient(client, reserve).converse(**request())


def test_oversized_prompt_never_invokes():
    client = SimpleNamespace(
        count_tokens=lambda **kw: {"inputTokens": 10001},
        converse=lambda **kw: pytest.fail("Must not bill"),
    )
    with pytest.raises(ValueError):
        CountedClient(client, lambda *args: pytest.fail("No reserve")).converse(**request())
