"""Typed message extraction and deterministic normalization of source spans."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

QUESTIONS = {
    "review_required": "A coordinator needs to review the interpretation of this message before any update is proposed.",
    "missing_units": "Which units does your capacity use: kilograms or crates?",
    "ambiguous_quantity": "What is your single confirmed maximum capacity, including its unit?",
    "unclear_time": "What exact availability start or finish time do you mean, including its timezone?",
    "other_person": "Whose offer is this? Please ask that participant to confirm their own availability and capacity.",
    "uncertain_offer": "Can you confirm whether this is a firm offer, and state any conditions?",
    "unsupported_handling": "Which handling requirement must the coordinator confirm before this offer can be used?",
    "unsupported_travel": "Which travel-time change should the coordinator verify before replanning?",
    "no_actionable_update": "What change to your own capacity or availability should the coordinator review?",
    "invalid_extraction": "Please confirm your capacity with units, any time limits with a timezone, and any conditions.",
}
Reason = Literal[
    "review_required",
    "missing_units",
    "ambiguous_quantity",
    "unclear_time",
    "other_person",
    "uncertain_offer",
    "unsupported_handling",
    "unsupported_travel",
    "no_actionable_update",
    "invalid_extraction",
]


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["draft", "clarify"]
    reason: Reason | None = None
    quantity_text: str | None = Field(default=None, max_length=100)
    available: bool | None = None
    opens_text: str | None = Field(default=None, max_length=100)
    closes_text: str | None = Field(default=None, max_length=100)
    condition_text: str | None = Field(default=None, max_length=500)
    evidence_quote: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def consistent(self):
        fields = (
            self.quantity_text,
            self.available,
            self.opens_text,
            self.closes_text,
            self.condition_text,
        )
        if self.decision == "clarify":
            if self.reason is None or any(x is not None for x in fields):
                raise ValueError("Clarification needs a reason and no proposed facts")
        elif (
            self.reason is not None
            or not any(x is not None for x in fields)
            or not self.evidence_quote
        ):
            raise ValueError(
                "A draft needs evidence and explicit facts, without a clarification reason"
            )
        return self


class NormalizationError(ValueError):
    def __init__(self, message, reason="invalid_extraction"):
        super().__init__(message)
        self.reason = reason


def capacity_kg(span, kg_per_crate):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(kg|kilograms?|crates?)", span, re.IGNORECASE)
    if not match:
        raise NormalizationError("Copy a numeric amount with its explicit kg or crate unit")
    quantity = Decimal(match[1])
    if match[2].lower().startswith("crate"):
        if (
            quantity != quantity.to_integral_value()
            or type(kg_per_crate) is not int
            or kg_per_crate <= 0
        ):
            raise NormalizationError("Crates must be whole and have an explicit weight")
        quantity *= kg_per_crate
    if quantity != quantity.to_integral_value() or not 0 <= quantity <= 10000:
        raise NormalizationError("Capacity must resolve to a supported whole kilogram amount")
    return int(quantity)


def minute_offset(span, start):
    match = re.fullmatch(r"(?:minute\s+)(\d+)", span, re.IGNORECASE)
    if match:
        result = int(match[1])
    else:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(UTC|[+-]\d{2}:\d{2})", span, re.IGNORECASE)
        if not match:
            raise NormalizationError(
                "Time needs an explicit minute offset or timezone-qualified clock time"
            )
        try:
            anchor = datetime.fromisoformat(start)
            zone = "+00:00" if match[3].upper() == "UTC" else match[3]
            clock = datetime.fromisoformat(
                f"{anchor.date()}T{int(match[1]):02}:{match[2]}:00{zone}"
            )
            minutes = (clock - anchor).total_seconds() / 60
            if not minutes.is_integer():
                raise ValueError("Subminute boundary")
            result = int(minutes)
        except (ValueError, TypeError) as exc:
            raise NormalizationError("Invalid or ambiguous clock time") from exc
    if not 0 <= result <= 1440:
        raise NormalizationError("Time falls outside the rehearsal day")
    return result


def condition_kind(span):
    # Small explicit grammar: unsupported wording stays a manual dependency.
    # The model cannot certify arbitrary conditions as satisfied by plan acceptance.
    expression = r"(?:only\s+)?(?:if|provided(?:\s+that)?)\s+(?:another|a second)\s+driver\s+(?:takes|carries|can take|can carry)\s+(?:the\s+)?(?:rest|remaining load)\.?"
    return (
        "remaining_load_covered"
        if re.fullmatch(expression, span.strip(), re.IGNORECASE)
        else "manual_review"
    )


def require_condition_coverage(extraction, text):
    """Reject known conditional cues unless the complete suffix is preserved.

    This is an omission guard, not a general language parser. Requiring the
    suffix deliberately sends complicated or condition-first messages to
    manual review instead of certifying a shortened dependency.
    """
    cue = re.search(
        r"\b(?:if|provided(?:\s+that)?|unless|only\s+when|as\s+long\s+as|"
        r"subject\s+to|on\s+condition\s+that)\b",
        text,
        re.IGNORECASE,
    )
    if cue is None:
        return
    condition = extraction.condition_text
    if condition is None:
        raise NormalizationError("The source contains an unrepresented condition")
    # Consider every occurrence: a repeated fragment must not accidentally
    # cover an earlier, more restrictive condition.
    end = len(text.rstrip())
    # Ignore exactly one terminal sentence period, never ellipses or extra words.
    if text.rstrip().endswith(".") and not text.rstrip().endswith(".."):
        end -= 1
    covered = any(
        match.start() <= cue.start() and match.end() >= end
        for match in re.finditer(re.escape(condition), text)
    )
    if not covered:
        raise NormalizationError("Preserve the complete source after its condition cue")


def require_source_coverage(extraction, message, state):
    """Conservative checks for known semantic omissions, not a general language parser."""
    text = message["text"]
    # Inspect the whole source, not only the model-selected quote. This bounded
    # guard rejects known execution/override requests mixed with a valid patch;
    # it is not a general prompt-injection detector.
    if re.search(
        r"\bignore\b[^.!?\n]*\b(?:rules|instructions)\b|"
        r"\b(?:mark|set)\b[^.!?\n]*\b(?:rescue|deliveries|delivery)\b"
        r"[^.!?\n]*\b(?:complete|completed|delivered)\b|"
        r"\bapprove\s+(?:revision|every driver|all drivers)\b",
        text, re.IGNORECASE,
    ):
        raise NormalizationError(
            "Execution requests must not be extracted as participant patches",
            "no_actionable_update",
        )
    if re.search(
        r"\b(?:maybe|might|probably|not sure|not decided|have not decided|could possibly)\b",
        text,
        re.IGNORECASE,
    ):
        raise NormalizationError("The offer is explicitly uncertain", "uncertain_offer")
    sender = message.get("resource_id")
    for resource in state["resources"]:
        if resource["id"] != sender and re.match(
            r"\s*" + re.escape(resource["name"]) + r"\b", text, re.IGNORECASE
        ):
            raise NormalizationError(
                "The message starts with another participant's offer", "other_person"
            )

    quantity_pattern = r"(?<![\w.,+−-])\d+(?:\.\d+)?\s*(?:kilograms?|kg|crates?)\b"
    amounts = list(re.finditer(quantity_pattern, text, re.IGNORECASE))
    selected = extraction.quantity_text
    if amounts:
        if selected is None:
            raise NormalizationError("An explicit quantity was omitted", "ambiguous_quantity")
        if len(amounts) == 1 and selected != amounts[0][0]:
            raise NormalizationError(
                "The extracted number is only part of the stated amount", "ambiguous_quantity"
            )
        if len(amounts) > 1:
            # Only this unambiguous correction grammar overrides earlier quantities.
            correction = re.fullmatch(
                r"[.\s]*Actually,?\s+correct that to\s+(" + quantity_pattern + r")[.\s]*",
                text[amounts[0].end() :],
                re.IGNORECASE,
            )
            if len(amounts) != 2 or not correction or selected != correction[1]:
                raise NormalizationError(
                    "Conflicting or unrepresented quantities need review", "ambiguous_quantity"
                )
        for amount in amounts:
            prefix = text[max(0, amount.start() - 80) : amount.start()]
            if re.search(
                r"\b(?:not|cannot|can't|can’t|couldn't|couldn’t)\b[^.;!?]*$", prefix, re.IGNORECASE
            ):
                raise NormalizationError(
                    "A negated quantity cannot become capacity", "ambiguous_quantity"
                )
    elif selected is not None and re.fullmatch(r"\d+(?:\.\d+)?", selected):
        raise NormalizationError("Quantity has no explicit unit", "missing_units")
    elif selected is not None:
        raise NormalizationError("Unsupported quantity notation", "ambiguous_quantity")
    elif re.search(r"\b(?:capacity|maximum|limit)\b[^.;!?]*\d", text, re.IGNORECASE):
        raise NormalizationError("Capacity has no supported explicit unit", "missing_units")

    times = list(
        re.finditer(r"\bminute\s+\d+\b|\b\d{1,2}:\d{2}(?:\s*(?:UTC|[+-]\d{2}:\d{2}))?", text, re.IGNORECASE)
    )
    represented = {x for x in (extraction.opens_text, extraction.closes_text) if x is not None}
    if any(m[0] not in represented for m in times):
        raise NormalizationError(
            "A stated time boundary was omitted or lacks a timezone", "unclear_time"
        )
    for m in times:
        prefix = text[max(0, m.start() - 45) : m.start()]
        if (
            re.search(r"\b(?:until|through|by|before)\s*$", prefix, re.IGNORECASE)
            and extraction.closes_text != m[0]
        ):
            raise NormalizationError("An end boundary was reversed", "unclear_time")
        if (
            re.search(r"\b(?:from|starting at|start at|after)\s*$", prefix, re.IGNORECASE)
            and extraction.opens_text != m[0]
        ):
            raise NormalizationError("A start boundary was reversed", "unclear_time")
    if not times and re.search(
        r"\b(?:come|leave|finish|available)\b[^.;!?]*\b(?:at|before|by|after)\s+\d", text, re.IGNORECASE
    ):
        raise NormalizationError("Unqualified clock boundary", "unclear_time")

    positive = re.search(
        r"\b(?:I am|I'm|I’m|we are|we're|we’re)\s+available\b|"
        r"^\s*My (?:availability is|available window is)\s+(?:from\s+)?(?:minute\s+\d|\d{1,2}:\d{2})",
        text, re.IGNORECASE,
    )
    negative = re.search(
        r"\b(?:I am|I'm|I’m|we are|we're|we’re)\s+(?:unavailable|not available)\b|\bI cannot participate\b",
        text,
        re.IGNORECASE,
    )
    if positive and negative:
        raise NormalizationError("Availability conflicts within this message", "uncertain_offer")
    stated = False if negative else True if positive else None
    if stated is not None and extraction.available is not stated:
        raise NormalizationError("Explicit availability was omitted or reversed", "uncertain_offer")
    if extraction.available is not None and stated is None:
        raise NormalizationError(
            "Availability lacks a supported explicit assertion", "uncertain_offer"
        )


def validated_clarification(extraction, text):
    """Check narrow, contradictory clarification claims without creating facts.

    A valid-looking offer still requires model/coordinator review. These rules
    never synthesize a draft, infer a unit, or discard a condition.
    """
    reason = extraction.reason
    if reason in {"no_actionable_update", "uncertain_offer"} and re.fullmatch(
        r"\s*I (?:cannot|can't|can’t) (?:carry|take) \d+(?:\.\d+)?\s*"
        r"(?:kg|kilograms?|crates?)[.]?\s*", text, re.IGNORECASE
    ):
        reason = "ambiguous_quantity"
    if reason == "no_actionable_update" and re.fullmatch(
        r"\s*(?:Please )?change the road travel time to (?:\d+|one|two|three|four|five) "
        r"minutes?[.]?\s*", text, re.IGNORECASE
    ):
        reason = "unsupported_travel"
    if reason == "unclear_time" and re.fullmatch(
        r"\s*Maybe I (?:need to|have to|must) (?:finish|leave) (?:before|by|at) "
        r"\d{1,2}:\d{2}[.!]?\s*", text, re.IGNORECASE
    ):
        reason = "uncertain_offer"
    if reason == "uncertain_offer" and re.fullmatch(
        r"\s*I (?:need to|have to|must) (?:finish|leave) (?:before|by|at) "
        r"minute \d+[.]?\s*", text, re.IGNORECASE
    ):
        reason = "review_required"
    if reason in {"missing_units", "uncertain_offer", "no_actionable_update"} and re.fullmatch(
        r"\s*I (?:need to|have to|must) (?:finish|leave) (?:before|by|at) "
        r"\d{1,2}:\d{2}[.!]?\s*", text, re.IGNORECASE
    ):
        reason = "unclear_time"
    # Deliberately full-message matching: '80 kg and another 9' is not covered.
    if reason == "missing_units" and re.fullmatch(
        r"\s*(?:I can (?:carry|take)|My maximum(?: capacity)? is|My capacity is) "
        r"\d+(?:\.\d+)?\s*(?:kg|kilograms?|crates?)[.]?\s*", text, re.IGNORECASE
    ):
        reason = "review_required"
    return {
        "kind": "clarification", "question": QUESTIONS[reason], "reason": reason,
        "model_reason": extraction.reason,
        "clarification_validation": "corrected" if reason != extraction.reason else "unchanged",
    }


def normalize(extraction, message, state):
    if extraction.decision == "clarify":
        return validated_clarification(extraction, message["text"])
    quote = extraction.evidence_quote
    if not quote or not quote.strip() or quote not in message["text"]:
        raise NormalizationError("Evidence must be copied from the actual message")
    for span in (
        extraction.quantity_text,
        extraction.opens_text,
        extraction.closes_text,
        extraction.condition_text,
    ):
        if span is not None and (not span.strip() or span not in quote):
            raise NormalizationError("Every extracted span must occur in the evidence quote")
    require_condition_coverage(extraction, message["text"])
    require_source_coverage(extraction, message, state)
    values = {
        "capacity": None,
        "available": extraction.available,
        "conditional": None,
        "window_opens": None,
        "window_closes": None,
        "condition_text": extraction.condition_text,
        "condition_kind": None,
        "evidence_quote": quote,
    }
    if extraction.quantity_text is not None:
        values["capacity"] = capacity_kg(extraction.quantity_text, state["kg_per_crate"])
    for field, span in (
        ("window_opens", extraction.opens_text),
        ("window_closes", extraction.closes_text),
    ):
        if span is not None:
            values[field] = minute_offset(span, state["logistics"]["starts_at"])
    if extraction.condition_text is not None:
        values["conditional"] = True
        values["condition_kind"] = condition_kind(extraction.condition_text)
    return {"kind": "draft", **values}
