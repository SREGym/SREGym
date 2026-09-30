import copy
import json
import re
import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHECKS = runpy.run_path(str(ROOT / "docker/blueprint-hotel/test_behavior.py"))


@pytest.mark.parametrize("arch,machine", [("arm64", 183), ("amd64", 62)])
def test_executable_architecture_is_checked_independently_of_image_metadata(arch, machine):
    header = b"\x7fELF\x02\x01" + bytes(12) + machine.to_bytes(2, "little")
    CHECKS["check_elf"](header, arch)
    other = "amd64" if arch == "arm64" else "arm64"
    with pytest.raises(AssertionError):
        CHECKS["check_elf"](header, other)


def workload_fixture():
    rows, summaries = [], []
    for second, count in enumerate((100, 100, 300, 300, 100, 100)):
        for index in range(count):
            rows.append(
                {
                    "Start": str((1_700_000_000 + second) * 1_000_000_000 + index * 1_000_000_000 // count),
                    "Duration": str(1000 + index),
                    "IsError": "false",
                }
            )
        summaries.append([str(second), str(1000 + (count - 1) / 2), str(count)])
    return rows, summaries


def test_workload_smoke_checks_the_actual_traffic_phases_and_latency_output():
    rows, summaries = workload_fixture()
    assert CHECKS["check_workload_rows"](rows, summaries) == {0: 200, 1: 600, 2: 200}


@pytest.mark.parametrize("corruption", ["no-spike", "wrong-load-column", "wrong-average", "failed-request"])
def test_workload_smoke_rejects_broken_behavior(corruption):
    rows, summaries = copy.deepcopy(workload_fixture())
    if corruption == "no-spike":
        for index, row in enumerate(rows):
            row["Start"] = str(1_700_000_000 * 1_000_000_000 + index * 6_000_000_000 // len(rows))
    elif corruption == "wrong-load-column":
        summaries[2][2] = "100"
    elif corruption == "wrong-average":
        summaries[2][1] = "1"
    else:
        rows[0]["IsError"] = "true"
    with pytest.raises(AssertionError):
        CHECKS["check_workload_rows"](rows, summaries)


def test_all_nine_blueprint_releases_are_locked_and_used_by_the_manifests():
    lock = json.loads((ROOT / "docker/images.lock.json").read_text())
    expected = {}
    for service in CHECKS["SERVICES"]:
        reference = lock[f"blueprint-hotel-{service}"]
        assert re.fullmatch(rf"ghcr\.io/sregym/blueprint-hotel:\d{{8}}-{service}@sha256:[a-f0-9]{{64}}", reference)
        expected[reference] = 2 if service == "workload" else 1

    actual = dict.fromkeys(expected, 0)
    for path in (ROOT / "SREGym-applications/BlueprintHotelReservation").rglob("*.yaml"):
        for document in yaml.safe_load_all(path.read_text()):
            if not isinstance(document, dict) or document.get("kind") not in ("Deployment", "Job"):
                continue
            for container in document["spec"]["template"]["spec"]["containers"]:
                reference = container["image"]
                assert "777lefty/" not in reference, path
                if reference in actual:
                    actual[reference] += 1
    assert actual == expected
