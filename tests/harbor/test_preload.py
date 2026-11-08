"""Image bundles preserve runtime references and never reuse cluster state."""

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from sregym.harbor import preload


def node(name, digest, *, architecture="amd64"):
    return {
        "metadata": {"name": name},
        "status": {
            "nodeInfo": {"operatingSystem": "linux", "architecture": architecture},
            "images": [{"names": [f"example.test/app@{digest}", "example.test/app:stable"]}],
        },
    }


def snapshot(tmp_path, *nodes):
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps({"items": nodes}))
    return path


def test_inventory_preserves_aliases_and_makes_shared_images_common(tmp_path):
    digest = "sha256:" + "a" * 64
    path = snapshot(tmp_path, node("kind-control-plane", digest), node("kind-worker", digest))
    plan = preload.inventory([path], "amd64")
    assert plan["images"] == {
        f"example.test/app@{digest}": {
            "aliases": ["example.test/app:stable", f"example.test/app@{digest}"],
            "role": "common",
        }
    }


def test_inventory_rejects_tag_drift_between_nodes(tmp_path):
    path = snapshot(tmp_path, node("kind-worker", "sha256:" + "a" * 64), node("kind-worker2", "sha256:" + "b" * 64))
    with pytest.raises(ValueError, match="tag changed"):
        preload.inventory([path], "amd64")


def test_inventory_rejects_platform_mismatch(tmp_path):
    path = snapshot(tmp_path, node("kind-worker", "sha256:" + "a" * 64, architecture="arm64"))
    with pytest.raises(ValueError, match="does not match"):
        preload.inventory([path], "amd64")


@pytest.fixture
def registry(tmp_path):
    """A real content-addressed index with another platform deliberately absent."""

    def store(data):
        digest = preload.digest_bytes(data)
        path = tmp_path / digest.split(":")[1]
        path.write_bytes(data)
        return {"digest": digest, "size": len(data)}, path

    config, config_path = store(b'{"architecture":"amd64","os":"linux"}')
    layer, layer_path = store(b"the original compressed image layer")
    leaf = {"schemaVersion": 2, "mediaType": preload.MANIFEST_TYPE, "config": config, "layers": [layer]}
    child, child_path = store(json.dumps(leaf).encode())
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
        "manifests": [
            {**child, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "f" * 64, "size": 42, "platform": {"os": "linux", "architecture": "arm64"}},
        ],
    }
    # Whitespace must remain byte-for-byte identical; reparsing must not rehash it.
    parent, parent_path = store(json.dumps(index, indent=3).encode() + b"\n")
    paths = {parent["digest"]: parent_path, child["digest"]: child_path}

    class FakeRegistry:
        def manifest(self, reference):
            path = paths[reference.rsplit("@", 1)[1]]
            return path, json.loads(path.read_bytes())

        def content(self, reference, manifest):
            assert reference == "example.test/app@" + child["digest"]
            return tmp_path

    return FakeRegistry(), parent, parent_path, layer_path


def test_archive_preserves_multiplatform_digest_and_all_requested_names(tmp_path, registry):
    client, parent, original, _ = registry
    source = "example.test/app@" + parent["digest"]
    aliases = [source, "example.test/app:stable"]
    archive_path = tmp_path / "images.tar"
    preload.write_archive(archive_path, {source: {"aliases": aliases}}, client, "amd64")
    with tarfile.open(archive_path) as archive:
        index = json.load(archive.extractfile("index.json"))
        # k3s rejects canonical-only names during its retagging step. Import a
        # normal tag against the exact original target; CRI derives its digest.
        assert {item["annotations"]["io.containerd.image.name"] for item in index["manifests"]} == {
            "example.test/app:stable"
        }
        assert {item["digest"] for item in index["manifests"]} == {parent["digest"]}
        raw = archive.extractfile("blobs/sha256/" + parent["digest"].split(":")[1]).read()
        assert raw == original.read_bytes()
        assert preload.digest_bytes(raw) == parent["digest"]
        assert "blobs/sha256/" + "f" * 64 not in archive.getnames()
        assert all(not name.startswith("/") and ".." not in Path(name).parts for name in archive.getnames())


def test_corrupt_blob_never_produces_a_finished_archive(tmp_path, registry):
    client, parent, _, layer_path = registry
    layer_path.write_bytes(b"corrupted layer")
    source = "example.test/app@" + parent["digest"]
    archive = tmp_path / "images.tar"
    with pytest.raises(ValueError, match="digest/size"):
        preload.write_archive(archive, {source: {"aliases": [source]}}, client, "amd64")
    assert not archive.exists()


def test_digest_only_references_receive_k3s_compatible_import_tags():
    digest = "sha256:" + "a" * 64
    source = f"registry.test:5000/team/app@{digest}"
    assert preload.import_tags(source, [source]) == [f"registry.test:5000/team/app:sregym-preload-{'a' * 64}"]


def test_existing_tags_are_preserved_for_each_repository_alias():
    digest = "sha256:" + "a" * 64
    source = f"registry.test/team/app@{digest}"
    assert preload.import_tags(source, [source, "registry.test/team/app:stable", f"mirror.test/app@{digest}"]) == [
        f"mirror.test/app:sregym-preload-{'a' * 64}",
        "registry.test/team/app:stable",
    ]


def test_archive_rejects_missing_target_platform(tmp_path, registry):
    client, parent, _, _ = registry
    with pytest.raises(ValueError, match="found 0"):
        preload.image_content(client, "example.test/app@" + parent["digest"], "s390x")


def test_context_records_archive_checksum_and_works_with_published_base(tmp_path, registry, monkeypatch):
    client, parent, _, _ = registry
    source = "example.test/app@" + parent["digest"]
    monkeypatch.setattr(preload, "Registry", lambda *args: client)
    plan = {"schema_version": 1, "architecture": "amd64", "images": {source: {"aliases": [source], "role": "workers"}}}
    output = tmp_path / "context"
    preload.build_context(plan, output, tmp_path / "cache", "example.test/sregym:validated")
    report = json.loads((output / "bundle.json").read_text())
    assert (
        report["archives"]["workers"]["sha256"]
        == hashlib.sha256((output / "archives/workers.tar").read_bytes()).hexdigest()
    )
    assert not (output / "archives/common.tar").exists()
    # The derived image installs the import hooks even when its published base
    # predates optional preloads. No database, snapshot or credentials are copied.
    assert (output / "start.sh").read_bytes() == (preload.ROOT / "docker/harbor/start.sh").read_bytes()
    assert (output / "node.sh").read_bytes() == (preload.ROOT / "docker/userns-k3s/node.sh").read_bytes()


def test_context_rejects_dockerfile_injection(tmp_path):
    with pytest.raises(ValueError, match="base image"):
        preload.build_context({}, tmp_path, tmp_path / "cache", "valid:tag\nRUN touch /bad")
