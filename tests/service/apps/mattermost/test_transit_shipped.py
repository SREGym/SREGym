"""What every tier ships where the agent can read it: programs without the authors' words."""

import ast
import io
import json
import re
import tokenize

import pytest

from sregym.service.apps.mattermost.transit_cascade import shipped, transit_application

TIERS = ("1", "1b", "2")

#: Phrases that would explain the incident's mechanism rather than run it.
MECHANISM_WORDS = (
    "own metric",
    "capacity requested",
    "capacity available",
    "instance quota",
    "quota",
    "spare",
    "stuck",
    "jam",
    "leak",
    "cascade",
    "composed",
    "outage",
    "postmortem",
    "slack",
    "half-provisioned",
)


def python_programs(app):
    """Every Python program the agent can read: Deployment commands and in-house image files."""
    programs = {}
    for document in app.render():
        pod = document.get("spec", {}).get("template", {}).get("spec", {})
        for container in [*pod.get("containers", []), *pod.get("initContainers", [])]:
            for argument in [*container.get("command", []), *container.get("args", [])]:
                if "import " in argument:
                    programs[f"{document['metadata']['name']}/{container['name']}"] = argument
    for image in getattr(app, "in_house_images", list)():
        for name, content in image.files.items():
            if name.endswith(".py"):
                programs[f"{image.repository}/{name}"] = content
    return programs


def agent_readable_text(app):
    files = [c for image in getattr(app, "in_house_images", list)() for c in image.files.values()]
    return "\n".join([json.dumps(app.render()), *files]).lower()


@pytest.mark.parametrize("tier", TIERS)
def test_shipped_programs_hold_no_bare_strings_or_comments(tier):
    programs = python_programs(transit_application(tier))
    assert programs
    for name, source in programs.items():
        bare = [
            node.value.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ]
        assert not bare, (name, bare)
        comments = [
            t.string for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type == tokenize.COMMENT
        ]
        assert not comments, (name, comments)


@pytest.mark.parametrize("tier", TIERS)
def test_nothing_the_agent_reads_explains_the_mechanism(tier):
    text = agent_readable_text(transit_application(tier))
    for word in MECHANISM_WORDS:
        assert not re.search(rf"\b{re.escape(word)}", text), word


def test_shipped_drops_strings_standing_alone_anywhere():
    source = (
        '"""module"""\nimport os\n"""after an import"""\n'
        'def f():\n    """doc"""\n    x = 1\n    "note"\n    if x:\n        "inside"\n    return "kept"\n'
    )
    result = shipped(source)
    assert result.count("'") == 2 and "'kept'" in result
    namespace = {}
    exec(result, namespace)
    assert namespace["f"]() == "kept"
