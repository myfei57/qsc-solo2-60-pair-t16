"""Turbidity projections and the fixed judgement policy.

The policy constants below are the single source of truth for the tiered
judgement. They are deliberately module level so the console, the audit
trail and the dose computation all read the same definitions.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tier names, ordered by severity.
NORMAL = "normal"
WATCH = "watch"
EXCEED = "exceed"
TIER_ORDER = {NORMAL: 0, WATCH: 1, EXCEED: 2}
TIER_LABELS = {NORMAL: "正常", WATCH: "关注", EXCEED: "超标"}

# Judgement lines in NTU. The watch line keeps the old floor semantics:
# anything below it is clean water and needs no coagulant.
WATCH_LINE = 1.0
EXCEED_LINE = 3.0

# Escalation policy, fixed by water-quality requirement:
# - watch is an early-warning tier, so a crossing must hold for one extra
#   round before it is confirmed; a brief crossing never escalates;
# - exceed is an effluent compliance event, so it confirms on the first
#   reading. The anti-flap burden sits on the downgrade side, not on
#   delaying a compliance alarm.
WATCH_CONFIRM_ROUNDS = 2
EXCEED_CONFIRM_ROUNDS = 1

# Downgrade policy, fixed as a sustained window: the reading must stay
# below the current tier for this many consecutive rounds, and the tier
# lands on the worst reading seen inside the window. A single confirming
# round is not accepted - that would be the old flapping line again.
DOWNGRADE_ROUNDS = 3

# Dose profile per tier: the confirmed tier scales the coagulant dose.
# Exceed boosts dosing to push flocculation back under the line.
TIER_DOSE_BOOST = {NORMAL: 1.0, WATCH: 1.0, EXCEED: 1.2}


def tier_of(turbidity: float) -> str:
    """Map a turbidity value onto its tier."""

    if turbidity >= EXCEED_LINE:
        return EXCEED
    if turbidity >= WATCH_LINE:
        return WATCH
    return NORMAL


@dataclass(frozen=True)
class Outcome:
    """One judged round: the confirmed tier and the dose derived from it."""

    tier: str
    verdict: float
    dose: float
    tier_rounds: int
    changed: bool
    degraded: bool
    recovered: bool
    replayed: bool
    missed_rounds: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "tier_label": TIER_LABELS[self.tier],
            "verdict": self.verdict,
            "dose": self.dose,
            "tier_rounds": self.tier_rounds,
            "changed": self.changed,
            "degraded": self.degraded,
            "recovered": self.recovered,
            "replayed": self.replayed,
            "missed_rounds": self.missed_rounds,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TurbState:
    """Current tier, its duration and the evidence being accumulated."""

    tier: str = NORMAL
    tier_rounds: int = 0
    up_streak: int = 0
    down_streak: int = 0
    down_candidate: str = ""
    degraded: bool = False
    missed_rounds: int = 0
    last_verdict: float = 0.0
    last_dose: float = 0.0
    last_reading_id: str = ""
    reason: str = "init"
    changed: bool = False
    recovered: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "tier_label": TIER_LABELS[self.tier],
            "tier_rounds": self.tier_rounds,
            "up_streak": self.up_streak,
            "down_streak": self.down_streak,
            "down_candidate": self.down_candidate,
            "degraded": self.degraded,
            "missed_rounds": self.missed_rounds,
            "last_verdict": self.last_verdict,
            "last_dose": self.last_dose,
            "last_reading_id": self.last_reading_id,
            "reason": self.reason,
            "changed": self.changed,
            "recovered": self.recovered,
            "watch_line": WATCH_LINE,
            "exceed_line": EXCEED_LINE,
            "watch_confirm_rounds": WATCH_CONFIRM_ROUNDS,
            "downgrade_rounds": DOWNGRADE_ROUNDS,
        }
