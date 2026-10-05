"""Persistent, hysteretic turbidity band state machine.

The machine consumes one mixed reading per control cycle (see
:class:`waterplant.turb.sampler.Sampler`) and decides the *confirmed* band.
Upgrade timers only advance while valid readings persist in the higher band;
a single dip resets them. Downgrades use one fixed confirmation window rather
than a single confirming reading. Interrupted readings hold the last band and
freeze every timer, so a partial sensor failure cannot move the line either
way.

Three independent durations are tracked, in control cycles:

* ``grade_cycles``      how long the currently confirmed band has stood.
* ``held_cycles``       consecutive cycles spent in an unconfirmed position
  (pending upgrade or pending downgrade); zero once a band is confirmed.
* ``interrupted_cycles`` consecutive cycles with no valid reading.

State lives under one JSON key and is a pure function of the ordered readings
applied to it; re-applying a reading with a duplicate ``reading_id`` returns
the stored outcome without mutating state or emitting another event, which
keeps retried calls idempotent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from waterplant.store.store import Store

from .bands import (
    DOWNGRADE_CYCLES,
    HISTORY_LIMIT,
    TURBIDITY_FLOOR,
    Grade,
    band_for,
    cycles_to_reach,
    entry_line,
)
from .report import GradeEvent, GradeOutcome

GRADEBOOK_KEY = "turb:gradebook"
REPLAY_LIMIT = 16


def _suffix_at_least(readings: list[float], band: Grade) -> int:
    """Length of the trailing run whose raw band is ``band`` or higher."""

    count = 0
    for value in reversed(readings):
        if band_for(value) >= band:
            count += 1
        else:
            break
    return count


def _clean_verdict(reading: float) -> float:
    """Dosing verdict for a reading followed directly (floor collapse)."""

    return max(reading, TURBIDITY_FLOOR) if reading >= TURBIDITY_FLOOR else 0.0


@dataclass(frozen=True)
class Decision:
    """Pure outcome of advancing one valid reading."""

    grade: Grade
    pending: str
    held: bool
    changed: bool
    held_cycles: int
    verdict: float
    basis: str


def decide(grade: Grade, readings: list[float]) -> Decision:
    """Advance one valid reading, given recent history and current band."""

    reading = readings[-1]

    # Upgrade: only one level per cycle, and only after the required run of
    # consecutive valid readings in the band being entered.
    if grade < Grade.EXCEEDED:
        candidate = Grade(int(grade) + 1)
        run = _suffix_at_least(readings, candidate)
        if run >= cycles_to_reach(candidate):
            return Decision(
                grade=candidate,
                pending="",
                held=False,
                changed=True,
                held_cycles=0,
                verdict=_clean_verdict(reading),
                basis=(
                    f"upgraded after {cycles_to_reach(candidate)} cycles "
                    f"at/above {entry_line(candidate):.1f}"
                ),
            )

    # Downgrade: one fixed confirmation window entirely below the current
    # entry line; settle at the highest band the window actually reached.
    window = readings[-DOWNGRADE_CYCLES:]
    if len(window) >= DOWNGRADE_CYCLES and all(band_for(value) < grade for value in window):
        settled = Grade(max(int(band_for(value)) for value in window))
        return Decision(
            grade=settled,
            pending="",
            held=False,
            changed=True,
            held_cycles=0,
            verdict=_clean_verdict(reading),
            basis=f"downgraded after {DOWNGRADE_CYCLES} cycles below {entry_line(grade):.1f}",
        )

    # Band and reading agree: follow the reading inside the confirmed band.
    if band_for(reading) == grade:
        return Decision(
            grade=grade,
            pending="",
            held=False,
            changed=False,
            held_cycles=0,
            verdict=_clean_verdict(reading),
            basis=f"reading inside {grade.label} band",
        )

    # Higher than confirmed: hold the confirmed band and clamp the dosing
    # basis to its envelope while the upgrade is being observed.
    if band_for(reading) > grade:
        if grade < Grade.EXCEEDED:
            candidate = Grade(int(grade) + 1)
            ceiling = entry_line(candidate)
            pending = candidate.label
            run = _suffix_at_least(readings, candidate)
        else:  # pragma: no cover - reading cannot exceed the top band
            ceiling = reading
            pending = ""
            run = 0
        verdict = min(_clean_verdict(reading), ceiling) if reading >= TURBIDITY_FLOOR else 0.0
        return Decision(
            grade=grade,
            pending=pending,
            held=True,
            changed=False,
            held_cycles=run,
            verdict=verdict,
            basis=f"pending {pending}; {run}/{cycles_to_reach(Grade(int(grade) + 1))} cycles, basis capped at {ceiling:.1f}",
        )

    # Lower than confirmed during the downgrade confirmation window.
    below_run = 0
    for value in reversed(readings):
        if band_for(value) < grade:
            below_run += 1
        else:
            break
    return Decision(
        grade=grade,
        pending="",
        held=True,
        changed=False,
        held_cycles=below_run,
        verdict=_clean_verdict(reading),
        basis=f"awaiting downgrade confirmation {below_run}/{DOWNGRADE_CYCLES} cycles below {entry_line(grade):.1f}",
    )


class GradeBook:
    """Hysteretic band state with append-only transition events."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._grade = Grade.NORMAL
        self._readings: list[float] = []
        self._last_verdict = 0.0
        self._grade_cycles = 0
        self._held_cycles = 0
        self._interrupted_cycles = 0
        self._last_reading_id = ""
        self._pending = ""
        self._basis = "boot; no reading yet"
        # Bounded replay cache: reading_id -> snapshot of the outcome produced
        # when that reading was first applied, so a redelivered recovery or
        # cycle is idempotent even when other readings arrived in between.
        self._replays: dict[str, dict[str, object]] = {}
        self._replay_order: list[str] = []
        self._load()

    def _load(self) -> None:
        raw, present = self._store.get(GRADEBOOK_KEY)
        if not present:
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        try:
            self._grade = Grade(int(payload.get("grade", 0)))
            self._readings = [float(value) for value in payload.get("readings", [])]
            self._last_verdict = float(payload.get("last_verdict", 0.0))
            self._grade_cycles = int(payload.get("grade_cycles", 0))
            self._held_cycles = int(payload.get("held_cycles", 0))
            self._interrupted_cycles = int(payload.get("interrupted_cycles", 0))
            self._last_reading_id = str(payload.get("last_reading_id", ""))
            self._pending = str(payload.get("pending", ""))
            self._basis = str(payload.get("basis", ""))
            replays = payload.get("replays", {})
            if isinstance(replays, dict):
                self._replays = {
                    str(key): dict(value)
                    for key, value in replays.items()
                    if isinstance(value, dict)
                }
                self._replay_order = list(self._replays.keys())[-REPLAY_LIMIT:]
        except (TypeError, ValueError):
            return

    @property
    def interrupted(self) -> bool:
        return self._interrupted_cycles > 0

    def _save(self) -> None:
        payload = {
            "grade": int(self._grade),
            "readings": self._readings[-HISTORY_LIMIT:],
            "last_verdict": self._last_verdict,
            "grade_cycles": self._grade_cycles,
            "held_cycles": self._held_cycles,
            "interrupted_cycles": self._interrupted_cycles,
            "last_reading_id": self._last_reading_id,
            "pending": self._pending,
            "basis": self._basis,
            "replays": {key: self._replays[key] for key in self._replay_order},
        }
        self._store.put(GRADEBOOK_KEY, json.dumps(payload, ensure_ascii=False))

    @property
    def grade(self) -> Grade:
        return self._grade

    def grade_cycles(self) -> int:
        return self._grade_cycles

    def held_cycles(self) -> int:
        return self._held_cycles

    def _outcome(
        self,
        reading: float,
        interrupted: bool,
        held: bool,
        events: list[GradeEvent],
    ) -> GradeOutcome:
        return GradeOutcome(
            grade=self._grade.label,
            grade_value=int(self._grade),
            pending=self._pending,
            interrupted=interrupted,
            held=held,
            reading=reading,
            verdict=self._last_verdict,
            grade_cycles=self._grade_cycles,
            held_cycles=self._held_cycles,
            interrupted_cycles=self._interrupted_cycles,
            basis=self._basis,
            events=tuple(events),
        )

    @staticmethod
    def _outcome_from_snapshot(snapshot: dict[str, object]) -> GradeOutcome:
        return GradeOutcome(
            grade=str(snapshot["grade"]),
            grade_value=int(snapshot["grade_value"]),  # type: ignore[arg-type]
            pending=str(snapshot["pending"]),
            interrupted=bool(snapshot["interrupted"]),
            held=bool(snapshot["held"]),
            reading=float(snapshot["reading"]),  # type: ignore[arg-type]
            verdict=float(snapshot["verdict"]),  # type: ignore[arg-type]
            grade_cycles=int(snapshot["grade_cycles"]),  # type: ignore[arg-type]
            held_cycles=int(snapshot["held_cycles"]),  # type: ignore[arg-type]
            interrupted_cycles=int(snapshot["interrupted_cycles"]),  # type: ignore[arg-type]
            basis=str(snapshot["basis"]),
            events=(),
        )

    def _remember(self, reading_id: str, outcome: GradeOutcome) -> None:
        """Store a replayable snapshot of a freshly applied outcome."""

        if not reading_id:
            return
        snapshot = outcome.as_dict()
        snapshot.pop("events", None)
        snapshot.pop("dose", None)
        self._replays[reading_id] = snapshot
        self._replay_order.append(reading_id)
        for stale in self._replay_order[:-REPLAY_LIMIT]:
            self._replays.pop(stale, None)
        self._replay_order = self._replay_order[-REPLAY_LIMIT:]

    def apply(self, reading_id: str, reading: float | None) -> GradeOutcome:
        """Judge one cycle. ``None`` marks an interrupted (partial) reading.

        A repeated ``reading_id`` (within the recent replay window, including
        across restarts) returns the outcome produced on its first
        application, without touching state or emitting new events, so retried
        cycles and recovered readings stay idempotent.
        """

        if reading_id and reading_id in self._replays:
            return self._outcome_from_snapshot(self._replays[reading_id])

        events: list[GradeEvent] = []

        if reading is None:
            # Partial failure: hold the confirmed band, freeze every timer.
            was_interrupted = self.interrupted
            self._interrupted_cycles += 1
            self._held_cycles = 0
            self._grade_cycles += 1
            self._pending = ""
            self._basis = "reading interrupted; held prior band and dosing basis"
            if not was_interrupted:
                events.append(
                    GradeEvent(
                        kind="turbidity",
                        detail=(
                            f"interrupted grade={self._grade.label} held=true "
                            f"grade_cycles={self._grade_cycles} "
                            f"verdict={self._last_verdict:.4f}"
                        ),
                    )
                )
            self._last_reading_id = reading_id
            outcome = self._outcome(
                reading=self._readings[-1] if self._readings else 0.0,
                interrupted=True,
                held=True,
                events=events,
            )
            self._remember(reading_id, outcome)
            self._save()
            return outcome

        # Valid reading: reading history (the timers) advances again.
        was_interrupted = self.interrupted
        previous = self._grade
        self._readings.append(reading)
        self._readings = self._readings[-HISTORY_LIMIT:]

        decision = decide(self._grade, self._readings)
        self._grade = decision.grade
        self._pending = decision.pending
        self._last_verdict = decision.verdict
        self._basis = decision.basis
        self._held_cycles = decision.held_cycles
        self._interrupted_cycles = 0
        self._grade_cycles = 1 if decision.changed else self._grade_cycles + 1
        self._last_reading_id = reading_id

        if was_interrupted:
            events.append(
                GradeEvent(
                    kind="turbidity",
                    detail=(
                        f"recovered grade={self._grade.label} held={str(decision.held).lower()} "
                        f"reading={reading:.4f} verdict={self._last_verdict:.4f}"
                    ),
                )
            )
        if decision.changed:
            direction = "upgraded" if self._grade > previous else "downgraded"
            events.append(
                GradeEvent(
                    kind="turbidity",
                    detail=(
                        f"{direction} {previous.label}->{self._grade.label} "
                        f"reading={reading:.4f} verdict={self._last_verdict:.4f} "
                        f"grade_cycles=1 basis={self._basis}"
                    ),
                )
            )

        outcome = self._outcome(
            reading=reading,
            interrupted=False,
            held=decision.held,
            events=events,
        )
        self._remember(reading_id, outcome)
        self._save()
        return outcome
