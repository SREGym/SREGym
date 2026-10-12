"""Host-only tests for the per-run KIND config written by the DinD entrypoint."""

import importlib.util
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # the other DinD tests need only the standard library
    yaml = None

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(yaml is None, "PyYAML is required")
class KindConfigTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("kind_config", ROOT / "docker/dind/kind_config.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def build(self, env):
        config = yaml.safe_load((ROOT / "kind/kind-config.yaml").read_text())
        return self.module.build_config(config, env)

    @staticmethod
    def mounts(node):
        return {mount["containerPath"]: mount for mount in node.get("extraMounts", [])}

    def test_default_keeps_topology_and_adds_memory_backed_etcd(self):
        config = self.build({})
        self.assertEqual([node["role"] for node in config["nodes"]], ["control-plane", "worker", "worker", "worker"])
        self.assertIn("/var/lib/etcd", self.mounts(config["nodes"][0]))
        self.assertNotIn("/var/lib/etcd", self.mounts(config["nodes"][1]))
        self.assertNotIn("containerdConfigPatches", config)

    def test_disk_backed_etcd(self):
        config = self.build({"SREGYM_ETCD_TMPFS_SIZE": "0"})
        self.assertNotIn("/var/lib/etcd", self.mounts(config["nodes"][0]))

    def test_extra_ca_and_mirror_apply_to_every_node(self):
        config = self.build({"SREGYM_EXTRA_CA_CERTS": "/ca.pem", "SREGYM_REGISTRY_MIRROR": "https://mirror.example"})
        for node in config["nodes"]:
            mounts = self.mounts(node)
            self.assertTrue(mounts["/etc/ssl/certs/ca-certificates.crt"]["readOnly"])
            self.assertEqual(mounts["/etc/containerd/certs.d"]["hostPath"], "/run/sregym-containerd-certs.d")
            self.assertIn("/run/udev", mounts)
        self.assertEqual(config["containerdConfigPatches"], [self.module.CONTAINERD_REGISTRY_PATCH])

    def test_hosts_that_forbid_negative_oom_scores_clamp_pod_scores(self):
        self.assertNotIn("containerdConfigPatches", self.build({}))
        config = self.module.build_config(
            yaml.safe_load((ROOT / "kind/kind-config.yaml").read_text()), {}, lower_oom_score=False
        )
        self.assertEqual(config["containerdConfigPatches"], [self.module.CONTAINERD_RESTRICT_OOM_PATCH])


if __name__ == "__main__":
    unittest.main()
