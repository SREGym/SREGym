import ast
import random
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_REPO = Path(__file__).resolve().parents[2]
_ASSET = _REPO / "sregym" / "conductor" / "problems" / "assets" / "thundering_herd_cascade_recommendation.py"
_PROBLEM = _REPO / "sregym" / "conductor" / "problems" / "thundering_herd_cascade.py"


def test_asset_fans_out_ten_list_products_calls():
    source = _ASSET.read_text(encoding="utf-8")
    assert "for _ in range(10):" in source
    assert "product_catalog_stub.ListProducts" in source
    assert "recommendation catalog refetch" in source
    assert "ListRecommendations" in source
    assert "GetProduct" not in source
    assert "check_feature_flag" not in source


def test_problem_file_is_x86_only_and_hides_eval_constants():
    source = _PROBLEM.read_text(encoding="utf-8")
    assert "x86-64" in source
    assert "run_default_workload = False" in source
    assert "24" not in source
    assert "66VCHSJNUP" not in source
    assert "ListProducts" in source
    assert "retry storm" in source


@pytest.mark.parametrize("exclusions", [("a", "b"), ("a,b",), ("a,b", "c")])
def test_overlay_honors_repeated_and_comma_separated_exclusions(exclusions):
    # Execute the deployed function without importing the service's generated
    # protobuf files or starting gRPC/OTel exporters in the unit-test process.
    tree = ast.parse(_ASSET.read_text(encoding="utf-8-sig"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "get_product_list")
    catalog_ids = {"a", "b", "c", "d", "e", "f", "g"}
    catalog = Mock()
    catalog.ListProducts.return_value = SimpleNamespace(
        products=[SimpleNamespace(id=product_id) for product_id in sorted(catalog_ids)]
    )
    tracer = Mock()
    tracer.start_as_current_span.side_effect = lambda _: nullcontext(Mock())
    namespace = {
        "tracer": tracer,
        "random": random.Random(0),
        "product_catalog_stub": catalog,
        "demo_pb2": SimpleNamespace(Empty=Mock()),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(_ASSET), "exec"), namespace)

    result = namespace["get_product_list"](exclusions)

    excluded_ids = {product_id for value in exclusions for product_id in value.split(",")}
    assert set(result) == catalog_ids - excluded_ids
    assert catalog.ListProducts.call_count == 10
