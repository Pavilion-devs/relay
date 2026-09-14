"""Persisted, pre-call cost reservations. No refund on unknown provider outcomes."""

import math
import os
import threading

from botocore.config import Config
from fastapi import HTTPException
from strands.models import BedrockModel

from relay_core.agent import interpret

MODEL = "us.anthropic.claude-sonnet-4-6"
CAP_MICRO_USD = int(os.environ.get("RELAY_DEMO_AI_CAP_MICRO_USD", "1500000"))
if not 0 < CAP_MICRO_USD <= 6_500_000:
    raise ValueError("Public AI allocation outside authorized configuration range")
inference_lock = threading.Lock()


def reserve(store, amount, input_tokens):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS public_ai_budget (id INTEGER PRIMARY KEY, reserved INTEGER NOT NULL, calls INTEGER NOT NULL)"
        )
        db.execute("INSERT OR IGNORE INTO public_ai_budget VALUES (1,0,0)")
        changed = db.execute(
            "UPDATE public_ai_budget SET reserved=reserved+?, calls=calls+1 WHERE id=1 AND reserved+?<=?",
            (amount, amount, CAP_MICRO_USD),
        ).rowcount
        if not changed:
            raise HTTPException(
                429,
                "The shared live AI allowance is used up. Structured controls remain available.",
            )


class CountedClient:
    def __init__(self, client, store):
        self.client, self.store, self.called = client, store, False

    def converse(self, **request):
        if self.called:
            raise RuntimeError("One model attempt per message")
        self.called = True
        if set(request) - {"modelId", "messages", "system", "inferenceConfig"}:
            raise ValueError("Unexpected inference options")
        if request["modelId"] != MODEL or request["inferenceConfig"]["maxTokens"] != 1000:
            raise ValueError("Unexpected model or output limit")
        count = self.client.count_tokens(
            modelId="anthropic.claude-sonnet-4-6",
            input={"converse": {k: request[k] for k in ("messages", "system") if k in request}},
        )["inputTokens"]
        if type(count) is not int or not 0 < count <= 10000:
            raise ValueError("Prompt exceeds bounded context")
        # Conservative $6/$30 per million tokens, including output maximum and input margin.
        reserve(self.store, math.ceil((count + 256) * 6 + 1000 * 30), count)
        return self.client.converse(**request)


def run(store, scoped, workspace, mid):
    if not inference_lock.acquire(blocking=False):
        raise HTTPException(429, "Another interpretation is running. Try again in a moment.")
    try:
        model = BedrockModel(
            model_id=MODEL,
            region_name="us-east-1",
            max_tokens=1000,
            temperature=0,
            streaming=False,
            boto_client_config=Config(
                retries={"total_max_attempts": 1}, read_timeout=60, connect_timeout=10
            ),
        )
        model.client = CountedClient(model.client, store)
        metrics = {}
        reply = interpret(scoped, workspace, mid, model=model, metrics=metrics)
        return reply, {
            k: metrics[k] for k in ("model_calls", "latency_seconds", "usage") if k in metrics
        }
    finally:
        inference_lock.release()
