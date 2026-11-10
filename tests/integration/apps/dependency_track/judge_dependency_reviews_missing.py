"""Calibrate the mechanism-gated diagnosis judge of dependency_track_dependency_reviews_missing.

Grades a correct, a correct-but-terse and three plausible-but-wrong diagnoses
(all name the same component and symptom; only the mechanism differs), each
``--repeat`` times (default 2). All correct ones must pass and all wrong ones
must fail. The expected root cause is the problem's default text, or, with
``--expected-from``, the incident-specific text a validation run recorded
(stage ``expected_root_cause``):

    JUDGE_MODEL_ID=anthropic/claude-opus-5-5 ANTHROPIC_API_KEY=... \
    python tests/integration/apps/dependency_track/judge_dependency_reviews_missing.py results/judge.json \
        [--expected-from results/validation.json]
"""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from sregym.conductor.oracles.apps.dependency_track.diagnosis import MechanismGatedJudgeOracle  # noqa: E402
from sregym.conductor.problems.apps.dependency_track.dependency_reviews_missing import root_cause_text  # noqa: E402

DIAGNOSES = {
    "correct": (
        "dependency-review's audit warns that checkout-web 5.13.0 has 2 vulnerable dependencies without review "
        "(micromatch 6.27.19 / INT-2026-0107 and axios / INT-2026-0071). The findings and the HIGH policy "
        "violation exist and the violation-tracker got its ticket, and every other release (payments-api 2.15.0 "
        "before, notification-worker 0.20.0 after) got its review items, so the rule and the webhook work. There "
        "is no publish-notification run for a NEW_VULNERABLE_DEPENDENCY of checkout-web 5.13.0: Dependency-Track "
        "never created the notification. CI uploaded the SBOM at 07:41:10, and at 07:41:13 the platform team's "
        "memory-limit change rolled dt-api-server; the import-bom run of that upload completed only at 07:43:20, "
        "and the new pod logged the checkout-web 5.13.0 import right after it started, so the import was executed "
        "a second time after the old pod was terminated. The first execution had already committed the "
        "components. On the re-execution all components already existed, so ImportBomActivity saw no new "
        "components (the 'new' marker is only kept in memory during the execution that inserted them), stored no "
        "vulnerability-analysis context and created the analyze-project run without context file metadata (other "
        "uploads' runs have it). NEW_VULNERABLE_DEPENDENCY is only emitted from that context during "
        "reconciliation, so it was never emitted, and re-analysis or re-uploads will not emit it because the "
        "components are no longer new. Fix: delete the two vulnerable components from the project and upload the "
        "release BOM again so they are imported as new components; the analysis then emits the notifications."
    ),
    "terse": (
        "The api-server rollout interrupted checkout-web 5.13.0's BOM import after its components were already "
        "saved, and the new pod re-ran the import. On that second run the components already existed, so none was "
        "treated as new and the analysis was created without the new-components context; NEW_VULNERABLE_DEPENDENCY "
        "is only sent for new components, so it never fired for micromatch and axios. Deleted the two components "
        "and re-uploaded the BOM."
    ),
    "wrong_publish_lost": (
        "checkout-web 5.13.0 has no review items although its findings exist. The dt-api-server rollout at "
        "07:41 happened while Dependency-Track was publishing that release's notifications: the "
        "NEW_VULNERABLE_DEPENDENCY notifications were in the notification outbox / publisher when the pod was "
        "terminated, the webhook deliveries to dependency-review were interrupted mid-flight and the in-memory "
        "publish queue was lost at shutdown, so they were never retried. The POLICY_VIOLATION alert happened to be "
        "delivered before the shutdown. Fix: re-analyse the release so the notifications are published again, and "
        "make the publisher retry failed webhooks."
    ),
    "wrong_dedup": (
        "dependency-review has nothing for checkout-web 5.13.0 because Dependency-Track only sends "
        "NEW_VULNERABLE_DEPENDENCY the first time a vulnerable dependency enters the portfolio. micromatch 6.27.19 "
        "and axios were already present in earlier checkout-web builds, so Dependency-Track deduplicated the alert "
        "for 5.13.0; the coinciding api-server rollout is unrelated. This is expected behaviour of the rule; the "
        "review queue should also subscribe NEW_VULNERABILITY. Fix: add NEW_VULNERABILITY to the "
        "dependency-review rule and re-analyse the release."
    ),
    "wrong_generic": (
        "The dt-api-server rollout at 07:41 interrupted Dependency-Track while it was processing checkout-web "
        "5.13.0, and some in-flight work was lost, so the NEW_VULNERABLE_DEPENDENCY notifications for micromatch "
        "and axios never reached dependency-review. Rollouts during processing are unsafe. Fix: re-run the "
        "release pipeline and avoid configuration changes during releases."
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
