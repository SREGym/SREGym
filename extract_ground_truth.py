#!/usr/bin/env python3
"""Extract ground truth for each problem and stage from SREGym problem definitions."""

import json
import sys
from pathlib import Path

# Add current directory to path
sys.path.append(str(Path(__file__).parent))

from sregym.conductor.problems.registry import ProblemRegistry


def extract_ground_truth():
    """Extract ground truth for all problems used in learning."""

    registry = ProblemRegistry()

    # Problems from the learning experiment (same as SREArena)
    problem_ids = [
        "social_net_hotel_res_astro_shop_concurrent_failures",
        "misconfig_app_hotel_res",
        "revoke_auth_mongodb-1",
        "astronomy_shop_ad_service_high_cpu",
        "valkey_memory_disruption",
        "network_policy_block",
        "duplicate_pvc_mounts_hotel_reservation",
    ]

    ground_truth = {}

    for problem_id in problem_ids:
        try:
            # Get problem instance
            problem = registry.get_problem_instance(problem_id)

            # Detection ground truth
            # All problems have faults injected, so detection should be "Yes"
            detection_ground_truth = "Yes"

            # Localization ground truth
            # SREGym uses diagnosis_oracle (LLMAsAJudgeOracle) with root_cause string
            # For localization, we extract from faulty_service attribute
            localization_ground_truth = None
            
            # Check if it's a MultipleIndependentFailures (has problems list)
            if hasattr(problem, "problems") and problem.problems:
                # Multiple failures - extract from all sub-problems
                all_expected = []
                for sub_problem in problem.problems:
                    if hasattr(sub_problem, "faulty_service") and sub_problem.faulty_service:
                        all_expected.append(sub_problem.faulty_service)
                localization_ground_truth = all_expected if all_expected else None
            # Check if it has faulty_service attribute directly
            elif hasattr(problem, "faulty_service") and problem.faulty_service:
                faulty_service = problem.faulty_service
                if isinstance(faulty_service, list):
                    localization_ground_truth = faulty_service
                else:
                    localization_ground_truth = [faulty_service]
            
            # For problems without explicit faulty_service, try to infer from root_cause
            if not localization_ground_truth and hasattr(problem, "root_cause") and problem.root_cause:
                # Try to extract service name from root_cause (heuristic)
                root_cause = problem.root_cause.lower()
                # Common patterns: "service `X`", "deployment `X`", "`X` service"
                import re
                # Look for backtick-quoted service names
                matches = re.findall(r'`([^`]+)`', root_cause)
                if matches:
                    # Filter out common non-service words
                    service_keywords = ["service", "deployment", "pod", "container", "image"]
                    services = [m for m in matches if any(kw in m.lower() for kw in service_keywords) or 
                              (len(m.split()) == 1 and not any(kw in m.lower() for kw in ["the", "a", "an", "is", "has", "are"]) and "-" in m)]
                    if services:
                        # Extract the service name (usually the first match that looks like a service)
                        potential_service = services[0].split()[0] if " " in services[0] else services[0]
                        # Clean up common prefixes/suffixes
                        potential_service = potential_service.replace("service", "").replace("deployment", "").strip("-")
                        if potential_service:
                            localization_ground_truth = [potential_service]
            
            # Mitigation ground truth
            mitigation_info = {}
            if hasattr(problem, "mitigation_oracle") and problem.mitigation_oracle:
                oracle = problem.mitigation_oracle
                oracle_type = type(oracle).__name__
                mitigation_info["oracle_type"] = oracle_type
                mitigation_info["description"] = get_mitigation_description(oracle_type, problem)

                # For compounded oracles, list all
                if hasattr(oracle, "oracles"):
                    mitigation_info["sub_oracles"] = []
                    for key, sub_oracle in oracle.oracles.items():
                        sub_type = type(sub_oracle).__name__
                        mitigation_info["sub_oracles"].append(
                            {
                                "name": key,
                                "type": sub_type,
                                "description": get_mitigation_description(sub_type, problem),
                            }
                        )
            else:
                mitigation_info["oracle_type"] = "None"
                mitigation_info["description"] = "No mitigation oracle attached"

            ground_truth[problem_id] = {
                "detection": {
                    "expected": detection_ground_truth,
                    "description": 'Detection should be "Yes" when fault is injected',
                },
                "localization": {
                    "expected": localization_ground_truth,
                    "description": "List of faulty service names that must be identified",
                },
                "mitigation": mitigation_info,
            }

            print(f"✅ Extracted ground truth for {problem_id}")
            if localization_ground_truth:
                print(f"   Localization: {localization_ground_truth}")

        except Exception as e:
            print(f"❌ Error extracting ground truth for {problem_id}: {e}")
            import traceback
            traceback.print_exc()
            ground_truth[problem_id] = {"error": str(e)}

    return ground_truth


def get_mitigation_description(oracle_type, problem):
    """Get description of what the mitigation oracle checks."""
    # Get policy name for network policy oracle
    policy_name = "unknown"
    if oracle_type == "NetworkPolicyMitigationOracle":
        if hasattr(problem, "policy_name"):
            policy_name = problem.policy_name
        elif hasattr(problem, "faulty_service"):
            policy_name = f"deny-all-{problem.faulty_service}"

    descriptions = {
        "MitigationOracle": "All pods must be Running and all containers must be Ready",
        "TargetPortMisconfigMitigationOracle": "Service targetPort must be reset to 9090 and all pods must be Running",
        "NetworkPolicyMitigationOracle": f'NetworkPolicy "{policy_name}" must be deleted',
        "ServiceEndpointMitigationOracle": "Service endpoint selector must be corrected",
        "CompoundedOracle": "Multiple oracles must all pass",
    }
    return descriptions.get(oracle_type, f"Checks specific mitigation requirements for {oracle_type}")


if __name__ == "__main__":
    print("=" * 80)
    print("EXTRACTING GROUND TRUTH FOR ALL PROBLEMS (SREGym)")
    print("=" * 80)
    print()

    ground_truth = extract_ground_truth()

    # Save to JSON
    output_file = Path("ground_truth_by_problem.json")
    with open(output_file, "w") as f:
        json.dump(ground_truth, f, indent=2, default=str)

    print()
    print("=" * 80)
    print("GROUND TRUTH SUMMARY")
    print("=" * 80)
    print()

    for problem_id, gt in ground_truth.items():
        if "error" in gt:
            print(f"❌ {problem_id}: {gt['error']}")
            continue

        print(f"\n📋 {problem_id}")
        print(f"  Detection:   {gt['detection']['expected']}")
        print(f"  Localization: {gt['localization']['expected']}")
        print(f"  Mitigation:  {gt['mitigation']['oracle_type']}")
        if "sub_oracles" in gt["mitigation"]:
            for sub in gt["mitigation"]["sub_oracles"]:
                print(f"    - {sub['name']}: {sub['type']}")

    print()
    print(f"✅ Ground truth saved to: {output_file}")

