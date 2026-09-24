"""Native image/ABI and action-protocol checks; no Kubernetes cluster required.

Run on each native host: python docker/openwhisk/test_runtime.py --arch arm64
References are read from images.lock.json unless --tag selects candidate builds.
"""

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

PROGRAMS = {
    "controller": ["java", "docker"],
    "invoker": ["java", "docker"],
    "utility": ["java", "python", "node", "kubectl", "wsk", "docker"],
    "zookeeper": ["java", "nc"],
    "alarmprovider": ["node"],
    "kafkaprovider": ["python"],
    "apigateway": ["api-gateway", "api-gateway-config-supervisor", "rclone", "/usr/local/api-gateway/lualib/cjson.so"],
    "nodejs14": ["node"],
    "python37": ["python", "/bin/proxy"],
}


def run(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def check_action(image, arch, code):
    container = run(
        "docker",
        "run",
        "-d",
        "--platform",
        f"linux/{arch}",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "-p",
        "127.0.0.1::8080",
        image,
    )
    try:
        port = run("docker", "port", container, "8080/tcp").rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"

        def post(path, value):
            request = urllib.request.Request(
                base + path, data=json.dumps(value).encode(), headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()

        deadline = time.monotonic() + 60
        while True:
            try:
                post("/init", {"value": {"name": "arm-check", "main": "main", "binary": False, "code": code}})
                break
            except urllib.error.HTTPError:
                raise  # A reachable runtime rejecting /init is not a startup delay.
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        result = json.loads(post("/run", {"value": {"answer": 42}}))
        assert result["answer"] == 42, result
        assert result["arch"] in ({"arm64", "aarch64"} if arch == "arm64" else {"x64", "x86_64"}), result
        return result
    except Exception:
        print(run("docker", "logs", container))
        raise
    finally:
        run("docker", "rm", "-f", "--volumes", container)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=["amd64", "arm64"], required=True)
    parser.add_argument("--tag", help="Candidate tag prefix in ghcr.io/sregym/openwhisk")
    args = parser.parse_args()
    host = run("docker", "info", "--format", "{{.Architecture}}")
    assert host in ({"aarch64", "arm64"} if args.arch == "arm64" else {"x86_64", "amd64"}), (
        "Use a native host, not emulation"
    )
    releases = json.loads((Path(__file__).resolve().parents[1] / "images.lock.json").read_text())
    images = {
        component: f"ghcr.io/sregym/openwhisk:{args.tag}-{component}"
        if args.tag
        else releases[f"openwhisk-{component}"]
        for component in PROGRAMS
    }
    machine = "183" if args.arch == "arm64" else "62"
    elf_check = 'expected=$1; shift; for program do path=$(command -v "$program"); actual=$(od -An -tu2 -j18 -N2 "$path" | tr -d " "); test "$actual" = "$expected" || { echo "$program: ELF $actual"; exit 1; }; done'
    for component, programs in PROGRAMS.items():
        if component == "utility" and args.arch == "arm64":
            # Upstream AMD64 downloads this tool during catalog installation;
            # the ARM port supplies it because that installer fetches AMD64.
            programs = [*programs, "wskdeploy"]
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--platform",
            f"linux/{args.arch}",
            "--entrypoint",
            "/bin/sh",
            images[component],
            "-ec",
            elf_check,
            "sh",
            machine,
            *programs,
        )
        print("PASS native ELF", component, flush=True)
    run(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--platform",
        f"linux/{args.arch}",
        "--entrypoint",
        "/usr/local/api-gateway/luajit/bin/luajit",
        images["apigateway"],
        "-e",
        "package.cpath='/usr/local/api-gateway/lualib/?.so;'..package.cpath; "
        "local json=require('cjson'); assert(json.decode(json.encode({answer=42})).answer==42)",
    )
    print("PASS gateway native JSON module", flush=True)
    actions = {
        "nodejs14": "function main(args) { return {answer: args.answer, arch: process.arch}; }",
        "python37": "import platform\ndef main(args):\n    return dict(answer=args['answer'], arch=platform.machine())\n",
    }
    for component, code in actions.items():
        print("PASS action protocol", component, check_action(images[component], args.arch, code), flush=True)


if __name__ == "__main__":
    main()
