-- Same result, rewritten as a CTE. sqldiff should call this IDENTICAL.
WITH per_customer AS (
    SELECT c.region, o.id AS order_id, o.total
    FROM customers c
    JOIN orders o ON o.customer_id = c.id
)
SELECT region,
       count(DISTINCT order_id) AS order_count,
       sum(total)               AS revenue
FROM per_customer
GROUP BY region
