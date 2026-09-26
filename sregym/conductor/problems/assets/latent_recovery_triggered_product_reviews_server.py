#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# Python

# Local
import logging
import os
import time
from concurrent import futures
from contextlib import suppress

import demo_pb2
import demo_pb2_grpc

# Pip
import grpc
from database import fetch_avg_product_review_score_from_db, fetch_product_reviews_from_db
from grpc_health.v1 import health_pb2, health_pb2_grpc
from metrics import init_metrics
from openfeature import api
from openfeature.contrib.provider.flagd import FlagdProvider
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
    OTLPLogExporter,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource


class ProductReviewService(demo_pb2_grpc.ProductReviewServiceServicer):
    def GetProductReviews(self, request, context):
        logger.info(f"Receive GetProductReviews for product id:{request.product_id}")
        product_reviews = demo_pb2.GetProductReviewsResponse()
        records = fetch_product_reviews_from_db(request.product_id)
        for row in records:
            product_reviews.product_reviews.add(username=row[0], description=row[1], score=str(row[2]))
        return product_reviews

    def GetAverageProductReviewScore(self, request, context):
        resp = demo_pb2.GetAverageProductReviewScoreResponse()
        resp.average_score = fetch_avg_product_review_score_from_db(request.product_id)
        return resp

    def AskProductAIAssistant(self, request, context):
        resp = demo_pb2.AskProductAIAssistantResponse()
        resp.response = "OK"
        return resp

    def Check(self, request, context):
        return health_pb2.HealthCheckResponse(status=health_pb2.HealthCheckResponse.SERVING)

    def Watch(self, request, context):
        return health_pb2.HealthCheckResponse(status=health_pb2.HealthCheckResponse.UNIMPLEMENTED)


def must_map_env(key: str):
    value = os.environ.get(key)
    if value is None:
        raise Exception(f"{key} environment variable must be set")
    return value


if __name__ == "__main__":
    service_name = must_map_env("OTEL_SERVICE_NAME")
    api.set_provider(FlagdProvider(host=os.environ.get("FLAGD_HOST", "flagd"), port=os.environ.get("FLAGD_PORT", 8013)))

    tracer = trace.get_tracer_provider().get_tracer(service_name)
    meter = metrics.get_meter_provider().get_meter(service_name)
    init_metrics(meter)

    logger_provider = LoggerProvider(resource=Resource.create({"service.name": service_name}))
    set_logger_provider(logger_provider)
    log_exporter = OTLPLogExporter(insecure=True)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    logger = logging.getLogger("main")
    logger.addHandler(handler)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    service = ProductReviewService()
    demo_pb2_grpc.add_ProductReviewServiceServicer_to_server(service, server)
    health_pb2_grpc.add_HealthServicer_to_server(service, server)

    catalog_addr = must_map_env("PRODUCT_CATALOG_ADDR")
    pc_channel = grpc.insecure_channel(catalog_addr)
    product_catalog_stub = demo_pb2_grpc.ProductCatalogServiceStub(pc_channel)

    for _warm_i in range(50):
        with suppress(Exception):
            product_catalog_stub.ListProducts(demo_pb2.Empty(), timeout=5.0)
        time.sleep(0.8)

    port = must_map_env("PRODUCT_REVIEWS_PORT")
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(f"Product reviews service started, listening on port {port}")
    server.wait_for_termination()
