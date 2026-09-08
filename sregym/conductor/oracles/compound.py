from sregym.conductor.oracles.base import Oracle
from sregym.conductor.oracles.failure import FailureClass, worst


class CompoundedOracle(Oracle):
    importance = 1.0

    def __init__(self, problem, *args, **kwargs):
        super().__init__(problem)
        self.oracles = dict()
        for i, oracle in enumerate(args):
            if not isinstance(oracle, Oracle):
                raise TypeError(f"Argument {i} is not an instance of Oracle: {oracle}")
            self.oracles[str(i) + "-" + oracle.__class__.__name__] = oracle
        for key, oracle in kwargs.items():
            if not isinstance(oracle, Oracle):
                raise TypeError(f"Keyword argument '{key}' is not an instance of Oracle: {oracle}")
            if key in self.oracles:
                raise ValueError(f"Duplicate oracle key: {key}")
            self.oracles[key] = oracle

    def capture_baseline(self) -> None:
        for oracle in self.oracles.values():
            oracle.capture_baseline()

    def evaluate(self, *args, **kwargs):
        result = {
            "success": True,
            "oracles": [],
            "accuracy": 0.0,
        }

        total_weight = sum(getattr(oracle, "importance", 1.0) for oracle in self.oracles.values())

        for key, oracle in self.oracles.items():
            try:
                res = oracle.evaluate(*args, **kwargs)
                res["name"] = key
                result["oracles"].append(res)

                if not res.get("success", False):
                    result["success"] = False

                accuracy_weight = getattr(oracle, "importance", 1.0) / total_weight
                if "accuracy" in res:
                    result["accuracy"] += res["accuracy"] * accuracy_weight
                else:
                    accuracy = 100.0 if res.get("success", False) else 0.0
                    result["accuracy"] += accuracy * accuracy_weight

            except Exception as e:
                print(f"[❌] Error during evaluation of oracle '{key}': {e}")
                result["success"] = False
                # A child that raised produced no verdict, so this is our fault
                # rather than the agent's or the cluster's. Recording the
                # exception text as well: the child never got to print anything
                # useful, so this is the only trace of what went wrong.
                result["oracles"].append(
                    {
                        "name": key,
                        "success": False,
                        "reason": "oracle_raised",
                        "failure_class": FailureClass.HARNESS_ERROR,
                        "error": f"{type(e).__name__}: {e}",
                    }
                )

        if result["accuracy"] > 100.0 - 1e-3:
            result["accuracy"] = 100.0
        elif result["accuracy"] < 0.0 + 1e-3:
            result["accuracy"] = 0.0

        if not result["success"]:
            result.update(self._aggregate_failure(result["oracles"]))
        return result

    @staticmethod
    def _aggregate_failure(child_results: list[dict]) -> dict:
        """Summarise why the children failed, at the top level of the verdict.

        Without this the per-oracle reason codes are invisible to anything
        downstream: the conductor merges only the top-level dict into
        ``self.results``, and the CSV flattens that -- so a compound problem
        would report a bare failure however carefully its children explained
        themselves.

        ``failure_class`` takes the dominating class rather than the first or
        the most common one, because the classes are not peers: see the
        precedence rationale in ``failure.py``.

        ``reason`` joins the failing children's reasons, so unlike a single
        oracle's it is not a stable code -- ``failure_class`` is the column to
        filter on for compound problems, and ``reason`` is for reading. Which
        child said what is deliberately not repeated here: the CSV flattens one
        level deep, so a nested list would stringify, and ``oracles`` already
        carries the children verbatim.
        """
        failures = [r for r in child_results if not r.get("success", False)]
        reasons = [r["reason"] for r in failures if r.get("reason")]
        classes = [r["failure_class"] for r in failures if r.get("failure_class")]

        summary = {"failure_class": worst(classes)}
        if reasons:
            summary["reason"] = "+".join(reasons)
        return summary
