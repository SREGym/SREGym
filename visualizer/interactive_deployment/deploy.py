import argparse
import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def list_problems():
    from sregym.conductor.problems.registry import ProblemRegistry

    registry = ProblemRegistry()
    for name in registry.PROBLEM_REGISTRY:
        print(name)


def deploy(application):
    from sregym.service.apps.app_registry import AppRegistry

    app_registry = AppRegistry()
    app = app_registry.get_app_instance(application)
    app.deploy()
    print(f"Deployed application: {application}")


def list_apps():
    from sregym.service.apps.app_registry import AppRegistry

    registry = AppRegistry()
    for name in registry.get_app_names():
        print(name)


async def run_problem(problem_id):
    from sregym.conductor.conductor import Conductor, ConductorConfig
    from sregym.generators.noise.manager import get_noise_manager

    config = ConductorConfig(deploy_loki=True)
    conductor = Conductor(config=config)
    conductor.problem_id = problem_id

    conductor.problem = conductor.problems.get_problem_instance(problem_id)
    conductor.app = conductor.problem.app

    print(f"[ATTACH] Attached to problem: {problem_id}")
    print(f"[ATTACH] Using namespace: {conductor.app.namespace}")

    conductor.get_problem_stages()
    conductor._build_stage_sequence()

    conductor._inject_fault()

    try:
        nm = get_noise_manager()
        context = {
            "namespace": conductor.app.namespace,
            "app_name": conductor.app.name,
        }
        nm.set_problem_context(context)
        nm.start_background_noises()
    except Exception as e:
        print(f"Failed to update NoiseManager context: {e}")

    conductor._advance_to_next_stage(start_index=0)

    print(f"[READY] Current stage: {conductor.submission_stage}")

    # print(f"Running problem: {problem}")
    # await conductor.start_problem()
    # print("Problem finished.")


def recover(problem):
    from sregym.conductor.conductor import Conductor, ConductorConfig

    config = ConductorConfig(deploy_loki=True)
    conductor = Conductor(config=config)
    conductor.problem_id = problem
    conductor.problem = conductor.problems.get_problem_instance(problem)
    conductor.app = conductor.problem.app
    print(f"[RECOVER] Attaching to problem: {problem}")

    try:
        conductor.problem.recover_fault()
        print("[RECOVER] Fault recovered")
    except Exception:
        print("[RECOVER] Failed")
    if conductor._baseline_captured:
        print("[CLEANUP] Reconciling cluster state to baseline...")
        try:
            changes = conductor.cluster_state.reconcile_to_baseline()
            if any(v for v in changes.values() if v):
                print(f"Cluster state reconciliation changes: {changes}")
            print("[CLEANUP] Cluster state reconciled")
        except Exception as e:
            print(f"Failed to reconcile cluster state: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["deploy", "run", "recover", "list-problems", "list-apps"])
    parser.add_argument("--application")
    parser.add_argument("--problem")

    args = parser.parse_args()

    if args.command == "list-problems":
        list_problems()

    elif args.command == "list-apps":
        list_apps()

    elif args.command == "deploy":
        deploy(args.application)

    elif args.command == "run":
        asyncio.run(run_problem(args.problem))
    elif args.command == "recover":
        recover(args.problem)
