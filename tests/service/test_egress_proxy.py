"""Tests for the egress proxy and proxy env-var injection."""

import shutil
import ssl
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from sregym.service.container_runner import ContainerConfig, ContainerRunner
from sregym.service.egress_proxy import (
    CA_BUNDLE_ENV_VARS,
    DEFAULT_DENY_DOMAINS,
    NO_PROXY,
    EgressProxy,
    _build_combined_ca_bundle,
    _pick_free_port,
)

# ---------------------------------------------------------------------------
# Helpers — extract _is_denied from the addon script for isolated testing
# ---------------------------------------------------------------------------


def _load_is_denied(deny_list: list[str]):
    """Return the addon's _is_denied function with a given deny list.

    The addon script imports mitmproxy (not in the project venv), so we
    stub the import and only extract the pure-Python _is_denied function.
    """
    import sys
    import types

    from sregym.service.egress_proxy import _ADDON_SCRIPT

    fake_http = types.ModuleType("mitmproxy.http")
    fake_http.HTTPFlow = type("HTTPFlow", (), {})
    fake_http.Response = type("Response", (), {"make": staticmethod(lambda *a, **kw: None)})
    fake_mitmproxy = types.ModuleType("mitmproxy")
    fake_mitmproxy.http = fake_http

    ns: dict = {}
    saved = sys.modules.copy()
    try:
        sys.modules["mitmproxy"] = fake_mitmproxy
        sys.modules["mitmproxy.http"] = fake_mitmproxy.http
        exec(compile(_ADDON_SCRIPT, "<addon>", "exec"), ns)
    finally:
        sys.modules.update(saved)
        for key in ["mitmproxy", "mitmproxy.http"]:
            if key not in saved:
                sys.modules.pop(key, None)

    ns["DENY_LIST"] = deny_list
    return ns["_is_denied"]


# ===================================================================
# Unit tests — EgressProxy class basics
# ===================================================================


class TestEgressProxyBasics:
    def test_rejects_invalid_mode(self):
        with pytest.raises(ValueError, match="invalid"):
            EgressProxy(mode="invalid")

    def test_start_raises_in_on_mode(self):
        proxy = EgressProxy(mode="on")
        with pytest.raises(RuntimeError, match="should not be started"):
            proxy.start()

    def test_default_deny_domains_cover_sregym(self):
        expected = [
            "github.com/SREGym",
            "raw.githubusercontent.com/SREGym",
            "api.github.com/repos/SREGym",
            "codeload.github.com/SREGym",
        ]
        for domain in expected:
            assert domain in DEFAULT_DENY_DOMAINS

    def test_blocked_requests_empty_before_start(self):
        proxy = EgressProxy(mode="filtered")
        assert proxy.blocked_requests() == []

    def test_cleanup_removes_tmpdir(self):
        proxy = EgressProxy(mode="filtered")
        proxy._tmpdir = tempfile.mkdtemp(prefix="sregym-test-")
        assert Path(proxy._tmpdir).exists()
        proxy.cleanup()
        assert proxy._tmpdir is None


# ===================================================================
# Shared constants
# ===================================================================


class TestSharedConstants:
    def test_no_proxy_covers_private_ranges(self):
        for entry in [
            "localhost",
            "127.0.0.1",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "host.docker.internal",
            ".svc",
            ".cluster.local",
        ]:
            assert entry in NO_PROXY

    def test_ca_bundle_env_vars(self):
        expected = {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE"}
        assert set(CA_BUNDLE_ENV_VARS) == expected


# ===================================================================
# Addon deny-list logic (tested in isolation)
# ===================================================================


class TestAddonDenyLogic:
    def test_addon_denies_sregym_path(self):
        is_denied = _load_is_denied(["github.com/SREGym"])
        assert is_denied("github.com", "/SREGym/SREGym") is True
        assert is_denied("github.com", "/SREGym/SREGym/tree/main") is True

    def test_addon_allows_other_github_path(self):
        is_denied = _load_is_denied(["github.com/SREGym"])
        assert is_denied("github.com", "/kubernetes/kubernetes") is False

    def test_addon_denies_bare_host(self):
        is_denied = _load_is_denied(["evil.com"])
        assert is_denied("evil.com", "/any/path") is True
        assert is_denied("evil.com", "/") is True
        assert is_denied("good.com", "/any/path") is False


# ===================================================================
# Helper functions
# ===================================================================


class TestHelpers:
    def test_pick_free_port_returns_valid_port(self):
        port = _pick_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_build_combined_ca_bundle(self, tmp_path):
        ca1 = tmp_path / "system.pem"
        ca1.write_text("-----BEGIN CERTIFICATE-----\nSYSTEM\n-----END CERTIFICATE-----")
        ca2 = tmp_path / "proxy.pem"
        ca2.write_text("-----BEGIN CERTIFICATE-----\nPROXY\n-----END CERTIFICATE-----")
        output = tmp_path / "combined.pem"

        with patch.object(ssl, "get_default_verify_paths") as mock_paths:
            mock_paths.return_value = type("P", (), {"cafile": str(ca1)})()
            _build_combined_ca_bundle(ca2, output)

        contents = output.read_text()
        assert "SYSTEM" in contents
        assert "PROXY" in contents

    def test_find_mitmdump_not_found(self, monkeypatch):
        from sregym.service import egress_proxy

        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(shutil, "which", lambda _name: None)

        with pytest.raises(FileNotFoundError, match="mitmdump not found"):
            egress_proxy._find_mitmdump()


# ===================================================================
# ContainerRunner proxy injection
# ===================================================================


class TestContainerRunnerProxy:
    def test_proxy_env_vars_injected(self, tmp_path):
        ca_bundle = tmp_path / "ca-bundle.pem"
        ca_bundle.write_text("cert")
        config = ContainerConfig(
            proxy_url="http://host.docker.internal:18080",
            proxy_ca_bundle=ca_bundle,
        )
        runner = ContainerRunner(config)
        flags = runner._build_env_flags()
        env = self._flags_to_dict(flags)

        assert env["http_proxy"] == "http://host.docker.internal:18080"
        assert env["HTTPS_PROXY"] == "http://host.docker.internal:18080"
        assert env["no_proxy"] == NO_PROXY
        assert env["SSL_CERT_FILE"] == "/etc/ssl/certs/sregym-ca-bundle.pem"
        assert env["REQUESTS_CA_BUNDLE"] == "/etc/ssl/certs/sregym-ca-bundle.pem"

    def test_no_proxy_vars_without_url(self):
        config = ContainerConfig()
        runner = ContainerRunner(config)
        flags = runner._build_env_flags()
        env = self._flags_to_dict(flags)

        assert "http_proxy" not in env
        assert "HTTPS_PROXY" not in env

    def test_ca_bundle_mount(self, tmp_path):
        ca_bundle = tmp_path / "ca-bundle.pem"
        ca_bundle.write_text("cert")
        config = ContainerConfig(
            proxy_url="http://host.docker.internal:18080",
            proxy_ca_bundle=ca_bundle,
        )
        runner = ContainerRunner(config)
        args = runner._build_base_docker_args()

        mount_args = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        ca_mounts = [m for m in mount_args if "sregym-ca-bundle.pem" in m]
        assert len(ca_mounts) == 1
        assert ca_mounts[0].endswith(":ro")

    @staticmethod
    def _flags_to_dict(flags: list[str]) -> dict[str, str]:
        """Convert ['-e', 'K=V', '-e', 'K2=V2'] into {'K': 'V', 'K2': 'V2'}."""
        env = {}
        it = iter(flags)
        for flag in it:
            if flag == "-e":
                kv = next(it)
                k, v = kv.split("=", 1)
                env[k] = v
        return env


# ===================================================================
# Integration test — requires mitmdump
# ===================================================================


@pytest.mark.integration
class TestEgressProxyIntegration:
    def test_proxy_filtered_blocks_sregym(self):
        proxy = EgressProxy(mode="filtered")
        try:
            port = proxy.start()
            assert port > 0
            assert proxy.ca_bundle_path is not None
            assert proxy.ca_bundle_path.exists()

            proxy_handler = urllib.request.ProxyHandler(
                {
                    "http": f"http://127.0.0.1:{port}",
                    "https": f"http://127.0.0.1:{port}",
                }
            )
            ctx = ssl.create_default_context(cafile=str(proxy.ca_bundle_path))
            opener = urllib.request.build_opener(proxy_handler, urllib.request.HTTPSHandler(context=ctx))

            # SREGym URL should be blocked
            with pytest.raises(urllib.error.HTTPError, match="403"):
                opener.open("https://github.com/SREGym/SREGym", timeout=10)

            # Non-SREGym GitHub URL should be allowed
            resp = opener.open("https://github.com/kubernetes/kubernetes", timeout=10)
            assert resp.status == 200

            blocked = proxy.blocked_requests()
            assert any("SREGym" in req for req in blocked)
        finally:
            proxy.stop()
            proxy.cleanup()
