from pathlib import Path
import importlib
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]        
GENI_LIB  = REPO_ROOT / "scripts" / "geni-lib"
sys.path.append(str(GENI_LIB))                          
cluster_setup = importlib.import_module("cluster_setup") 
class DummyExecutor:
    def __init__(self, host: str, user: str, key_path: str | None = None):
        self.host = host
        self.user = user
        self.key  = key_path
        self.exec_calls: list[str] = []

    def exec(self, cmd: str, timeout: int | None = None):
        self.exec_calls.append(cmd)
        return 0, "", ""      

    def close(self):
        pass


def test_deploy_srearena(monkeypatch, tmp_path):
    """
    1. Create a fake deploy key on disk.
    2. Monkey-patch cluster_setup.RemoteExecutor → DummyExecutor.
    3. Call deploy_srearena(); expect no exception.
    4. Assert that both the git-clone *and* cleanup commands happened.
    """
    key_file = tmp_path / "dummy_deploy_key"
    key_file.write_text("-----BEGIN FAKE KEY-----\nfake\n-----END FAKE KEY-----")

    monkeypatch.setattr(cluster_setup, "RemoteExecutor", DummyExecutor)

    ex = DummyExecutor("node0.test", "tester")           
    cluster_setup.deploy_srearena(ex, key_file.as_posix())

    assert any("git clone" in c for c in ex.exec_calls), \
        "SREArena repository was not cloned"
    assert any("rm -f ~/.ssh/srearena_deploy" in c for c in ex.exec_calls), \
        "Deploy key was not cleaned up"
