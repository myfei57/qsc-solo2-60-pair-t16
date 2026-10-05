"""Turbidity projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GradeEvent:
    """A state machine transition or a reading-interruption lifecycle event."""

    kind: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class GradeOutcome:
    """Result of judging one reading window.

    ``grade`` is always the *confirmed* band; ``pending`` names a higher band
    that readings are building toward. ``held`` marks a decision that stayed
    on the confirmed band while an upgrade or downgrade was still being
    confirmed, and ``interrupted`` marks a partial sensor failure where the
    prior band and dosing basis were kept.
    """

    grade: str
    grade_value: int
    pending: str
    interrupted: bool
    held: bool
    reading: float
    verdict: float
    grade_cycles: int
    held_cycles: int
    interrupted_cycles: int
    basis: str
    dose: float = 0.0
    events: tuple[GradeEvent, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "grade": self.grade,
            "grade_value": self.grade_value,
            "pending": self.pending,
            "interrupted": self.interrupted,
            "held": self.held,
            "reading": self.reading,
            "verdict": self.verdict,
            "grade_cycles": self.grade_cycles,
            "held_cycles": self.held_cycles,
            "interrupted_cycles": self.interrupted_cycles,
            "basis": self.basis,
            "dose": self.dose,
            "events": [event.as_dict() for event in self.events],
        }


@dataclass(frozen=True)
class TurbState:
    """Confirmed turbidity band, timing and the dose it drives."""

    grade: str = "normal"
    grade_value: int = 0
    pending: str = ""
    interrupted: bool = False
    held: bool = False
    last_reading: float = 0.0
    last_verdict: float = 0.0
    grade_cycles: int = 0
    held_cycles: int = 0
    interrupted_cycles: int = 0
    basis: str = "boot"
    dose: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "grade": self.grade,
            "grade_value": self.grade_value,
            "pending": self.pending,
            "interrupted": self.interrupted,
            "held": self.held,
            "last_reading": self.last_reading,
            "last_verdict": self.last_verdict,
            "grade_cycles": self.grade_cycles,
            "held_cycles": self.held_cycles,
            "interrupted_cycles": self.interrupted_cycles,
            "basis": self.basis,
            "dose": self.dose,
        }
