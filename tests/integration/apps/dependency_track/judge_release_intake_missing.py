"""Calibrate the mechanism-gated diagnosis judge of dependency_track_release_intake_missing.

Grades a correct, a correct-but-terse and three plausible-but-wrong diagnoses
(an api-server rollout losing the notification, the second upload merely
superseding the first, moment treated as already known), each ``--repeat``
times (default 2). All correct ones must pass and all wrong ones must fail. The
expected root cause is the problem's default text, or, with ``--expected-from``,
the incident-specific text a validation run recorded (stage
``expected_root_cause``):

    JUDGE_MODEL_ID=anthropic/claude-opus-5-5 ANTHROPIC_API_KEY=... \
    python tests/integration/apps/dependency_track/judge_release_intake_missing.py results/judge.json \
        [--expected-from results/validation.json]
"""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from sregym.conductor.oracles.apps.dependency_track.diagnosis import MechanismGatedJudgeOracle  # noqa: E402
from sregym.conductor.problems.apps.dependency_track.release_intake_missing import root_cause_text  # noqa: E402

DIAGNOSES = {
    "correct": (
        "The violation-tracker audit reports one vulnerable dependency without an intake ticket for checkout-web "
        "5.13.0: moment 7.9.19 (INT-2026-0114, HIGH). The finding and its two violations exist and have SEC "
        "tickets, but Dependency-Track never published a NEW_VULNERABLE_DEPENDENCY notification for it (no "
        "publish-notification run). The release tag pipeline uploaded 5.13.0 at 08:41:12, and 17 s later the "
        "main pipeline (Renovate's micromatch 6.27.19 -> 6.28.1 security update) uploaded an SBOM under the same "
        "version. Import and analysis of one project are not serialized (import-bom vs analyze-project keys), so "
        "the second import ran while the release upload's vuln-analysis was in progress and deleted the "
        "micromatch 6.27.19 component, which that analysis had snapshotted and found vulnerable (INT-2026-0107). "
        "reconcile-vuln-analysis-results then failed all 6 attempts at COMMIT with 'violates foreign key "
        "constraint COMPONENTS_VULNERABILITIES_COMPONENT_FK, Key (COMPONENT_ID)=(48211) is not present in table "
        "COMPONENT' (the postgres log and the run history show it), so the first analyze-project run is FAILED "
        "and its NEW_VULNERABLE_DEPENDENCY notifications (for all components that import created) were never "
        "emitted. The second upload's analysis completed and created the findings and violations, but it only "
        "notifies NEW_VULNERABLE_DEPENDENCY for components its own import created, and moment already existed; "
        "re-analysis carries no such context. Fix: deleted the moment 7.9.19 component from checkout-web 5.13.0 "
        "and re-ran the release pipeline so the import creates it again; the intake ticket DEP-31 arrived."
    ),
    "terse": (
        "dt-api-server: the release upload of checkout-web 5.13.0 and the main-branch upload right after it "
        "(micromatch update) overlapped. The second import removed micromatch 6.27.19 while the first upload's "
        "vulnerability analysis was still running with it, so that analysis failed on a component foreign key "
        "error on every retry and its new-vulnerable-dependency notification for moment 7.9.19 was never sent; "
        "the second analysis doesn't send it because moment wasn't new to that import. Removed the moment "
        "component and re-uploaded the release SBOM."
    ),
    "wrong_rollout": (
        "The dt-api-server rollout at 08:48 (CPU request change) restarted the api-server while checkout-web "
        "5.13.0's notifications were still being published. The NEW_VULNERABLE_DEPENDENCY notification for moment "
        "7.9.19 (INT-2026-0114) was in flight in the old pod and was lost when it was terminated, so the "
        "violation-tracker never opened the intake ticket; the policy violation tickets had already been "
        "delivered. Notifications are not persisted across restarts. Fix: re-trigger the notification by "
        "deleting the moment component and re-uploading the release SBOM, and avoid api-server rollouts while "
        "uploads are being processed."
    ),
    "wrong_superseded": (
        "Two uploads of checkout-web 5.13.0 arrived 17 s apart: the release pipeline and the main pipeline "
        "(Renovate's micromatch update). Dependency-Track serializes analyses per project, so the second upload "
        "superseded the first one: the first analysis was cancelled and marked FAILED, and only the second "
        "upload's analysis ran. From the second import's point of view moment 7.9.19 already existed (the first "
        "import had created it), so it was not a new dependency and no NEW_VULNERABLE_DEPENDENCY notification was "
        "sent. This is expected deduplication behaviour; the CI should not upload twice per version. Fix: delete "
        "the moment component and re-upload the release SBOM."
    ),
    "wrong_known_component": (
        "moment is already a dependency of earlier checkout-web versions and of mobile-bff and storefront, so "
        "Dependency-Track did not treat moment 7.9.19 in checkout-web 5.13.0 as a newly introduced dependency and "
        "only reported the new vulnerability, not a NEW_VULNERABLE_DEPENDENCY. The analysis failure of the first "
        "upload is a transient database foreign key error that the second analysis recovered from. The intake "
        "register therefore misses it. Fix: delete the component and re-upload so it is new."
    ),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--expected-from", help="validation record whose expected_root_cause stage to use")
    parser.add_argument(
        "--expected-kwargs", help="JSON keyword arguments for the problem's root_cause_text (incident specifics)"
    )
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()

    problem = SimpleNamespace(namespace="dependency-track")
    if args.expected_from:
        expected = json.loads(Path(args.expected_from).read_text())["stages"]["expected_root_cause"]["text"]
    else:
        kwargs = json.loads(args.expected_kwargs) if args.expected_kwargs else {}
        expected = "[fault_spec] component=deployment/dt-api-server; namespace=dependency-track || " + (
            root_cause_text(**kwargs)
        )
    oracle = MechanismGatedJudgeOracle(problem=problem, expected=expected)
    results = {"expected": expected, "verdicts": {}}
    for name, text in DIAGNOSES.items():
        runs = []
        for _ in range(args.repeat):
            verdict = oracle.evaluate(text)
            runs.append(
                {
                    "success": verdict.get("success"),
                    "accuracy": verdict.get("accuracy"),
                    "required_items": verdict.get("required_items"),
                    "gated_by": verdict.get("gated_by"),
                    "dimensions": {k: v["score"] for k, v in (verdict.get("dimensions") or {}).items()},
                    "checklist": verdict.get("checklist"),
                }
            )
            print(name, runs[-1]["success"], runs[-1]["accuracy"], runs[-1]["required_items"], flush=True)
        results["verdicts"][name] = {"diagnosis": text, "runs": runs}
        Path(args.output).write_text(json.dumps(results, indent=1, default=str))
    summary = {name: [r["success"] for r in v["runs"]] for name, v in results["verdicts"].items()}
    print("SUMMARY", json.dumps(summary))


if __name__ == "__main__":
    main()
