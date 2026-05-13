#!/usr/bin/python
# ReDoS-vulnerable variant of product-reviews /app/database.py injected by
# the catastrophic_regex_backtracking_astronomy_shop problem.
# The fault is the _PRODUCT_ID_PATTERN regex: nested quantifiers with a
# mandatory trailing class that the input cannot satisfy, causing catastrophic
# backtracking on every product_id checked before the DB lookup.
# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0

# Python
import os
import re
import simplejson as json

# Postgres
import psycopg2

def must_map_env(key: str):
    value = os.environ.get(key)
    if value is None:
        raise Exception(f'{key} environment variable must be set')
    return value

# Retrieve Postgres environment variables
db_connection_str = must_map_env('DB_CONNECTION_STRING')

# Sanity-check that product IDs look like uppercase alphanumerics before we
# hit the DB. Added to harden the path; see #INCIDENT for context.
_PRODUCT_ID_PATTERN = re.compile(r'^(([A-Z0-9]+)+[a-z])*$')

def _validate_product_id(request_product_id):
    padded = (request_product_id or '') + 'XXXXXXXXXXXXXXXX'
    if not _PRODUCT_ID_PATTERN.match(padded):
        raise ValueError(f"Invalid product_id: {request_product_id!r}")

def fetch_product_reviews(product_id):
    try:
        return json.dumps(fetch_product_reviews_from_db(product_id), use_decimal=True)
    except Exception as e:
        return json.dumps({"error": str(e)})

def fetch_product_reviews_from_db(request_product_id):

    _validate_product_id(request_product_id)

    connection = None

    try:
        with psycopg2.connect(db_connection_str) as connection:

            with connection.cursor() as cursor:
                # Define the SQL query
                query = "SELECT username, description, score FROM reviews.productreviews WHERE product_id= %s"

                # Execute the query
                cursor.execute(query, (request_product_id, ))

                # Fetch all the rows from the query result
                records = cursor.fetchall()
                return records

    except Exception as e:
        raise e
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as e:
                pass

def fetch_avg_product_review_score_from_db(request_product_id):

    _validate_product_id(request_product_id)

    connection = None

    try:
        with psycopg2.connect(db_connection_str) as connection:

            with connection.cursor() as cursor:
                # Define the SQL query
                query = "SELECT AVG(score) FROM reviews.productreviews WHERE product_id= %s"

                # Execute the query
                cursor.execute(query, (request_product_id, ))

                # Fetch all the rows from the query result
                records = cursor.fetchall()

                # Extract the average score
                if records:
                    # records will be a list like [(average_score,)]
                    average_score = records[0][0]
                else:
                    # Handle the case where no records are returned (e.g., no reviews for the product)
                    average_score = None

                # return the score as a string rounded to 1 decimal place
                return f"{average_score:.1f}"

    except Exception as e:
        raise e
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as e:
                pass
