"""Direct public RPC probes independent of frontend caches and service images."""

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from sregym.generators.workload.hotel_search import KubectlPortForward


def _message_types():
    # Only the public ID fields are needed. Protobuf ignores the remaining
    # product metadata, so these probes do not depend on generated image files.
    field = descriptor_pb2.FieldDescriptorProto
    schema = descriptor_pb2.FileDescriptorProto(name="sregym_rpc_probe.proto", package="probe", syntax="proto3")
    product = schema.message_type.add(name="Product")
    product.field.add(name="id", number=1, type=field.TYPE_STRING)
    catalog = schema.message_type.add(name="Catalog")
    catalog.field.add(
        name="products", number=1, type=field.TYPE_MESSAGE, type_name=".probe.Product", label=field.LABEL_REPEATED
    )
    request = schema.message_type.add(name="RecommendationsRequest")
    request.field.add(name="product_ids", number=2, type=field.TYPE_STRING, label=field.LABEL_REPEATED)
    response = schema.message_type.add(name="RecommendationsResponse")
    response.field.add(name="product_ids", number=1, type=field.TYPE_STRING, label=field.LABEL_REPEATED)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return {
        name: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"probe.{name}"))
        for name in ("Catalog", "RecommendationsRequest", "RecommendationsResponse")
    }


def _call(problem, service: str, method: str, request: bytes, response_type):
    target = problem.kubectl.core_v1_api.read_namespaced_service(
        name=service, namespace=problem.namespace, _request_timeout=10
    )
    ports = target.spec.ports
    port = next((item.port for item in ports if item.name == "grpc"), None)
    if port is None:
        if len(ports) != 1:
            raise RuntimeError(f"cannot identify the gRPC port for service {service}")
        port = ports[0].port
    tunnel = KubectlPortForward(problem.namespace, service, port)
    try:
        local_port = tunnel.start(timeout=20)
        with grpc.insecure_channel(f"127.0.0.1:{local_port}") as channel:
            call = channel.unary_unary(method, response_deserializer=response_type.FromString)
            return call(request, timeout=15)
    finally:
        tunnel.stop()


def catalog_product_ids(problem) -> set[str]:
    types = _message_types()
    response = _call(problem, "product-catalog", "/oteldemo.ProductCatalogService/ListProducts", b"", types["Catalog"])
    return {product.id for product in response.products if product.id}


def recommendation_product_ids(problem, exclusions: tuple[str, ...]) -> tuple[str, ...]:
    types = _message_types()
    request = types["RecommendationsRequest"](product_ids=exclusions).SerializeToString()
    response = _call(
        problem,
        "recommendation",
        "/oteldemo.RecommendationService/ListRecommendations",
        request,
        types["RecommendationsResponse"],
    )
    return tuple(response.product_ids)
