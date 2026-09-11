-- Revenue per region. The reference implementation.
SELECT c.region,
       count(DISTINCT o.id) AS order_count,
       sum(o.total)         AS revenue
FROM customers c
JOIN orders o ON o.customer_id = c.id
GROUP BY c.region
