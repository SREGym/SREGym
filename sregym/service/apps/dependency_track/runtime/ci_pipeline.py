"""Acme build pipelines: CycloneDX SBOM generation and upload to Dependency-Track.

Each pipeline run builds the SBOM of one service, uploads it with the CI API key
(PUT /api/v1/bom), waits until Dependency-Track has processed it and prints the
policy gate result. Pushes to main come from the build schedule; tags pushed to
a repository (POST /hooks/git) run that service's release pipeline.
"""

import base64
import hashlib
import http.server
import json
import os
import queue
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DTRACK_URL = os.environ.get("DTRACK_URL", "http://dt-api-server:8080")
API_KEY_FILE = os.environ.get("DTRACK_API_KEY_FILE", "/etc/dtrack/api-key")
SCHEDULE_SECONDS = (
    int(os.environ.get("SCHEDULE_MIN_SECONDS", "40")),
    int(os.environ.get("SCHEDULE_MAX_SECONDS", "75")),
)
PORT = int(os.environ.get("PORT", "8080"))
#: "continuous": one scheduled main build every SCHEDULE_*_SECONDS; "train": every service once per TRAIN_INTERVAL_SECONDS.
SCHEDULE_MODE = os.environ.get("SCHEDULE_MODE", "continuous")
TRAIN_INTERVAL_SECONDS = int(os.environ.get("TRAIN_INTERVAL_SECONDS", "14400"))
#: Portfolio parent project new service versions are created under (empty: none).
PARENT_PROJECT = os.environ.get("PARENT_PROJECT", "")
#: Report Dependency-Track's analysis status of the upload with the policy gate (warn when it did not complete).
VERIFY_ANALYSIS = os.environ.get("VERIFY_ANALYSIS", "") == "true"

NPM = [
    "react",
    "react-dom",
    "react-router",
    "react-router-dom",
    "redux",
    "react-redux",
    "@reduxjs/toolkit",
    "lodash",
    "axios",
    "express",
    "body-parser",
    "cookie-parser",
    "cors",
    "helmet",
    "morgan",
    "debug",
    "ms",
    "chalk",
    "commander",
    "yargs",
    "dotenv",
    "uuid",
    "nanoid",
    "dayjs",
    "date-fns",
    "moment",
    "classnames",
    "clsx",
    "prop-types",
    "styled-components",
    "@emotion/react",
    "@emotion/styled",
    "graphql",
    "@apollo/client",
    "graphql-tag",
    "zod",
    "yup",
    "formik",
    "react-hook-form",
    "i18next",
    "react-i18next",
    "rxjs",
    "tslib",
    "core-js",
    "regenerator-runtime",
    "@babel/runtime",
    "@babel/core",
    "@babel/preset-env",
    "@babel/preset-react",
    "webpack",
    "webpack-cli",
    "babel-loader",
    "css-loader",
    "style-loader",
    "sass",
    "sass-loader",
    "postcss",
    "autoprefixer",
    "tailwindcss",
    "eslint",
    "prettier",
    "jest",
    "@testing-library/react",
    "@testing-library/jest-dom",
    "ts-jest",
    "typescript",
    "@types/node",
    "@types/react",
    "@types/react-dom",
    "@types/express",
    "@types/lodash",
    "semver",
    "minimist",
    "glob",
    "rimraf",
    "mkdirp",
    "fs-extra",
    "chokidar",
    "ws",
    "socket.io",
    "socket.io-client",
    "node-fetch",
    "qs",
    "querystring-es3",
    "buffer",
    "stream-browserify",
    "path-browserify",
    "jsonwebtoken",
    "bcryptjs",
    "passport",
    "passport-jwt",
    "pino",
    "pino-http",
    "winston",
    "http-proxy-middleware",
    "compression",
    "serve-static",
    "mime",
    "mime-types",
    "content-type",
    "raw-body",
    "iconv-lite",
    "safe-buffer",
    "inherits",
    "once",
    "wrappy",
    "picomatch",
    "micromatch",
    "braces",
    "fill-range",
    "to-regex-range",
    "is-number",
    "escape-string-regexp",
    "ansi-styles",
    "supports-color",
    "has-flag",
    "color-convert",
    "color-name",
    "string-width",
    "strip-ansi",
    "ansi-regex",
    "emoji-regex",
    "source-map",
    "source-map-support",
    "esbuild",
    "vite",
    "rollup",
    "@rollup/plugin-node-resolve",
    "terser",
    "browserslist",
    "caniuse-lite",
    "electron-to-chromium",
    "node-releases",
    "picocolors",
    "postcss-value-parser",
    "immer",
    "reselect",
    "swr",
    "@tanstack/react-query",
    "zustand",
    "framer-motion",
    "recharts",
    "d3",
    "d3-scale",
    "d3-shape",
    "chart.js",
    "react-chartjs-2",
    "marked",
    "dompurify",
    "highlight.js",
    "prismjs",
    "sharp",
    "jimp",
    "form-data",
    "combined-stream",
    "delayed-stream",
    "asynckit",
]
MAVEN = [
    "org.springframework.boot:spring-boot",
    "org.springframework.boot:spring-boot-autoconfigure",
    "org.springframework.boot:spring-boot-starter-web",
    "org.springframework.boot:spring-boot-starter-actuator",
    "org.springframework.boot:spring-boot-starter-data-jpa",
    "org.springframework:spring-core",
    "org.springframework:spring-context",
    "org.springframework:spring-beans",
    "org.springframework:spring-web",
    "org.springframework:spring-webmvc",
    "org.springframework:spring-jdbc",
    "org.springframework:spring-tx",
    "org.springframework:spring-aop",
    "org.springframework:spring-expression",
    "com.fasterxml.jackson.core:jackson-core",
    "com.fasterxml.jackson.core:jackson-databind",
    "com.fasterxml.jackson.core:jackson-annotations",
    "com.fasterxml.jackson.datatype:jackson-datatype-jsr310",
    "com.fasterxml.jackson.module:jackson-module-parameter-names",
    "org.apache.tomcat.embed:tomcat-embed-core",
    "org.apache.tomcat.embed:tomcat-embed-el",
    "org.apache.tomcat.embed:tomcat-embed-websocket",
    "org.hibernate.orm:hibernate-core",
    "org.hibernate.validator:hibernate-validator",
    "jakarta.persistence:jakarta.persistence-api",
    "jakarta.validation:jakarta.validation-api",
    "jakarta.annotation:jakarta.annotation-api",
    "jakarta.transaction:jakarta.transaction-api",
    "org.slf4j:slf4j-api",
    "ch.qos.logback:logback-classic",
    "ch.qos.logback:logback-core",
    "org.apache.logging.log4j:log4j-api",
    "org.apache.logging.log4j:log4j-to-slf4j",
    "org.yaml:snakeyaml",
    "com.zaxxer:HikariCP",
    "org.postgresql:postgresql",
    "org.flywaydb:flyway-core",
    "io.micrometer:micrometer-core",
    "io.micrometer:micrometer-registry-prometheus",
    "io.netty:netty-buffer",
    "io.netty:netty-codec",
    "io.netty:netty-codec-http",
    "io.netty:netty-common",
    "io.netty:netty-handler",
    "io.netty:netty-resolver",
    "io.netty:netty-transport",
    "com.google.guava:guava",
    "com.google.guava:failureaccess",
    "com.google.code.gson:gson",
    "com.google.protobuf:protobuf-java",
    "io.grpc:grpc-api",
    "io.grpc:grpc-core",
    "io.grpc:grpc-netty-shaded",
    "io.grpc:grpc-protobuf",
    "io.grpc:grpc-stub",
    "org.apache.commons:commons-lang3",
    "org.apache.commons:commons-text",
    "org.apache.commons:commons-collections4",
    "commons-io:commons-io",
    "commons-codec:commons-codec",
    "org.apache.httpcomponents.client5:httpclient5",
    "org.apache.httpcomponents.core5:httpcore5",
    "org.apache.kafka:kafka-clients",
    "org.lz4:lz4-java",
    "org.xerial.snappy:snappy-java",
    "com.github.luben:zstd-jni",
    "io.projectreactor:reactor-core",
    "org.reactivestreams:reactive-streams",
    "org.jetbrains.kotlin:kotlin-stdlib",
    "org.aspectj:aspectjweaver",
    "net.bytebuddy:byte-buddy",
    "org.objenesis:objenesis",
    "org.mockito:mockito-core",
    "org.junit.jupiter:junit-jupiter-api",
    "org.junit.jupiter:junit-jupiter-engine",
    "org.assertj:assertj-core",
    "org.testcontainers:testcontainers",
    "io.swagger.core.v3:swagger-annotations",
    "org.springdoc:springdoc-openapi-starter-webmvc-ui",
    "org.mapstruct:mapstruct",
    "org.projectlombok:lombok",
    "io.github.resilience4j:resilience4j-circuitbreaker",
    "io.opentelemetry:opentelemetry-api",
    "io.opentelemetry:opentelemetry-sdk",
    "com.squareup.okhttp3:okhttp",
    "com.squareup.okio:okio",
    "org.bouncycastle:bcprov-jdk18on",
    "com.nimbusds:nimbus-jose-jwt",
    "org.mariadb.jdbc:mariadb-java-client",
    "com.h2database:h2",
    "org.jooq:jooq",
    "com.github.ben-manes.caffeine:caffeine",
]
PYPI = [
    "requests",
    "urllib3",
    "idna",
    "certifi",
    "charset-normalizer",
    "flask",
    "werkzeug",
    "jinja2",
    "markupsafe",
    "itsdangerous",
    "click",
    "fastapi",
    "starlette",
    "pydantic",
    "pydantic-core",
    "uvicorn",
    "anyio",
    "sniffio",
    "h11",
    "httpx",
    "httpcore",
    "sqlalchemy",
    "psycopg2-binary",
    "alembic",
    "redis",
    "celery",
    "kombu",
    "billiard",
    "vine",
    "amqp",
    "boto3",
    "botocore",
    "s3transfer",
    "jmespath",
    "python-dateutil",
    "six",
    "pytz",
    "tzdata",
    "pyyaml",
    "orjson",
    "ujson",
    "structlog",
    "sentry-sdk",
    "prometheus-client",
    "opentelemetry-api",
    "protobuf",
    "grpcio",
    "cryptography",
    "cffi",
    "pycparser",
    "pyjwt",
    "bcrypt",
    "passlib",
    "tenacity",
    "backoff",
    "attrs",
    "cattrs",
    "typing-extensions",
    "packaging",
    "setuptools",
    "wheel",
    "pip",
    "pytest",
    "pluggy",
    "iniconfig",
    "coverage",
    "mypy",
    "black",
    "ruff",
]
GOLANG = [
    "github.com/gin-gonic/gin",
    "github.com/go-chi/chi/v5",
    "github.com/gorilla/mux",
    "github.com/spf13/cobra",
    "github.com/spf13/viper",
    "github.com/spf13/pflag",
    "github.com/sirupsen/logrus",
    "go.uber.org/zap",
    "go.uber.org/atomic",
    "go.uber.org/multierr",
    "github.com/prometheus/client_golang",
    "github.com/prometheus/common",
    "github.com/prometheus/procfs",
    "github.com/golang/protobuf",
    "google.golang.org/protobuf",
    "google.golang.org/grpc",
    "google.golang.org/genproto",
    "golang.org/x/net",
    "golang.org/x/sys",
    "golang.org/x/text",
    "golang.org/x/crypto",
    "golang.org/x/oauth2",
    "golang.org/x/sync",
    "golang.org/x/time",
    "github.com/google/uuid",
    "github.com/stretchr/testify",
    "github.com/davecgh/go-spew",
    "github.com/pmezard/go-difflib",
    "gopkg.in/yaml.v3",
    "github.com/jackc/pgx/v5",
    "github.com/redis/go-redis/v9",
    "github.com/golang-jwt/jwt/v5",
    "github.com/coreos/go-oidc/v3",
    "go.opentelemetry.io/otel",
    "go.opentelemetry.io/otel/trace",
    "github.com/cespare/xxhash/v2",
    "github.com/beorn7/perks",
    "github.com/klauspost/compress",
    "github.com/json-iterator/go",
    "github.com/modern-go/reflect2",
]
ECOSYSTEMS = {"npm": NPM, "maven": MAVEN, "pypi": PYPI, "golang": GOLANG}
PERMISSIVE = ["MIT", "MIT", "MIT", "Apache-2.0", "Apache-2.0", "BSD-3-Clause", "ISC", "BSD-2-Clause"]
COPYLEFT = ["GPL-3.0-only", "AGPL-3.0-only", "GPL-2.0-only"]
WEAK_COPYLEFT = ["LGPL-2.1-only", "MPL-2.0", "EPL-2.0"]
VARIANTS = ["plugin", "utils", "compat", "extra", "lite"]
UNDER_REVIEW = {"moment", "request", "com.h2database:h2"}

#: name -> (repository, ecosystem, third-party components, internal packages, team, version, versioning)
SERVICES = {
    "checkout-web": ("acme/checkout-web", "npm", 210, 9, "team-checkout", "5.12.0", "continuous"),
    "payments-api": ("acme/payments-api", "maven", 150, 7, "team-payments", "2.14.0", "continuous"),
    "inventory-service": ("acme/inventory-service", "maven", 120, 5, "team-supply", "1.33.0", "continuous"),
    "notification-worker": ("acme/notification-worker", "pypi", 80, 4, "team-messaging", "0.19.0", "continuous"),
    "search-indexer": ("acme/search-indexer", "maven", 135, 6, "team-search", "3.2.0", "continuous"),
    "identity-gateway": ("acme/identity-gateway", "golang", 60, 5, "team-identity", "1.8.0", "continuous"),
    "mobile-bff": ("acme/mobile-bff", "npm", 230, 8, "team-mobile", "4.4.0", "continuous"),
    "storefront": ("acme/storefront", "npm", 240, 14, "team-storefront", "3.9.4", "tags"),
}
#: Workspace packages of the storefront monorepo from 4.0 on (the former design-system repositories).
STOREFRONT_WORKSPACES = ("ui", "icons", "tokens", "forms", "charts", "layout", "a11y", "motion", "theme", "data")
INTERNAL_WORDS = (
    "core",
    "auth",
    "config",
    "client",
    "events",
    "metrics",
    "logging",
    "errors",
    "testing",
    "money",
    "catalog",
    "cart",
    "pricing",
    "flags",
    "tracing",
    "storage",
    "cache",
    "schema",
)


def log(level, msg, **fields):
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "msg": msg, **fields}
    print(json.dumps(record), flush=True)


def rng_for(*parts):
    return random.Random(int(hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def purl_for(ecosystem, name, version):
    if ecosystem == "npm":
        return f"pkg:npm/{urllib.parse.quote(name, safe='/')}@{version}"
    if ecosystem == "maven":
        group, artifact = name.split(":")
        return f"pkg:maven/{group}/{artifact}@{version}"
    if ecosystem == "pypi":
        return f"pkg:pypi/{name}@{version}"
    return f"pkg:golang/{name}@v{version}"


def component(ecosystem, name, version, license_id):
    entry = {"type": "library", "version": version, "purl": purl_for(ecosystem, name, version)}
    if ecosystem == "maven":
        entry["group"], entry["name"] = name.split(":")
    elif ecosystem == "npm" and name.startswith("@"):
        entry["group"], entry["name"] = name.split("/", 1)
    else:
        entry["name"] = name
    entry["bom-ref"] = entry["purl"]
    if license_id:
        entry["licenses"] = [{"license": {"id": license_id}}]
    return entry


def third_party_name(pool, index):
    name = pool[index % len(pool)]
    if index < len(pool):
        return name
    variant = VARIANTS[(index // len(pool) - 1) % len(VARIANTS)]
    if ":" in name:
        group, artifact = name.split(":")
        return f"{group}:{artifact}-{variant}"
    return f"{name}-{variant}"


def internal_name(ecosystem, team, index):
    area = team.removeprefix("team-")
    word = INTERNAL_WORDS[index % len(INTERNAL_WORDS)] + ("" if index < len(INTERNAL_WORDS) else f"-{index // 18}")
    if ecosystem == "npm":
        return f"@acme/{area}-{word}"
    if ecosystem == "maven":
        return f"com.acme.{area}:{word}"
    if ecosystem == "pypi":
        return f"acme-{area}-{word}"
    return f"git.acme.internal/{area}/{word}"


def build_sbom(service, version, build):
    """The SBOM of one build: the dependency set of the release line plus small drift between builds."""
    repo, ecosystem, third_party, internal, team = SERVICES[service][:5]
    major, minor = (int(part) for part in version.split(".")[:2])
    line = rng_for(service, major, minor)
    pool = ECOSYSTEMS[ecosystem]
    components = []
    for index in range(third_party):
        name = third_party_name(pool, index)
        roll = line.random()
        if roll < 0.006:
            license_id = line.choice(COPYLEFT)
        elif roll < 0.04:
            license_id = line.choice(WEAK_COPYLEFT)
        else:
            license_id = line.choice(PERMISSIVE)
        # The update bot only bumps permissive dependencies that are not under review.
        floating = license_id in PERMISSIVE and name not in UNDER_REVIEW
        drifted = floating and rng_for(service, version, build, index).random() < 0.02
        pinned_rng = rng_for(service, major, minor, index, build if drifted else 0)
        pinned = f"{pinned_rng.choice([0, 1, 1, 2, 2, 3, 4, 5, 6, 7])}.{pinned_rng.randint(0, 30)}.{pinned_rng.randint(0, 20)}"
        components.append(component(ecosystem, name, pinned, license_id))
    # Every release line after the one the service started from adds two new internal modules.
    base_major, base_minor = (int(part) for part in SERVICES[service][5].split(".")[:2])
    if SERVICES[service][6] == "continuous" and (major, minor) > (base_major, base_minor):
        internal += 2 * (minor - base_minor if major == base_major else minor + 1)
    for index in range(internal):
        components.append(
            component(ecosystem, internal_name(ecosystem, team, index), f"{major}.{minor}.{build % 7}", None)
        )
    if service == "storefront" and major >= 4:
        for workspace in STOREFRONT_WORKSPACES:
            for index in range(120):
                components.append(component("npm", f"@acme/{workspace}-{index:03d}", f"{major}.{minor}.0", None))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.UUID(int=rng_for(service, version, build, 'serial').getrandbits(128))}",
        "version": 1,
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tools": {"components": [{"type": "application", "name": "cdxgen", "version": "11.4.4"}]},
            "component": {
                "type": "application",
                "name": service,
                "version": version,
                "bom-ref": f"{service}@{version}",
            },
            "properties": [{"name": "acme:repository", "value": repo}, {"name": "acme:build", "value": str(build)}],
        },
        "components": components,
    }


class DependencyTrack:
    def __init__(self, url=DTRACK_URL):
        self.url = url.rstrip("/")

    @staticmethod
    def api_key():
        try:
            with open(API_KEY_FILE) as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def request(self, method, path, body=None, timeout=60):
        headers = {"X-Api-Key": self.api_key(), "Accept": "application/json", "User-Agent": "acme-ci/2.3"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode()
            return response.status, response.headers, (json.loads(text) if text else None)

    def upload(self, service, version, team, sbom):
        payload = {
            "projectName": service,
            "projectVersion": version,
            "autoCreate": True,
            "isLatest": True,
            "projectTags": [{"name": team}],
            "bom": base64.b64encode(json.dumps(sbom).encode()).decode(),
        }
        if PARENT_PROJECT:
            payload["parentName"] = PARENT_PROJECT
        return self.request("PUT", "/api/v1/bom", payload, timeout=120)[2]

    def processing(self, token):
        return self.token(token).get("processing", False)

    def token(self, token):
        return self.request("GET", f"/api/v1/event/token/{token}")[2] or {}

    def violations(self, project_uuid):
        found, page = [], 1
        while True:
            _, headers, items = self.request(
                "GET", f"/api/v1/violation/project/{project_uuid}?pageSize=500&pageNumber={page}"
            )
            found.extend(items or [])
            if len(found) >= int(headers.get("X-Total-Count", "0")) or not items:
                return found
            page += 1


class Pipelines:
    def __init__(self):
        self.dt = DependencyTrack()
        self.queue = queue.Queue()
        self.versions = {name: spec[5] for name, spec in SERVICES.items()}
        self.random = random.Random()
        self.uploaded = set()
        self.builds = {name: self.random.randint(100, 400) for name in SERVICES}
        self.run_number = int(time.time()) % 100000
        self.running = None

    def resume_versions(self):
        """Continue from the latest version each service has in Dependency-Track."""
        for name in SERVICES:
            try:
                latest = self.dt.request("GET", f"/api/v1/project/latest/{urllib.parse.quote(name)}")[2]
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if latest and latest.get("version"):
                self.versions[name] = latest["version"]

    def next_scheduled(self, service=None):
        names = sorted(SERVICES)
        weights = [0.5 if SERVICES[name][6] == "tags" else 1.0 for name in names]
        service = service or self.random.choices(names, weights)[0]
        uploaded = service in self.uploaded
        if SERVICES[service][6] == "continuous" and uploaded and self.random.random() < 0.35:
            major, minor, patch = (int(part) for part in self.versions[service].split("."))
            self.versions[service] = f"{major}.{minor}.{patch + 1}"
        return {"service": service, "trigger": "push", "ref": "refs/heads/main", "version": self.versions[service]}

    def run(self, job):
        service, version = job["service"], job["version"]
        team = SERVICES[service][4]
        self.builds[service] += 1
        self.run_number += 1
        run = {
            "pipeline": service,
            "run": self.run_number,
            "trigger": job["trigger"],
            "ref": job["ref"],
            "version": version,
        }
        sbom = build_sbom(service, version, self.builds[service])
        if SCHEDULE_MODE == "train":
            # Compile, test and package before the SBOM step (scheduled and release builds of the build farm).
            seconds = min(150, 40 + len(sbom["components"]) // 8) * self.random.uniform(0.8, 1.2)
            time.sleep(seconds)
            log("info", "build finished", **run, seconds=round(seconds))
        log("info", "sbom generated", **run, components=len(sbom["components"]))
        started = time.time()
        try:
            uploaded = self.dt.upload(service, version, team, sbom)
        except (urllib.error.URLError, OSError, ValueError) as error:
            log("error", "sbom upload failed", **run, error=str(error)[:300])
            return
        log("info", "sbom uploaded", **run, token=uploaded.get("token"), project=uploaded.get("projectUuid"))
        self.uploaded.add(service)
        deadline = time.time() + 900
        analysis = {}
        while time.time() < deadline:
            time.sleep(3)
            try:
                analysis = self.dt.token(uploaded["token"])
                if not analysis.get("processing", False):
                    break
            except (urllib.error.URLError, OSError, ValueError):
                continue
        else:
            log("warning", "sbom still processing, policy gate skipped", **run, waited_s=900)
            return
        if VERIFY_ANALYSIS:
            run["analysis"] = analysis.get("status")
            if analysis.get("status") != "COMPLETED":
                log("warning", "dependency-track analysis did not complete", **run, token=uploaded.get("token"))
        try:
            violations = self.dt.violations(uploaded["projectUuid"])
        except (urllib.error.URLError, OSError, ValueError) as error:
            log("error", "policy gate unavailable", **run, error=str(error)[:300])
            return
        states = {}
        for violation in violations:
            state = violation["policyCondition"]["policy"]["violationState"]
            states[state] = states.get(state, 0) + 1
        result = "failed" if states.get("FAIL") else "passed"
        log(
            "info", "policy gate", **run, result=result, violations=states, processing_s=round(time.time() - started, 1)
        )

    def worker(self):
        if not self.dt.api_key():
            log("warning", "no Dependency-Track API key mounted yet, waiting", path=API_KEY_FILE)
        while not self.dt.api_key():
            time.sleep(10)
        self.resume_versions()
        next_train = time.time()
        while True:
            if SCHEDULE_MODE == "train":
                if time.time() >= next_train:
                    log("info", "scheduled builds started", services=len(SERVICES))
                    for service in sorted(SERVICES):
                        self.execute(self.next_scheduled(service))
                    next_train = time.time() + TRAIN_INTERVAL_SECONDS
                    log("info", "scheduled builds finished", next_in_s=TRAIN_INTERVAL_SECONDS)
                try:
                    job = self.queue.get(timeout=max(1, min(60, next_train - time.time())))
                except queue.Empty:
                    continue
            else:
                try:
                    job = self.queue.get(timeout=self.random.uniform(*SCHEDULE_SECONDS))
                except queue.Empty:
                    job = self.next_scheduled()
            self.execute(job)

    def execute(self, job):
        self.running = job
        try:
            self.run(job)
        except Exception as error:  # a broken run must not stop the runner
            log("error", "pipeline run crashed", pipeline=job.get("service"), error=repr(error)[:300])
        finally:
            self.running = None

    def on_tag(self, repository, ref):
        for name, spec in SERVICES.items():
            if spec[0] == repository and ref.startswith("refs/tags/v"):
                version = ref.removeprefix("refs/tags/v")
                self.versions[name] = version
                self.queue.put({"service": name, "trigger": "tag", "ref": ref, "version": version})
                log("info", "release pipeline queued", pipeline=name, ref=ref)
                return True
        return False


def serve(pipelines):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self.respond(200, {"status": "ok"})
            elif self.path == "/pipelines":
                self.respond(
                    200,
                    {
                        "versions": pipelines.versions,
                        "builds": pipelines.builds,
                        "queued": pipelines.queue.qsize(),
                        "running": pipelines.running,
                    },
                )
            else:
                self.respond(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/hooks/git":
                self.respond(404, {"error": "not found"})
                return
            try:
                event = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            except ValueError:
                self.respond(400, {"error": "invalid json"})
                return
            accepted = pipelines.on_tag(event.get("repository", ""), event.get("ref", ""))
            self.respond(202 if accepted else 200, {"accepted": accepted})

        def respond(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    runner = Pipelines()
    threading.Thread(target=serve, args=(runner,), daemon=True).start()
    log("info", "pipeline runner started", services=sorted(SERVICES), dtrack=DTRACK_URL)
    runner.worker()
