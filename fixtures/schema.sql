-- Synthetic schema for exercising sqldiff. Contains no real data of any kind.
DROP TABLE IF EXISTS order_items, orders, customers CASCADE;

CREATE TABLE customers (
    id      integer PRIMARY KEY,
    region  text NOT NULL,
    name    text NOT NULL
);

CREATE TABLE orders (
    id           integer PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES customers(id),
    total        numeric(10,2) NOT NULL
);

-- Two items per order: the table that makes a careless join fan out.
CREATE TABLE order_items (
    id        integer PRIMARY KEY,
    order_id  integer NOT NULL REFERENCES orders(id),
    sku       text NOT NULL
);

INSERT INTO customers VALUES
    (1, 'north', 'Ash'), (2, 'north', 'Bo'), (3, 'south', 'Cy');

INSERT INTO orders VALUES
    (10, 1, 100.00), (11, 1, 50.00), (12, 2, 75.00), (13, 3, 20.00);

INSERT INTO order_items VALUES
    (100, 10, 'sku-a'), (101, 10, 'sku-b'),
    (102, 11, 'sku-a'), (103, 11, 'sku-c'),
    (104, 12, 'sku-d'), (105, 12, 'sku-e'),
    (106, 13, 'sku-f'), (107, 13, 'sku-g');
