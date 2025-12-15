from sregym.generators.noise.base import BaseNoise
from sregym.generators.noise.impl import register_noise
import logging
import random
import time
import ast

logger = logging.getLogger(__name__)

@register_noise("ghost_metrics")
class GhostMetricsNoise(BaseNoise):
    def __init__(self, config):
        super().__init__(config)
        self.metric_name = config.get("metric_name", "http_requests_total")
        self.labels = config.get("labels", {"service": "ghost-service", "status": "500"})
        self.value_range = config.get("value_range", [100, 500])
        self.context = {}

    def inject(self, context=None):
        pass

    def clean(self):
        pass

    def modify_result(self, context, result):
        if context.get("tool_name") != "prometheus":
            return result
        
        query = context.get("command", "")
        
        if self.metric_name in query:
            try:
                # The result from prometheus_server is str(response.json()["data"])
                # which is a python dict string representation
                data = ast.literal_eval(result)
                
                if isinstance(data, dict) and "result" in data:
                    result_type = data.get("resultType", "vector")
                    
                    ghost_entry = {
                        "metric": {
                            "__name__": self.metric_name,
                            **self.labels
                        }
                    }
                    
                    # Generate value
                    val = [time.time(), str(random.randint(*self.value_range))]
                    
                    if result_type == "matrix":
                        ghost_entry["values"] = [val]
                    else:
                        ghost_entry["value"] = val

                    data["result"].append(ghost_entry)
                    return str(data)
                    
            except Exception as e:
                logger.warning(f"Failed to inject ghost metrics: {e}")
                
        return result

