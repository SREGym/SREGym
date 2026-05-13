#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# Python
import os
import simplejson as json

# Postgres
import psycopg2

# Missing-stale-cache-fallback variant of database.py injected by the
# missing_stale_cache_fallback_product_reviews problem. Every DB query is
# pinned to a 1 ms connection/statement timeout and has no error handling or
# fallback — the exception propagates directly to the gRPC handler, turning
# every review lookup into an error response. The correct fix is a source-
# code change: restore reasonable timeouts and wrap queries in a try/except
# that returns an empty or cached list on failure.

def must_map_env(key: str):
    value = os.environ.get(key)
    if value is None:
        raise Exception(f'{key} environment variable must be set')
    return value

# Pin connection timeout to 1 ms — well below the pod-to-pod RTT so every
# connect() raises psycopg2.OperationalError.
db_connection_str = must_map_env('DB_CONNECTION_STRING') + " connect_timeout=1 options='-c statement_timeout=1'"

def fetch_product_reviews(product_id):
    # NOTE: no try/except; callers see the raw exception.
    return json.dumps(fetch_product_reviews_from_db(product_id), use_decimal=True)

def fetch_product_reviews_from_db(request_product_id):
    with psycopg2.connect(db_connection_str) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT username, description, score FROM reviews.productreviews WHERE product_id= %s",
                (request_product_id,),
            )
            return cursor.fetchall()

def fetch_avg_product_review_score_from_db(request_product_id):
    with psycopg2.connect(db_connection_str) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT AVG(score) FROM reviews.productreviews WHERE product_id= %s",
                (request_product_id,),
            )
            records = cursor.fetchall()
            if records:
                average_score = records[0][0]
            else:
                average_score = None
            return f"{average_score:.1f}"
