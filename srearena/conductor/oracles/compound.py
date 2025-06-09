from srearena.conductor.oracles.base import Oracle


class CompoundedOracle(Oracle):
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
            self.oracles[key] = oracle

    def evaluate(self, *args, **kwargs):
        result = {
            "success": True,
            "oracles": [],
        }
        for key, oracle in self.oracles.items():
            try:
                res = oracle.evaluate(*args, **kwargs)
                res["name"] = key
                result["oracles"].append(res)
            except Exception as e:
                print(f"[❌] Error during evaluation of oracle '{key}': {e}")
                result["success"] = False
                result["oracles"].append(
                    {
                        "name": key,
                        "success": False,
                    }
                )
        return result
