import asyncio
import csv
import threading

from rich.console import Console

from api import run_api
from srearena.conductor.conductor import Conductor


def driver_loop(conductor: Conductor):
    """
    Deploy each problem and wait for submissions.
    Returns a list of flattened dicts with results per problem.
    """

    async def driver():
        console = Console()
        await asyncio.sleep(1)  # allow API to bind

        all_results = []
        for pid in conductor.problems.get_problem_ids():
            console.log(f"\n🔍 Starting problem: {pid}")
            conductor.problem_id = pid
            await conductor.start_problem()

            with console.status(f"⏳ Waiting for grading… (stage={conductor.submission_stage})") as status:
                while conductor.submission_stage != "done":
                    await asyncio.sleep(1)
                    status.update(f"⏳ Waiting for grading… (stage={conductor.submission_stage})")

            console.log(f"✅ Completed {pid}: results={conductor.results}")

            # Flatten and snapshot the results dict
            snapshot = {"problem_id": pid}
            for stage, outcome in conductor.results.items():
                if isinstance(outcome, dict):
                    for k, v in outcome.items():
                        snapshot[f"{stage}.{k}"] = v
                else:
                    snapshot[stage] = outcome
            all_results.append(snapshot)

        return all_results

    return asyncio.run(driver())


def main():
    conductor = Conductor()

    # 1) Launch the HTTP API once in a background thread
    threading.Thread(target=lambda: run_api(conductor, host="0.0.0.0", port=8000), daemon=True).start()
    print("📡 HTTP API server launching at http://localhost:8000")

    # 2) Run driver in main thread, collecting results
    results = driver_loop(conductor)

    # 3) Write out a CSV
    if results:
        # Gather all field names across snapshots
        fieldnames = sorted({key for row in results for key in row.keys()})
        csv_path = "srea_results.csv"
        with open(csv_path, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)

        print(f"✅ Benchmark complete! Results written to {csv_path}")
    else:
        print("⚠️ No results to write.")

    # 4) Exit (daemon API thread will shut down)
    print("Exiting.")


if __name__ == "__main__":
    main()
