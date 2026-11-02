CREATE OR REPLACE VIEW dim_tier AS
SELECT * FROM (VALUES ('Basic', 1), ('Pro', 2), ('Enterprise', 3)) AS t(tier, tier_rank);

-- Two 30-day reporting windows anchored on the latest event date
CREATE OR REPLACE VIEW ref_periods AS
WITH b AS (SELECT MAX(event_date) AS end_d FROM fact_platform_events)
SELECT 'current' AS period, end_d - 29 AS start_d, end_d AS end_d FROM b
UNION ALL
SELECT 'prior', end_d - 59, end_d - 30 FROM b;

CREATE OR REPLACE VIEW events_tagged AS
SELECT e.*, c.region, c.subscription_tier, c.industry, f.feature_name, p.period
FROM fact_platform_events e
JOIN ref_periods p ON e.event_date BETWEEN p.start_d AND p.end_d
JOIN dim_customers c ON c.customer_id = e.customer_id
JOIN dim_features f ON f.feature_id = e.feature_id;

-- Monthly. Customers, vehicles and platform adoption
CREATE OR REPLACE VIEW mart_product_adoption AS
WITH subs AS (
  SELECT month_start,
         COUNT(*) FILTER (WHERE status = 'active') AS subscribed_customers,
         SUM(billable_vehicles) FILTER (WHERE status = 'active') AS billable_vehicles
  FROM fact_subscriptions GROUP BY 1),
eng AS (
  SELECT s.month_start, COUNT(DISTINCT e.customer_id) AS engaged_customers
  FROM fact_platform_events e
  JOIN fact_subscriptions s ON s.customer_id = e.customer_id
   AND s.month_start = date_trunc('month', e.event_date)::DATE AND s.status = 'active'
  GROUP BY 1),
veh AS (
  SELECT s.month_start,
         COUNT(DISTINCT a.vehicle_id) FILTER (WHERE a.is_connected) AS transmitting_vehicles,
         ROUND(100.0 * AVG(CASE WHEN a.is_connected THEN 1 ELSE 0 END), 1) AS connected_vehicle_day_rate_pct
  FROM fact_vehicle_activity a
  JOIN dim_vehicles v ON v.vehicle_id = a.vehicle_id
  JOIN fact_subscriptions s ON s.customer_id = v.customer_id
   AND s.month_start = date_trunc('month', a.activity_date)::DATE AND s.status = 'active'
  GROUP BY 1)
SELECT s.month_start, s.subscribed_customers, e.engaged_customers,
       ROUND(100.0 * e.engaged_customers / s.subscribed_customers, 1) AS customer_activation_pct,
       s.billable_vehicles, v.transmitting_vehicles, v.connected_vehicle_day_rate_pct
FROM subs s
LEFT JOIN eng e ON e.month_start = s.month_start
LEFT JOIN veh v ON v.month_start = s.month_start;

-- Daily. DAU plus rolling WAU and MAU (distinct customers)
CREATE OR REPLACE VIEW mart_customer_engagement AS
WITH cd AS (
  SELECT customer_id, event_date, SUM(event_count) AS events
  FROM fact_platform_events GROUP BY 1, 2),
days AS (SELECT DISTINCT event_date AS report_date FROM cd)
SELECT d.report_date,
       COUNT(DISTINCT CASE WHEN cd.event_date = d.report_date THEN cd.customer_id END) AS dau,
       COUNT(DISTINCT CASE WHEN cd.event_date > d.report_date - 7 THEN cd.customer_id END) AS wau,
       COUNT(DISTINCT cd.customer_id) AS mau,
       SUM(CASE WHEN cd.event_date = d.report_date THEN cd.events ELSE 0 END) AS events,
       d.report_date >= (SELECT MIN(event_date) + 29 FROM cd) AS full_30d_window
FROM days d
JOIN cd ON cd.event_date BETWEEN d.report_date - 29 AND d.report_date
GROUP BY d.report_date;

-- Per feature and period. Adoption against eligible customers
CREATE OR REPLACE VIEW mart_feature_performance AS
WITH usage AS (
  SELECT p.period, e.feature_id,
         COUNT(DISTINCT e.customer_id) AS adopters, SUM(e.event_count) AS events
  FROM fact_platform_events e
  JOIN ref_periods p ON e.event_date BETWEEN p.start_d AND p.end_d
  GROUP BY 1, 2),
eligible AS (
  SELECT p.period, f.feature_id, COUNT(*) AS eligible_customers
  FROM ref_periods p
  CROSS JOIN dim_features f
  JOIN dim_tier ft ON ft.tier = f.min_tier
  JOIN dim_customers c ON c.signup_date <= p.end_d
   AND (c.churn_date IS NULL OR c.churn_date >= p.start_d)
  JOIN dim_tier ct ON ct.tier = c.subscription_tier AND ct.tier_rank >= ft.tier_rank
  GROUP BY 1, 2)
SELECT el.period, f.feature_name, f.category, el.eligible_customers,
       COALESCE(u.adopters, 0) AS adopters,
       ROUND(100.0 * COALESCE(u.adopters, 0) / el.eligible_customers, 1) AS adoption_rate_pct,
       COALESCE(u.events, 0) AS events,
       ROUND(COALESCE(u.events, 0) * 1.0 / NULLIF(u.adopters, 0), 1) AS events_per_adopter
FROM eligible el
JOIN dim_features f ON f.feature_id = el.feature_id
LEFT JOIN usage u ON u.period = el.period AND u.feature_id = el.feature_id;

-- Monthly logo retention (active last month, still active this month)
CREATE OR REPLACE VIEW mart_customer_retention AS
SELECT c.month_start,
       COUNT(*) AS opening_customers,
       COUNT(*) FILTER (WHERE c.status = 'active') AS retained_customers,
       COUNT(*) FILTER (WHERE c.status = 'churned') AS churned_customers,
       ROUND(100.0 * COUNT(*) FILTER (WHERE c.status = 'active') / COUNT(*), 1) AS logo_retention_pct,
       ROUND(100.0 * COUNT(*) FILTER (WHERE c.status = 'churned') / COUNT(*), 1) AS logo_churn_pct
FROM fact_subscriptions p
JOIN fact_subscriptions c ON c.customer_id = p.customer_id
 AND c.month_start = (p.month_start + INTERVAL 1 MONTH)::DATE
WHERE p.status = 'active'
GROUP BY c.month_start;

-- Monthly MRR, growth, ARPA and churned MRR
CREATE OR REPLACE VIEW mart_revenue_performance AS
WITH m AS (
  SELECT month_start, SUM(mrr_usd) AS mrr,
         COUNT(*) FILTER (WHERE status = 'active') AS paying_customers,
         SUM(billable_vehicles) FILTER (WHERE status = 'active') AS billable_vehicles
  FROM fact_subscriptions GROUP BY 1),
ch AS (
  SELECT c.month_start, SUM(p.mrr_usd) AS churned_mrr
  FROM fact_subscriptions c
  JOIN fact_subscriptions p ON p.customer_id = c.customer_id
   AND (p.month_start + INTERVAL 1 MONTH)::DATE = c.month_start
  WHERE c.status = 'churned'
  GROUP BY 1)
SELECT m.month_start, m.mrr,
       ROUND(100.0 * (m.mrr / LAG(m.mrr) OVER (ORDER BY m.month_start) - 1), 1) AS mrr_growth_pct,
       m.paying_customers, ROUND(m.mrr / m.paying_customers, 0) AS arpa_usd,
       m.billable_vehicles, ROUND(m.mrr / m.billable_vehicles, 2) AS mrr_per_vehicle,
       COALESCE(ch.churned_mrr, 0) AS churned_mrr
FROM m LEFT JOIN ch ON ch.month_start = m.month_start;

-- Preview: product adoption
SELECT * FROM mart_product_adoption ORDER BY month_start;

-- Preview: engagement, latest 8 days
SELECT * FROM mart_customer_engagement ORDER BY report_date DESC LIMIT 8;

-- Preview: feature performance
SELECT * FROM mart_feature_performance ORDER BY feature_name, period DESC;

-- Preview: retention
SELECT * FROM mart_customer_retention ORDER BY month_start;

-- Preview: revenue
SELECT * FROM mart_revenue_performance ORDER BY month_start;