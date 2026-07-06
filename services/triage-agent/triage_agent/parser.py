"""Resolution_Plan parsing from arbitrary LLM output (Property 3 target).

``parse_resolution_plan`` must never raise and must always yield a confidence
score in [0.00, 1.00] with at most two decimal places. Unusable responses
degrade to an empty plan with confidence 0.0, which routes to escalation.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from typing import Any

from shared.models import ACTION_TYPES, PlannedAction, ResolutionPlan

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def clamp_confidence(value: Any) -> float:
    """Coerce any value into [0.0, 1.0] rounded to two decimal places."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(score):
        return 0.0
    score = min(1.0, max(0.0, score))
    return float(round(Decimal(str(score)), 2))


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of the first JSON object in an LLM response."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    match = _JSON_BLOCK.search(text or "")
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return None


def _extract_actions(raw: Any) -> list[PlannedAction]:
    actions: list[PlannedAction] = []
    if not isinstance(raw, list):
        return actions
    for item in raw:
        if not isinstance(item, dict):
            continue
        action_type = item.get("action_type")
        if action_type not in ACTION_TYPES:
            continue
        parameters = item.get("parameters")
        actions.append(
            PlannedAction(
                action_type=action_type,
                parameters=parameters if isinstance(parameters, dict) else {},
            )
        )
    return actions


def parse_resolution_plan(ticket_id: str, llm_response: str) -> ResolutionPlan:
    """Parse an arbitrary LLM response string into a valid ResolutionPlan.

    Never raises; garbage in -> confidence 0.0 (escalation route).
    """
    data = _extract_json(llm_response if isinstance(llm_response, str) else "")
    if data is None:
        return ResolutionPlan(ticket_id=ticket_id, actions=[], confidence_score=0.0)
    return ResolutionPlan(
        ticket_id=ticket_id,
        actions=_extract_actions(data.get("actions")),
        confidence_score=clamp_confidence(data.get("confidence_score")),
    )
