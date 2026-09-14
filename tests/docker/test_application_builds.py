import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("prepatched", [False, True])
def test_redis_patch_is_idempotent(tmp_path, prepatched):
    header = tmp_path / "redis_cluster.h"
    member = "    ShardsPool* get_shards_pool(){ return &_pool; }\n" if prepatched else ""
    header.write_text(
        "struct ShardsPool {};\nstruct RedisCluster {\n    ShardsPool _pool;\n"
        + member
        + "    Transaction transaction(const std::string &hash_tag);\n};\n"
    )
    script = ROOT / "SREGym-applications/socialNetwork/docker/patch-redis-plus-plus.py"
    for _ in range(2):
        subprocess.run([sys.executable, str(script), str(header)], check=True)
    assert header.read_text().count("get_shards_pool") == 1
    assert "return &_pool" in header.read_text()


def test_redis_patch_rejects_an_unknown_source_without_modifying_it(tmp_path):
    header = tmp_path / "redis_cluster.h"
    header.write_text("unexpected source\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "SREGym-applications/socialNetwork/docker/patch-redis-plus-plus.py"), str(header)],
        capture_output=True,
    )
    assert result.returncode != 0
    assert header.read_text() == "unexpected source\n"


def test_ui_descriptor_wrapper_preserves_lower_limits():
    import yaml

    values = yaml.safe_load((ROOT / "sregym/service/apps/values/astronomy-shop-fixes.yaml").read_text())
    command = values["components"]["flagd"]["sidecarContainers"][0]["command"][2]
    # Intercept exec, so the actual wrapper runs without needing the UI binary.
    for limit, expected in (("2048", ""), ("unlimited", "limit -n 65536\n"), ("1073741816", "limit -n 65536\n")):
        prefix = 'ulimit() { if [ "$#" = 1 ]; then printf "%s\\n" "$TEST_LIMIT"; else printf "limit %s\\n" "$*"; fi; }; exec() { printf "exec %s\\n" "$*"; }; '
        result = subprocess.run(
            ["bash", "-c", prefix + command],
            env={**os.environ, "TEST_LIMIT": limit},
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout == expected + "exec /app/bin/server\n"
