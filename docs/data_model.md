# FleetPulse Data Model

## Layers
raw CSVs -> staging (stg_*) -> dimensions and facts -> marts (views)

## Table grains
| Table | Grain | PK |
|---|---|---|
| dim_customers | one row per customer | customer_id |
| fact_platform_events | customer x feature x day, only days with events | (customer_id, feature_id, event_date) |
| ... | ... | ... |

## Assumptions
- Events are pre-aggregated to a daily rollup (prototype decision; production would keep the raw event log).
- Zero-event days are not stored, so "inactive" means "no row".
- MRR = billable vehicles x tier price per vehicle per month.
- Churned customers have mrr_usd = 0 in their churn month.
- All data is synthetic.

## Metric definitions
(add as we build them: WAU, feature adoption, retention, MRR)