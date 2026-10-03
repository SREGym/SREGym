from sregym.generators.workload.recommendation_herd import extract_product_ids


def test_extract_product_ids_from_product_ids_key():
    assert extract_product_ids({"productIds": ["OLJCESPC7Z", "66VCHSJNUP"]}) == [
        "OLJCESPC7Z",
        "66VCHSJNUP",
    ]


def test_extract_product_ids_from_nested_products():
    payload = {"products": [{"id": "A"}, {"id": "B"}, {"name": "no-id"}]}
    assert extract_product_ids(payload) == ["A", "B"]


def test_extract_product_ids_from_list_payload():
    assert extract_product_ids(["x", {"id": "y"}]) == ["x", "y"]


def test_extract_product_ids_empty_on_unknown_shape():
    assert extract_product_ids({"ok": True}) == []
    assert extract_product_ids(None) == []
