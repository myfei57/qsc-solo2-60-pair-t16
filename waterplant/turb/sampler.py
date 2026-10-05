"""Tiered turbidity judgement feeding the coagulant dose.

Every round runs in a fixed order: the sample window is mixed, the tier
state machine confirms a tier, and only then is the coagulant dose
computed from that confirmed tier. The dose therefore always matches the
tier the console shows.

A missing sample window is a partial failure, not clean water: the
previous tier is held and flagged as degraded until readings resume.
Callers that may retry a round pass a ``reading_id``; a repeated id
returns the stored outcome without advancing the machine or writing a
second audit entry.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace

from waterplant.audit import Auditor
from waterplant.coag import Doser
from waterplant.intake import mix
from waterplant.store.store import Store

from .report import (
    DOWNGRADE_ROUNDS,
    EXCEED,
    EXCEED_LINE,
    NORMAL,
    TIER_DOSE_BOOST,
    TIER_LABELS,
    TIER_ORDER,
    WATCH,
    WATCH_CONFIRM_ROUNDS,
    WATCH_LINE,
    Outcome,
    TurbState,
    tier_of,
)

TURBIDITY_FLOOR = WATCH_LINE
STATE_KEY = "turb:state"
TIER_AUDIT_KIND = "turbidity-tier"
HOLD_AUDIT_KIND = "turbidity-hold"
RECOVER_AUDIT_KIND = "turbidity-recover"


def verdict_for(turbidity: float) -> float:
    """Collapse readings below the floor to a clean verdict."""

    if turbidity < TURBIDITY_FLOOR:
        return 0.0
    return turbidity


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class Sampler:
    """Confirms a turbidity tier, then derives the coagulant dose from it."""

    def __init__(self, doser: Doser, store: Store) -> None:
        self._doser = doser
        self._auditor = Auditor(store)
        self._store = store

    def judge(self, raw: Iterable[float], reading_id: str = "") -> Outcome:
        """Judge one round of samples.

        ``reading_id`` makes the round idempotent: passing the id of an
        already processed round returns the stored outcome unchanged.
        """

        state = self._load()
        if reading_id and reading_id == state.last_reading_id:
            return self._outcome(state, replayed=True)
        samples = [float(value) for value in raw]
        if not samples:
            return self._hold(state, reading_id)
        return self._advance(state, verdict_for(mix(samples)), reading_id)

    def state(self) -> TurbState:
        return self._load()

    def describe(self) -> str:
        state = self._load()
        text = (
            f"turbidity tier={state.tier}({TIER_LABELS[state.tier]}) "
            f"rounds={state.tier_rounds} verdict={state.last_verdict:.4f} "
            f"dose={state.last_dose:.4f}"
        )
        if state.degraded:
            text += f" degraded missed={state.missed_rounds}"
        return text

    def _hold(self, state: TurbState, reading_id: str) -> Outcome:
        """Reading interrupted: keep the tier, flag the round, dose on the held tier."""

        if not state.degraded:
            self._auditor.record(
                HOLD_AUDIT_KIND,
                json.dumps(
                    {
                        "tier": state.tier,
                        "reason": "reading_interrupted",
                        "held_dose": round(state.last_dose, 4),
                    },
                    ensure_ascii=False,
                ),
            )
        next_state = replace(
            state,
            tier_rounds=state.tier_rounds + 1,
            degraded=True,
            missed_rounds=state.missed_rounds + 1,
            last_reading_id=reading_id,
            reason="hold:reading_interrupted",
            changed=False,
            recovered=False,
        )
        next_state = replace(
            next_state, last_dose=self._dose_for(next_state.tier, next_state.last_verdict)
        )
        self._save(next_state)
        return self._outcome(next_state, replayed=False)

    def _advance(self, state: TurbState, verdict: float, reading_id: str) -> Outcome:
        recovered = state.degraded
        if recovered:
            self._auditor.record(
                RECOVER_AUDIT_KIND,
                json.dumps(
                    {
                        "missed_rounds": state.missed_rounds,
                        "resumed_verdict": round(verdict, 4),
                        "tier": state.tier,
                    },
                    ensure_ascii=False,
                ),
            )

        reading_tier = tier_of(verdict)
        tier = state.tier
        tier_rounds = state.tier_rounds + 1
        up_streak = 0
        down_streak = 0
        down_candidate = ""
        changed = False
        reason = "steady"
        evidence = ""

        if TIER_ORDER[reading_tier] > TIER_ORDER[tier]:
            if reading_tier == EXCEED:
                # Effluent compliance event: confirm on the first reading.
                tier = EXCEED
                changed = True
                reason = "escalate:exceed_confirmed"
                evidence = (
                    f"reading {verdict:.2f} >= exceed line {EXCEED_LINE:.2f}; "
                    "confirmed immediately (effluent compliance event)"
                )
            else:
                # Watch crossing: observe one extra round before confirming.
                up_streak = state.up_streak + 1
                if up_streak >= WATCH_CONFIRM_ROUNDS:
                    tier = WATCH
                    changed = True
                    reason = "escalate:watch_confirmed"
                    evidence = (
                        f"reading >= watch line {WATCH_LINE:.2f} for "
                        f"{WATCH_CONFIRM_ROUNDS} consecutive rounds"
                    )
                    up_streak = 0
                else:
                    reason = "escalate:watch_pending"
        elif TIER_ORDER[reading_tier] < TIER_ORDER[tier]:
            # Downgrade: fixed sustained window, landing on the worst
            # reading tier seen inside the window.
            down_streak = state.down_streak + 1
            seen = [reading_tier]
            if state.down_candidate:
                seen.append(state.down_candidate)
            down_candidate = max(seen, key=lambda name: TIER_ORDER[name])
            if down_streak >= DOWNGRADE_ROUNDS:
                tier = down_candidate
                changed = True
                reason = "downgrade:confirmed"
                evidence = (
                    f"reading below {state.tier} for {DOWNGRADE_ROUNDS} "
                    f"consecutive rounds; landing on worst window tier {down_candidate}"
                )
                down_streak = 0
                down_candidate = ""
            else:
                reason = "downgrade:pending"

        # The dose is computed only after the tier is final, so the dose
        # and the tier can never disagree.
        dose = self._dose_for(tier, verdict)
        if changed:
            self._auditor.record(
                TIER_AUDIT_KIND,
                json.dumps(
                    {
                        "from": state.tier,
                        "to": tier,
                        "verdict": round(verdict, 4),
                        "dose": round(dose, 4),
                        "tier_rounds_before": state.tier_rounds,
                        "reason": reason,
                        "evidence": evidence,
                    },
                    ensure_ascii=False,
                ),
            )
            tier_rounds = 1

        next_state = TurbState(
            tier=tier,
            tier_rounds=tier_rounds,
            up_streak=up_streak,
            down_streak=down_streak,
            down_candidate=down_candidate,
            degraded=False,
            missed_rounds=0,
            last_verdict=verdict,
            last_dose=dose,
            last_reading_id=reading_id,
            reason=reason,
            changed=changed,
            recovered=recovered,
        )
        self._save(next_state)
        return self._outcome(next_state, replayed=False)

    def _dose_for(self, tier: str, verdict: float) -> float:
        """Dose follows the confirmed tier, never the raw reading directly."""

        return self._doser.dose_for_turbidity(verdict) * TIER_DOSE_BOOST[tier]

    def _outcome(self, state: TurbState, replayed: bool) -> Outcome:
        return Outcome(
            tier=state.tier,
            verdict=state.last_verdict,
            dose=state.last_dose,
            tier_rounds=state.tier_rounds,
            changed=state.changed,
            degraded=state.degraded,
            recovered=state.recovered,
            replayed=replayed,
            missed_rounds=state.missed_rounds,
            reason=state.reason,
        )

    def _load(self) -> TurbState:
        raw, present = self._store.get(STATE_KEY)
        if not present:
            return TurbState()
        try:
            payload = json.loads(raw)
        except ValueError:
            return TurbState()
        if not isinstance(payload, dict):
            return TurbState()
        tier = str(payload.get("tier", NORMAL))
        if tier not in TIER_ORDER:
            tier = NORMAL
        return TurbState(
            tier=tier,
            tier_rounds=_as_int(payload.get("tier_rounds")),
            up_streak=_as_int(payload.get("up_streak")),
            down_streak=_as_int(payload.get("down_streak")),
            down_candidate=str(payload.get("down_candidate", "")),
            degraded=bool(payload.get("degraded", False)),
            missed_rounds=_as_int(payload.get("missed_rounds")),
            last_verdict=_as_float(payload.get("last_verdict")),
            last_dose=_as_float(payload.get("last_dose")),
            last_reading_id=str(payload.get("last_reading_id", "")),
            reason=str(payload.get("reason", "init")),
            changed=bool(payload.get("changed", False)),
            recovered=bool(payload.get("recovered", False)),
        )

    def _save(self, state: TurbState) -> None:
        self._store.put(
            STATE_KEY,
            json.dumps(
                {
                    "tier": state.tier,
                    "tier_rounds": state.tier_rounds,
                    "up_streak": state.up_streak,
                    "down_streak": state.down_streak,
                    "down_candidate": state.down_candidate,
                    "degraded": state.degraded,
                    "missed_rounds": state.missed_rounds,
                    "last_verdict": state.last_verdict,
                    "last_dose": state.last_dose,
                    "last_reading_id": state.last_reading_id,
                    "reason": state.reason,
                    "changed": state.changed,
                    "recovered": state.recovered,
                },
                ensure_ascii=False,
            ),
        )
