-- The classic refactor bug: joining order_items to reach a column multiplies
-- every order row by its item count, so revenue is silently double-counted.
-- Row counts and DISTINCT order_count still look plausible, which is exactly
-- why this needs EXCEPT ALL rather than eyeballing.
SELECT c.region,
       count(DISTINCT o.id) AS order_count,
       sum(o.total)         AS revenue
FROM customers c
JOIN orders o ON o.customer_id = c.id
JOIN order_items i ON i.order_id = o.id
GROUP BY c.region
