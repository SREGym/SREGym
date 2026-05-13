#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# Python
import os
import simplejson as json

# Postgres
import psycopg2

# Thundering-herd-cascade variant of database.py injected by the
# thundering_herd_cascade_product_reviews problem. Each review request
# performs 10 fresh postgres connections + queries back-to-back, with no
# pooling, no cache, and no deduplication. Under concurrency the multiplier
# is enough to saturate the DB connection pool and take product-reviews
# down. The correct fix is a source-level edit: pool connections and
# coalesce concurrent callers into a single upstream fetch (single-flight).

def must_map_env(key: str):
    value = os.environ.get(key)
    if value is None:
        raise Exception(f'{key} environment variable must be set')
    return value

db_connection_str = must_map_env('DB_CONNECTION_STRING')

_FANOUT = 10  # Every request performs this many fresh connect+query cycles.

def _one_query(sql, params):
    connection = None
    try:
        with psycopg2.connect(db_connection_str) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall()
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

def fetch_product_reviews(product_id):
    try:
        return json.dumps(fetch_product_reviews_from_db(product_id), use_decimal=True)
    except Exception as e:
        return json.dumps({"error": str(e)})

def fetch_product_reviews_from_db(request_product_id):
    # Fan out the same query N times with no coalescing.
    records = None
    for _ in range(_FANOUT):
        records = _one_query(
            "SELECT username, description, score FROM reviews.productreviews WHERE product_id= %s",
            (request_product_id,),
        )
    return records

def fetch_avg_product_review_score_from_db(request_product_id):
    records = None
    for _ in range(_FANOUT):
        records = _one_query(
            "SELECT AVG(score) FROM reviews.productreviews WHERE product_id= %s",
            (request_product_id,),
        )
    if records:
        average_score = records[0][0]
    else:
        average_score = None
    return f"{average_score:.1f}"
