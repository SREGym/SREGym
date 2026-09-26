#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# Python
import os
import simplejson as json

# Postgres
import psycopg2

def must_map_env(key: str):
    value = os.environ.get(key)
    if value is None:
        raise Exception(f'{key} environment variable must be set')
    return value

db_connection_str = must_map_env('DB_CONNECTION_STRING') + " connect_timeout=1 options='-c statement_timeout=50'"

def _run_query(sql, params):
    while True:
        try:
            with psycopg2.connect(db_connection_str) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    return cursor.fetchall()
        except Exception:
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
