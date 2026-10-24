"""Two MySQL servers in separate regions joined by asynchronous GTID replication."""

import json
import subprocess
import time
from collections.abc import Callable

MYSQL_IMAGE = "mysql:8.4.6"
REGIONS = ("east", "west")
SERVER_IDS = {"east": 1, "west": 2}
GTID_CHARACTERS = set("0123456789abcdef:,-")
TABLE_CHARACTERS = set("abcdefghijklmnopqrstuvwxyz0123456789_")
REPLICATION_SQL = """
SELECT s.SOURCE_UUID, s.SERVICE_STATE, a.SERVICE_STATE
  FROM performance_schema.replication_connection_status s
  JOIN performance_schema.replication_applier_status a USING (CHANNEL_NAME);
"""


def other(region):
    return REGIONS[1 - REGIONS.index(region)]


def checked_gtids(value):
    # GTID sets come from the servers and are interpolated into SQL functions.
    if not set(value) <= GTID_CHARACTERS:
        raise ValueError(f"Unexpected GTID set: {value!r}")
    return value


class MySQLRegions:
    """One server per region; the rw service selects whichever region takes writes."""

    def __init__(self, command: Callable, name: str, database: str, storage_class: str, secret: str):
        self.command = command
        self.name = name
        self.database = database
        self.storage_class = storage_class
        self.secret = secret

    def service(self, region):
        return f"{self.name}-{region}"

    def pod(self, region):
        return f"{self.name}-{region}-0"

    @property
    def write_service(self):
        return f"{self.name}-rw"

    def labels(self, region):
        return {"app": self.name, "region": region}

    def render(self, user_secret):
        documents = [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": self.write_service, "labels": {"app": self.name}},
                "spec": {"selector": self.labels("east"), "ports": [{"port": 3306, "targetPort": 3306}]},
            }
        ]
        for region in REGIONS:
            secret = {"name": self.secret, "key": "root-password"}
            documents += [
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": self.service(region), "labels": {"app": self.name}},
                    "spec": {"selector": self.labels(region), "ports": [{"port": 3306, "targetPort": 3306}]},
                },
                {
                    "apiVersion": "apps/v1",
                    "kind": "StatefulSet",
                    "metadata": {"name": self.service(region), "labels": {"app": self.name}},
                    "spec": {
                        "serviceName": self.service(region),
                        "replicas": 1,
                        "selector": {"matchLabels": self.labels(region)},
                        "template": {
                            "metadata": {"labels": self.labels(region)},
                            "spec": {
                                "automountServiceAccountToken": False,
                                "terminationGracePeriodSeconds": 30,
                                "containers": [
                                    {
                                        "name": "mysql",
                                        "image": MYSQL_IMAGE,
                                        "args": [
                                            f"--server-id={SERVER_IDS[region]}",
                                            "--gtid-mode=ON",
                                            "--enforce-gtid-consistency=ON",
                                            "--log-replica-updates=ON",
                                            "--relay-log-recovery=ON",
                                            "--innodb-buffer-pool-size=128M",
                                            # Many servers on one node exhaust the kernel's AIO contexts.
                                            "--innodb-use-native-aio=OFF",
                                        ],
                                        "env": [
                                            {"name": "MYSQL_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": secret}},
                                            {"name": "MYSQL_DATABASE", "value": self.database},
                                            {
                                                "name": "MYSQL_USER",
                                                "valueFrom": {"secretKeyRef": {"name": user_secret, "key": "username"}},
                                            },
                                            {
                                                "name": "MYSQL_PASSWORD",
                                                "valueFrom": {"secretKeyRef": {"name": user_secret, "key": "password"}},
                                            },
                                        ],
                                        "ports": [{"containerPort": 3306}],
                                        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/mysql"}],
                                        "readinessProbe": {
                                            "exec": {
                                                "command": [
                                                    "sh",
                                                    "-c",
                                                    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqladmin -h127.0.0.1 -uroot ping',
                                                ]
                                            },
                                            "periodSeconds": 5,
                                            "timeoutSeconds": 5,
                                        },
                                        "resources": {
                                            "requests": {"cpu": "100m", "memory": "512Mi"},
                                            "limits": {"cpu": "1", "memory": "1Gi"},
                                        },
                                    }
                                ],
                            },
                        },
                        "volumeClaimTemplates": [
                            {
                                "metadata": {"name": "data"},
                                "spec": {
                                    "accessModes": ["ReadWriteOnce"],
                                    "storageClassName": self.storage_class,
                                    "resources": {"requests": {"storage": "2Gi"}},
                                },
                            }
                        ],
                    },
                },
            ]
        return documents

    def sql(self, region, statement, timeout=60):
        return self.command(
            "exec",
            "-i",
            self.pod(region),
            "-c",
            "mysql",
            "--",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --batch --raw --skip-column-names "$1"',
            "sql",
            self.database,
            input_text=statement,
            timeout=timeout,
        ).strip()

    def wait_ready(self, timeout=600):
        for region in REGIONS:
            self.command(
                "rollout",
                "status",
                f"statefulset/{self.service(region)}",
                f"--timeout={timeout}s",
                timeout=timeout + 20,
            )

    def initialize(self, replication_password):
        # A promoted region has no replication channel but must not be reset.
        replicator = "SELECT COUNT(*) FROM mysql.user WHERE user = 'replicator';"
        if any(self.sql(region, replicator) != "0" for region in REGIONS):
            return
        # Both servers ran the image's first-start setup independently.
        for region in REGIONS:
            self.sql(region, "RESET BINARY LOGS AND GTIDS;")
        self.sql(
            "east",
            f"CREATE USER IF NOT EXISTS 'replicator'@'%' IDENTIFIED BY '{replication_password}';\n"
            "GRANT REPLICATION SLAVE ON *.* TO 'replicator'@'%';",
        )
        self.replicate("west", "east", replication_password)
        self.wait_for_gtids("west", self.gtid_executed("east"))

    def replicate(self, replica, source, replication_password):
        self.sql(
            replica,
            "STOP REPLICA;\n"
            f"CHANGE REPLICATION SOURCE TO SOURCE_HOST='{self.service(source)}', SOURCE_PORT=3306, "
            f"SOURCE_USER='replicator', SOURCE_PASSWORD='{replication_password}', "
            "SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1, SOURCE_CONNECT_RETRY=5;\n"
            "START REPLICA;\n"
            "SET PERSIST super_read_only = ON;",
        )

    def replication(self, region):
        rows = self.sql(region, REPLICATION_SQL).splitlines()
        if len(rows) != 1:
            return {"channels": len(rows)} if rows else None
        source, io, applier = rows[0].split("\t")
        return {"source": source, "io": io, "sql": applier}

    def server_uuid(self, region):
        return self.sql(region, "SELECT @@GLOBAL.server_uuid;")

    def applies_everything(self, region):
        """No replication filters or skipped errors that would let a replica drift silently."""
        filters = self.sql(region, "SELECT COUNT(*) FROM performance_schema.replication_applier_filters;")
        modes = self.sql(region, "SELECT @@GLOBAL.replica_skip_errors, @@GLOBAL.replica_exec_mode;")
        return filters == "0" and modes.split() == ["OFF", "STRICT"]

    def checksums(self, region):
        tables = self.sql(
            region,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' ORDER BY table_name;",
        ).split()
        if not all(set(t) <= TABLE_CHARACTERS for t in tables):
            raise ValueError("Unexpected table name")
        if not tables:
            return {}
        rows = self.sql(region, "CHECKSUM TABLE " + ", ".join(f"`{t}`" for t in tables) + " EXTENDED;")
        return dict(row.split("\t") for row in rows.splitlines())

    def consistent(self, replica, source, attempts=5):
        """Same data on both servers; retried because the source keeps taking writes."""
        for _ in range(attempts):
            if not self.converged(replica, source):
                return False
            gtids = self.gtid_executed(source)
            expected = self.checksums(source)
            if self.gtid_executed(source) != gtids:
                continue
            self.wait_for_gtids(replica, gtids)
            if self.checksums(replica) == expected:
                return True
        return False

    def gtid_executed(self, region):
        return checked_gtids(self.sql(region, "SELECT REPLACE(@@GLOBAL.gtid_executed, '\\n', '');"))

    def wait_for_gtids(self, region, gtids, timeout=120):
        result = self.sql(
            region,
            f"SELECT WAIT_FOR_EXECUTED_GTID_SET('{checked_gtids(gtids)}', {int(timeout)});",
            timeout=timeout + 30,
        )
        if result != "0":
            raise RuntimeError(f"MySQL {region} did not apply {gtids} within {timeout}s")

    def converged(self, replica, source, timeout=60):
        """The replica has applied everything the source has, and nothing else."""
        try:
            self.wait_for_gtids(replica, self.gtid_executed(source), timeout)
        except RuntimeError:
            return False
        replica_gtids = self.gtid_executed(replica)
        source_gtids = self.gtid_executed(source)
        return self.sql(source, f"SELECT GTID_SUBSET('{replica_gtids}', '{source_gtids}');") == "1"

    def writable(self, region):
        return self.sql(region, "SELECT @@GLOBAL.read_only, @@GLOBAL.super_read_only;") == "0\t0"

    def reachable(self, region, target):
        try:
            self.command(
                "exec",
                self.pod(region),
                "-c",
                "mysql",
                "--",
                "sh",
                "-c",
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqladmin -h "$1" -uroot --connect-timeout=3 ping',
                "probe",
                self.service(target),
                timeout=30,
            )
        except subprocess.CalledProcessError:
            return False
        return True

    def promote(self, region):
        self.sql(
            region,
            "STOP REPLICA;\nRESET REPLICA ALL;\nSET PERSIST super_read_only = OFF;\nSET PERSIST read_only = OFF;",
        )

    def write_region(self):
        return self.command("get", "service", self.write_service, "-o", "jsonpath={.spec.selector.region}").strip()

    def write_pods(self):
        """Pods the rw service actually selects, whatever its selector says."""
        service = json.loads(self.command("get", "service", self.write_service, "-o", "json"))
        selector = ",".join(f"{k}={v}" for k, v in sorted(service["spec"].get("selector", {}).items()))
        if not selector:
            return []
        return sorted(self.command("get", "pods", "-l", selector, "-o", "name").split())

    def reconnect(self, region):
        """Drop the replica's open connection so it must dial the source again."""
        self.sql(region, "STOP REPLICA IO_THREAD;\nSTART REPLICA IO_THREAD;")

    def route_writes(self, region):
        self.command(
            "patch",
            "service",
            self.write_service,
            "--type=merge",
            "-p",
            json.dumps({"spec": {"selector": self.labels(region)}}),
        )

    def partition_documents(self, namespace):
        return [
            {
                "apiVersion": "crd.projectcalico.org/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": f"default.region-{region}", "namespace": namespace},
                "spec": {
                    "tier": "default",
                    "order": 100,
                    "selector": f"region == '{region}'",
                    "types": ["Ingress", "Egress"],
                    "ingress": [
                        {"action": "Deny", "source": {"selector": f"region == '{other(region)}'"}},
                        {"action": "Allow"},
                    ],
                    "egress": [
                        {"action": "Deny", "destination": {"selector": f"region == '{other(region)}'"}},
                        {"action": "Allow"},
                    ],
                },
            }
            for region in REGIONS
        ]

    def heal(self):
        self.command(
            "delete",
            "networkpolicies.crd.projectcalico.org",
            *[f"default.region-{region}" for region in REGIONS],
            "--ignore-not-found",
        )

    def rebuild(self, replica, source, replication_password):
        """Discard the replica's data and history, then reload it from the source."""
        self.sql(replica, "STOP REPLICA;\nRESET REPLICA ALL;\nSET PERSIST super_read_only = OFF;")
        self.sql(replica, f"DROP DATABASE IF EXISTS `{self.database}`;\nRESET BINARY LOGS AND GTIDS;")
        self.command(
            "exec",
            self.pod(replica),
            "-c",
            "mysql",
            "--",
            "sh",
            "-c",
            'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; set -o pipefail; '
            'mysqldump -h "$1" -uroot --single-transaction --set-gtid-purged=ON --databases "$2" | mysql -uroot',
            "rebuild",
            self.service(source),
            self.database,
            timeout=900,
        )
        self.replicate(replica, source, replication_password)
        deadline = time.monotonic() + 120
        while not self.converged(replica, source):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"MySQL {replica} did not converge with {source} after rebuild")
            time.sleep(3)
