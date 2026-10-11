import io
import os
import tarfile
from unittest.mock import Mock

import pytest

from sregym.service import workload_volumes as volumes
from sregym.service.workload_volumes import WorkloadVolumes, collect_archive


def archive_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "../../trusted"
            data = b"agent log" if kind == tarfile.REGTYPE else b""
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data) if data else None)
    return stream.getvalue()


def test_regular_output_is_collected_into_dedicated_directory(tmp_path):
    parent = tmp_path / "run"
    parent.mkdir()
    metadata = parent / "result.json"
    metadata.write_text("trusted result")
    collect_archive(
        archive_bytes([("./sessions", tarfile.DIRTYPE), ("./sessions/agent.jsonl", tarfile.REGTYPE)]), parent / "agent"
    )
    assert (parent / "agent/sessions/agent.jsonl").read_bytes() == b"agent log"
    assert metadata.read_text() == "trusted result"
    if os.name == "posix":
        assert (parent / "agent/sessions/agent.jsonl").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../../result.json", tarfile.REGTYPE),
        ("/etc/passwd", tarfile.REGTYPE),
        ("x\\..\\result.json", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("link", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
        ("pipe", tarfile.FIFOTYPE),
    ],
)
def test_unsafe_outputs_are_rejected_before_any_writes(tmp_path, name, kind):
    with pytest.raises(ValueError, match="Unsafe"):
        collect_archive(archive_bytes([("first.log", tarfile.REGTYPE), (name, kind)]), tmp_path / "agent")
    assert not (tmp_path / "agent").exists()


@pytest.mark.parametrize(
    "entries",
    [
        [("same", tarfile.REGTYPE), ("same", tarfile.REGTYPE)],
        [("parent", tarfile.REGTYPE), ("parent/child", tarfile.REGTYPE)],
    ],
)
def test_collisions_are_rejected_before_writes(tmp_path, entries):
    with pytest.raises(ValueError, match="Duplicate|Conflicting"):
        collect_archive(archive_bytes(entries), tmp_path / "agent")
    assert not (tmp_path / "agent").exists()


def test_output_limits_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(volumes, "MAX_OUTPUT_FILES", 1)
    with pytest.raises(ValueError, match="limit"):
        collect_archive(archive_bytes([("a", tarfile.REGTYPE), ("b", tarfile.REGTYPE)]), tmp_path / "agent")
    assert not (tmp_path / "agent").exists()


@pytest.mark.skipif(os.name != "posix", reason="Linux symlink boundary")
def test_existing_output_symlink_cannot_replace_trusted_file(tmp_path):
    trusted = tmp_path / "trusted"
    trusted.write_text("private")
    output = tmp_path / "agent"
    output.mkdir()
    (output / "log").symlink_to(trusted)
    with pytest.raises(ValueError, match="symlink"):
        collect_archive(archive_bytes([("log", tarfile.REGTYPE)]), output)
    assert trusted.read_text() == "private"


@pytest.mark.skipif(os.name != "posix", reason="Linux rootless mount paths")
def test_selected_private_input_is_seeded_without_host_mount(tmp_path):
    private = tmp_path / "input"
    private.mkdir(mode=0o700)
    selected = private / "config"
    selected.write_text("selected agent credential")
    selected.chmod(0o600)
    (private / "oracle.pickle").write_text("private grader state")
    output = tmp_path / "agent"
    output.mkdir()
    runner = WorkloadVolumes("agent-image", "unix:///workload.sock")
    calls, archives = [], []

    def run(*args, **kwargs):
        calls.append(args)
        if "stdin" in kwargs:
            with tarfile.open(fileobj=kwargs["stdin"]) as archive:
                archives.append(
                    {member.name: archive.extractfile(member).read() for member in archive if member.isfile()}
                )

    runner._run = run
    rewritten = runner.rewrite(
        ["docker", "run", "-v", f"{selected}:/root/.kube/config:ro", "-v", f"{output}:/logs"], {output.resolve()}
    )
    assert not any(str(private) in argument or str(output) in argument for argument in rewritten)
    assert archives == [{"config": b"selected agent credential"}]
    assert any(argument.endswith("dst=/root/.kube,volume-nocopy,readonly") for argument in rewritten)
    assert all("volume-nocopy" in command[command.index("--mount") + 1] for command in calls if command[0] == "create")
    assert all(command[0] != "run" for command in calls)
    runner.close(collect=False)
    assert len([command for command in calls if command[:2] == ("volume", "rm")]) == 2


def test_failed_collection_still_removes_every_volume(tmp_path):
    runner = WorkloadVolumes("image")
    runner.volumes = [("first", tmp_path), ("second", None)]
    runner._collect = Mock(side_effect=ValueError("unsafe"))
    runner._run = Mock()
    with pytest.raises(ValueError, match="unsafe"):
        runner.close()
    assert runner.volumes == []
    assert [call.args for call in runner._run.call_args_list] == [
        ("volume", "rm", "-f", "first"),
        ("volume", "rm", "-f", "second"),
    ]


@pytest.mark.parametrize(
    "kind,size",
    [
        (tarfile.DIRTYPE, -1024),
        (tarfile.DIRTYPE, 1),
        (tarfile.REGTYPE, 128 * 1024 * 1024 + 1),
        (tarfile.GNUTYPE_SPARSE, 0),
    ],
)
def test_invalid_sizes_and_sparse_outputs_fail_before_writes(tmp_path, kind, size):
    member = tarfile.TarInfo("invalid")
    member.type, member.size = kind, size
    raw = member.tobuf(format=tarfile.GNU_FORMAT) + bytes(2048)
    with pytest.raises(ValueError, match="Unsafe"):
        collect_archive(raw, tmp_path / "agent")
    assert not (tmp_path / "agent").exists()


@pytest.mark.parametrize("name", ["a/" * 200 + "log", "x" * 256, "a/" * 3000 + "log", "é" * 200])
def test_adversarial_long_paths_fail_before_writes(tmp_path, name):
    with pytest.raises(ValueError, match="limit"):
        collect_archive(archive_bytes([(name, tarfile.REGTYPE)]), tmp_path / "agent")
    assert not (tmp_path / "agent").exists()


def test_total_regular_bytes_are_bounded_before_writes(tmp_path, monkeypatch):
    first = tarfile.TarInfo("a")
    second = tarfile.TarInfo("b")
    first.size = second.size = 9
    monkeypatch.setattr(volumes, "MAX_OUTPUT_BYTES", 16)
    members = [first, second]

    class FakeArchive:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return iter(members)

    monkeypatch.setattr(volumes.tarfile, "open", lambda **kwargs: FakeArchive())
    with pytest.raises(ValueError, match="limit"):
        collect_archive(b"", tmp_path / "agent")
    assert not (tmp_path / "agent").exists()


def test_failed_volume_removal_is_visible_and_retryable(tmp_path):
    runner = WorkloadVolumes("image")
    runner.volumes = [("first", tmp_path), ("second", None)]
    runner._run = Mock(side_effect=[RuntimeError("remove failed"), None])
    with pytest.raises(RuntimeError, match="remove failed"):
        runner.close(collect=False)
    assert runner.volumes == [("first", tmp_path)]
    assert runner._run.call_count == 2
    runner._run = Mock()
    runner.close(collect=False)
    assert runner.volumes == []
    runner._run.assert_called_once_with("volume", "rm", "-f", "first")


def test_failed_carrier_removal_is_visible_and_retryable():
    runner = WorkloadVolumes("image")
    runner._run = Mock(side_effect=[None, RuntimeError("carrier removal failed")])
    with pytest.raises(RuntimeError, match="carrier removal failed"), runner._carrier("owned-volume"):
        pass
    assert len(runner._carriers) == 1
    carrier = next(iter(runner._carriers))
    runner._run = Mock()
    runner.close(collect=False)
    runner._run.assert_called_once_with("rm", "-f", carrier)
    assert not runner._carriers


def test_uncertain_volume_create_is_tracked_and_cleaned(tmp_path):
    output = tmp_path / "agent"
    output.mkdir()
    runner = WorkloadVolumes("image")
    runner._run = Mock(side_effect=[TimeoutError("daemon response lost"), None])
    with pytest.raises(TimeoutError, match="daemon response lost"):
        runner.rewrite(["-v", f"{output}:/logs"], {output.resolve()})
    volume = runner._run.call_args_list[0].args[2]
    assert runner._run.call_args_list[1].args == ("volume", "rm", "-f", volume)
    assert not runner.volumes
