"""Verdict helpers of the workspace gateway recovery oracle."""

from sregym.conductor.oracles.apps.workspace.gateway_recovery import mail_verdict, probes_meeting


def status(**ratios):
    return {"probes": {k.replace("_", "."): {"success_ratio": v, "samples": 60} for k, v in ratios.items()}}


def test_every_region_and_service_must_meet_the_floor():
    healthy = {f"{s}_{r}": 1.0 for s in ("mail", "chat") for r in "abc"}
    assert probes_meeting(status(**healthy), 0.99, 30) == (True, {})
    ok, shortfalls = probes_meeting(status(**{**healthy, "mail_b": 0.9}), 0.99, 30)
    assert not ok and list(shortfalls) == ["mail.b"]


def test_missing_probe_windows_count_as_failures():
    ok, shortfalls = probes_meeting({"probes": {}}, 0.99, 30, services=("mail",), regions=("a",))
    assert not ok and shortfalls["mail.a"]["samples"] == 0


def test_too_few_samples_do_not_pass():
    ok, _ = probes_meeting(status(mail_a=1.0), 0.99, 30, services=("mail",), regions=("a",))
    assert ok
    thin = {"probes": {"mail.a": {"success_ratio": 1.0, "samples": 3}}}
    assert not probes_meeting(thin, 0.99, 30, services=("mail",), regions=("a",))[0]


def test_mail_verdict_separates_bounced_missing_and_duplicated():
    verdict = mail_verdict(
        accepted=["<1>", "<2>", "<3>", "<4>", "<5>"],
        delivered=["<1>", "<2>", "<2>"],
        bounced=["<3>"],
        in_flight=["<5>"],
    )
    assert verdict == {"accepted": 4, "bounced": ["<3>"], "missing": ["<4>"], "duplicated": ["<2>"]}


def test_bounces_for_mail_we_never_sent_are_ignored():
    assert mail_verdict(["<1>"], ["<1>"], ["<other>"])["bounced"] == []


def report(rows):
    return "\n".join(
        f"t={t:4d}s clients=700 connected={c} ok={o} fail={f} dropped=0 p50=2ms p99=3ms" for t, c, o, f in rows
    )


def test_a_unit_that_holds_its_connections_passes_the_load_test():
    from sregym.conductor.oracles.apps.workspace.gateway_recovery import unit_load_verdict

    rows = [(t, 700, 10, 0) for t in range(1, 151)]
    assert unit_load_verdict(report(rows), 700)["held"]


def test_a_unit_that_sheds_connections_after_the_ramp_fails():
    from sregym.conductor.oracles.apps.workspace.gateway_recovery import unit_load_verdict

    rows = [(t, 700 if t < 100 else 250, 3, 70 if t >= 100 else 0) for t in range(1, 151)]
    verdict = unit_load_verdict(report(rows), 700)
    assert not verdict["held"] and verdict["lowest_connected"] == 250


def test_ramp_up_failures_before_the_settle_point_are_ignored():
    from sregym.conductor.oracles.apps.workspace.gateway_recovery import unit_load_verdict

    rows = [(t, min(700, t * 12), 10, 50 if t < 30 else 0) for t in range(1, 151)]
    assert unit_load_verdict(report(rows), 700)["held"]


def test_an_empty_report_fails():
    from sregym.conductor.oracles.apps.workspace.gateway_recovery import unit_load_verdict

    assert not unit_load_verdict("", 700)["held"]
