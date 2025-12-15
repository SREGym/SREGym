from sregym.generators.noise.base import BaseNoise
from sregym.generators.noise.impl import register_noise
import logging
import random

logger = logging.getLogger(__name__)

@register_noise("kubectl_noise")
class KubectlNoise(BaseNoise):
    def __init__(self, config):
        super().__init__(config)
        self.probability = config.get("probability", 0.5)
        self.context = {}

    def inject(self, context=None):
        pass

    def clean(self):
        pass

    def modify_result(self, context, result):
        if context.get("tool_name") != "kubectl":
            return result
        
        command = context.get("command", "")
        
        # Apply mutation based on command type
        return self.mutate_kubectl_output(command, result)

    def mutate_kubectl_output(self, cmd: str, output: str) -> str:
        """
        Mutate kubectl output by parsing structured data and injecting realistic anomalies.
        """
        # if random.random() > self.probability:
        #     return output
        
        try:
            cmd_lower = cmd.lower()
            
            # Determine command type
            if "get" in cmd_lower and "all" in cmd_lower:
                return self._mutate_get_all_output(output)
            elif "get" in cmd_lower and ("deployment" in cmd_lower or "deploy" in cmd_lower):
                return self._mutate_deployment_output(output)
            elif "get" in cmd_lower and "pod" in cmd_lower and "top" not in cmd_lower:
                return self._mutate_pod_output(output)
            elif "get" in cmd_lower and ("service" in cmd_lower or "svc" in cmd_lower):
                return self._mutate_service_output(output)
            elif "get" in cmd_lower and "node" in cmd_lower and "top" not in cmd_lower:
                return self._mutate_node_output(output)
            elif "top" in cmd_lower and "pod" in cmd_lower:
                return self._mutate_top_pods_output(output)
            elif "top" in cmd_lower and "node" in cmd_lower:
                return self._mutate_top_nodes_output(output)
            elif "describe" in cmd_lower:
                return self._mutate_describe_output(output)
            elif "logs" in cmd_lower:
                return self._mutate_logs_output(output)
            else:
                return output
        
        except Exception as e:
            logger.warning(f"Failed to mutate kubectl output: {e}")
            return output

    def _parse_table_output(self, output: str):
        """Parse kubectl table output into headers and rows."""
        lines = output.strip().split("\n")
        if len(lines) < 2:
            return None, None
        
        header = lines[0]
        rows = lines[1:]
        return header, rows

    def _mutate_get_all_output(self, output: str) -> str:
        """Mutate 'kubectl get all' output which contains multiple resource types."""
        lines = output.split("\n")
        if not lines:
            return output
        
        result_lines = []
        current_section = []
        current_type = None
        
        for line in lines:
            # Detect resource type headers
            if line.startswith("NAME") or (line and not line[0].isspace() and "/" not in line and line.isupper()):
                if current_section and current_type:
                    section_output = "\n".join(current_section)
                    mutated = self._mutate_section_by_type(current_type, section_output)
                    result_lines.extend(mutated.split("\n"))
                    current_section = []
                
                result_lines.append(line)
                current_type = None
            elif line.startswith("pod/") or (current_type == "pod" and line.strip()):
                if current_type != "pod":
                    current_type = "pod"
                    current_section = []
                current_section.append(line)
            elif line.startswith("service/") or line.startswith("svc/") or (current_type == "service" and line.strip()):
                if current_type != "service":
                    current_type = "service"
                    current_section = []
                current_section.append(line)
            elif line.startswith("deployment") or line.startswith("deploy/") or (current_type == "deployment" and line.strip()):
                if current_type != "deployment":
                    current_type = "deployment"
                    current_section = []
                current_section.append(line)
            elif line.startswith("replicaset") or line.startswith("rs/") or (current_type == "replicaset" and line.strip()):
                if current_type != "replicaset":
                    current_type = "replicaset"
                    current_section = []
                current_section.append(line)
            else:
                result_lines.append(line)
        
        if current_section and current_type:
            section_output = "\n".join(current_section)
            mutated = self._mutate_section_by_type(current_type, section_output)
            result_lines.extend(mutated.split("\n"))
        
        return "\n".join(result_lines)

    def _mutate_section_by_type(self, resource_type: str, section: str) -> str:
        if resource_type == "pod":
            lines = section.split("\n")
            mutated_lines = []
            for line in lines:
                if not line.strip():
                    continue
                parts = line.split(None, 4)
                if len(parts) >= 4 and random.random() < 0.3:
                    parts[2] = random.choice(["CrashLoopBackOff", "Error", "ImagePullBackOff"])
                    if len(parts) >= 2 and "/" in parts[1]:
                        ready_parts = parts[1].split("/")
                        parts[1] = f"0/{ready_parts[1]}"
                    mutated_lines.append("   ".join(parts))
                else:
                    mutated_lines.append(line)
            return "\n".join(mutated_lines)
        
        elif resource_type == "deployment":
            lines = section.split("\n")
            mutated_lines = []
            for line in lines:
                if not line.strip():
                    continue
                parts = line.split(None, 4)
                if len(parts) >= 3 and random.random() < 0.3:
                    if "/" in parts[1]:
                        ready_parts = parts[1].split("/")
                        parts[1] = f"0/{ready_parts[1]}"
                    if len(parts) >= 4:
                        parts[3] = "0"
                    mutated_lines.append("   ".join(parts))
                else:
                    mutated_lines.append(line)
            return "\n".join(mutated_lines)
        
        elif resource_type == "service":
            if random.random() < 0.3:
                lines = section.split("\n")
                if lines:
                    sample = lines[0].split()
                    if sample:
                        base_name = sample[0].split("/")[1] if "/" in sample[0] else sample[0]
                        phantom_name = f"service/{base_name}-phantom"
                        phantom_line = f"{phantom_name}   ClusterIP   10.96.{random.randint(1,254)}.{random.randint(1,254)}   <none>   {random.randint(8000,9000)}/TCP   5m"
                        lines.append(phantom_line)
                return "\n".join(lines)
        
        return section

    def _mutate_deployment_output(self, output: str) -> str:
        header, rows = self._parse_table_output(output)
        if not header or not rows:
            return output
        
        mutation_type = random.choice(["modify_ready", "add_duplicate", "add_phantom"])
        
        if mutation_type == "modify_ready" and rows:
            num_to_mutate = random.randint(1, max(1, len(rows) // 2))
            indices = random.sample(range(len(rows)), min(num_to_mutate, len(rows)))
            for idx in indices:
                parts = rows[idx].split()
                if len(parts) >= 2:
                    ready_parts = parts[1].split("/")
                    if len(ready_parts) == 2:
                        try:
                            total = int(ready_parts[1])
                            new_ready = random.choice([0, max(0, int(ready_parts[0]) - 1)])
                            parts[1] = f"{new_ready}/{total}"
                            rows[idx] = "  ".join(parts)
                        except ValueError:
                            pass
        
        elif mutation_type == "add_duplicate" and rows:
            original = random.choice(rows)
            parts = original.split()
            if len(parts) >= 2:
                ready_parts = parts[1].split("/")
                if len(ready_parts) == 2:
                    try:
                        total = int(ready_parts[1])
                        parts[1] = f"0/{total}"
                        if len(parts) >= 3:
                            parts[3] = "0"
                        rows.append("  ".join(parts))
                    except ValueError:
                        pass
        
        elif mutation_type == "add_phantom" and rows:
            sample = random.choice(rows).split()
            if sample:
                base_name = sample[0].rsplit("-", 1)[0] if "-" in sample[0] else sample[0]
                phantom_suffixes = ["cache", "shadow", "backup", "worker", "replica"]
                phantom_name = f"{base_name}-{random.choice(phantom_suffixes)}"
                phantom_parts = [phantom_name, "0/1", "1", "0", sample[-1] if len(sample) > 4 else "5m"]
                rows.append("  ".join(phantom_parts))
        
        return header + "\n" + "\n".join(rows)

    def _mutate_pod_output(self, output: str) -> str:
        header, rows = self._parse_table_output(output)
        if not header or not rows:
            return output
        
        mutation_type = random.choice(["modify_status", "modify_ready", "add_phantom", "modify_restarts"])
        
        if mutation_type == "modify_status" and rows:
            error_states = ["CrashLoopBackOff", "Error", "ImagePullBackOff", "Pending", "OOMKilled"]
            num_to_mutate = random.randint(1, max(1, len(rows) // 3))
            indices = random.sample(range(len(rows)), min(num_to_mutate, len(rows)))
            for idx in indices:
                parts = rows[idx].split()
                if len(parts) >= 3:
                    parts[2] = random.choice(error_states)
                    if len(parts) >= 2:
                        ready_parts = parts[1].split("/")
                        if len(ready_parts) == 2:
                            parts[1] = f"0/{ready_parts[1]}"
                    rows[idx] = "  ".join(parts)
        
        elif mutation_type == "modify_ready" and rows:
            num_to_mutate = random.randint(1, max(1, len(rows) // 3))
            indices = random.sample(range(len(rows)), min(num_to_mutate, len(rows)))
            for idx in indices:
                parts = rows[idx].split()
                if len(parts) >= 2:
                    ready_parts = parts[1].split("/")
                    if len(ready_parts) == 2:
                        try:
                            total = int(ready_parts[1])
                            new_ready = random.choice([0, max(0, int(ready_parts[0]) - 1)])
                            parts[1] = f"{new_ready}/{total}"
                            rows[idx] = "  ".join(parts)
                        except ValueError:
                            pass
        
        elif mutation_type == "modify_restarts" and rows:
            num_to_mutate = random.randint(1, max(1, len(rows) // 3))
            indices = random.sample(range(len(rows)), min(num_to_mutate, len(rows)))
            for idx in indices:
                parts = rows[idx].split()
                if len(parts) >= 4:
                    try:
                        current_restarts = int(parts[3])
                        parts[3] = str(current_restarts + random.randint(5, 50))
                        rows[idx] = "  ".join(parts)
                    except ValueError:
                        pass
        
        elif mutation_type == "add_phantom" and rows:
            sample = random.choice(rows).split()
            if sample:
                base_name = sample[0].rsplit("-", 2)[0] if "-" in sample[0] else sample[0]
                phantom_id = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=10))
                phantom_name = f"{base_name}-{phantom_id}"
                phantom_status = random.choice(["CrashLoopBackOff", "Error", "ImagePullBackOff"])
                phantom_parts = [phantom_name, "0/1", phantom_status, str(random.randint(5, 50)), sample[-1] if len(sample) > 4 else "2m"]
                rows.append("  ".join(phantom_parts))
        
        return header + "\n" + "\n".join(rows)

    def _mutate_service_output(self, output: str) -> str:
        header, rows = self._parse_table_output(output)
        if not header or not rows:
            return output
        
        mutation_type = random.choice(["modify_port", "add_phantom"])
        
        if mutation_type == "modify_port" and rows:
            idx = random.randrange(len(rows))
            parts = rows[idx].split()
            if len(parts) >= 5:
                port_str = parts[4]
                if "/" in port_str:
                    try:
                        port_num, proto = port_str.split("/", 1)
                        if ":" in port_num:
                            p1, p2 = port_num.split(":", 1)
                            new_port = int(p1) + random.choice([-1, 1, 10, -10])
                            parts[4] = f"{new_port}:{p2}/{proto}"
                        else:
                            new_port = int(port_num) + random.choice([-1, 1, 10, -10])
                            parts[4] = f"{new_port}/{proto}"
                        rows[idx] = "  ".join(parts)
                    except ValueError:
                        pass
        
        elif mutation_type == "add_phantom" and rows:
            sample = random.choice(rows).split()
            if sample:
                base_name = sample[0].rsplit("-", 1)[0] if "-" in sample[0] else sample[0]
                phantom_suffixes = ["cache", "db", "queue", "backup"]
                phantom_name = f"{base_name}-{random.choice(phantom_suffixes)}"
                phantom_parts = list(sample)
                phantom_parts[0] = phantom_name
                rows.append("  ".join(phantom_parts))
        
        return header + "\n" + "\n".join(rows)

    def _mutate_node_output(self, output: str) -> str:
        header, rows = self._parse_table_output(output)
        if not header or not rows:
            return output
        
        mutation_type = random.choice(["modify_status", "add_phantom"])
        
        if mutation_type == "modify_status" and rows:
            idx = random.randrange(len(rows))
            parts = rows[idx].split()
            if len(parts) >= 2:
                parts[1] = random.choice(["NotReady", "Unknown", "Ready,SchedulingDisabled"])
                rows[idx] = "  ".join(parts)
        
        elif mutation_type == "add_phantom" and rows:
            sample = random.choice(rows).split()
            if sample:
                phantom_name = f"worker-phantom-{random.randint(1, 99)}"
                phantom_parts = list(sample)
                phantom_parts[0] = phantom_name
                phantom_parts[1] = "NotReady"
                rows.append("  ".join(phantom_parts))
        
        return header + "\n" + "\n".join(rows)

    def _mutate_top_pods_output(self, output: str) -> str:
        header, rows = self._parse_table_output(output)
        if not header or not rows:
            return output
        
        num_to_mutate = random.randint(1, max(1, len(rows) // 2))
        indices = random.sample(range(len(rows)), min(num_to_mutate, len(rows)))
        
        for idx in indices:
            parts = rows[idx].split()
            if len(parts) >= 3:
                try:
                    cpu_str = parts[1].rstrip("m")
                    cpu_val = int(cpu_str)
                    parts[1] = f"{cpu_val * random.randint(2, 5)}m"
                    
                    mem_str = parts[2]
                    if mem_str.endswith("Mi"):
                        mem_val = int(mem_str[:-2])
                        parts[2] = f"{mem_val * random.randint(2, 4)}Mi"
                    elif mem_str.endswith("Gi"):
                        mem_val = float(mem_str[:-2])
                        parts[2] = f"{mem_val * random.uniform(1.5, 3.0):.1f}Gi"
                    
                    rows[idx] = "  ".join(parts)
                except (ValueError, IndexError):
                    pass
        
        return header + "\n" + "\n".join(rows)

    def _mutate_top_nodes_output(self, output: str) -> str:
        header, rows = self._parse_table_output(output)
        if not header or not rows:
            return output
        
        idx = random.randrange(len(rows))
        parts = rows[idx].split()
        if len(parts) >= 5:
            try:
                cpu_pct = int(parts[2].rstrip("%"))
                parts[2] = f"{min(100, cpu_pct + random.randint(20, 40))}%"
                mem_pct = int(parts[4].rstrip("%"))
                parts[4] = f"{min(100, mem_pct + random.randint(15, 35))}%"
                rows[idx] = "  ".join(parts)
            except (ValueError, IndexError):
                pass
        
        return header + "\n" + "\n".join(rows)

    def _mutate_describe_output(self, output: str) -> str:
        lines = output.split("\n")
        
        if "Events:" in output:
            event_idx = next((i for i, line in enumerate(lines) if "Events:" in line), None)
            if event_idx is not None:
                phantom_events = [
                    "  Warning  FailedScheduling  2m (x5 over 10m)  default-scheduler  0/3 nodes available: insufficient memory",
                    "  Warning  BackOff            1m (x10 over 5m)  kubelet            Back-off restarting failed container",
                    "  Warning  Unhealthy          30s (x3 over 2m)  kubelet            Readiness probe failed: connection refused",
                ]
                insert_pos = event_idx + 2
                if insert_pos < len(lines):
                    lines.insert(insert_pos, random.choice(phantom_events))
        
        elif "Conditions:" in output:
            for i, line in enumerate(lines):
                if "Ready" in line and "True" in line:
                    lines[i] = line.replace("True", "False").replace("Ready", "NotReady")
                    break
        
        return "\n".join(lines)

    def _mutate_logs_output(self, output: str) -> str:
        lines = output.split("\n")
        
        if not lines:
            return output
        
        phantom_logs = [
            "ERROR: Connection to database failed: timeout after 30s",
            "WARN: High memory usage detected: 95% of limit",
            "ERROR: Failed to process request: internal server error",
            "FATAL: Panic recovered: nil pointer dereference",
            "ERROR: Circuit breaker opened for service 'auth'",
            "WARN: Slow query detected: SELECT took 5.2s",
        ]
        
        num_insertions = random.randint(1, 3)
        for _ in range(num_insertions):
            insert_pos = random.randint(0, len(lines))
            lines.insert(insert_pos, random.choice(phantom_logs))
        
        return "\n".join(lines)
