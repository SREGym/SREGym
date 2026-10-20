from sregym.service.apps.openstack.ledger import SLO, error_class, summarize, violations


def _record(t, op, ok, tenant="tenant-01", latency=1.0, error=None):
    return {"t": t, "end": t + latency, "tenant": tenant, "op": op, "ok": ok, "latency_s": latency, "error": error}


SLO_ = SLO(
    min_success={"boot": 0.9, "pet_reboot": 0.95}, min_attempts={"boot": 2}, tenant_min_success={"pet_reboot": 0.8}
)


def test_window_counts_operations_that_start_inside_it():
    records = [
        _record(5, "boot", False),
        _record(10, "boot", True),
        _record(20, "boot", True),
        _record(31, "boot", False),
    ]
    summary = summarize(records, 10, 30)
    assert summary["by_op"]["boot"]["attempts"] == 2
    assert summary["by_op"]["boot"]["success_ratio"] == 1.0


def test_too_few_attempts_is_a_violation_not_a_pass():
    summary = summarize([_record(10, "boot", True), *[_record(11 + i, "pet_reboot", True) for i in range(5)]], 0, 100)
    assert violations(summary, SLO_) == ["boot: 1 attempts < 2 required"]


def test_failure_concentrated_on_one_tenant_is_not_averaged_away():
    records = [_record(i, "boot", True) for i in range(4)]
    # 19 of 20 reboots succeed overall (meets 0.95), but tenant-03 loses half of its own.
    records += [_record(10 + i, "pet_reboot", True, tenant="tenant-01") for i in range(18)]
    records += [
        _record(40, "pet_reboot", True, tenant="tenant-03"),
        _record(41, "pet_reboot", False, tenant="tenant-03"),
    ]
    problems = violations(summarize(records, 0, 100), SLO_)
    assert len(problems) == 1 and problems[0].startswith("tenant-03 pet_reboot: success 0.500")


def test_error_classes_drop_identifiers_and_numbers():
    assert error_class("status=REBOOT task_state=rebooting") == "status REBOOT task_state rebooting"
    assert error_class("reboot rejected: HTTP 409 Cannot 'reboot' instance 1b2c3d") == "reboot rejected"
    assert error_class(None) == "unknown"


def test_latency_quantiles():
    summary = summarize([_record(i, "boot", True, latency=float(i)) for i in range(1, 11)], 0, 100)
    assert summary["by_op"]["boot"]["p50_s"] == 5.0
    assert summary["by_op"]["boot"]["p95_s"] == 10.0
