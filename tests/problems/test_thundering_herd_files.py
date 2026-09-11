from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ASSET = _REPO / "sregym" / "conductor" / "problems" / "assets" / "thundering_herd_cascade_recommendation.py"
_PROBLEM = _REPO / "sregym" / "conductor" / "problems" / "thundering_herd_cascade.py"


def test_asset_fans_out_ten_list_products_calls():
    source = _ASSET.read_text(encoding="utf-8")
    assert "for _ in range(10):" in source
    assert "product_catalog_stub.ListProducts" in source
    assert "ListRecommendations" in source


def test_problem_file_is_x86_only_and_hides_eval_constants():
    source = _PROBLEM.read_text(encoding="utf-8")
    assert "x86-64" in source
    assert "run_default_workload = False" in source
    assert "24" not in source
    assert "66VCHSJNUP" not in source
    assert "ListProducts" in source
    assert "retry storm" in source
