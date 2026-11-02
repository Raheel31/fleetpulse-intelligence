CREATE OR REPLACE VIEW stable_customers AS
SELECT c.* FROM dim_customers c
CROSS JOIN (SELECT MIN(start_d) AS prior_start FROM ref_periods) p
WHERE c.churn_date IS NULL AND c.signup_date <= p.prior_start;

-- I1 Bridge: how much of the change is churned, new, or stable customers
WITH p AS (SELECT MIN(start_d) AS prior_start FROM ref_periods),
cls AS (
  SELECT c.customer_id,
         CASE WHEN c.churn_date < p.prior_start THEN 'already_churned'
              WHEN c.churn_date IS NOT NULL THEN 'churned_in_window'
              WHEN c.signup_date > p.prior_start THEN 'new_in_window'
              ELSE 'stable' END AS cust_class
  FROM dim_customers c CROSS JOIN p),
ev AS (
  SELECT customer_id,
         SUM(event_count) FILTER (WHERE period = 'prior') AS prior_events,
         SUM(event_count) FILTER (WHERE period = 'current') AS current_events
  FROM events_tagged GROUP BY 1)
SELECT cls.cust_class, COUNT(*) AS customers,
       COALESCE(SUM(ev.prior_events), 0) AS prior_events,
       COALESCE(SUM(ev.current_events), 0) AS current_events,
       COALESCE(SUM(ev.current_events), 0) - COALESCE(SUM(ev.prior_events), 0) AS abs_change
FROM cls LEFT JOIN ev ON ev.customer_id = cls.customer_id
GROUP BY 1 ORDER BY abs_change;

-- I2 Like-for-like by region and tier (stable customers only)
WITH a AS (
  SELECT region, subscription_tier, COUNT(DISTINCT customer_id) AS customers,
         SUM(event_count) FILTER (WHERE period = 'prior') AS prior_events,
         SUM(event_count) FILTER (WHERE period = 'current') AS current_events
  FROM events_tagged
  WHERE customer_id IN (SELECT customer_id FROM stable_customers)
  GROUP BY 1, 2)
SELECT *, ROUND(100.0 * (current_events * 1.0 / prior_events - 1), 1) AS pct_change
FROM a ORDER BY pct_change;

-- I3 Feature impact: Europe Basic and Pro vs rest of base (stable customers)
WITH a AS (
  SELECT feature_name,
         CASE WHEN region = 'Europe' AND subscription_tier IN ('Basic', 'Pro')
              THEN 'Europe Basic/Pro' ELSE 'Rest of base' END AS segment,
         SUM(event_count) FILTER (WHERE period = 'prior') AS prior_events,
         SUM(event_count) FILTER (WHERE period = 'current') AS current_events
  FROM events_tagged
  WHERE customer_id IN (SELECT customer_id FROM stable_customers)
  GROUP BY 1, 2)
SELECT *, ROUND(100.0 * (current_events * 1.0 / prior_events - 1), 1) AS pct_change
FROM a ORDER BY feature_name, segment;

-- I4 Depth of decline per customer, with MRR exposure (latest month)
WITH cust AS (
  SELECT customer_id,
         CASE WHEN region = 'Europe' AND subscription_tier = 'Basic' THEN 'Europe Basic'
              WHEN region = 'Europe' AND subscription_tier = 'Pro' THEN 'Europe Pro'
              ELSE 'Rest of base' END AS segment,
         COALESCE(SUM(event_count) FILTER (WHERE period = 'prior'), 0) AS prior_e,
         COALESCE(SUM(event_count) FILTER (WHERE period = 'current'), 0) AS cur_e
  FROM events_tagged
  WHERE customer_id IN (SELECT customer_id FROM stable_customers)
  GROUP BY 1, 2),
mrr AS (
  SELECT customer_id, mrr_usd FROM fact_subscriptions
  WHERE month_start = (SELECT MAX(month_start) FROM fact_subscriptions))
SELECT cust.segment,
       CASE WHEN cur_e < 0.35 * prior_e THEN '1 sharply reduced (<35% of prior)'
            WHEN cur_e < 0.7 * prior_e THEN '2 reduced (35-70%)'
            WHEN cur_e <= 1.3 * prior_e THEN '3 steady (70-130%)'
            ELSE '4 growing (>130%)' END AS usage_bucket,
       COUNT(*) AS customers, SUM(mrr.mrr_usd) AS latest_mrr_usd
FROM cust JOIN mrr ON mrr.customer_id = cust.customer_id
WHERE prior_e > 0
GROUP BY 1, 2 ORDER BY 1, 2;

-- I5 Timing: Reports + Dashboards events in 7-day blocks ending on the latest date (index, first block = 100)
WITH b AS (SELECT MAX(event_date) AS end_d FROM fact_platform_events),
w AS (
    SELECT b.end_d - CAST(7 * ((b.end_d - e.event_date) // 7) AS INTEGER) AS block_end,
         CASE WHEN c.region = 'Europe' AND c.subscription_tier IN ('Basic', 'Pro')
              THEN 'europe_basic_pro' ELSE 'rest_of_base' END AS segment,
         SUM(e.event_count) AS events
  FROM fact_platform_events e
  CROSS JOIN b
  JOIN dim_customers c ON c.customer_id = e.customer_id
  WHERE e.feature_id IN ('F03', 'F04')
    AND e.customer_id IN (SELECT customer_id FROM stable_customers)
    AND e.event_date > b.end_d - 70
  GROUP BY 1, 2)
SELECT block_end, segment, events,
       ROUND(100.0 * events / FIRST_VALUE(events) OVER (PARTITION BY segment ORDER BY block_end), 0) AS index_vs_first
FROM w ORDER BY segment, block_end;