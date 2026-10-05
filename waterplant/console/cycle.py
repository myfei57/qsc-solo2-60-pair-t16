"""One full treatment control cycle."""

from __future__ import annotations

from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import Sensor, mix, validate_flow
from waterplant.quota import validate_amount

from .http import Request, Response, json_response
from .runtime import Runtime


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the whole line once, in production order."""

    flow = request.float_field("flow")
    samples_raw = request.payload.get("samples")
    samples = None if samples_raw is None else request.float_list("samples")
    reading_id = request.str_field("reading_id")
    demand = request.float_field("demand")
    level = request.float_field("level")
    bed_id = request.str_field("bed_id")
    zone = request.int_field("zone")
    amount = request.float_field("amount")

    validate_flow(flow)
    validate_factor(rt.calibration.current())
    validate_zone(zone)
    validate_amount(amount)
    validate_level(level)

    rt.flow_repository.record(Sensor(flow=flow, turbidity=mix(samples or [])))
    rt.trend.record(flow)
    coag_dose = rt.coag_doser.update_flow_and_dose(flow)
    # Timing dependency: confirm the turbidity band first, then take the dose
    # computed against that band's verdict (not a re-computed raw reading).
    turb_outcome = rt.sampler.judge(samples, reading_id)
    turb_dose = turb_outcome.dose
    ph_verdict = rt.stabilizer.stabilize()
    rt.well.update_residual_demand(demand)
    chlor_dose = rt.chlor_doser.apply_residual() if ph_verdict.stable else 0.0
    min_level = rt.well.adjust_level(level, rt.inlet, rt.outlet)
    rotation = rt.backwash.order_rotation()
    duty = rt.bank.on_duty()
    if bed_id:
        rt.backwash.start(bed_id)
    quota_value = rt.accumulator.add(amount)

    return json_response(
        {
            "coag_dose": coag_dose,
            "turb_dose": turb_dose,
            "turb_grade": turb_outcome.grade,
            "turb_pending": turb_outcome.pending,
            "turb_held": turb_outcome.held,
            "turb_interrupted": turb_outcome.interrupted,
            "turb_grade_cycles": turb_outcome.grade_cycles,
            "turb_held_cycles": turb_outcome.held_cycles,
            "turb_interrupted_cycles": turb_outcome.interrupted_cycles,
            "turb_basis": turb_outcome.basis,
            "chlor_dose": chlor_dose,
            "min_level": min_level,
            "level": rt.well.level(),
            "quota": quota_value,
            "rotation": rotation,
            "on_duty": duty or "",
            "backwash": bed_id,
            "drains": rt.backwash.drain_count(),
            "audit_count": rt.auditor.count(),
            "ph_stable": ph_verdict.stable,
            "ph_adjustment": ph_verdict.adjustment,
        }
    )
