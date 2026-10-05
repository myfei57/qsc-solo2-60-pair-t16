"""Turbidity judgement feeding the coagulant dose.

Timing dependency: the band is decided first by :class:`GradeBook`, and only
then is the coagulant dose computed against the verdict that belongs to that
confirmed band. The verdict is already clamped to the confirmed band envelope
while an upgrade is pending and frozen while a reading is interrupted, so the
band shown on the console and the dosed amount always share one basis.
"""

from __future__ import annotations

from collections.abc import Iterable

from waterplant.audit import Auditor
from waterplant.coag import Doser
from waterplant.intake import mix
from waterplant.store.store import Store

from .gradebook import GradeBook
from .report import GradeOutcome, TurbState

TURBIDITY_FLOOR = 1.0


def verdict_for(turbidity: float) -> float:
    """Collapse readings below the floor to a clean verdict."""

    if turbidity < TURBIDITY_FLOOR:
        return 0.0
    return turbidity


class Sampler:
    """Mixes a sample window, confirms a band, then doses against it."""

    def __init__(self, store: Store, doser: Doser, auditor: Auditor | None = None) -> None:
        self._doser = doser
        self._auditor = auditor
        self._book = GradeBook(store)
        self._last: GradeOutcome | None = None

    def _with_dose(self, outcome: GradeOutcome) -> GradeOutcome:
        dose = self._doser.dose_for_turbidity(outcome.verdict)
        return GradeOutcome(
            grade=outcome.grade,
            grade_value=outcome.grade_value,
            pending=outcome.pending,
            interrupted=outcome.interrupted,
            held=outcome.held,
            reading=outcome.reading,
            verdict=outcome.verdict,
            grade_cycles=outcome.grade_cycles,
            held_cycles=outcome.held_cycles,
            interrupted_cycles=outcome.interrupted_cycles,
            basis=outcome.basis,
            dose=dose,
            events=outcome.events,
        )

    def judge(self, raw: Iterable[float] | None, reading_id: str = "") -> GradeOutcome:
        """Confirm the band for one window, then compute the coagulant dose.

        ``None`` (or a missing window) is a reading interruption: a partial
        failure that holds the prior band and dosing basis. A repeated
        ``reading_id`` replays the same outcome and writes no new audit entry.
        """

        reading = None if raw is None else mix(raw)
        outcome = self._with_dose(self._book.apply(reading_id, reading))
        for event in outcome.events:
            if self._auditor is not None:
                self._auditor.record(event.kind, event.detail)
        self._last = outcome
        return outcome

    def state(self) -> TurbState:
        if self._last is None:
            return TurbState()
        return TurbState(
            grade=self._last.grade,
            grade_value=self._last.grade_value,
            pending=self._last.pending,
            interrupted=self._last.interrupted,
            held=self._last.held,
            last_reading=self._last.reading,
            last_verdict=self._last.verdict,
            grade_cycles=self._last.grade_cycles,
            held_cycles=self._last.held_cycles,
            interrupted_cycles=self._last.interrupted_cycles,
            basis=self._last.basis,
            dose=self._last.dose,
        )

    def describe(self) -> str:
        state = self.state()
        held = " held" if state.held else ""
        pending = f" pending={state.pending}" if state.pending else ""
        return (
            f"turbidity grade={state.grade}{pending}{held} "
            f"verdict={state.last_verdict:.4f} dose={state.dose:.4f}"
        )
