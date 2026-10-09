"""Run a live mitigation oracle in a hardened, runner-owned Docker container."""

import base64
import contextlib
import dataclasses
import hashlib
import io
import ipaddress
import json
import math
import os
import queue
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from sregym.service.docker_runtime import docker_command, trusted_docker_host
from sregym.service.verifier_sources import source_files as _source_files
from sregym.service.verifier_state import snapshot_oracle
from sregym.service.verifier_worker import MAX_FRAME_BYTES

BUILD_TIMEOUT_SECONDS = 1800
DEFAULT_TIMEOUT_SECONDS = 300
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_SCRATCH_BYTES = 8 * 1024**3
SCRATCH_DISK_RESERVE_BYTES = 1024**3


def _scratch_budget(value):
    if type(value) is not int or not 0 <= value <= MAX_SCRATCH_BYTES:
        raise VerifierError("Verifier scratch budget must be an integer between zero and 8 GiB")
    return value


def _process_budget(value):
    if type(value) is not int or not 256 <= value <= 2048:
        raise VerifierError("Verifier process budget must be an integer between 256 and 2048")
    return value


def _worker_capacity(value, resource):
    if type(value) is not int or not 1 <= value <= 8:
        raise VerifierError(f"Verifier {resource} budget must be an integer between 1 and 8")
    return value


@dataclasses.dataclass
class _ScratchVolume:
    name: str
    run_id: str
    owner: str
    created_at: str | None = None

    @property
    def labels(self):
        return {"io.sregym.verifier.run": self.run_id, "io.sregym.verifier.owner": self.owner}


class VerifierError(RuntimeError):
    """The harness could not obtain a trustworthy verdict."""


def _json_frame(line: bytes):
    if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
        raise VerifierError("Oversized or incomplete verifier protocol frame")

    def reject_constant(value):
        raise VerifierError(f"Nonfinite verifier JSON value: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise VerifierError(f"Duplicate verifier JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(line, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise VerifierError("Malformed verifier protocol frame") from exc
    if not isinstance(value, dict):
        raise VerifierError("Verifier protocol requires an object")
    return value


def build_verifier_image(root: Path, *, docker_host: str | None = None) -> str:
    """Build from a source-only context, then pin the Docker image ID for the run."""
    docker_host = docker_host or trusted_docker_host()
    if sys.implementation.name != "cpython":
        raise VerifierError("Verifier snapshots require a CPython runner matching the container interpreter")
    if sys.version_info.releaselevel != "final":
        raise VerifierError("Verifier snapshots require a final CPython release with a matching official image")
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    digest = hashlib.sha256()
    # cloudpickle supports only matching Python versions. The exact final
    # release is both a Docker build input and part of the image cache key.
    # Frozen dependencies must build successfully before any fault injection.
    digest.update(f"python\0{python_version}\0".encode())
    # The same bytes feed both the digest and Docker, preventing a build-context
    # race and excluding .env, credentials, Git data and host virtualenvs.
    with tempfile.TemporaryFile() as context:
        with tarfile.open(fileobj=context, mode="w") as archive:
            for path in _source_files(root):
                name = path.relative_to(root).as_posix()
                data = path.read_bytes()
                digest.update(name.encode() + b"\0" + data + b"\0")
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o644
                archive.addfile(member, io.BytesIO(data))
        tag = f"sregym-verifier:{digest.hexdigest()}"
        inspection = subprocess.run(
            docker_command("image", "inspect", "--format", "{{.Id}}", tag, host=docker_host),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if inspection.returncode != 0:
            context.seek(0)
            subprocess.run(
                [
                    *docker_command("build", host=docker_host),
                    "--build-arg",
                    f"PYTHON_VERSION={python_version}",
                    "-f",
                    "docker/verifier/Dockerfile",
                    "-t",
                    tag,
                    "-",
                ],
                stdin=context,
                check=True,
                timeout=BUILD_TIMEOUT_SECONDS,
            )
            inspection = subprocess.run(
                docker_command("image", "inspect", "--format", "{{.Id}}", tag, host=docker_host),
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        image = inspection.stdout.strip()
        if not image.startswith("sha256:") or len(image) != 71:
            raise VerifierError("Docker did not return an immutable verifier image ID")
        return image


def verifier_connection(kubeconfig_path: Path | None = None, *, docker_host: str | None = None) -> tuple[dict, str]:
    """Flatten private credentials; reach Kind directly from its Docker bridge."""
    command = ["kubectl"]
    if kubeconfig_path is not None:
        command.extend(["--kubeconfig", str(kubeconfig_path)])
    raw = subprocess.run(
        [*command, "config", "view", "--raw", "--flatten", "--minify", "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    config = json.loads(raw.stdout)
    cluster = config["clusters"][0]["cluster"]
    if cluster.get("insecure-skip-tls-verify"):
        raise VerifierError("Verifier Kubernetes access requires TLS certificate verification")
    user = config["users"][0]["user"]
    if "exec" in user or "auth-provider" in user:
        # Resolve the operator's normal auth on its owning host. Transport the
        # resulting Kubernetes credential, never the plugin or cloud secrets.
        from kubernetes.client import Configuration
        from kubernetes.config.kube_config import KubeConfigLoader

        private_config = Configuration()
        KubeConfigLoader(config_dict=config).load_and_set(private_config)
        authorization = private_config.get_api_key_with_prefix("authorization")
        if authorization and authorization.startswith("Bearer "):
            config["users"][0]["user"] = {"token": authorization.removeprefix("Bearer ")}
        elif private_config.cert_file and private_config.key_file:
            config["users"][0]["user"] = {
                "client-certificate-data": base64.b64encode(Path(private_config.cert_file).read_bytes()).decode(),
                "client-key-data": base64.b64encode(Path(private_config.key_file).read_bytes()).decode(),
            }
        else:
            raise VerifierError("Could not materialize Kubernetes credentials for the verifier")
    parsed = urlsplit(cluster["server"])
    if parsed.scheme != "https" or not parsed.hostname:
        raise VerifierError("Verifier Kubernetes access requires an HTTPS server")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if not loopback:
        return config, "bridge"

    # A host loopback address is not reachable from a bridge container. Match
    # the actual API port to a Kind control-plane container, without disabling
    # TLS or granting host networking / access to the Docker socket.
    nodes = json.loads(
        subprocess.run(
            [*command, "get", "nodes", "-o", "json"], capture_output=True, text=True, check=True, timeout=30
        ).stdout
    )
    for node in nodes.get("items", []):
        labels = node.get("metadata", {}).get("labels", {})
        provider = node.get("spec", {}).get("providerID", "")
        if "node-role.kubernetes.io/control-plane" not in labels or not provider.startswith("kind://"):
            continue
        name = provider.rsplit("/", 1)[-1]
        info = json.loads(
            subprocess.run(
                docker_command("inspect", "--type", "container", name, host=docker_host or trusted_docker_host()),
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
        )[0]
        bindings = info["NetworkSettings"].get("Ports", {}).get("6443/tcp") or []
        if not any(int(binding["HostPort"]) == (parsed.port or 443) for binding in bindings):
            continue
        for network, settings in info["NetworkSettings"]["Networks"].items():
            if settings.get("IPAddress"):
                cluster["server"] = f"https://{settings['IPAddress']}:6443"
                cluster.setdefault("tls-server-name", parsed.hostname)
                return config, network
    raise VerifierError(
        "Loopback Kubernetes endpoint is not a matching Kind API; provide a reachable verifier kubeconfig"
    )


def _resource_call(workloads, frame):
    index, operation, args = frame.get("index"), frame.get("op"), frame.get("args")
    if type(index) is not int or not 0 <= index < len(workloads) or not isinstance(args, list):
        raise VerifierError("Invalid verifier workload resource")
    workload = workloads[index]
    from sregym.service.codehub_verification_journal import CodeHubVerificationJournal

    if isinstance(workload, CodeHubVerificationJournal):
        entry = CodeHubVerificationJournal.OPERATIONS.get(operation)
        if entry is None or len(args) != entry[1]:
            raise VerifierError("Invalid verifier receipt operation")
        return getattr(workload, entry[0])(*args)
    if isinstance(workload, tuple) and workload[0] == "model":
        if operation != "model_inference" or len(args) != 1 or not isinstance(args[0], list):
            raise VerifierError("Invalid verifier model operation")
        from langchain_core.messages import messages_from_dict

        return workload[1].inference(messages_from_dict(args[0])).content
    if operation in {"start", "stop", "metrics"} and not args:
        if operation == "metrics":
            return workload.metrics.snapshot()
        return getattr(workload, operation)()
    if operation in {"snapshot", "set_rate"} and len(args) == 1:
        value = args[0]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 100_000:
            raise VerifierError("Invalid verifier workload argument")
        if operation == "snapshot":
            return dataclasses.asdict(workload.snapshot(value))
        return workload.set_rate(value)
    raise VerifierError("Unsupported verifier workload operation")


class VerifierRuntime:
    def __init__(
        self,
        *,
        kubeconfig_path: Path | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        docker_host: str | None = None,
    ):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Verifier timeout must be positive and finite")
        self.docker_host = docker_host or trusted_docker_host()
        self.kubeconfig_path = kubeconfig_path
        self.timeout_seconds = timeout_seconds
        self.image = None
        self.kubeconfig = None
        self.network = None
        self._lock = threading.Lock()
        self._active_name = None
        self._active_process = None
        self._active_run_id = None
        self._cancelled = threading.Event()
        self.last_log_path = None

    def prepare(self):
        # Cancellation belongs to this attempt and is permanent. A later
        # attempt gets a new runtime after the conductor drains prior work.
        if self._cancelled.is_set():
            raise VerifierError("Verifier invocation was cancelled")
        image = self.image or build_verifier_image(
            Path(__file__).resolve().parents[2], **({"docker_host": self.docker_host} if self.docker_host else {})
        )
        if self._cancelled.is_set():
            raise VerifierError("Verifier invocation was cancelled")
        kubeconfig, network = verifier_connection(
            self.kubeconfig_path, **({"docker_host": self.docker_host} if self.docker_host else {})
        )
        if self._cancelled.is_set():
            raise VerifierError("Verifier invocation was cancelled")
        self.image, self.kubeconfig, self.network = image, kubeconfig, network

    def docker_command(
        self, name, *, scratch_volume=None, scratch_bytes=0, process_limit=256, cpu_limit=2, memory_gib_limit=2
    ):
        if not self.image or not self.network:
            raise VerifierError("Verifier has not been prepared")
        arguments = [
            *docker_command("run", host=self.docker_host),
            "--rm",
            "-i",
            "--name",
            name,
            "--user=10001:10001",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--cpus={_worker_capacity(cpu_limit, 'CPU')}",
            f"--memory={_worker_capacity(memory_gib_limit, 'memory')}g",
            f"--pids-limit={_process_budget(process_limit)}",
            "--env=GOMAXPROCS=2",
            "--log-driver=none",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            f"--network={self.network}",
        ]
        if scratch_volume is not None:
            arguments.extend(
                [
                    f"--mount=type=volume,source={scratch_volume.name},target=/scratch,volume-nocopy",
                    "--env=TMPDIR=/scratch",
                    f"--env=SREGYM_VERIFIER_SCRATCH_BYTES={_scratch_budget(scratch_bytes)}",
                ]
            )
        return [*arguments, self.image]

    def _scratch_host_capacity(self, scratch_bytes, remaining):
        if os.name != "posix" or not self.docker_host or not self.docker_host.startswith("unix://"):
            raise VerifierError("Verifier scratch requires an explicit local trusted Unix Docker endpoint")
        endpoint = Path(self.docker_host.removeprefix("unix://"))
        if not endpoint.is_absolute() or endpoint.is_symlink():
            raise VerifierError("Trusted scratch endpoint must be an absolute root-owned socket")
        metadata = endpoint.stat()
        if metadata.st_uid != 0 or not stat.S_ISSOCK(metadata.st_mode):
            raise VerifierError("Trusted scratch endpoint must be a root-owned Unix socket")
        if self.docker_host == os.environ.get("DOCKER_HOST"):
            raise VerifierError("Scratch cannot use the workload Docker endpoint")
        info = json.loads(
            subprocess.run(
                docker_command("info", "--format", "{{json .}}", host=self.docker_host),
                capture_output=True,
                text=True,
                check=True,
                timeout=min(15, remaining()),
            ).stdout
        )
        root = Path(info.get("DockerRootDir", ""))
        if (
            info.get("OSType") != "linux"
            or any("rootless" in option for option in info.get("SecurityOptions", []))
            or not root.is_absolute()
            or not root.is_dir()
            or shutil.disk_usage(root).free < scratch_bytes + SCRATCH_DISK_RESERVE_BYTES
        ):
            raise VerifierError("Trusted Docker scratch storage lacks verified local capacity")

    def _inspect_scratch(self, volume, *, timeout=15):
        result = subprocess.run(
            docker_command("volume", "inspect", volume.name, host=self.docker_host),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode:
            if "no such volume" in result.stderr.lower():
                return None
            raise VerifierError("Could not inspect the owned verifier scratch volume")
        try:
            values = json.loads(result.stdout)
            info = values[0]
            valid = (
                len(values) == 1
                and info["Name"] == volume.name
                and info["Driver"] == "local"
                and info["Scope"] == "local"
                and not info.get("Options")
                and all((info.get("Labels") or {}).get(key) == value for key, value in volume.labels.items())
                and isinstance(info.get("CreatedAt"), str)
                and bool(info["CreatedAt"])
                and (volume.created_at is None or info["CreatedAt"] == volume.created_at)
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise VerifierError("Malformed scratch volume ownership inspection") from exc
        if not valid:
            raise VerifierError("Verifier scratch volume ownership changed")
        volume.created_at = info["CreatedAt"]
        return info

    def _initialize_scratch(self, volume, scratch_bytes, remaining):
        if (
            type(self.image) is not str
            or not self.image.startswith("sha256:")
            or len(self.image) != 71
            or any(character not in "0123456789abcdef" for character in self.image[7:])
        ):
            raise VerifierError("Scratch initialization requires the immutable trusted verifier image ID")
        if self._inspect_scratch(volume, timeout=min(15, remaining())) is None:
            raise VerifierError("Created verifier scratch volume disappeared")
        name = f"sregym-verifier-scratch-init-{volume.run_id}"
        # chmod precedes chown: the initializer has CHOWN only, not FOWNER.
        program = (
            "import os,pathlib,shutil; p=pathlib.Path('/scratch'); "
            "assert p.is_dir() and not p.is_symlink() and not any(p.iterdir()), 'scratch must be empty'; "
            f"assert shutil.disk_usage(p).free >= {scratch_bytes + SCRATCH_DISK_RESERVE_BYTES}, 'insufficient scratch capacity'; "
            "os.chmod(p,0o700); os.chown(p,10001,10001)"
        )
        command = [
            *docker_command("run", host=self.docker_host),
            "--rm",
            "--name",
            name,
            "--user=0:0",
            "--read-only",
            "--network=none",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--security-opt=no-new-privileges",
            "--cpus=0.5",
            "--memory=128m",
            "--memory-swap=128m",
            "--pids-limit=32",
            "--log-driver=none",
            "--tmpfs=/tmp:rw,nosuid,nodev,size=16m",
            f"--label=io.sregym.verifier.run={volume.run_id}",
            f"--label=io.sregym.verifier.owner={volume.owner}",
            f"--mount=type=volume,source={volume.name},target=/scratch,volume-nocopy",
            "--entrypoint=python",
            self.image,
            "-c",
            program,
        ]
        process = None
        try:
            with self._lock:
                remaining()
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                self._active_name, self._active_process = name, process
            while True:
                try:
                    process.communicate(timeout=min(0.1, remaining()))
                    break
                except subprocess.TimeoutExpired:
                    continue
            remaining()
            if process.returncode != 0:
                raise VerifierError("Private verifier scratch initialization failed")
        finally:
            if process is not None:
                with contextlib.suppress(subprocess.SubprocessError, OSError):
                    subprocess.run(
                        docker_command("rm", "-f", name, host=self.docker_host),
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=15)
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                with self._lock:
                    if self._active_process is process:
                        self._active_name, self._active_process = None, None

    def _remove_scratch(self, volume):
        if self._inspect_scratch(volume) is not None:
            result = subprocess.run(
                docker_command("volume", "rm", volume.name, host=self.docker_host),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode:
                raise VerifierError("Owned verifier scratch volume could not be removed")

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            name, process = self._active_name, self._active_process
        try:
            if name:
                subprocess.run(
                    docker_command("rm", "-f", name, host=self.docker_host),
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
        finally:
            if process and process.poll() is None:
                process.kill()

    def evaluate(self, oracle, *args) -> dict:
        scratch_bytes = _scratch_budget(getattr(oracle, "verification_scratch_bytes", 0))
        process_limit = _process_budget(getattr(oracle, "verification_process_limit", 256))
        _worker_capacity(getattr(oracle, "verification_cpu_limit", 2), "CPU")
        _worker_capacity(getattr(oracle, "verification_memory_gib_limit", 2), "memory")
        if self._cancelled.is_set():
            raise VerifierError("Verifier invocation was cancelled")
        if self.kubeconfig is None:
            self.prepare()
        else:
            # Agent stages may outlast an exec plugin's credential lifetime.
            self.kubeconfig, self.network = verifier_connection(
                self.kubeconfig_path, **({"docker_host": self.docker_host} if self.docker_host else {})
            )
        if self._cancelled.is_set():
            raise VerifierError("Verifier invocation was cancelled")
        prepare = getattr(oracle.problem, "prepare_verification", None)
        if prepare is not None and not args:
            prepare()
        process_limit = _process_budget(getattr(oracle, "verification_process_limit", 256))
        payload, workloads = snapshot_oracle(oracle, Path(__file__).resolve().parents[2])
        return self.evaluate_snapshot(
            payload,
            workloads,
            getattr(oracle, "evaluation_timeout_seconds", None),
            args=args,
            scratch_bytes=scratch_bytes,
            process_limit=process_limit,
            cpu_limit=_worker_capacity(getattr(oracle, "verification_cpu_limit", 2), "CPU"),
            memory_gib_limit=_worker_capacity(getattr(oracle, "verification_memory_gib_limit", 2), "memory"),
        )

    def evaluate_snapshot(
        self,
        payload,
        workloads=(),
        oracle_timeout=None,
        *,
        args=(),
        scratch_bytes=0,
        process_limit=256,
        cpu_limit=2,
        memory_gib_limit=2,
    ) -> dict:
        scratch_bytes = _scratch_budget(scratch_bytes)
        process_limit = _process_budget(process_limit)
        cpu_limit = _worker_capacity(cpu_limit, "CPU")
        memory_gib_limit = _worker_capacity(memory_gib_limit, "memory")
        if self._cancelled.is_set():
            raise VerifierError("Verifier invocation was cancelled")
        if self.kubeconfig is None:
            self.prepare()
        if self._cancelled.is_set():
            raise VerifierError("Verifier invocation was cancelled")
        timeout = self.timeout_seconds
        if type(oracle_timeout) in (int, float) and math.isfinite(oracle_timeout):
            timeout = max(timeout, oracle_timeout)
        deadline = time.monotonic() + timeout
        run_id = uuid.uuid4().hex
        name = f"sregym-verifier-{run_id}"
        request = {
            "run_id": run_id,
            "kubeconfig": self.kubeconfig,
            "oracle": base64.b64encode(payload).decode(),
            "args": args,
        }
        initial_frame = json.dumps(request, allow_nan=False).encode() + b"\n"
        if len(initial_frame) > MAX_FRAME_BYTES:
            raise VerifierError("Oracle snapshot exceeds the verifier input limit")
        log_dir = Path(tempfile.mkdtemp(prefix="sregym-verifier-"))
        log_path = log_dir / "oracle.log"
        messages = queue.Queue(maxsize=16)
        stopped = threading.Event()
        process = None
        scratch_volume = None

        def remaining():
            if self._cancelled.is_set():
                raise VerifierError("Verifier invocation was cancelled")
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("Verifier exceeded its grading deadline")
            return budget

        def publish(message):
            while not stopped.is_set():
                try:
                    messages.put(message, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def bounded_call(operation):
            # Live workload IO and pipe writes must obey the same deadline as
            # the container. A stuck adapter must not strand the conductor.
            completed = queue.Queue(maxsize=1)

            def invoke():
                try:
                    completed.put((True, operation()))
                except Exception as exc:
                    completed.put((False, exc))

            thread = threading.Thread(target=invoke, daemon=True)
            thread.start()
            while True:
                try:
                    ok, value = completed.get(timeout=min(0.1, remaining()))
                    break
                except queue.Empty:
                    continue
            if not ok:
                raise value
            return value

        try:
            with self._lock:
                remaining()
                if self._active_process is not None or self._active_run_id is not None:
                    raise VerifierError("A verifier invocation is already active")
                self._active_run_id = run_id
            if scratch_bytes:
                self._scratch_host_capacity(scratch_bytes, remaining)
                remaining()
                scratch_volume = _ScratchVolume(f"sregym-verifier-scratch-{run_id}", run_id, uuid.uuid4().hex)
                command = docker_command("volume", "create", "--driver", "local", host=self.docker_host)
                for key, value in scratch_volume.labels.items():
                    command.extend(["--label", f"{key}={value}"])
                subprocess.run(
                    [*command, scratch_volume.name],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=min(15, remaining()),
                )
                self._initialize_scratch(scratch_volume, scratch_bytes, remaining)
                if self._inspect_scratch(scratch_volume, timeout=min(15, remaining())) is None:
                    raise VerifierError("Verifier scratch disappeared before grading")
            with self._lock:
                if self._cancelled.is_set():
                    raise VerifierError("Verifier invocation was cancelled")
                if self._active_process is not None:
                    raise VerifierError("A verifier invocation is already active")
                self.last_log_path = log_path
                worker_options = {}
                if scratch_volume is not None:
                    worker_options.update(scratch_volume=scratch_volume, scratch_bytes=scratch_bytes)
                if process_limit != 256:
                    worker_options["process_limit"] = process_limit
                if cpu_limit != 2:
                    worker_options["cpu_limit"] = cpu_limit
                if memory_gib_limit != 2:
                    worker_options["memory_gib_limit"] = memory_gib_limit
                process = subprocess.Popen(
                    self.docker_command(name, **worker_options),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self._active_name, self._active_process = name, process

            def read_stdout():
                try:
                    while True:
                        line = process.stdout.readline(MAX_FRAME_BYTES + 1)
                        publish(line)
                        if not line or len(line) > MAX_FRAME_BYTES:
                            return
                except Exception as exc:
                    publish(exc)

            def read_stderr():
                size = 0
                try:
                    with log_path.open("wb") as output:
                        log_path.chmod(0o600)
                        while chunk := process.stderr.read(8192):
                            output.write(chunk[: max(0, MAX_LOG_BYTES - size)])
                            size += len(chunk)
                except Exception as exc:
                    # Teardown can close the pipe before this thread runs.
                    # An unexpected drain failure must still prevent a verdict.
                    publish(exc)

            reader = threading.Thread(target=read_stdout, daemon=True)
            logger = threading.Thread(target=read_stderr, daemon=True)
            reader.start()
            logger.start()

            def write_initial_frame():
                try:
                    process.stdin.write(initial_frame)
                    process.stdin.flush()
                except Exception as exc:
                    publish(exc)

            writer = threading.Thread(target=write_initial_frame, daemon=True)
            writer.start()
            writer.join(timeout=remaining())
            if writer.is_alive():
                raise TimeoutError("Verifier did not consume its input before the deadline")
            while True:
                try:
                    line = messages.get(timeout=remaining())
                except queue.Empty as exc:
                    raise TimeoutError("Verifier exceeded its grading deadline") from exc
                if isinstance(line, Exception) or not line:
                    raise VerifierError("Verifier exited without a verdict")
                frame = _json_frame(line)
                if frame.get("run_id") != run_id:
                    raise VerifierError("Verifier result belongs to a different invocation")
                if frame.get("type") == "resource":
                    # These are IO adapters, never oracle or arbitrary method
                    # execution. Preserve errors caught by existing oracles.
                    try:
                        value = bounded_call(lambda frame=frame: _resource_call(workloads, frame))
                        response = {"run_id": run_id, "type": "resource_result", "value": value}
                    except (VerifierError, TimeoutError):
                        raise
                    except Exception as exc:
                        response = {"run_id": run_id, "type": "resource_result", "error": str(exc)}
                    response_bytes = json.dumps(response, allow_nan=False).encode() + b"\n"
                    if len(response_bytes) > MAX_FRAME_BYTES:
                        raise VerifierError("Verifier resource response exceeds the input limit")

                    def write_response(response_bytes=response_bytes):
                        process.stdin.write(response_bytes)
                        process.stdin.flush()

                    bounded_call(write_response)
                    continue
                if frame.get("type") != "verdict":
                    raise VerifierError("Verifier could not produce a verdict; see its private oracle log")
                result = frame.get("result")
                if not isinstance(result, dict) or type(result.get("success")) is not bool:
                    raise VerifierError("Verifier returned an invalid success value")
                process.stdin.close()
                if process.wait(timeout=remaining()) != 0:
                    raise VerifierError("Verifier exited unsuccessfully after reporting a verdict")
                reader.join(timeout=remaining())
                logger.join(timeout=remaining())
                if reader.is_alive() or logger.is_alive():
                    raise VerifierError("Verifier output did not close")
                trailing = []
                while not messages.empty():
                    trailing.append(messages.get_nowait())
                if trailing != [b""]:
                    raise VerifierError("Verifier wrote unexpected data after its verdict")
                return result
        finally:
            original_error = sys.exc_info()[1]
            stopped.set()
            try:
                if process is not None:
                    # Docker may keep the container alive after its attached
                    # CLI dies. Always remove this invocation by its private
                    # name, including after an already-exited client.
                    with contextlib.suppress(subprocess.SubprocessError, OSError):
                        subprocess.run(
                            docker_command("rm", "-f", name, host=self.docker_host),
                            capture_output=True,
                            timeout=15,
                            check=False,
                        )
                    if process.poll() is None:
                        with contextlib.suppress(OSError):
                            process.kill()
                        with contextlib.suppress(subprocess.SubprocessError):
                            process.wait(timeout=15)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None and not stream.closed:
                            with contextlib.suppress(OSError):
                                stream.close()
            finally:
                scratch_error = None
                if scratch_volume is not None:
                    try:
                        self._remove_scratch(scratch_volume)
                    except Exception as exc:
                        scratch_error = exc
                with self._lock:
                    if self._active_run_id == run_id:
                        self._active_name, self._active_process = None, None
                        self._active_run_id = None
                if scratch_error is not None:
                    if original_error is not None:
                        original_error.add_note(f"Owned verifier scratch cleanup failed: {scratch_error}")
                    else:
                        raise scratch_error
