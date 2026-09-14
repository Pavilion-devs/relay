"""Offline protocol tests use the real Strands loop with a scripted model.

These prove SDK/tool integration, not language understanding or Bedrock access.
"""

import json
from uuid import uuid4

import pytest
from strands.models.model import Model

from relay_core.agent_legacy import interpret
from relay_core.engine import seed
from relay_core.store import Store


class ScriptedModel(Model):
    def __init__(self, target="tunde", draft=None):
        self.draft = draft
        self.calls = 0
        self.target = target

    def update_config(self, **kw):
        pass

    def get_config(self):
        return {"model_id": "test-script-only"}

    async def structured_output(self, *args, **kw):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.calls <= 2:
            name = "read_rescue" if self.calls == 1 else "suggest_change"
            values = (
                {}
                if self.calls == 1
                else self.draft
                if self.draft is not None
                else {
                    "resource_id": self.target,
                    "capacity": 160,
                    "conditional": True,
                    "evidence_quote": "I can take 20 crates if another driver takes the rest.",
                    "condition_text": "if another driver takes the rest",
                    "condition_kind": "remaining_load_covered",
                    "explanation": "20 crates, conditional on another driver.",
                }
            )
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"toolUse": {"toolUseId": f"tool-{self.calls}", "name": name}},
                }
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": json.dumps(values)}},
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"text": "Interpretation recorded for review."},
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}


def prepare(tmp_path):
    store = Store(str(tmp_path / "agent.sqlite3"))
    s = seed()
    mid = str(uuid4())
    s["messages"].append(
        {
            "id": mid,
            "resource_id": "tunde",
            "text": "I can take 20 crates if another driver takes the rest.",
        }
    )
    store.create(s)
    return store, s["id"], mid


def test_real_sdk_calls_tools_and_persists_review_draft(tmp_path):
    store, wid, mid = prepare(tmp_path)
    model = ScriptedModel()
    reply = interpret(store, wid, mid, model=model)
    s = store.read(wid)
    assert model.calls == 3
    assert "review" in reply
    assert s["suggestions"][0]["capacity"] == 160
    assert s["suggestions"][0]["conditional"] is True
    assert s["facts_version"] == 1
    assert next(r for r in s["resources"] if r["id"] == "tunde")["capacity"] == 192


def test_model_cannot_change_another_participants_resources(tmp_path):
    store, wid, mid = prepare(tmp_path)
    with pytest.raises(RuntimeError, match="did not record"):
        interpret(store, wid, mid, model=ScriptedModel(target="harbour"))
    assert store.read(wid)["suggestions"] == []
