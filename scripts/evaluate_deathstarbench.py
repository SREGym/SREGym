"""Validate matched scale tiers, then run Codex and report outcome rates.

Run inside the DinD environment. Each problem is freshly deployed for validation
and for every agent attempt. Raw traces/phase ledgers stay in the normal results
tree. A failed lifecycle gates that problem's agent evaluation.
"""

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APPLICATIONS = ("hotel_reservation", "social_network", "gitea", "gitlab_ce", "mattermost", "stripe_marathon")
DEFAULT_APPLICATIONS = ("hotel_reservation", "social_network")
TIERS = ("legacy", "single", "replicated", "expanded")
SUPPORTED_TIERS = {app: TIERS for app in DEFAULT_APPLICATIONS} | {
    app: ("single", "replicated") for app in ("gitea", "gitlab_ce", "mattermost", "stripe_marathon")
}


#: Client packages each agent's driver imports. A missing helper package would
#: only surface as an ImportError inside the agent container, mid-attempt.
AGENT_CLIENT_SOURCES = {"codex": ("codex", "harness", "jev"), "claudecode": ("claudecode", "harness")}


def prepare_agent_registry(root, output, version, agent):
    """Pin the runtime-installed CLI without changing the user's registry."""
    import yaml

    source = Path(os.environ.get("SREGYM_AGENT_REGISTRY", root / "agents.yaml"))
    registry = yaml.safe_load(source.read_text())
    registration = next((entry for entry in registry["agents"] if entry["name"] == agent), None)
    if registration is None:
        raise ValueError(f"The selected agent registry has no {agent} registration")
    registration["agent_version"] = version
    destination = output / "agent-registry.yaml"
    # Registry entries may contain user configuration: publish atomically with
    # the private permissions supplied by NamedTemporaryFile.
    with tempfile.NamedTemporaryFile(mode="w", dir=output, delete=False) as stream:
        temporary = Path(stream.name)
        yaml.safe_dump(registry, stream, sort_keys=False)
    temporary.replace(destination)
    return destination


def prepare_agent_image(root, agent):
    """Use the checkout's client and helper modules on the released runtime."""
    from sregym.service.container_runner import DEFAULT_AGENT_IMAGE

    names = AGENT_CLIENT_SOURCES[agent]
    sources = sorted(p for name in names for p in (root / "clients" / name).rglob("*.py"))
    digest = hashlib.sha256(DEFAULT_AGENT_IMAGE.encode())
    for source in sources:
        digest.update(str(source.relative_to(root)).encode() + b"\0" + source.read_bytes())
    tag = f"sregym-{agent}-driver:{digest.hexdigest()[:16]}"
    with tempfile.TemporaryDirectory(prefix=f"sregym-{agent}-build-") as directory:
        context = Path(directory)
        for source in sources:
            target = context / source.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        (context / "Dockerfile").write_text(f"FROM {DEFAULT_AGENT_IMAGE}\nCOPY clients/ /opt/sregym/clients/\n")
        subprocess.run(["docker", "build", "--tag", tag, str(context)], check=True)
    return tag


def problem_id(app, tier, incident="wrong_service_selector"):
    if app == "gitlab_ce" and tier == "expanded" and incident == "notification_delayed_audit":
        return "gitlab_notification_delayed_audit_expanded"
    if incident in (
        "notification_recovery",
        "notification_ambiguity",
        "notification_intermittent",
        "notification_delayed_audit",
    ):
        if app != "gitlab_ce" or tier != "replicated":
            raise ValueError("Notification recovery supports GitLab CE replicated only")
        return f"gitlab_{incident}_replicated"
    if incident == "database_deletion":
        if app not in ("gitea", "gitlab_ce") or tier not in SUPPORTED_TIERS[app]:
            raise ValueError("Database deletion supports Gitea and GitLab CE single and replicated tiers")
        prefix = "gitlab" if app == "gitlab_ce" else "gitea"
        return f"{prefix}_database_deletion_{tier}"
    if incident == "feature_config":
        if app != "stripe_marathon" or tier not in SUPPORTED_TIERS[app]:
            raise ValueError("Feature configuration supports Stripe single and replicated tiers")
        return f"stripe_feature_config_{tier}"
    if incident != "wrong_service_selector":
        raise ValueError(f"Unknown incident: {incident}")
    return f"wrong_service_selector_{app}" + (f"_{tier}" if tier != "legacy" else "")


def summarize(rows, attempts):
    # Do not count infrastructure failures or missing grades as evidence of difficulty.
    complete = [
        row
        for row in rows
        if row.get("run_status") == "complete"
        and row.get("Mitigation.success", "").lower() in {"true", "false"}
        and row.get("deploy_failed", "").lower() != "true"
        and row.get("Mitigation.failure_class") not in {"environment_error", "harness_error"}
    ]
    successes = sum(row["Mitigation.success"].lower() == "true" for row in complete)
    ambiguous = sum(
        row["Mitigation.success"].lower() == "false" and row.get("Mitigation.failure_class") != "agent_error"
        for row in complete
    )
    return {
        "requested": attempts,
        "complete": len(complete),
        "successes": successes,
        "mitigation_pass_rate": successes / len(complete) if complete else None,
        "difficulty": 1 - successes / len(complete) if complete else None,
        "ambiguous_failures": ambiguous,
        "inconclusive": len(complete) != attempts or ambiguous > 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_CLIENT_SOURCES),
        default="codex",
        help="Agent client to evaluate. Attempts from different agents are not one cohort",
    )
    parser.add_argument("--model", required=True, help="Explicit model recorded in every comparison")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument(
        "--incident",
        choices=(
            "wrong_service_selector",
            "database_deletion",
            "feature_config",
            "notification_recovery",
            "notification_ambiguity",
            "notification_intermittent",
            "notification_delayed_audit",
        ),
        default="wrong_service_selector",
    )
    parser.add_argument("--applications", nargs="+", choices=APPLICATIONS, default=list(DEFAULT_APPLICATIONS))
    parser.add_argument(
        "--tiers", nargs="+", choices=TIERS, help="Defaults to the selected application's supported tiers"
    )
    parser.add_argument("--profile", choices=("full", "svelte"), default="full")
    parser.add_argument("--agent-timeout", type=int, default=900)
    parser.add_argument(
        "--agent-image", help="Override the automatically built image containing this checkout's agent driver"
    )
    parser.add_argument(
        "--agent-version", help="Pin the runtime-installed agent CLI version using a private registry copy"
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("results/deathstarbench"))
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be positive")
    for app in args.applications:
        delayed_audit_tiers = (
            args.incident == "notification_delayed_audit"
            and app == "gitlab_ce"
            and args.tiers
            and set(args.tiers) <= {"replicated", "expanded"}
        )
        if (
            args.incident
            in (
                "notification_recovery",
                "notification_ambiguity",
                "notification_intermittent",
                "notification_delayed_audit",
            )
            and not delayed_audit_tiers
            and (app != "gitlab_ce" or args.tiers != ["replicated"])
        ):
            tiers = "replicated and/or expanded" if args.incident == "notification_delayed_audit" else "replicated"
            parser.error(f"--incident {args.incident} requires --applications gitlab_ce --tiers {tiers}")
        if args.incident == "database_deletion" and app not in ("gitea", "gitlab_ce"):
            parser.error("--incident database_deletion requires --applications gitea and/or gitlab_ce")
        if args.incident == "feature_config" and app != "stripe_marathon":
            parser.error("--incident feature_config requires --applications stripe_marathon")
        if args.tiers and not delayed_audit_tiers and any(tier not in SUPPORTED_TIERS[app] for tier in args.tiers):
            parser.error(f"{app} supports only {SUPPORTED_TIERS[app]}")
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    agent_environment = os.environ.copy()
    registry_path = None
    if args.agent_version:
        registry_path = prepare_agent_registry(root, output, args.agent_version, args.agent)
        agent_environment["SREGYM_AGENT_REGISTRY"] = str(registry_path)
    agent_image = None if args.validate_only else args.agent_image or prepare_agent_image(root, args.agent)
    report = {
        "model": args.model,
        "attempts": args.attempts,
        "profile": args.profile,
        "incident": args.incident,
        "stages": ["mitigation"],
        "agent_image": agent_image,
        "requested_agent_version": args.agent_version,
        "agent_registry": str(registry_path) if registry_path else None,
        "agent_image_id": (
            subprocess.check_output(
                ["docker", "image", "inspect", "--format", "{{.Id}}", agent_image], text=True
            ).strip()
            if agent_image
            else None
        ),
        "agent": args.agent,
        "uses_checkout_driver": not args.agent_image,
        "driver_sha256": hashlib.sha256((root / "clients" / args.agent / "driver.py").read_bytes()).hexdigest(),
        "results": {},
    }
    for app in args.applications:
        for tier in args.tiers or SUPPORTED_TIERS[app]:
            pid = problem_id(app, tier, args.incident)
            validation = output / f"{pid}.validation.json"
            command = [
                sys.executable,
                "tests/integration/validate_problem.py",
                "--problem",
                pid,
                "--profile",
                args.profile,
                "--summary",
                str(output / f"{pid}.validation.md"),
                "--json-summary",
                str(validation),
            ]
            with (output / f"{pid}.validation.log").open("w") as log:
                validated = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT).returncode == 0
            result = {"validated": validated, "application": app, "tier": tier}
            report["results"][pid] = result
            if validated and not args.validate_only:
                start = time.time()
                command = [
                    sys.executable,
                    "main.py",
                    "--problem",
                    pid,
                    "--agent",
                    args.agent,
                    "--model",
                    args.model,
                    "--n-attempts",
                    str(args.attempts),
                    "--profile",
                    args.profile,
                    "--stages",
                    "mitigation",
                    "--agent-timeout",
                    str(args.agent_timeout),
                    "--agent-image",
                    agent_image,
                ]
                with (output / f"{pid}.agent.log").open("w") as log:
                    result["agent_exit_code"] = subprocess.run(
                        command, cwd=root, stdout=log, stderr=subprocess.STDOUT, env=agent_environment
                    ).returncode
                csvs = [
                    p
                    for p in (root / "results").glob(f"*/{args.agent}/{pid}/{pid}_{args.agent}_results.csv")
                    if p.stat().st_mtime >= start
                ]
                rows = []
                for path in csvs:
                    with path.open() as stream:
                        rows.extend(csv.DictReader(stream))
                result.update(summarize(rows, args.attempts))
                result["csvs"] = [str(p.relative_to(root)) for p in csvs]
            (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
            print(pid, json.dumps(result), flush=True)
    lines = [
        f"`{args.agent}` `{args.model}`, {args.attempts} requested attempts per tier, `{args.profile}` profile.",
        "",
        "| Application | Tier | Lifecycle | Complete | Mitigation passes | Difficulty (1 − pass rate) |",
        "|---|---|---|---|---|---|",
    ]
    for result in report["results"].values():
        difficulty = result.get("difficulty")
        display = f"{difficulty:.0%}" if difficulty is not None and not result.get("inconclusive") else "inconclusive"
        lines.append(
            f"| {result['application']} | {result['tier']} | {result['validated']} | "
            f"{result.get('complete', 0)}/{args.attempts} | {result.get('successes', '—')} | {display} |"
        )
    lines += [
        "",
        "Three attempts are an exploratory screen, not a statistically reliable difficulty ranking.",
        "Compare tiers within an incident family; different faults and oracle contracts are not controlled comparisons.",
    ]
    (output / "comparison.md").write_text("\n".join(lines) + "\n")
    return int(
        any(
            not r["validated"] or (not args.validate_only and r.get("inconclusive", True))
            for r in report["results"].values()
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
