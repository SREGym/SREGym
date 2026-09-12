import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("sync_images", ROOT / "docker/sync_images.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
IMAGES = json.loads((ROOT / "docker/images.lock.json").read_text())


def test_committed_consumers_match_the_lock():
    outputs = sync.render_consumers(ROOT, IMAGES)
    assert len(outputs) >= 30
    assert all(file.read_text() == content for file, content in outputs.items())


def test_sync_updates_references_without_changing_database_images(tmp_path, monkeypatch):
    # Include databases and monitoring, not only files selected by the updater.
    hotel = Path("SREGym-applications/hotelReservation/kubernetes")
    shutil.copytree(ROOT / hotel, tmp_path / hotel)
    for file in sync.render_consumers(ROOT, IMAGES):
        destination = tmp_path / file.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
    originals = {file: file.read_text() for file in (tmp_path / hotel).rglob("*.yaml")}
    images = {key: f"registry.test/{key}:release@sha256:{'a' * 64}" for key in IMAGES}
    lock = tmp_path / "docker/images.lock.json"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text(json.dumps(images))
    monkeypatch.setattr(sync, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["sync_images.py", "--check"])
    assert sync.main() == 1
    assert all(file.read_text() == original for file, original in originals.items())

    monkeypatch.setattr(sys, "argv", ["sync_images.py"])
    assert sync.main() == 0
    updated = 0
    for file, original in originals.items():
        expected = yaml.safe_load(original)
        for container in expected.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            if container["name"] in sync.HOTEL_CONTAINERS:
                container["image"] = images["hotel-reservation"]
                updated += 1
        assert yaml.safe_load(file.read_text()) == expected
    assert updated == 8
    monkeypatch.setattr(sys, "argv", ["sync_images.py", "--check"])
    assert sync.main() == 0


def test_yaml_updates_preserve_other_documents_and_comments():
    source = "image: original # first document\n---\nimage: old # second document\n"
    result = sync.yaml_references(source, {(1, "image"): "new"})
    assert result == 'image: original # first document\n---\nimage: "new" # second document\n'


def test_missing_or_duplicate_yaml_fields_fail():
    with pytest.raises(ValueError, match="Expected one YAML field"):
        sync.yaml_references("image: first\nimage: second\n", {(0, "image"): "new"})
    with pytest.raises(ValueError, match="Expected one YAML field"):
        sync.yaml_references("unrelated: value\n", {(0, "image"): "new"})


def test_invalid_lock_is_rejected_before_consumer_edits():
    with pytest.raises(ValueError, match="multiarch index"):
        sync.render_consumers(ROOT, {**IMAGES, "kind-node": "kindest/node:latest"})
