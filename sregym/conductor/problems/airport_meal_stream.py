"""Conductor operations for the airport meal Kafka topics and Flink sessions."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import selectors
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

_ASSETS = Path(__file__).parent / "assets" / "airport_meal_clearing" / "stream"
_SESSIONS = {
    "1.18.1": ("flink-118", "flink@sha256:f0e9ffd07f3d43291dfaf7e0609eb2871462193275d452affbe5fac091017ae3"),
    "1.19.0": ("flink-119", "flink@sha256:61355051b493c0b2784ae5755608798e7c54d9db0f8467e9e3f183e3a48e617a"),
}
_KAFKA_IMAGE = "apache/kafka@sha256:8bd63e1bd445e5e19427a4bdbcc3d23bf6efd774b058a41b36ba87fda7623e34"
_ROUTES = {"production": "issue-orders", "rehearsal": "preview-orders"}
_TERMINAL = {"FINISHED", "CANCELED", "FAILED", "SUSPENDED"}
_JOB_STATES = _TERMINAL | {
    "INITIALIZING",
    "CREATED",
    "RUNNING",
    "FAILING",
    "CANCELLING",
    "RESTARTING",
    "RECONCILING",
}
_PARTITIONS = set(range(20))
_DISK = "/stream-state"
_LOG = logging.getLogger(__name__)
_OBSERVATION_ERRORS = (
    RuntimeError,
    OSError,
    subprocess.TimeoutExpired,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_SPLIT = r"\[Partition: allowances-(\d+), StartingOffset: -?\d+, StoppingOffset: -?\d+\]"
_SPLIT_LIST = re.compile(rf"\[(?:{_SPLIT}(?:, {_SPLIT})*)?\]")


class _FlinkHTTPError(RuntimeError):
    def __init__(self, status, method, path, body):
        self.status = status
        self.body = body
        super().__init__(f"Flink {method} {path}: HTTP {status}: {body}")

    def confirms_missing_job(self, job_id):
        if self.status != 404:
            return False
        try:
            reply = json.loads(self.body)
        except ValueError:
            return False
        errors = reply.get("errors") if isinstance(reply, dict) else None
        if not isinstance(errors, list) or len(errors) != 1 or not isinstance(errors[0], str):
            return False
        # Flink's job-details handler can send the message as a stack trace.
        first_line = errors[0].partition("\n")[0].rstrip("\r")
        return first_line in {
            f"Job {job_id} not found",
            f"org.apache.flink.runtime.rest.NotFoundException: Job {job_id} not found",
        }


def _validate_jar_id(jar):
    if not isinstance(jar, str) or not jar.endswith(".jar") or Path(jar).name != jar:
        raise ValueError(f"Invalid Flink JAR ID {jar!r}")


def validate_release_artifact(spec):
    """Check the recorded artifact, allowing older releases to use the bundled JAR."""
    if "jar_id" in spec:
        if "jar_path" in spec:
            raise ValueError("A release must choose either jar_id or jar_path")
        _validate_jar_id(spec["jar_id"])
    elif "jar_path" in spec:
        path = spec["jar_path"]
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError(f"JAR path must be absolute in the JobManager: {path!r}")


def _bundled_jar(spec):
    return f"{_DISK}/jars/meal-orders-{spec['version']}.jar"


def _empty_owners():
    return {part: [] for part in sorted(_PARTITIONS)}


def _wait(check, description, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.75)
    raise TimeoutError(f"Timed out waiting for {description}")


def _transaction_reply(process, timeout=90):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if selector.select(timeout):
            line = process.stdout.readline()
            if line:
                return json.loads(line)
            error = process.stderr.read()
            raise RuntimeError(f"Kafka transaction process stopped: {error.strip()}")
    raise TimeoutError("Timed out waiting for the Kafka transaction process")


class _Transaction:
    def __init__(self, process, receipts):
        self._process = process
        self.receipts = receipts
        self._finished = False

    def _finish(self, operation):
        if self._finished:
            raise RuntimeError("Kafka transaction is already closed")
        try:
            self._process.stdin.write(operation + "\n")
            self._process.stdin.flush()
            result = _transaction_reply(self._process)
            self._process.stdin.close()
            if self._process.wait(timeout=15):
                raise RuntimeError(self._process.stderr.read().strip())
            return result
        finally:
            self._finished = True
            if self._process.poll() is None:
                self._process.kill()
                self._process.wait()
            for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                pipe.close()

    def commit(self):
        return self._finish("commit")

    def abort(self):
        return self._finish("abort")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if not self._finished:
            self.abort()


class Stream:
    """Operate one stream namespace using the selected kubeconfig context."""

    def __init__(self, namespace="airport-meals", *, kubeconfig=None, context=None, artifact_dir=None):
        self.namespace = namespace
        self.artifact_dir = Path(artifact_dir or os.environ.get("AIRPORT_MEAL_STREAM_ARTIFACTS") or _ASSETS / "dist")
        self._kubectl = ["kubectl"]
        if kubeconfig is not None:
            self._kubectl.extend(("--kubeconfig", str(kubeconfig)))
        if context is not None:
            self._kubectl.extend(("--context", context))
        self._kubectl.extend(("--namespace", namespace))
        self._releases = {}

    def _kube(self, *args, stdin=None, timeout=45):
        result = subprocess.run(
            [*self._kubectl, *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"kubectl {' '.join(args[:4])}: {message}")
        return result.stdout.decode()

    def _get(self, kind, name):
        value = self._kube("get", kind, name, "--ignore-not-found", "-o", "json")
        return json.loads(value) if value.strip() else None

    def _apply(self, manifest):
        self._kube("apply", "-f", "-", stdin=manifest.encode())

    def _rollout(self, resource, timeout=180):
        self._kube("rollout", "status", resource, f"--timeout={timeout}s", timeout=timeout + 10)

    def _config_map(self, name, data, *, binary=False):
        self._apply(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": name},
                    "binaryData" if binary else "data": data,
                }
            )
        )

    def deploy(self, *, offsets_retention_minutes=10080, offsets_retention_check_interval_ms=600000):
        """Install the broker and initial session in the existing namespace."""
        paths = [
            self.artifact_dir / filename
            for filename in (
                "meal-orders-1.18.1.jar",
                "meal-orders-1.19.0.jar",
                "kafka-ops.jar",
            )
        ]
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(f"Missing {path}. Run the stream build and stage its dist directory.")
        self._apply((_ASSETS / "storage.yaml").read_text())
        tools = base64.b64encode(paths[2].read_bytes()).decode()
        self._config_map("stream-kafka-tools", {"kafka-ops.jar": tools}, binary=True)

        broker = self._get("statefulset", "kafka")
        if broker:
            env = broker["spec"]["template"]["spec"]["containers"][0]["env"]
            cluster_id = next(item["value"] for item in env if item["name"] == "CLUSTER_ID")
        else:
            cluster_id = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")
        self._apply(
            (_ASSETS / "kafka.yaml")
            .read_text()
            .replace("__KAFKA_IMAGE__", _KAFKA_IMAGE)
            .replace("__CLUSTER_ID__", cluster_id)
            .replace("__OFFSETS_RETENTION_MINUTES__", str(int(offsets_retention_minutes)))
            .replace("__OFFSETS_RETENTION_CHECK_INTERVAL_MS__", str(int(offsets_retention_check_interval_ms)))
        )
        self._rollout("statefulset/kafka")
        self._deploy_session("1.18.1")
        for topic, count in (("allowances", 20), ("issue-orders", 4), ("preview-orders", 4)):
            settings = {"cleanup.policy": "delete", "retention.ms": "-1"}
            if topic == "allowances":
                settings["file.delete.delay.ms"] = "1000"
            self._broker("ensure_topic", topic, partitions=count, settings=settings)
        for path in paths[:2]:
            self._write(self._jobmanager({"version": "1.18.1"}), f"{_DISK}/jars/{path.name}", path.read_bytes())

    def _deploy_session(self, version):
        session, image = _SESSIONS[version]
        self._apply(
            (_ASSETS / "flink.yaml").read_text().replace("__SESSION__", session).replace("__FLINK_IMAGE__", image)
        )
        self._rollout(f"deployment/{session}-jobmanager")
        self._rollout(f"deployment/{session}-taskmanager")
        _wait(lambda: self._rest({"version": version}, "/overview")["slots-total"] == 10, f"{session} slots")

    def undeploy_session(self, version):
        session = _SESSIONS[version][0]
        if self._get("deployment", f"{session}-jobmanager"):
            jobs = self._rest({"version": version}, "/jobs/overview")["jobs"]
            if any(job["state"] not in _TERMINAL for job in jobs):
                raise RuntimeError(f"{session} still has active jobs")
        self._kube(
            "delete",
            "deployment",
            f"{session}-jobmanager",
            f"{session}-taskmanager",
            "--ignore-not-found",
            "--wait=true",
            timeout=90,
        )
        self._kube("delete", "service", session, "--ignore-not-found")

    @staticmethod
    def _jobmanager(release):
        if "jobmanager" in release:
            resource = release["jobmanager"]
            if not re.fullmatch(r"(deployment|statefulset|pod)/[a-z0-9][a-z0-9.-]*", resource):
                raise ValueError(f"Invalid JobManager resource {resource!r}")
            return resource
        return f"deployment/{_SESSIONS[release['version']][0]}-jobmanager"

    def _rest(self, release, path, *, method="GET", payload=None, form=None):
        command = [
            "exec",
            "-i",
            self._jobmanager(release),
            "--",
            "curl",
            "-sS",
            "--write-out",
            "\n%{http_code}",
            "--max-time",
            "25",
            "-X",
            method,
            f"http://127.0.0.1:8081{path}",
        ]
        if payload is not None:
            command.extend(("-H", "Content-Type: application/json", "--data-binary", "@-"))
        if form is not None:
            command.extend(("-F", form))
        response = self._kube(*command, stdin=json.dumps(payload).encode() if payload is not None else None)
        data, _, code = response.rpartition("\n")
        if not re.fullmatch(r"[1-5]\d\d", code):
            raise RuntimeError(f"Flink {method} {path}: missing HTTP status")
        if not 200 <= int(code) < 300:
            raise _FlinkHTTPError(int(code), method, path, data)
        try:
            return json.loads(data) if data.strip() else None
        except ValueError as error:
            raise RuntimeError(f"Flink {method} {path}: malformed JSON") from error

    def check_release_artifact(self, spec):
        """Return artifact availability, raising if the read could not be completed."""
        validate_release_artifact(spec)
        if "jar_id" in spec:
            response = self._rest(spec, "/jars")
            files = response.get("files") if isinstance(response, dict) else None
            if not isinstance(files, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in files
            ):
                raise RuntimeError("Flink GET /jars: malformed retained JAR list")
            return any(item["id"] == spec["jar_id"] for item in files)

        path = spec["jar_path"] if "jar_path" in spec else _bundled_jar(spec)
        result = self._kube(
            "exec",
            self._jobmanager(spec),
            "--",
            "sh",
            "-ec",
            'if error=$(LC_ALL=C stat -L -- "$1" 2>&1); then '
            'if test -f "$1" && test -r "$1" && test -s "$1"; then '
            'head -c 1 -- "$1" >/dev/null; printf readable; else printf unavailable; fi; '
            'elif case "$error" in *": No such file or directory") true;; *) false;; esac; then '
            'printf unavailable; else printf "%s\\n" "$error" >&2; exit 1; fi',
            "sh",
            path,
        )
        if result not in {"readable", "unavailable"}:
            raise RuntimeError(f"Malformed JAR path check for {path}")
        return result == "readable"

    def _write(self, jobmanager, path, data):
        self._kube(
            "exec",
            "-i",
            jobmanager,
            "--",
            "sh",
            "-ec",
            'mkdir -p "$(dirname "$1")"; f=$(mktemp "$1.XXXXXX"); '
            'trap \'rm -f "$f"\' EXIT; cat > "$f"; chmod 0644 "$f"; mv "$f" "$1"',
            "sh",
            path,
            stdin=data,
            timeout=90,
        )

    def _storage_jobmanager(self):
        releases = [self.release(route) for route in _ROUTES]
        releases.extend({"version": version} for version in _SESSIONS)
        for release in filter(None, releases):
            jobmanager = self._jobmanager(release)
            kind, name = jobmanager.split("/", 1)
            resource = self._get(kind, name)
            if resource:
                status = resource["status"]
                if status.get("readyReplicas") or any(
                    item["type"] == "Ready" and item["status"] == "True" for item in status.get("conditions", [])
                ):
                    return jobmanager
        raise RuntimeError("A session JobManager must be ready to access stream storage")

    @staticmethod
    def _artifact_path(label):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", label):
            raise ValueError(f"Invalid artifact label {label!r}")
        return f"{_DISK}/archive/{label}.json"

    def archive(self, label, data):
        path = self._artifact_path(label)
        self._write(self._storage_jobmanager(), path, json.dumps(data, indent=2).encode() + b"\n")
        return "file://" + path

    def artifacts(self):
        paths = self._kube(
            "exec",
            self._storage_jobmanager(),
            "--",
            "sh",
            "-ec",
            'for f in /stream-state/archive/*.json; do if test -f "$f"; then basename "$f" .json; fi; done',
        )
        return sorted(paths.splitlines())

    def read_artifact(self, label):
        return json.loads(
            self._kube(
                "exec",
                self._storage_jobmanager(),
                "--",
                "cat",
                self._artifact_path(label),
            )
        )

    def release(self, route="production"):
        if route not in _ROUTES:
            raise ValueError(f"Unknown stream route {route!r}")
        config = self._get("configmap", f"stream-release-{route}")
        if config is None:
            return None
        return self._release_from_config(config)

    @staticmethod
    def _release_from_config(config):
        spec = json.loads(config["data"]["release.json"])
        if spec["offsets"] is not None:
            spec["offsets"] = {int(part): offset for part, offset in spec["offsets"].items()}
        return spec

    def _publish(self, spec):
        self._config_map(f"stream-release-{spec['route']}", {"release.json": json.dumps(spec, indent=2)})
        self._releases[spec["job_id"]] = spec

    def _update_release(self, previous, spec):
        """Replace the designated release only while it still matches the observed record."""
        route = previous["route"]
        config = self._get("configmap", f"stream-release-{route}")
        if config is None or self._release_from_config(config) != previous:
            raise RuntimeError(f"{route} release changed during the operation")
        config["data"]["release.json"] = json.dumps(spec, indent=2)
        self._kube("replace", "-f", "-", stdin=json.dumps(config).encode())
        self._releases[previous["job_id"]] = previous
        self._releases[spec["job_id"]] = spec

    def _retire(self, spec):
        self._releases[spec["job_id"]] = spec
        current = self.release(spec["route"])
        if current and current["job_id"] == spec["job_id"]:
            self._publish(spec)

    def _release_for_job(self, job_id):
        if not _JOB_ID.fullmatch(job_id):
            raise ValueError(f"Invalid Flink job ID {job_id!r}")
        for route in _ROUTES:
            spec = self.release(route)
            if spec and spec["job_id"] == job_id:
                self._releases[job_id] = spec
                return spec
        if job_id in self._releases:
            return self._releases[job_id]
        archived = self.read_artifact(f"job-{job_id}")
        self._releases[job_id] = archived["release"]
        return archived["release"]

    def start(
        self,
        *,
        route="production",
        version="1.18.1",
        parallelism=5,
        group=None,
        start="earliest",
        offsets=None,
        offset_reset="none",
        compression=True,
        checkpoint_ms=2500,
        savepoint=None,
        restore_mode="NO_CLAIM",
        name=None,
    ):
        if not 1 <= parallelism <= 10:
            raise ValueError("The bundled session has ten slots")
        spec = {
            "route": route,
            "version": version,
            "name": name or f"Meal orders ({route})",
            "group": group or f"meal-{route}",
            "target_topic": _ROUTES[route],
            "parallelism": parallelism,
            "start": start,
            "offsets": offsets,
            "offset_reset": offset_reset,
            "compression": compression,
            "checkpoint_ms": checkpoint_ms,
            "savepoint": savepoint,
            "restore_mode": restore_mode,
            "jobmanager": self._jobmanager({"version": version}),
            "jar_path": _bundled_jar({"version": version}),
        }
        start_argument = self._start_argument(spec)
        self._check_route(spec)
        self._deploy_session(version)
        return self._submit(spec, start_argument)

    def available_slots(self, release):
        """Return currently free slots in the release's running session."""
        return self._rest(release, "/overview")["slots-available"]

    def restore_in_place(self, release, savepoint, *, parallelism):
        """Submit a stopped route's savepoint using its deployed session and JAR."""
        if not savepoint:
            raise ValueError("A savepoint is required for in-place restore")
        spec = release | {"parallelism": parallelism, "savepoint": savepoint}
        for field in ("job_id", "reader", "status", "stopped_savepoint", "stop_operation", "pending_restore"):
            spec.pop(field, None)
        spec["jobmanager"] = self._jobmanager(spec)
        if "jar_id" not in spec:
            spec.setdefault("jar_path", _bundled_jar(spec))
        if spec["offsets"] is not None:
            spec["offsets"] = {int(part): offset for part, offset in spec["offsets"].items()}
        validate_release_artifact(spec)
        start_argument = self._start_argument(spec)
        current, stopped_status = self._check_route(spec, stopped_job=release["job_id"])
        attempt = current.get("pending_restore")
        state = self._restore_state(attempt) if attempt else None
        if state in _TERMINAL:
            attempt = None
        settings = json.loads(json.dumps(spec))
        if attempt and attempt["settings"] != settings:
            raise RuntimeError(f"Restore {attempt['job_id']} already records different job settings")
        if attempt is None or state is None:
            _wait(
                lambda: self.available_slots(spec) >= parallelism, f"{parallelism} free slots in {spec['jobmanager']}"
            )
            if attempt is None:
                attempt = {
                    "job_id": uuid.uuid4().hex,
                    "reader": f"allowances-{uuid.uuid4().hex[:12]}",
                    "settings": settings,
                }
                pending = current | {
                    "status": stopped_status,
                    "stopped_savepoint": current.get("stopped_savepoint", savepoint),
                    "pending_restore": attempt,
                }
                self._update_release(current, pending)
                current = pending
            try:
                self._launch(spec, start_argument, attempt["reader"], job_id=attempt["job_id"])
            except _OBSERVATION_ERRORS as error:
                if self._restore_state(attempt) is None:
                    if isinstance(error, _FlinkHTTPError) and error.status in {400, 404}:
                        stopped = {key: value for key, value in current.items() if key != "pending_restore"}
                        self._update_release(current, stopped)
                    raise
        replacement = spec | {key: attempt[key] for key in ("job_id", "reader")}
        self._await_running(replacement)
        self._update_release(current, replacement | {"status": "RUNNING"})
        return replacement["job_id"]

    def _restore_state(self, attempt):
        path = f"/jobs/{attempt['job_id']}/status"
        try:
            response = self._rest(attempt["settings"], path)
        except _FlinkHTTPError as error:
            if error.status != 404:
                raise
            return None
        status = response.get("status") if isinstance(response, dict) else None
        if not isinstance(status, str) or not status:
            raise RuntimeError(f"Malformed Flink job status at {path}")
        return status

    @staticmethod
    def _start_argument(spec):
        start, offsets, offset_reset = spec["start"], spec["offsets"], spec["offset_reset"]
        if start not in {"earliest", "latest", "explicit", "committed"}:
            raise ValueError(f"Unknown starting position {start!r}")
        if offset_reset not in {"none", "latest", "earliest"}:
            raise ValueError(f"Unknown reset policy {offset_reset!r}")
        if spec["restore_mode"] not in {"CLAIM", "NO_CLAIM"}:
            raise ValueError(f"Unknown restore mode {spec['restore_mode']!r}")
        if spec["parallelism"] < 1 or spec["checkpoint_ms"] <= 0:
            raise ValueError("Parallelism and checkpoint interval must be positive")
        if start == "explicit":
            if offsets is None or set(offsets) != _PARTITIONS or min(offsets.values()) < 0:
                raise ValueError("Explicit positions require nonnegative offsets for all 20 partitions")
            return ",".join(f"{part}:{offsets[part]}" for part in sorted(offsets))
        if offsets is not None:
            raise ValueError("Use explicit starting position to supply a partition vector")
        return offset_reset.upper()

    def _check_route(self, spec, *, stopped_job=None):
        route, group = spec["route"], spec["group"]
        previous = None
        previous_state = None
        for other_route in _ROUTES:
            current = self.release(other_route)
            if other_route == route and stopped_job is not None:
                if current is None or current["job_id"] != stopped_job:
                    raise RuntimeError(f"{route} no longer designates job {stopped_job}")
            if current is None:
                continue
            if other_route != route and group == current["group"]:
                raise RuntimeError(f"Group {group} is configured for {other_route}")
            if other_route == route:
                previous = current
                if current.get("pending_restore") and stopped_job is None:
                    raise RuntimeError(f"{route} has a pending in-place restore")
            if other_route == route and (current["status"] not in _TERMINAL or stopped_job is not None):
                try:
                    previous_state = self._rest(current, f"/jobs/{current['job_id']}")["state"]
                except _FlinkHTTPError as error:
                    if (
                        error.status != 404
                        or current["status"] not in _TERMINAL
                        or not current.get("stopped_savepoint")
                    ):
                        raise
                    previous_state = current["status"]
                if previous_state not in _TERMINAL:
                    raise RuntimeError(f"{route} already has an active job")
        return previous, previous_state

    def _submit(self, spec, start_argument):
        reader = f"allowances-{uuid.uuid4().hex[:12]}"
        job_id = self._launch(spec, start_argument, reader)
        spec = spec | {"job_id": job_id, "reader": reader, "status": "SUBMITTED"}
        self._publish(spec)
        self._await_running(spec)
        self._publish(spec | {"status": "RUNNING"})
        return job_id

    def _launch(self, spec, start_argument, reader, *, job_id=None):
        name, parallelism, checkpoint_ms = spec["name"], spec["parallelism"], spec["checkpoint_ms"]
        validate_release_artifact(spec)
        jar = spec.get("jar_id")
        uploaded = jar is None
        if uploaded:
            path = spec["jar_path"]
            quoted_path = path.replace("\\", "\\\\").replace('"', '\\"')
            response = self._rest(spec, "/jars/upload", method="POST", form=f'jarfile=@"{quoted_path}"')
            jar = Path(response["filename"]).name
            _validate_jar_id(jar)
        jar_path = f"/jars/{quote(jar, safe='')}"
        payload = {
            "entryClass": "airport.meal.MealOrders",
            "parallelism": parallelism,
            "programArgsList": [
                "kafka:9092",
                spec["group"],
                spec["target_topic"],
                name,
                reader,
                str(spec["compression"]).lower(),
                str(parallelism),
                str(checkpoint_ms),
                spec["start"],
                start_argument,
            ],
        }
        if spec["savepoint"]:
            payload.update(savepointPath=spec["savepoint"], restoreMode=spec["restore_mode"])
        if job_id is not None:
            payload["jobId"] = job_id
        try:
            submitted = self._rest(spec, f"{jar_path}/run", method="POST", payload=payload)["jobid"]
            if not isinstance(submitted, str) or not _JOB_ID.fullmatch(submitted) or job_id not in (None, submitted):
                raise RuntimeError(f"Flink returned an unexpected job ID: {submitted!r}")
        finally:
            if uploaded:
                try:
                    self._rest(spec, jar_path, method="DELETE")
                except _OBSERVATION_ERRORS as error:
                    _LOG.warning("Could not remove Flink upload %s: %s", jar, error)
        return submitted

    def _await_running(self, spec):
        job_id = spec["job_id"]
        job_path = f"/jobs/{job_id}"

        def running():
            state = self._rest(spec, job_path)
            if state["state"] in _TERMINAL:
                raise RuntimeError(f"{spec['name']} is {state['state']}: {self._rest(spec, job_path + '/exceptions')}")
            return state["state"] == "RUNNING" and all(v["status"] == "RUNNING" for v in state["vertices"])

        def checkpoint():
            if running():
                return self._rest(spec, job_path + "/checkpoints")["latest"].get("completed")
            return None

        _wait(running, f"job {job_id} to run")
        _wait(
            checkpoint,
            f"job {job_id} first checkpoint",
            timeout=max(90, spec["checkpoint_ms"] / 1000 + 45),
        )

    def _job_endpoint(self, job_id, suffix, archived_key):
        spec = self._release_for_job(job_id)
        try:
            return self._rest(spec, f"/jobs/{job_id}{suffix}")
        except RuntimeError:
            if f"job-{job_id}" not in self.artifacts():
                raise
            return self.read_artifact(f"job-{job_id}")[archived_key]

    def job(self, job_id):
        return self._job_endpoint(job_id, "", "job")

    def live_job(self, release):
        """Read the registered release from its JobManager without using history."""
        job_id = release.get("job_id")
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise RuntimeError(f"Unconfirmed Flink job identity: {job_id!r}")
        path = f"/jobs/{job_id}"
        try:
            job = self._rest(release, path)
        except _FlinkHTTPError as error:
            if error.confirms_missing_job(job_id):
                return None
            raise
        if not isinstance(job, dict) or job.get("jid") != job_id:
            raise RuntimeError(f"Unconfirmed Flink job identity at {path}")
        state, vertices = job.get("state"), job.get("vertices")
        if (
            not isinstance(state, str)
            or state not in _JOB_STATES
            or not isinstance(vertices, list)
            or any(
                not isinstance(vertex, dict)
                or not isinstance(vertex.get("name"), str)
                or not vertex["name"]
                or type(vertex.get("parallelism")) is not int
                or vertex["parallelism"] <= 0
                for vertex in vertices
            )
        ):
            raise RuntimeError(f"Malformed Flink job details at {path}")
        return job

    def checkpoints(self, job_id):
        return self._job_endpoint(job_id, "/checkpoints", "checkpoints")

    @staticmethod
    def _source_vertices(state, reader):
        name = f"Source: {reader}"
        return [v for v in state["vertices"] if v["name"] == name or v["name"].startswith(name + " -> ")]

    def _source_vertex(self, state, reader):
        vertices = self._source_vertices(state, reader)
        if len(vertices) != 1:
            raise RuntimeError(f"Expected one source named {reader!r} for Flink job {state['jid']}")
        return vertices[0]

    @staticmethod
    def _source_attempts(source):
        subtasks = source["subtasks"]
        if not isinstance(subtasks, list):
            raise RuntimeError("Malformed Flink source subtask list")
        attempts = {}
        for subtask in subtasks:
            concurrent = subtask.get("other-concurrent-attempts", [])
            if not isinstance(concurrent, list):
                raise RuntimeError("Malformed concurrent source attempts")
            for attempt in (subtask, *concurrent):
                identity = attempt["subtask"], attempt["attempt"]
                if any(type(value) is not int or value < 0 for value in identity) or identity in attempts:
                    raise RuntimeError(f"Invalid or repeated source attempt {identity!r}")
                attempts[identity] = {
                    field: attempt[field] for field in ("host", "taskmanager-id", "status", "start-time", "end-time")
                }
        return attempts

    @staticmethod
    def _overlapping_threads(attempts, other_attempts):
        for identity in attempts.keys() & other_attempts.keys():
            first, second = attempts[identity], other_attempts[identity]
            if first["host"] != second["host"] or min(first["start-time"], second["start-time"]) <= 0:
                continue
            if 0 < first["end-time"] < second["start-time"] or 0 < second["end-time"] < first["start-time"]:
                continue
            return True
        return False

    def _reader_logs(self, vertex, attempts, *, strict=False):
        hosts = {attempt["host"] for attempt in attempts.values() if attempt["start-time"] > 0}
        if not hosts:
            return []
        pods = json.loads(self._kube("get", "pods", "-o", "json"))["items"]
        if not isinstance(pods, list):
            raise RuntimeError("Malformed TaskManager pod list")
        by_host = {}
        for pod in pods:
            name = pod["metadata"]["name"]
            ip = pod["status"].get("podIP")
            for host in {name, pod["spec"].get("hostname"), ip, ip.replace(".", "-") if ip else None} & hosts:
                by_host.setdefault(host, []).append(name)
        pattern = re.compile(
            rf"\[{re.escape(vertex['name'])} \((\d+)/{vertex['parallelism']}\)#(\d+)\] "
            r"- Adding split\(s\) to reader:"
        )
        logs = []
        for host in sorted(hosts):
            names = by_host.get(host, [])
            if len(names) != 1:
                if strict:
                    raise RuntimeError(f"Cannot resolve TaskManager host {host!r} to one pod")
                continue
            for raw in self._kube("logs", f"pod/{names[0]}", "--timestamps").splitlines():
                timestamp, _, line = raw.partition(" ")
                match = pattern.search(line)
                if match:
                    logs.append(
                        {
                            "subtask": int(match[1]) - 1,
                            "attempt": int(match[2]),
                            "host": host,
                            "timestamp": timestamp,
                            "line": line,
                        }
                    )
        return logs

    def _reader_evidence(self, job_id, state, spec, *, live=False):
        vertex = self._source_vertex(state, spec["reader"])
        path = f"/jobs/{job_id}/vertices/{vertex['id']}"
        source = self._rest(spec, path)
        attempts = self._source_attempts(source)
        selected = attempts
        if live:
            selected = {identity: row for identity, row in attempts.items() if row["status"] == "RUNNING"}
            parallelism = vertex["parallelism"]
            if (
                type(parallelism) is not int
                or parallelism <= 0
                or len(selected) != parallelism
                or {subtask for subtask, _ in selected} != set(range(parallelism))
                or any(
                    not row["host"] or not row["taskmanager-id"] or row["start-time"] <= 0 or row["end-time"] > 0
                    for row in selected.values()
                )
            ):
                raise RuntimeError(f"Expected one running source attempt per subtask at {path}")
        logs = self._reader_logs(vertex, selected, strict=live)
        # A split line names the task thread. Only use it when that name can
        # be attributed to this job during the observation window.
        conflicts = []
        first_start = min((a["start-time"] for a in selected.values() if a["start-time"] > 0), default=0)
        jobs = self._rest(spec, "/jobs/overview")["jobs"]
        if not isinstance(jobs, list):
            raise RuntimeError("Malformed Flink jobs overview")
        for other in jobs:
            if other["jid"] == job_id or 0 < other["end-time"] < first_start:
                continue
            if 0 < state["end-time"] < other["start-time"]:
                continue
            other_path = f"/jobs/{other['jid']}"
            for candidate in self._source_vertices(self._rest(spec, other_path), spec["reader"]):
                if (candidate["name"], candidate["parallelism"]) != (vertex["name"], vertex["parallelism"]):
                    continue
                other_source = self._rest(spec, f"{other_path}/vertices/{candidate['id']}")
                if self._overlapping_threads(selected, self._source_attempts(other_source)):
                    conflicts.append(other["jid"])
                    break
        result = {"source_vertex": source, "assignment_logs": logs, "reader_conflicts": conflicts}
        if live and not conflicts:
            result["reader_metrics"] = self._source_metrics(
                spec, job_id, vertex, (".KafkaSourceReader.KafkaConsumer.assigned-partitions",)
            )
        if self._source_attempts(self._rest(spec, path)) != attempts:
            return None
        return result

    def _owners(self, evidence, *, live=False):
        owners = _empty_owners()
        if evidence is None or evidence.get("reader_conflicts"):
            return owners
        source = evidence.get("source_vertex")
        if source is None:
            # Older archives only retained the observed attempt identities
            # in their map. Use those to select the original raw lines.
            legacy_attempts = {
                (owner["subtask"], owner["attempt"])
                for rows in evidence.get("assignments", {}).values()
                for owner in rows
            }
        else:
            attempts = self._source_attempts(source)

        for entry in evidence["assignment_logs"]:
            identity = entry["subtask"], entry["attempt"]
            if source is None:
                if identity not in legacy_attempts:
                    continue
            else:
                attempt = attempts.get(identity)
                if not attempt or attempt["host"] != entry["host"] or (live and attempt["status"] != "RUNNING"):
                    continue
                timestamp = datetime.fromisoformat(entry["timestamp"])
                if live and timestamp.utcoffset() is None:
                    raise ValueError("Reader assignment log timestamp has no timezone")
                recorded_at = int(timestamp.timestamp() * 1000)
                if not (0 < attempt["start-time"] <= recorded_at):
                    continue
                if 0 < attempt["end-time"] < recorded_at:
                    continue
            if live:
                splits = entry["line"].split(" - Adding split(s) to reader:", 1)[1].strip()
                if not _SPLIT_LIST.fullmatch(splits):
                    raise ValueError(f"Malformed assignment log for source attempt {identity}")
            for part in re.findall(r"\[Partition: allowances-(\d+),", entry["line"]):
                partition = int(part)
                if live and partition not in owners:
                    raise ValueError(f"Unexpected allowances partition {partition} for source attempt {identity}")
                if partition in owners:
                    owner = {"subtask": identity[0], "attempt": identity[1]}
                    if owner not in owners[partition]:
                        owners[partition].append(owner)
        for values in owners.values():
            values.sort(key=lambda owner: (owner["subtask"], owner["attempt"]))
        return owners

    def assignments(self, job_id):
        spec = self._release_for_job(job_id)
        if spec["status"] in _TERMINAL:
            return self._owners(self.read_artifact(f"job-{job_id}"))
        observation = self._assignment_observation(job_id, spec)
        return observation["owners"] if observation["complete"] else _empty_owners()

    def assignment_observation(self, job_id):
        """Observe current reader ownership with an explicit completeness result."""
        try:
            spec = self._release_for_job(job_id)
        except _OBSERVATION_ERRORS as error:
            return {"complete": False, "owners": _empty_owners(), "reason": f"Read release for {job_id}: {error}"}
        return self._assignment_observation(job_id, spec)

    def _assignment_observation(self, job_id, spec):
        owners = _empty_owners()
        try:
            path = f"/jobs/{job_id}"
            state = self._rest(spec, path)
            if not isinstance(state, dict) or not all(isinstance(state.get(key), str) for key in ("jid", "state")):
                raise RuntimeError(f"Malformed Flink job details at {path}")
            if state["jid"] != job_id or state["state"] != "RUNNING":
                raise RuntimeError(f"Expected RUNNING job {job_id}, observed {state['jid']} in {state['state']}")
            evidence = self._reader_evidence(job_id, state, spec, live=True)
            if evidence is None:
                raise RuntimeError("Source attempts changed during observation")
            if evidence["reader_conflicts"]:
                raise RuntimeError(f"Overlapping source threads in jobs {evidence['reader_conflicts']}")
            owners = self._owners(evidence, live=True)
            metrics = evidence["reader_metrics"]
            counts = {subtask: 0 for subtask in metrics}
            for rows in owners.values():
                for owner in rows:
                    counts[owner["subtask"]] += 1
            for subtask, gauges in metrics.items():
                if len(gauges) != 1:
                    raise RuntimeError(f"Expected one assigned-partitions gauge for source subtask {subtask}")
                value = next(iter(gauges.values()))
                if not re.fullmatch(r"\d+(?:\.0+)?", value):
                    raise ValueError(f"Invalid assigned-partitions gauge for source subtask {subtask}: {value!r}")
                assigned = int(value.split(".")[0])
                if assigned != counts[subtask]:
                    raise RuntimeError(
                        f"Source subtask {subtask} logs show {counts[subtask]} partitions, native gauge shows {assigned}"
                    )
            if not any(counts.values()):
                raise RuntimeError("No native split assignments observed yet")
        except _OBSERVATION_ERRORS as error:
            return {"complete": False, "owners": owners, "reason": f"Read reader ownership for {job_id}: {error}"}
        return {"complete": True, "owners": owners, "reason": None}

    def source_metrics(self, job_id):
        spec = self._release_for_job(job_id)
        state = self.job(job_id)
        vertex = self._source_vertex(state, spec["reader"])
        return self._source_metrics(spec, job_id, vertex, ("assigned-partitions", "records-lag-max"))

    def _source_metrics(self, spec, job_id, vertex, suffixes):
        result = {}
        for subtask in range(vertex["parallelism"]):
            path = f"/jobs/{job_id}/vertices/{vertex['id']}/subtasks/{subtask}/metrics"
            listed = self._rest(spec, path)
            if not isinstance(listed, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in listed
            ):
                raise RuntimeError(f"Malformed metric list at {path}")
            keys = [item["id"] for item in listed if item["id"].endswith(suffixes)]
            result[subtask] = {}
            if keys:
                values = self._rest(spec, path + "?get=" + ",".join(quote(key, safe="") for key in keys))
                if (
                    not isinstance(values, list)
                    or any(
                        not isinstance(item, dict)
                        or item.get("id") not in keys
                        or not isinstance(item.get("value"), str)
                        for item in values
                    )
                    or len({item["id"] for item in values}) != len(values)
                ):
                    raise RuntimeError(f"Malformed metric values at {path}")
                result[subtask] = {item["id"]: item["value"] for item in values}
        return result

    def archive_job(self, job_id):
        spec = self._release_for_job(job_id)
        if spec["status"] in _TERMINAL and f"job-{job_id}" in self.artifacts():
            return self.read_artifact(f"job-{job_id}")
        return self._archive_job(spec)

    def _archive_job(self, spec):
        job_id = spec["job_id"]
        job_path = f"/jobs/{job_id}"
        state = self._rest(spec, job_path)
        evidence = _wait(lambda: self._reader_evidence(job_id, state, spec), f"stable reader attempts for {job_id}")
        result = {
            "release": spec | {"status": state["state"]},
            "job": state,
            "checkpoints": self._rest(spec, job_path + "/checkpoints"),
            **evidence,
        }
        self.archive(f"job-{job_id}", result)
        return result

    def stop(self, job_id, *, drain=False):
        spec = self._release_for_job(job_id)
        if spec.get("stopped_savepoint"):
            return spec["stopped_savepoint"]
        job_path = f"/jobs/{job_id}"
        state = self._rest(spec, job_path)["state"]
        operation = spec.get("stop_operation")
        if operation is None:
            if state in _TERMINAL:
                raise RuntimeError(f"Job {job_id} is already {state} without a recorded stop savepoint")
            operation = {"trigger_id": uuid.uuid4().hex, "drain": drain}
            pending = spec | {"stop_operation": operation}
            self._update_release(spec, pending)
            spec = pending
        elif operation["drain"] != drain:
            raise ValueError("Drain setting differs from the pending stop")

        location = operation.get("location")
        if location is None:
            response = self._stop_result(spec, operation, state)
            location = response.get("location")
            failure = response.get("failure-cause")
            if failure is not None and location is None:
                cleared = {key: value for key, value in spec.items() if key != "stop_operation"}
                self._update_release(spec, cleared)
                raise RuntimeError(f"Stop with savepoint failed for {job_id}: {failure}")
            if not isinstance(location, str) or not location.strip() or failure is not None:
                raise RuntimeError(f"Malformed completed stop operation for {job_id}")
            pending = spec | {"stop_operation": operation | {"location": location}}
            self._update_release(spec, pending)
            spec = pending

        def terminal():
            status = self._rest(spec, job_path)["state"]
            return status if status in _TERMINAL else None

        status = state if state in _TERMINAL else _wait(terminal, f"job {job_id} to stop")
        stopped = spec | {"stopped_savepoint": location, "status": status}
        stopped.pop("stop_operation")
        self._update_release(spec, stopped)
        try:
            self._archive_job(stopped)
        except _OBSERVATION_ERRORS as error:
            _LOG.warning("Stopped Flink job %s at %s. Job archive failed: %s", job_id, location, error)
        return location

    def _stop_result(self, spec, operation, state):
        job_id = spec["job_id"]
        trigger = operation["trigger_id"]
        path = f"/jobs/{job_id}/savepoints/{trigger}"
        submitted = False

        def poll():
            nonlocal submitted
            try:
                response = self._rest(spec, path)
            except _FlinkHTTPError as error:
                if error.status != 404 or state in _TERMINAL or submitted:
                    raise
                submitted = True
                try:
                    reply = self._rest(
                        spec,
                        f"/jobs/{job_id}/stop",
                        method="POST",
                        payload={
                            "drain": operation["drain"],
                            "formatType": "CANONICAL",
                            "targetDirectory": f"file://{_DISK}/savepoints",
                            "triggerId": trigger,
                        },
                    )
                    if reply["request-id"] != trigger:
                        raise RuntimeError(f"Unexpected request ID in Flink stop reply for {job_id}")
                except _OBSERVATION_ERRORS as error:
                    _LOG.warning(
                        "Stop reply for job %s was unresolved. Checking operation %s: %s", job_id, trigger, error
                    )
                return None
            status = response["status"]["id"]
            if status == "IN_PROGRESS":
                return None
            if status != "COMPLETED" or not isinstance(response.get("operation"), dict):
                raise RuntimeError(f"Malformed Flink savepoint status at {path}")
            return response["operation"]

        return _wait(poll, f"savepoint operation {trigger} for {job_id}")

    def cancel(self, job_id):
        spec = self._release_for_job(job_id)
        if spec.get("stopped_savepoint"):
            return
        if self.job(job_id)["state"] not in _TERMINAL:
            self._rest(spec, f"/jobs/{job_id}?mode=cancel", method="PATCH")
            _wait(lambda: self.job(job_id)["state"] in _TERMINAL, f"job {job_id} to cancel")
        evidence = self.archive_job(job_id)
        self._retire(evidence["release"])

    @staticmethod
    def _broker_command():
        return (
            "exec",
            "-i",
            "kafka-0",
            "--",
            "java",
            "-Xms16m",
            "-Xmx80m",
            "-XX:ActiveProcessorCount=1",
            "-Dlog4j.configuration=file:/opt/kafka/config/tools-log4j.properties",
            "-cp",
            "/opt/kafka/libs/*:/stream-tools/kafka-ops.jar",
            "airport.meal.KafkaOps",
            "kafka:9092",
        )

    def _broker(self, op, topic="allowances", *, timeout=90, **params):
        request = json.dumps({"op": op, "topic": topic, **params}).encode() + b"\n"
        return json.loads(self._kube(*self._broker_command(), stdin=request, timeout=timeout))

    def produce(self, records, *, topic="allowances"):
        return self._broker("produce", topic, records=list(records))

    def begin_transaction(self, records, *, topic="allowances", transactional_id=None):
        if not transactional_id:
            transactional_id = "meal-upload-" + uuid.uuid4().hex
        request = json.dumps(
            {"op": "transaction", "topic": topic, "records": list(records), "transactional_id": transactional_id}
        )
        process = subprocess.Popen(
            [*self._kubectl, *self._broker_command()],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            process.stdin.write(request + "\n")
            process.stdin.flush()
            return _Transaction(process, _transaction_reply(process))
        except BaseException:
            process.kill()
            process.communicate()
            raise

    def read_records(self, topic, *, isolation="read_committed", offsets=None):
        return self._broker("read", topic, isolation=isolation, offsets=offsets or {})

    def read_orders(self, topic="issue-orders", *, offsets=None):
        rows = self.read_records(topic, offsets=offsets)
        for row in rows:
            row.update(json.loads(row.pop("value")))
        return rows

    def topic_id(self, topic="allowances", *, timeout=90):
        return self._broker("topic_id", topic, timeout=timeout)

    def bounds(self, topic="allowances", *, isolation="read_committed"):
        return {int(part): row for part, row in self._broker("bounds", topic, isolation=isolation).items()}

    def group_offsets(self, group, *, topic="allowances", timeout=90):
        offsets = self._broker("group_offsets", topic, group=group, timeout=timeout)
        return {int(part): offset for part, offset in offsets.items()}

    def set_group_offsets(self, group, offsets, *, topic="allowances"):
        self._broker("set_group_offsets", topic, group=group, offsets=offsets)

    def topic_config(self, topic="allowances"):
        return self._broker("topic_config", topic)

    def alter_topic_config(self, settings, *, topic="allowances"):
        self._broker("alter_topic_config", topic, settings=settings)

    def segment_files(self, topic="allowances"):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", topic):
            raise ValueError(f"Invalid Kafka topic {topic!r}")
        paths = self._kube("exec", "kafka-0", "--", "find", "/var/lib/kafka/data", "-type", "f")
        files = {}
        pattern = re.compile(rf"/{re.escape(topic)}-(\d+)/([^/]+)$")
        for path in paths.splitlines():
            match = pattern.search(path)
            if match:
                files.setdefault(int(match[1]), []).append(match[2])
        return {part: sorted(entries) for part, entries in files.items()}

    def segments(self, topic="allowances"):
        return {
            part: [int(name[:-4]) for name in files if re.fullmatch(r"\d+\.log", name)]
            for part, files in self.segment_files(topic).items()
        }

    def set_offset_retention(self, minutes, *, check_interval_ms=600000):
        self._kube(
            "set",
            "env",
            "statefulset/kafka",
            f"KAFKA_OFFSETS_RETENTION_MINUTES={int(minutes)}",
            f"KAFKA_OFFSETS_RETENTION_CHECK_INTERVAL_MS={int(check_interval_ms)}",
        )
        self._rollout("statefulset/kafka")
        self.topic_id()
