"""Copy selected agent inputs and collect outputs without host bind mounts."""

import contextlib
import io
import os
import subprocess
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path, PurePosixPath

from sregym.service.docker_runtime import docker_command

MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_FILES = 20_000


def collect_archive(data: bytes, destination: Path) -> None:
    """Validate the entire untrusted archive before writing regular files."""
    if len(data) > MAX_OUTPUT_BYTES:
        raise ValueError("Agent output exceeds the collection limit")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        entries = []
        seen = set()
        size = 0
        for member in archive:
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or "\\" in member.name
                or ":" in member.name
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unsafe agent output archive")
            if relative == PurePosixPath(".") and member.isdir():
                continue
            if relative in seen:
                raise ValueError("Duplicate agent output archive entry")
            seen.add(relative)
            entries.append((member, relative))
            size += member.size
            if len(entries) > MAX_OUTPUT_FILES or size > MAX_OUTPUT_BYTES:
                raise ValueError("Agent output exceeds the collection limit")
        files = {relative for member, relative in entries if member.isfile()}
        if any(parent in files for _, relative in entries for parent in relative.parents):
            raise ValueError("Conflicting agent output archive paths")
        destination.mkdir(parents=True, exist_ok=True)
        for member, relative in entries:
            output = destination.joinpath(*relative.parts)
            checked = destination
            for component in relative.parts[:-1]:
                checked = checked / component
                if checked.is_symlink():
                    raise ValueError("Agent output destination contains a symlink")
                checked.mkdir(exist_ok=True, mode=0o700)
            if destination.is_symlink():
                raise ValueError("Agent output destination contains a symlink")
            if output.is_symlink():
                raise ValueError("Agent output destination contains a symlink")
            if member.isdir():
                output.mkdir(exist_ok=True, mode=0o700)
            else:
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(output, flags, 0o600), "wb") as handle, archive.extractfile(member) as source:
                    while chunk := source.read(1024 * 1024):
                        handle.write(chunk)


class WorkloadVolumes:
    def __init__(self, image: str, docker_host: str | None = None):
        self.image = image
        self.docker_host = docker_host
        self.volumes: list[tuple[str, Path | None]] = []

    def _run(self, *args, **kwargs):
        return subprocess.run(
            docker_command(*args, host=self.docker_host),
            check=True,
            capture_output=True,
            timeout=120,
            **kwargs,
        )

    @contextlib.contextmanager
    def _carrier(self, volume):
        # An unstarted container provides Docker's copy interface. It never
        # executes the image or receives a runtime socket or host directory.
        name = f"agent-volume-copy-{uuid.uuid4().hex}"
        try:
            self._run(
                "create",
                "--name",
                name,
                "--network=none",
                "--entrypoint=/bin/true",
                "-v",
                f"{volume}:/data",
                self.image,
            )
            yield name
        finally:
            with contextlib.suppress(Exception):
                self._run("rm", "-f", name)

    def rewrite(self, args: list[str], outputs: set[Path]) -> list[str]:
        """Replace absolute bind sources with volumes, grouping file parents."""
        result, groups = [], {}
        index = 0
        while index < len(args):
            if args[index] != "-v":
                result.append(args[index])
                index += 1
                continue
            binding = args[index + 1]
            source, target, *mode = binding.split(":")
            path = Path(source)
            index += 2
            if not path.is_absolute():
                result.extend(["-v", binding])
                continue
            readonly = mode == ["ro"]
            destination = path.resolve() if path.resolve() in outputs and not readonly else None
            if not readonly and destination is None:
                raise ValueError("Rootless agents may write only their output volumes")
            if path.is_symlink() or not path.exists():
                raise ValueError("Agent volume input must exist and must not be a symlink")
            container_path = PurePosixPath(target)
            if not container_path.is_absolute() or ".." in container_path.parts:
                raise ValueError("Invalid agent volume destination")
            mount = str(container_path if path.is_dir() else container_path.parent)
            group = groups.setdefault(mount, {"readonly": readonly, "output": destination, "inputs": []})
            if (group["readonly"], group["output"]) != (readonly, destination):
                raise ValueError("Conflicting agent volume destinations")
            if readonly:
                group["inputs"].append((path, "" if path.is_dir() else container_path.name))
        try:
            for mount, group in groups.items():
                volume = f"agent-files-{uuid.uuid4().hex}"
                self._run("volume", "create", volume)
                self.volumes.append((volume, group["output"]))
                if group["inputs"]:
                    with tempfile.TemporaryFile() as stream:
                        with tarfile.open(fileobj=stream, mode="w") as archive:
                            names = set()
                            for source, filename in group["inputs"]:
                                paths = [source] if filename else sorted(source.rglob("*"))
                                for path in paths:
                                    if path.is_symlink() or not (path.is_dir() or path.is_file()):
                                        raise ValueError("Agent inputs may contain only regular files and directories")
                                    name = filename or path.relative_to(source).as_posix()
                                    if name in names:
                                        raise ValueError("Duplicate agent input path")
                                    names.add(name)
                                    member = archive.gettarinfo(str(path), arcname=name)
                                    member.uid = member.gid = 0
                                    member.uname = member.gname = ""
                                    member.mode = 0o700 if member.isdir() or member.mode & 0o100 else 0o600
                                    if member.isfile():
                                        with path.open("rb") as handle:
                                            archive.addfile(member, handle)
                                    else:
                                        archive.addfile(member)
                        stream.seek(0)
                        with self._carrier(volume) as carrier:
                            self._run("cp", "-", f"{carrier}:/data", stdin=stream)
                result.extend(["-v", f"{volume}:{mount}" + (":ro" if group["readonly"] else "")])
            return result
        except BaseException:
            self.close(collect=False)
            raise

    def _collect(self, volume: str, destination: Path):
        with self._carrier(volume) as carrier:
            process = subprocess.Popen(
                docker_command("cp", f"{carrier}:/data/.", "-", host=self.docker_host),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            chunks = []
            reader = threading.Thread(
                target=lambda: chunks.append(process.stdout.read(MAX_OUTPUT_BYTES + 1)), daemon=True
            )
            reader.start()
            try:
                reader.join(timeout=60)
                if reader.is_alive():
                    raise TimeoutError("Timed out collecting agent output")
                if not chunks or len(chunks[0]) > MAX_OUTPUT_BYTES:
                    raise ValueError("Agent output exceeds the collection limit")
                if process.wait(timeout=5) != 0:
                    raise RuntimeError("Docker could not collect agent output")
                collect_archive(chunks[0], destination)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                reader.join(timeout=5)
                process.stdout.close()

    def close(self, *, collect=True):
        volumes, self.volumes = self.volumes, []
        failure = None
        for volume, output in volumes:
            try:
                if collect and output is not None:
                    self._collect(volume, output)
            except Exception as exc:
                failure = failure or exc
            finally:
                with contextlib.suppress(Exception):
                    self._run("volume", "rm", "-f", volume)
        if failure:
            raise failure
