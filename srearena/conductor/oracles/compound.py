from srearena.conductor.oracles.base import Oracle


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
                result["oracles"].append(
                    {
                        "name": key,
                        "success": False,
                    }
                )

        if result["accuracy"] > 100.0 - 1e-3:
            result["accuracy"] = 100.0
        elif result["accuracy"] < 0.0 + 1e-3:
            result["accuracy"] = 0.0
        return result
