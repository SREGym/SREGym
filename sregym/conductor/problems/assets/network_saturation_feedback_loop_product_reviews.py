#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# Python
import os
import simplejson as json

# Postgres
import psycopg2

# Network-saturation-feedback-loop variant of database.py injected by the
# network_saturation_feedback_loop_product_reviews problem. Every DB query is
# wrapped in an unbounded `while True: try/except: continue` loop with a 50
# ms statement timeout — under any backend slowness this spins at thousands
# of connect attempts per second per caller, saturating postgres and the
# pod-local network. The fix is a source-level edit: replace the unbounded
# loop with a bounded retry budget, exponential backoff with jitter, and a
# circuit breaker.

def must_map_env(key: str):
    value = os.environ.get(key)
    if value is None:
        raise Exception(f'{key} environment variable must be set')
    return value

db_connection_str = must_map_env('DB_CONNECTION_STRING') + " connect_timeout=1 options='-c statement_timeout=50'"

def _run_query(sql, params):
    # Unbounded retry loop — the bug shape.
    while True:
        try:
            with psycopg2.connect(db_connection_str) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    return cursor.fetchall()
        except Exception:
            # No backoff, no cap, no jitter.
            continue

def fetch_product_reviews(product_id):
    try:
        return json.dumps(fetch_product_reviews_from_db(product_id), use_decimal=True)
    except Exception as e:
        return json.dumps({"error": str(e)})

def fetch_product_reviews_from_db(request_product_id):
    return _run_query(
        "SELECT username, description, score FROM reviews.productreviews WHERE product_id= %s",
        (request_product_id,),
    )

def fetch_avg_product_review_score_from_db(request_product_id):
    records = _run_query(
        "SELECT AVG(score) FROM reviews.productreviews WHERE product_id= %s",
        (request_product_id,),
    )
    if records:
        average_score = records[0][0]
    else:
        average_score = None
    return f"{average_score:.1f}"
