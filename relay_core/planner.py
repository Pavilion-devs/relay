"""Bounded single-pickup planning with explicit operator inputs and directed travel times.

No geocoding, traffic, food-safety certification, or cross-rescue reservations.
The matrix adapter can be replaced without changing the commitment engine.
"""

from collections import deque
from datetime import datetime, timedelta
from itertools import combinations, permutations, product
from math import ceil
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Minute = Annotated[int, Field(strict=True, ge=0, le=1440)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Window(Input):
    opens: Minute
    closes: Minute

    @model_validator(mode="after")
    def ordered(self):
        if self.closes <= self.opens:
            raise ValueError("A window must close after it opens")
        return self


class Participant(Input):
    window: Window
    handling: list[str] = Field(max_length=10)


class Logistics(Input):
    starts_at: datetime
    pickup: Window
    loading_minutes: Minute
    unloading_minutes: Minute
    required_handling: str = Field(min_length=1, max_length=80)
    participants: dict[str, Participant]
    travel_minutes: dict[str, dict[str, Minute]]
    source: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def aware(self):
        if self.starts_at.tzinfo is None:
            raise ValueError("The scenario start must include a timezone")
        if len(self.participants) > 12 or len(self.travel_minutes) > 13:
            raise ValueError("This rehearsal supports a small known network")
        if any(len(row) > 13 for row in self.travel_minutes.values()):
            raise ValueError("Travel matrix is too large")
        return self


class TravelTimes(Protocol):
    def minutes(self, origin: str, destination: str) -> int | None: ...


class MatrixTravelTimes:
    def __init__(self, matrix):
        self.matrix = matrix

    def minutes(self, origin, destination):
        return self.matrix.get(origin, {}).get(destination)


def fixture(start, resources):
    return {
        "starts_at": start.isoformat(),
        "pickup": {"opens": 20, "closes": 90},
        "loading_minutes": 10,
        "unloading_minutes": 5,
        "required_handling": "sealed_produce",
        "participants": {
            r["id"]: {
                "window": {"opens": 0 if r["role"] == "driver" else 20, "closes": 170},
                "handling": ["sealed_produce"],
            }
            for r in resources
        },
        "travel_minutes": {
            "ada": {"pickup": 10},
            "tunde": {"pickup": 12},
            "amara": {"pickup": 18},
            "pickup": {"harbour": 20, "garden": 30},
            "harbour": {"garden": 12},
            "garden": {"harbour": 15},
        },
        "source": "Synthetic travel minutes and operator handling rules; no live traffic",
    }


def route(driver, order, data, travel, earliest, deadline):
    config = data.participants[driver["id"]]
    departure = max(earliest, config.window.opens)
    drive = travel.minutes(driver["id"], "pickup")
    if drive is None:
        return None
    pickup = max(departure + drive, data.pickup.opens)
    clock = pickup + data.loading_minutes
    if clock > data.pickup.closes:
        return None
    stops = []
    origin = "pickup"
    for rid in order:
        minutes = travel.minutes(origin, rid)
        if minutes is None:
            return None
        drive += minutes
        window = data.participants[rid].window
        arrival = clock + minutes
        service = max(arrival, window.opens)
        clock = service + data.unloading_minutes
        if clock > window.closes:
            return None
        stops.append(
            {
                "recipient": rid,
                "arrival_minute": arrival,
                "service_minute": service,
                "finish_minute": clock,
            }
        )
        origin = rid
    if clock > min(config.window.closes, deadline):
        return None
    return {
        "driver": driver["id"],
        "departure_minute": departure,
        "pickup_minute": pickup,
        "loaded_minute": pickup + data.loading_minutes,
        "finish_minute": clock,
        "travel_minutes": drive,
        "stops": stops,
    }


def flow(drivers, recipients, routes, total):
    """Integral max flow: driver capacity -> reachable recipients -> receiving capacity."""
    residual, original = {}, {}

    def edge(a, b, capacity):
        residual.setdefault(a, {})[b] = capacity
        residual.setdefault(b, {})[a] = 0
        original[a, b] = capacity

    for d, r in zip(drivers, routes, strict=True):
        edge("source", "d:" + d["id"], d["capacity"] if r else 0)
        for stop in r["stops"] if r else []:
            edge("d:" + d["id"], "r:" + stop["recipient"], total)
    for r in recipients:
        edge("r:" + r["id"], "sink", r["capacity"])
    sent = 0
    while sent < total:
        parents, queue = {"source": None}, deque(["source"])
        while queue and "sink" not in parents:
            node = queue.popleft()
            for target, capacity in residual.get(node, {}).items():
                if capacity > 0 and target not in parents:
                    parents[target] = node
                    queue.append(target)
        if "sink" not in parents:
            return None
        amount, node = total - sent, "sink"
        while parents[node] is not None:
            previous = parents[node]
            amount = min(amount, residual[previous][node])
            node = previous
        node = "sink"
        while parents[node] is not None:
            previous = parents[node]
            residual[previous][node] -= amount
            residual[node][previous] += amount
            node = previous
        sent += amount
    return [
        {"driver": a[2:], "recipient": b[2:], "kg": capacity - residual[a][b]}
        for (a, b), capacity in original.items()
        if a.startswith("d:") and b.startswith("r:") and capacity > residual[a][b]
    ]


def solve(s, at):
    def failed(code, message, reasons=()):
        return {"ok": False, "code": code, "message": message, "reasons": list(reasons)}

    try:
        data = Logistics.model_validate(s.get("logistics"))
    except ValidationError:
        return failed(
            "MISSING_LOGISTICS",
            "Pickup, travel and handling inputs are required. Start a new rehearsal or configure logistics.",
        )
    eligible, reasons = [], []
    for resource in s["resources"]:
        r = dict(resource)
        remaining = s.get("reservation_snapshot", {}).get(r["id"], r["capacity"])
        if remaining < r["capacity"]:
            reasons.append(f"{r['name']}: shared reservations leave {remaining} kg available.")
        r["capacity"] = min(r["capacity"], remaining)
        if not r["available"] or r["capacity"] <= 0:
            continue
        config = data.participants.get(r["id"])
        if config is None:
            return failed(
                "MISSING_LOGISTICS",
                f"A time window and handling confirmation are missing for {r['name']}.",
            )
        if data.required_handling not in config.handling:
            reasons.append(f"{r['name']}: required handling is not confirmed.")
        else:
            eligible.append(r)
    drivers = [r for r in eligible if r["role"] == "driver"]
    recipients = [r for r in eligible if r["role"] == "recipient"]
    for role in ("driver", "recipient"):
        short = max(0, s["total_kg"] - sum(r["capacity"] for r in eligible if r["role"] == role))
        if short:
            return failed(
                "INSUFFICIENT_CAPACITY",
                f"Cannot cover this rescue: {short} kg {role} capacity shortfall after handling checks.",
                reasons,
            )
    if len(drivers) > 3 or len(recipients) > 4:
        return failed(
            "PLANNER_LIMIT",
            "This planner supports at most three available drivers and four recipients. Coordinator planning is required.",
        )
    travel = MatrixTravelTimes(data.travel_minutes)
    # No silent straight-line estimate or guessed reverse leg when an input is missing.
    missing = [
        f"{d['name']} → pickup" for d in drivers if travel.minutes(d["id"], "pickup") is None
    ]
    missing += [
        f"pickup → {r['name']}" for r in recipients if travel.minutes("pickup", r["id"]) is None
    ]
    missing += [
        f"{a['name']} → {b['name']}"
        for a in recipients
        for b in recipients
        if a != b and travel.minutes(a["id"], b["id"]) is None
    ]
    if missing:
        return failed(
            "MISSING_TRAVEL",
            "Travel inputs are incomplete; confirm the missing directed legs.",
            missing,
        )
    earliest = max(0, ceil((at - data.starts_at).total_seconds() / 60)) + 5
    deadline = (datetime.fromisoformat(s["deadline"]) - data.starts_at).total_seconds() / 60
    options = []
    for driver in drivers:
        choices = [None]
        ids = [r["id"] for r in recipients]
        for count in range(1, len(ids) + 1):
            for subset in combinations(ids, count):
                feasible = [
                    candidate
                    for order in permutations(subset)
                    if (candidate := route(driver, order, data, travel, earliest, deadline))
                ]
                if feasible:
                    choices.append(
                        min(feasible, key=lambda r: (r["travel_minutes"], r["finish_minute"]))
                    )
        if len(choices) == 1:
            reasons.append(f"{driver['name']}: no route fits pickup, receiving and driver windows.")
        options.append(choices)
    best, best_score = None, None
    for routes in product(*options):
        score = (sum(r["travel_minutes"] for r in routes if r), sum(r is not None for r in routes))
        if best_score is not None and score >= best_score:
            continue
        legs = flow(drivers, recipients, routes, s["total_kg"])
        if legs is None:
            continue
        # Keep scheduled visits even when flow assigns zero kg there. Removing one could
        # invalidate travel feasibility in a directed, non-metric operator matrix.
        used = [r for r in routes if r]
        best_score = score
        best = {
            "ok": True,
            "legs": legs,
            "routes": used,
            "travel_minutes": score[0],
            "starts_at": data.starts_at.isoformat(),
            "reasons": reasons,
            "dispatch_by": min(
                data.starts_at + timedelta(minutes=r["departure_minute"]) for r in used
            ).isoformat(),
            "source": data.source,
        }
    return best or failed(
        "NO_FEASIBLE_ROUTE",
        "Available kilograms are sufficient, but no complete route fits the confirmed time windows. Adjust availability or escalate.",
        reasons,
    )
