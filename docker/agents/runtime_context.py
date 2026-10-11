"""Assemble the public agent sources without copying the private harness."""

import argparse
import ast
import hashlib
import re
from pathlib import Path

PUBLIC_MODULES = (
    "sregym/__init__.py",
    "sregym/paths.py",
    "sregym/service/__init__.py",
    "sregym/service/kubectl.py",
    "sregym/service/helm.py",
    "sregym/service/apps/base.py",
    "sregym/service/apps/helpers.py",
)
PUBLIC_SUFFIXES = {".py", ".yaml", ".txt", ".sh"}
PRIVATE_MODULES = {"llm_backend/judge_bridge.py", "clients/cursor/judge_bridge.py"}
IDENTIFIER = re.compile("sregym", re.IGNORECASE)
IMMUTABLE_IMAGE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")
SESSION_HELPER = b"""from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def session_url(url, session_id):
    parts = urlsplit(str(url))
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != "session_id"]
    query.append(("session_id", session_id))
    return urlunsplit(parts._replace(query=urlencode(query)))
"""


def _public_session_transport(text: str) -> str:
    """Use the server's existing neutral query protocol in generated clients."""
    if "sregym_ssid" not in text:
        return text
    tree = ast.parse(text)
    changed = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "SSETransport":
            continue
        headers = next((keyword for keyword in node.keywords if keyword.arg == "headers"), None)
        url = next((keyword for keyword in node.keywords if keyword.arg == "url"), None)
        if headers is None or not isinstance(headers.value, ast.Dict):
            continue
        index = next(
            (
                index
                for index, key in enumerate(headers.value.keys)
                if isinstance(key, ast.Constant) and key.value == "sregym_ssid"
            ),
            None,
        )
        if index is None:
            continue
        if url is None:
            raise ValueError("Session transport requires an explicit URL")
        session_id = headers.value.values.pop(index)
        headers.value.keys.pop(index)
        if not headers.value.keys:
            node.keywords.remove(headers)
        url.value = ast.Call(
            func=ast.Name(id="_public_session_url", ctx=ast.Load()), args=[url.value, session_id], keywords=[]
        )
        changed = True
    if not changed:
        raise ValueError("Unrecognized public session transport")
    insertion = 0
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        insertion = 1
    while (
        insertion < len(tree.body)
        and isinstance(tree.body[insertion], ast.ImportFrom)
        and tree.body[insertion].module == "__future__"
    ):
        insertion += 1
    tree.body.insert(
        insertion,
        ast.ImportFrom(
            module="incident_runtime.session",
            names=[ast.alias(name="session_url", asname="_public_session_url")],
            level=0,
        ),
    )
    result = ast.unparse(ast.fix_missing_locations(tree)) + "\n"
    if "sregym_ssid" in result:
        raise ValueError("Unrecognized public session transport")
    return result


def public_sources(root: Path) -> dict[str, bytes]:
    """Return the allowlisted, independently importable public runtime."""
    selected = list(PUBLIC_MODULES)
    for directory in ("clients", "logger", "llm_backend", "docker/agents/install-scripts"):
        selected.extend(
            path.relative_to(root).as_posix()
            for path in (root / directory).rglob("*")
            if path.is_file()
            and path.suffix in PUBLIC_SUFFIXES
            and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(root).parts)
        )
    sources = {}
    for name in sorted(set(selected)):
        if name in PRIVATE_MODULES:
            continue
        source = root / name
        if any(
            root.joinpath(*Path(name).parts[:index]).is_symlink()
            or root.joinpath(*Path(name).parts[:index]).is_junction()
            for index in range(1, len(Path(name).parts) + 1)
        ) or not source.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Public runtime source escapes the checkout: {name}")
        target = name.replace("sregym/", "incident_runtime/", 1)
        if target.startswith("docker/agents/"):
            target = target.removeprefix("docker/agents/")
        text = source.read_text(encoding="utf-8")
        if name.endswith(".py"):
            text = _public_session_transport(text)
        text = text.replace("SREGym-applications", "applications")
        text = text.replace("SREGYM_ARTIFACT_ID", "RUN_ARTIFACT_ID")
        text = text.replace("SREGYM_PROBLEM_ID", "PROBLEM_ID")
        text = IDENTIFIER.sub("incident_runtime", text)
        sources[target] = text.encode("utf-8")
    # Agent helpers need application utility paths, not fault-script or baseline locations.
    sources["incident_runtime/paths.py"] = (
        b"from pathlib import Path\n\n"
        b"BASE_DIR = Path(__file__).resolve().parent\n"
        b"BASE_PARENT_DIR = BASE_DIR.parent\n"
        b'TARGET_MICROSERVICES = BASE_PARENT_DIR / "applications"\n'
    )
    sources["incident_runtime/session.py"] = SESSION_HELPER
    return sources


def _write_fresh(destination: Path, sources: dict[str, bytes]) -> None:
    """Refuse overlays and redirected writes before creating a private context."""
    destination = destination.absolute()
    for path in (destination, *destination.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Public runtime destination has symlink ancestry")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, data in sources.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Public runtime target escapes the context")
        target = destination / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (target, *target.parents):
            if path.is_symlink() or path.is_junction():
                raise ValueError("Public runtime destination has symlink ancestry")
        with target.open("xb") as output:
            output.write(data)


def write_context(root: Path, destination: Path, *, base_image: str) -> str:
    """Write a fresh public context bound to an immutable dependency image.

    Removing an old path hides it in the merged filesystem, not parent layers.
    The dependency image must separately meet the deployment's image policy.
    """
    if not IMMUTABLE_IMAGE.fullmatch(base_image):
        raise ValueError("Public runtime base image must be an immutable SHA-256 reference")
    sources = public_sources(root)
    dockerfile = (
        f"FROM {base_image}\nUSER 0\n".encode() + b"RUN rm -rf /opt/sregym\n"
        b"COPY runtime/ /opt/runtime/\n"
        b"ENV PYTHONPATH=/opt/runtime\n"
        b'LABEL io.incident.runtime.root="/opt/runtime"\n'
        b"RUN chmod +x /opt/runtime/install-scripts/*.sh\n"
        b'WORKDIR /logs\nENTRYPOINT ["/bin/bash", "-c"]\n'
    )
    digest = hashlib.sha256(base_image.encode() + dockerfile)
    for name, data in sources.items():
        digest.update(name.encode() + b"\0" + data + b"\0")
    _write_fresh(destination, {"Dockerfile": dockerfile, **{"runtime/" + name: data for name, data in sources.items()}})
    return digest.hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    _write_fresh(args.destination, public_sources(args.root))
