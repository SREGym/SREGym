from sregym.generators.noise.base import BaseNoise
from sregym.generators.noise.impl import register_noise
import logging
import random
import ast

logger = logging.getLogger(__name__)

@register_noise("jaeger_noise")
class JaegerNoise(BaseNoise):
    def __init__(self, config):
        super().__init__(config)
        self.probability = config.get("probability", 0.5)

    def inject(self, context=None):
        pass

    def clean(self):
        pass

    def modify_result(self, context, result):
        if context.get("tool_name") != "jaeger":
            return result
        
        command = context.get("command", "")
        
        try:
            if "get_traces" in command:
                return self._mutate_traces(result)
            elif "get_services" in command:
                return self._mutate_services(result)
            elif "get_operations" in command:
                return self._mutate_operations(result)
        except Exception as e:
            logger.warning(f"Failed to mutate jaeger output: {e}")
            return result
        
        return result

    def _mutate_services(self, result):
        if result == "None":
            return result
        
        try:
            data = ast.literal_eval(result)
            if isinstance(data, list) and random.random() < self.probability:
                phantom_service = f"phantom-service-{random.randint(100, 999)}"
                data.append(phantom_service)
                return str(data)
        except:
            pass
        return result

    def _mutate_operations(self, result):
        if result == "None":
            return result
        
        try:
            data = ast.literal_eval(result)
            if isinstance(data, list) and random.random() < self.probability:
                phantom_op = f"phantom-op-{random.randint(100, 999)}"
                data.append(phantom_op)
                return str(data)
        except:
            pass
        return result

    def _mutate_traces(self, result):
        if result == "None":
            return result
            
        try:
            data = ast.literal_eval(result)
            
            # Modify data (list of traces)
            if isinstance(data, list):
                mutated = False
                for trace in data:
                    if random.random() < self.probability:
                        # Inject error in spans
                        spans = trace.get("spans", [])
                        if spans:
                            span = random.choice(spans)
                            # Add error tag
                            tags = span.get("tags", [])
                            # Check if error tag already exists
                            has_error = any(t.get('key') == 'error' for t in tags)
                            if not has_error:
                                tags.append({'key': 'error', 'type': 'bool', 'value': True})
                                span["tags"] = tags
                                mutated = True
                            
                            # Increase duration significantly to simulate latency
                            if "duration" in span:
                                span["duration"] *= random.randint(5, 20)
                                mutated = True
                
                if mutated:
                    return str(data)
            
            return result
        except Exception as e:
            logger.warning(f"Failed to mutate jaeger traces: {e}")
            return result
