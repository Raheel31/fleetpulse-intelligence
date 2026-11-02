# FleetPulse Data Model

Synthetic data only. Independent prototype, not affiliated with Geotab.

## 1. Architecture

raw CSVs -> staging (`stg_*`) -> dimensions and facts -> marts (views)

- Generator: `src/generate_data.py` (seeded with `SEED = 42`, fully reproducible)
- Load: `src/etl.py` rebuilds the DuckDB warehouse from scratch on each run
- Validation: `src/quality.py` runs 14 business-rule checks against the warehouse
- Analysis: `sql/02_marts.sql` (marts) and `sql/03_decline_investigation.sql` (investigation)

Refresh the whole warehouse with:

    python src\generate_data.py
    python src\etl.py
    python src\quality.py

## 2. Tables, grains and keys

| Table | Grain (one row per...) | Primary key | Foreign keys |
|---|---|---|---|
| dim_date | calendar day | date_key | none |
| dim_customers | customer | customer_id | none |
| dim_vehicles | vehicle | vehicle_id | customer_id -> dim_customers |
| dim_features | platform feature | feature_id | none |
| fact_platform_events | customer x feature x day, only days with at least one event | (customer_id, feature_id, event_date) | customer_id -> dim_customers, feature_id -> dim_features |
| fact_vehicle_activity | vehicle x day, from activation until customer churn | (vehicle_id, activity_date) | vehicle_id -> dim_vehicles |
| fact_subscriptions | customer x billing month | (customer_id, month_start) | customer_id -> dim_customers |

Duplicate keys and orphaned foreign keys are prevented by the PK and FK constraints in `sql/schema.sql`. A load that violated them would fail, so they are not repeated as quality checks.

## 3. Analytical views (marts)

| View | Grain | Question it answers |
|---|---|---|
| mart_product_adoption | month | How many customers and vehicles are adopting the platform? |
| mart_customer_engagement | day | DAU, rolling WAU and MAU, and event volume |
| mart_feature_performance | feature x reporting period | Adoption rate and usage depth per feature |
| mart_customer_retention | month | Logo retention and churn |
| mart_revenue_performance | month | MRR, growth, ARPA, churned MRR |

Helper views: `dim_tier` (tier ranking), `ref_periods` (reporting windows), `events_tagged` (events labelled with period and customer attributes), `stable_customers` (like-for-like cohort for the investigation).

## 4. Reporting periods

- **Current period:** the 30 days ending on the latest event date (2026-10-02 to 2026-10-31)
- **Prior period:** the 30 days immediately before (2026-09-02 to 2026-10-01)
- Data window: 2026-06-04 to 2026-10-31. June is a partial month and should not be interpreted.
- **Stable customers:** not churned, and signed up on or before the start of the prior period. Used to separate behaviour change from customer-base change.

## 5. Metric definitions

| Metric | Definition |
|---|---|
| DAU / WAU / MAU | COUNT(DISTINCT customer_id) with at least one event on the day, in the trailing 7 days, or in the trailing 30 days. Customer-level, not user-level. WAU and MAU are rolling windows. Never computed by summing across feature rows. |
| Customer activation | Subscribed (active) customers with at least one platform event in the month, divided by subscribed customers |
| Feature adoption | Customers using the feature in the period divided by eligible customers. Eligible = tier at or above the feature's minimum tier, signed up by the period end, and not churned before the period start. |
| Events per adopter | Feature events divided by customers using the feature |
| Connected vehicle-day rate | Share of vehicle-days with is_connected = true. Vehicle-level, separate from customer logins. |
| Logo retention | Customers active last month and still active this month, divided by customers active last month |
| Logo churn | 100% minus logo retention |
| MRR | Billable vehicles x tier price per vehicle per month (Basic $12, Pro $20, Enterprise $30). Churned months contribute 0. |
| ARPA | MRR divided by paying (active) customers |
| MRR per vehicle | MRR divided by billable vehicles |
| Churned MRR | Prior-month MRR of customers whose status becomes churned this month |
| Revenue at risk | Latest-month MRR of customers whose current-period usage fell below 35% of prior period |

## 6. Assumptions

- Events are pre-aggregated to a daily rollup per customer, feature and day. Production would keep the raw event log and build this as a rollup.
- Zero-event days are not stored. A missing row means no recorded activity, not that the customer was inactive, churned or ineligible.
- churn_date is the last day of activity. Activity on the churn date is valid.
- Customer tier is a static attribute. There is no upgrade or downgrade history.
- Billable vehicles are counted from vehicle activation date. Individual vehicles do not churn separately from their customer.
- active_users is a synthetic field, so DAU/WAU/MAU use customer counts instead.
- All data is synthetic and generated with a fixed seed.

## 7. Data quality rules (14)

| # | Rule | Why |
|---|---|---|
| 1 | No events after customer churn date | Churned customers cannot be active |
| 2 | No vehicle activity before activation | Vehicles cannot transmit before activation |
| 3 | No vehicle activity after customer churn | Same logic, vehicle level |
| 4 | active_users never exceeds event_count | Internal consistency |
| 5 | event_count is positive | Zero-event days are not stored |
| 6 | Disconnected vehicles report zero distance | Internal consistency |
| 7 | Feature used only at or above its minimum tier | Entitlement rule |
| 8 | No unknown subscription tiers | Prevents silent misclassification |
| 9 | Churned subscription rows have zero MRR | Revenue consistency |
| 10 | No negative MRR or billable vehicles | Value sanity |
| 11 | No subscription rows after the churn month | Billing stops at churn |
| 12 | Active MRR equals billable vehicles x tier price | Revenue reconciliation |
| 13 | All event dates exist in dim_date | Date dimension coverage |
| 14 | No nulls in core customer attributes | Completeness |

These rules mostly pass by construction on synthetic data. They show that the load preserved the rules, not that real feeds would be clean. They are written to catch violations on real data.

## 8. Seeded scenario (validation ground truth)

- In the last 30 days, Reports and Dashboards usage in Europe Basic and Pro falls by about 50%.
- About 15% of Europe Basic customers go nearly dormant from 5 days after the decline starts.
- The investigation SQL should recover this without being told. Customer-level dormancy flags are not written to disk.

## 9. Known limitations

- Small segments: Europe Basic is about 35 customers, so the near-dormant group is only a handful of accounts. Treat findings as indicative.
- Synthetic data: findings illustrate method, not real customer behaviour.
- Full refresh load: production would use incremental, partitioned loads and upserts on the grain keys.