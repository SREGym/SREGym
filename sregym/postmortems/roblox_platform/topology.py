"""Service graph and independently configurable scale dimensions."""

SERVICES = (
    "edge",
    "identity",
    "profiles",
    "inventory",
    "catalog",
    "assets",
    "matchmaking",
    "allocator",
    "sessions",
    "persistence",
    "economy",
    "outbox",
    "analytics",
    "telemetry",
    "placement",
)
TIERS = {
    "development": {"workers": 3, "replicas": 2, "players": 1000, "tenants": 8, "rps": 2},
    "expanded": {"workers": 6, "replicas": 4, "players": 10000, "tenants": 64, "rps": 8},
    "fleet": {"workers": 12, "replicas": 8, "players": 50000, "tenants": 256, "rps": 20},
}


def job(name, *, count, environment, command="application.py", cpu=200, memory=192):
    return {
        "ID": name,
        "Name": name,
        "Type": "service",
        "Datacenters": ["dc1"],
        "TaskGroups": [
            {
                "Name": name,
                "Count": count,
                "Constraints": [{"Operand": "distinct_hosts", "RTarget": "true"}],
                "RestartPolicy": {"Attempts": 3, "Interval": 60000000000, "Delay": 1000000000, "Mode": "delay"},
                "Networks": [{"Mode": "host", "DynamicPorts": [{"Label": "http"}]}],
                "Tasks": [
                    {
                        "Name": name,
                        "Driver": "docker",
                        "Config": {
                            "image": "sregym-platform:app",
                            "network_mode": "host",
                            "command": "python",
                            "args": [command],
                        },
                        "Env": {**environment, "SERVICE": name},
                        "Resources": {"CPU": cpu, "MemoryMB": memory},
                        "Services": [
                            {
                                "Name": name,
                                "PortLabel": "http",
                                "Provider": "consul",
                                "Checks": [
                                    {
                                        "Name": "process",
                                        "Type": "http",
                                        "Path": "/health",
                                        "Interval": 10000000000,
                                        "Timeout": 2000000000,
                                    }
                                ],
                            }
                        ],
                        "LogConfig": {"MaxFiles": 5, "MaxFileSizeMB": 10},
                    }
                ],
            }
        ],
    }


def cache_job(name, worker):
    """Schedule a persistent Redis pool through Nomad on its assigned worker."""
    return {
        "ID": name,
        "Name": name,
        "Type": "service",
        "Datacenters": ["dc1"],
        "TaskGroups": [
            {
                "Name": name,
                "Count": 1,
                "Constraints": [{"LTarget": "${node.unique.name}", "Operand": "=", "RTarget": worker}],
                "RestartPolicy": {"Attempts": 3, "Interval": 60000000000, "Delay": 1000000000, "Mode": "delay"},
                "Networks": [{"Mode": "host", "DynamicPorts": [{"Label": "cache"}]}],
                "Tasks": [
                    {
                        "Name": "redis",
                        "Driver": "docker",
                        "Config": {
                            "image": "redis:7.2.10-bookworm",
                            "network_mode": "host",
                            "command": "sh",
                            "args": ["-c", 'exec redis-server --port "$NOMAD_PORT_cache" --save "" --appendonly yes'],
                            "volumes": [f"/state/cache-pools/{name}:/data"],
                        },
                        "User": "999",
                        "Resources": {"CPU": 200, "MemoryMB": 512},
                        "Services": [
                            {
                                "Name": name,
                                "PortLabel": "cache",
                                "Provider": "consul",
                                "Checks": [
                                    {
                                        "Name": "redis-ready",
                                        "Type": "tcp",
                                        "Interval": 10000000000,
                                        "Timeout": 2000000000,
                                    }
                                ],
                            }
                        ],
                        "LogConfig": {"MaxFiles": 5, "MaxFileSizeMB": 10},
                    }
                ],
            }
        ],
    }
