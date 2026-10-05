"""Component health checks."""

from __future__ import annotations

from .runtime import Runtime


def run_checks(rt: Runtime) -> list[dict[str, str]]:
    """Return one check row per mutable component."""

    checks = [{"name": "store", "status": "ok", "detail": f"keys={rt.store.count()}"}]
    if rt.calibration.current() <= 0:
        checks.append({"name": "calibration", "status": "fail", "detail": "factor must be positive"})
    else:
        checks.append({"name": "calibration", "status": "ok", "detail": "factor set"})
    if rt.well.residual_target() <= 0:
        checks.append({"name": "residual", "status": "fail", "detail": "target must be positive"})
    else:
        checks.append({"name": "residual", "status": "ok", "detail": "target set"})
    if rt.accumulator.value() < 0:
        checks.append({"name": "quota", "status": "fail", "detail": "accumulator is negative"})
    else:
        checks.append({"name": "quota", "status": "ok", "detail": "accumulator set"})
    if rt.flow_repository.load_flow()[1]:
        checks.append({"name": "intake", "status": "ok", "detail": "flow reading present"})
    else:
        checks.append({"name": "intake", "status": "warn", "detail": "no flow reading yet"})
    if rt.stabilizer.is_stable():
        checks.append({"name": "ph", "status": "ok", "detail": "ph in the stable band"})
    else:
        checks.append({"name": "ph", "status": "warn", "detail": "ph outside the stable band"})
    turb = rt.sampler.state()
    if turb.interrupted:
        checks.append(
            {"name": "turbidity", "status": "warn", "detail": f"reading interrupted; holding {turb.grade}"}
        )
    elif turb.grade == "exceeded":
        checks.append(
            {"name": "turbidity", "status": "fail", "detail": f"exceeded for {turb.grade_cycles} cycles"}
        )
    elif turb.grade == "watch" or turb.pending:
        detail = f"watch for {turb.grade_cycles} cycles" if turb.grade == "watch" else f"pending {turb.pending}"
        checks.append({"name": "turbidity", "status": "warn", "detail": detail})
    else:
        checks.append({"name": "turbidity", "status": "ok", "detail": "normal band"})
    alerts = rt.inventory.needs_reorder()
    if alerts:
        checks.append(
            {"name": "inventory", "status": "warn", "detail": "reorder " + ", ".join(alerts)}
        )
    else:
        checks.append({"name": "inventory", "status": "ok", "detail": "stock above reorder level"})
    return checks
