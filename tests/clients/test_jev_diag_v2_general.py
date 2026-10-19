"""v2's knowledge stays general: no code string names a benchmark application, one of its services, or a fault's
own objects. Kubernetes kinds, symptom classes and technologies (a database, a cache, a broker) are allowed."""

import ast
import pathlib
import re

V2 = pathlib.Path(__file__).resolve().parents[2] / "clients" / "jev_diag" / "v2"
APP_NAMES = (
    "hotel", "reservation", "social-network", "socialnetwork", "astronomy", "otel-demo", "product-catalog",
    "compose-post", "home-timeline", "social-graph", "user-timeline", "fraud-detection", "valkey-cart", "mongodb-geo",
    "mongodb-rate", "frontend-proxy", "flagd", "recommendation", "media-frontend", "url-shorten", "text-service",
    "orders-validator", "orders-fulfillment", "audit-log", "cleanup-controller", "fleetcast", "fleet-cast",
    "trainticket", "train-ticket", "blueprint",
)  # fmt: skip


def _code_strings(tree: ast.AST):
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node


def test_no_v2_code_string_names_an_application_or_its_services():
    found = []
    for path in sorted(V2.glob("*.py")):
        for node in _code_strings(ast.parse(path.read_text())):
            for name in APP_NAMES:
                if re.search(rf"(?i)(?<![a-z]){re.escape(name)}(?![a-z])", node.value):
                    found.append(f"{path.name}:{node.lineno} {name}")
    assert not found, found
