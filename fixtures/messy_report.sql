-- Quarterly revenue and engagement report.
-- Synthetic. Deliberately messy: the shape real report queries drift into after
-- a few years of additions.
WITH active_customers AS (
    SELECT c.id,
           c.region,
           c.segment,
           c.name,
           c.signed_up_at,
           c.account_manager_id
    FROM customers c
    WHERE c.active = true
      AND c.deleted_at IS NULL
      AND c.signed_up_at < '2025-01-01'
),
order_base AS (
    SELECT o.id AS order_id,
           o.customer_id,
           o.placed_at,
           o.total,
           o.currency,
           o.status,
           o.channel
    FROM orders o
    WHERE o.placed_at >= '2024-01-01'
      AND o.placed_at < '2025-01-01'
      AND o.status NOT IN (SELECT s.name FROM excluded_statuses s)
),
order_items_expanded AS (
    SELECT oi.order_id,
           oi.sku,
           oi.quantity,
           oi.unit_price,
           oi.discount_pct,
           p.category,
           p.supplier_id
    FROM order_items oi
    JOIN products p ON p.sku = oi.sku
),
support_activity AS (
    SELECT t.customer_id,
           count(*) AS ticket_count,
           avg(extract(epoch FROM (t.resolved_at - t.opened_at)) / 3600.0) AS avg_hours_to_resolve
    FROM support_tickets t
    WHERE t.opened_at >= '2024-01-01'
    GROUP BY t.customer_id
),
logins AS (
    SELECT l.customer_id,
           count(DISTINCT date_trunc('day', l.occurred_at)) AS active_days
    FROM login_events l
    WHERE l.occurred_at >= '2024-01-01'
    GROUP BY l.customer_id
),
combined AS (
    SELECT ac.id AS customer_id,
           ac.region,
           ac.segment,
           ac.name,
           ac.account_manager_id,
           ob.order_id,
           ob.placed_at,
           ob.total,
           ob.channel,
           oie.sku,
           oie.quantity,
           oie.unit_price,
           oie.category,
           sa.ticket_count,
           sa.avg_hours_to_resolve,
           lg.active_days
    FROM active_customers ac
    JOIN order_base ob ON ob.customer_id = ac.id
    JOIN order_items_expanded oie ON oie.order_id = ob.order_id
    LEFT JOIN support_activity sa ON sa.customer_id = ac.id
    LEFT JOIN logins lg ON lg.customer_id = ac.id
),
regional_rollup AS (
    SELECT region,
           segment,
           count(DISTINCT customer_id) AS customers,
           count(DISTINCT order_id) AS orders,
           sum(total) AS gross_revenue,
           sum(quantity * unit_price) AS line_revenue,
           avg(ticket_count) AS avg_tickets,
           avg(active_days) AS avg_active_days
    FROM combined
    GROUP BY region, segment
),
manager_rollup AS (
    SELECT ac.account_manager_id,
           am.name AS manager_name,
           count(DISTINCT cb.order_id) AS managed_orders,
           sum(cb.total) AS managed_revenue
    FROM combined cb
    JOIN active_customers ac ON ac.id = cb.customer_id
    JOIN account_managers am ON am.id = ac.account_manager_id
    GROUP BY ac.account_manager_id, am.name
)
SELECT rr.region,
       rr.segment,
       rr.customers,
       rr.orders,
       rr.gross_revenue,
       rr.line_revenue,
       rr.avg_tickets,
       rr.avg_active_days,
       mr.manager_name,
       mr.managed_revenue,
       CASE WHEN rr.gross_revenue > 0
            THEN round((rr.line_revenue / rr.gross_revenue)::numeric, 4)
            ELSE NULL END AS line_to_gross_ratio
FROM regional_rollup rr
LEFT JOIN manager_rollup mr ON mr.managed_revenue > rr.gross_revenue * 0.1
ORDER BY rr.gross_revenue DESC, rr.region, rr.segment
