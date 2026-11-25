-- Larkbank's keyspace. Three copies of every row. Amounts are in pence.
CREATE KEYSPACE IF NOT EXISTS bank WITH replication = {'class': 'NetworkTopologyStrategy', 'dc1': 3};

-- Everything of a customer's is in the customer's partition.
CREATE TABLE IF NOT EXISTS bank.customer (
    customer_id text PRIMARY KEY,
    name        text,
    joined      timestamp
);

-- The people a customer pays.
CREATE TABLE IF NOT EXISTS bank.payee (
    customer_id    text,
    payee_id       text,
    name           text,
    sort_code      text,
    account_number text,
    created_at     timestamp,
    PRIMARY KEY (customer_id, payee_id)
);

-- A standing order pays a payee the same amount every every_seconds, first at first_due.
CREATE TABLE IF NOT EXISTS bank.standing_order (
    customer_id   text,
    order_id      text,
    payee_id      text,
    amount        bigint,
    reference     text,
    every_seconds int,
    first_due     timestamp,
    created_at    timestamp,
    PRIMARY KEY (customer_id, order_id)
);

-- The payments standing orders have made: one for each due time.
CREATE TABLE IF NOT EXISTS bank.payment (
    customer_id    text,
    order_id       text,
    due            timestamp,
    payment_id     text,
    payee_id       text,
    sort_code      text,
    account_number text,
    amount         bigint,
    reference      text,
    sent_at        timestamp,
    PRIMARY KEY (customer_id, order_id, due)
);
