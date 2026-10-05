"""Turbidity bands and the pinned hysteresis policy.

The old judgement was a single line: above it alarmed, below it cleared, so a
noisy reading flipped the alarm every cycle. The policy below is fixed per the
water-quality requirements and must not be retuned to silence an alarm:

* Three bands: NORMAL / WATCH / EXCEEDED.
* Upgrades are never immediate. A level rise is confirmed only after the
  reading has lived in the higher band for a fixed number of consecutive
  valid cycles (observe first, escalate after persistence).
* Downgrades wait for one full confirmation window, never a single confirming
  reading.
* Interrupted readings are a partial failure: the confirmed band is held and
  every timer is frozen until valid readings resume.

Dosing caliber (the verdict fed to the coagulant doser) is always derived
*after* the band is confirmed, from one function whose result is what the
console, audit and dose share:

* While a rise is still being observed the verdict is clamped to the
  confirmed band envelope, so coagulant cannot run ahead of an unconfirmed
  upgrade ("first grade, then dose").
* While a fall is awaiting its confirmation window the verdict follows the
  live (lower) reading, so the plant does not keep over-dosing on a stale
  high reading; the band label still waits for the full window.
* During an interruption the verdict, like the band, is frozen.
"""

from __future__ import annotations

from enum import IntEnum

#: Readings strictly below this are NORMAL.
WATCH_LINE = 5.0
#: Readings at or above this are EXCEEDED; the band between both lines is WATCH.
EXCEED_LINE = 10.0

#: Readings below this floor collapse to a zero dosing verdict (clean water).
TURBIDITY_FLOOR = 1.0

#: Consecutive valid cycles above ``WATCH_LINE`` before NORMAL may rise.
UPGRADE_TO_WATCH_CYCLES = 3
#: Consecutive valid cycles at/above ``EXCEED_LINE`` before WATCH may rise.
UPGRADE_TO_EXCEEDED_CYCLES = 2
#: Consecutive valid cycles below the current band line before it may fall.
DOWNGRADE_CYCLES = 3

#: How many recent valid readings the state machine keeps for suffix checks.
HISTORY_LIMIT = max(UPGRADE_TO_WATCH_CYCLES, UPGRADE_TO_EXCEEDED_CYCLES, DOWNGRADE_CYCLES) + 2


class Grade(IntEnum):
    """Confirmed turbidity band, ordered by severity."""

    NORMAL = 0
    WATCH = 1
    EXCEEDED = 2

    @property
    def label(self) -> str:
        return self.name.lower()


def entry_line(grade: Grade) -> float:
    """Lower bound of a band; NORMAL starts at zero."""

    if grade is Grade.WATCH:
        return WATCH_LINE
    if grade is Grade.EXCEEDED:
        return EXCEED_LINE
    return 0.0


def cycles_to_reach(grade: Grade) -> int:
    """Consecutive valid cycles required to upgrade into ``grade``."""

    if grade is Grade.WATCH:
        return UPGRADE_TO_WATCH_CYCLES
    if grade is Grade.EXCEEDED:
        return UPGRADE_TO_EXCEEDED_CYCLES
    return 0


def band_for(reading: float) -> Grade:
    """Classify a single valid reading into its raw band."""

    if reading >= EXCEED_LINE:
        return Grade.EXCEEDED
    if reading >= WATCH_LINE:
        return Grade.WATCH
    return Grade.NORMAL
