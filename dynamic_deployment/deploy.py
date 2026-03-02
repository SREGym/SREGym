import argparse
import asyncio
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

def list_problems():
    from sregym.conductor.problems.registry import ProblemRegistry
    registry = ProblemRegistry()
    for name in registry.PROBLEM_REGISTRY.keys():
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

async def run_problem(problem):
    from sregym.conductor.conductor import Conductor, ConductorConfig

    config = ConductorConfig(deploy_loki=True)
    conductor = Conductor(config=config)
    conductor.problem_id = problem

    print(f"Running problem: {problem}")
    await conductor.start_problem()
    print("Problem finished.")


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["deploy", "run", "list-problems", "list-apps"])
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
    