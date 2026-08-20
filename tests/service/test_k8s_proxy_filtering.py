from sregym.service.k8s_proxy import (
    HIDDEN_LABELS,
    HIDDEN_NAMESPACES,
    filter_resource_list,
    has_hidden_label,
    should_filter_response,
)

HELM_SECRET = {
    "metadata": {
        "name": "sh.helm.release.v1.astronomy-shop.v1",
        "namespace": "astronomy-shop",
        "labels": {"owner": "helm", "name": "astronomy-shop", "status": "deployed"},
    },
    "type": "helm.sh/release.v1",
}

APP_SECRET = {
    "metadata": {
        "name": "product-catalog-db-conn",
        "namespace": "astronomy-shop",
        "labels": {},
    },
    "type": "Opaque",
}

LOAD_GENERATOR_POD = {
    "metadata": {
        "name": "load-generator-abc",
        "namespace": "astronomy-shop",
        "labels": {"app": "load-generator"},
    }
}

APP_POD = {
    "metadata": {
        "name": "frontend-abc",
        "namespace": "astronomy-shop",
        "labels": {"app": "frontend"},
    }
}


def test_helm_release_secret_carries_hidden_label():
    assert has_hidden_label(HELM_SECRET["metadata"], HIDDEN_LABELS) is True
    assert has_hidden_label(APP_SECRET["metadata"], HIDDEN_LABELS) is False


def test_namespaced_secret_list_is_filtered():
    # kubectl get secrets -n astronomy-shop
    assert should_filter_response("/api/v1/namespaces/astronomy-shop/secrets") == "resources"

    data = {"items": [HELM_SECRET, APP_SECRET]}
    filtered = filter_resource_list(data, HIDDEN_NAMESPACES, HIDDEN_LABELS)

    assert [item["metadata"]["name"] for item in filtered["items"]] == ["product-catalog-db-conn"]


def test_cluster_wide_secret_list_is_filtered():
    # kubectl get secrets --all-namespaces
    assert should_filter_response("/api/v1/secrets") == "resources"

    data = {"items": [HELM_SECRET, APP_SECRET]}
    filtered = filter_resource_list(data, HIDDEN_NAMESPACES, HIDDEN_LABELS)

    assert [item["metadata"]["name"] for item in filtered["items"]] == ["product-catalog-db-conn"]


def test_single_secret_get_is_left_to_direct_access_check():
    # A trailing name segment means a single resource: the proxy handles these
    # via the direct-access 403 path, driven by has_hidden_label on the object.
    path = "/api/v1/namespaces/astronomy-shop/secrets/sh.helm.release.v1.astronomy-shop.v1"
    assert should_filter_response(path) is None
    assert has_hidden_label(HELM_SECRET["metadata"], HIDDEN_LABELS) is True


def test_namespaced_pod_list_filters_hidden_labels():
    assert should_filter_response("/api/v1/namespaces/astronomy-shop/pods") == "resources"

    data = {"items": [LOAD_GENERATOR_POD, APP_POD]}
    filtered = filter_resource_list(data, HIDDEN_NAMESPACES, HIDDEN_LABELS)

    assert [item["metadata"]["name"] for item in filtered["items"]] == ["frontend-abc"]


def test_table_format_rows_are_filtered():
    data = {
        "rows": [
            {"object": HELM_SECRET, "cells": ["sh.helm.release.v1.astronomy-shop.v1"]},
            {"object": APP_SECRET, "cells": ["product-catalog-db-conn"]},
        ]
    }
    filtered = filter_resource_list(data, HIDDEN_NAMESPACES, HIDDEN_LABELS)

    assert [row["object"]["metadata"]["name"] for row in filtered["rows"]] == ["product-catalog-db-conn"]


def test_hidden_namespace_items_are_filtered_from_lists():
    chaos_pod = {"metadata": {"name": "chaos-daemon-abc", "namespace": "chaos-mesh", "labels": {}}}
    data = {"items": [chaos_pod, APP_POD]}
    filtered = filter_resource_list(data, HIDDEN_NAMESPACES, HIDDEN_LABELS)

    assert [item["metadata"]["name"] for item in filtered["items"]] == ["frontend-abc"]


def test_subresource_and_query_paths():
    assert should_filter_response("/api/v1/namespaces/astronomy-shop/pods/frontend-abc/log") is None
    assert should_filter_response("/api/v1/namespaces/astronomy-shop/secrets?limit=500") == "resources"
    assert should_filter_response("/api/v1/namespaces") == "namespaces"
    assert should_filter_response("/api/v1/namespaces?limit=500") == "namespaces"
    assert should_filter_response("/apis/apps/v1/namespaces/astronomy-shop/deployments") == "resources"
    assert should_filter_response("/api/v1/namespaces/astronomy-shop") is None
