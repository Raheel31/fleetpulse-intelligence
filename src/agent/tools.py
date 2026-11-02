
"""Bounded, read-only analytics tools for FleetPulse Intelligence."""

import duckdb
from src.quality import run_checks

DB = "data/warehouse/fleetpulse.duckdb"


def _records(con, sql, params=None):
    """Execute a predefined query and return dictionaries."""
    result = con.execute(sql, params or []).fetchdf()
    return result.to_dict(orient="records")


def get_platform_overview(con):
    """Compare recorded platform activity across the two reporting windows."""

    sql = """
    WITH periods AS (
        SELECT period, start_d, end_d FROM ref_periods
    ),
    metrics AS (
        SELECT
            p.period,
            p.start_d,
            p.end_d,
            COUNT(DISTINCT e.customer_id) AS active_customers,
            COALESCE(SUM(e.event_count), 0) AS events
        FROM periods p
        LEFT JOIN fact_platform_events e
            ON e.event_date BETWEEN p.start_d AND p.end_d
        GROUP BY 1, 2, 3
    )
    SELECT *,
        ROUND(
            events * 1.0 / NULLIF(active_customers, 0), 1
        ) AS events_per_active_customer
    FROM metrics
    ORDER BY start_d
    """

    return {
        "reporting_periods": _records(con, sql),
        "latest_event_date": con.execute(
            "SELECT MAX(event_date) FROM fact_platform_events"
        ).fetchone()[0],
        "interpretation": (
            "Active customers are distinct customers with recorded events "
            "during the reporting window."
        )
    }


def compare_segments(con):
    """Compare churn/new/stable customer contributions and stable segments."""

    bridge_sql = """
    WITH p AS (
        SELECT MIN(start_d) AS prior_start FROM ref_periods
    ),
    cls AS (
        SELECT c.customer_id,
            CASE
                WHEN c.churn_date < p.prior_start THEN 'already_churned'
                WHEN c.churn_date IS NOT NULL THEN 'churned_in_window'
                WHEN c.signup_date > p.prior_start THEN 'new_in_window'
                ELSE 'stable'
            END AS cust_class
        FROM dim_customers c CROSS JOIN p
    ),
    ev AS (
        SELECT customer_id,
            COALESCE(SUM(event_count)
                FILTER (WHERE period = 'prior'), 0) AS prior_events,
            COALESCE(SUM(event_count)
                FILTER (WHERE period = 'current'), 0) AS current_events
        FROM events_tagged
        GROUP BY 1
    )
    SELECT cls.cust_class,
        COUNT(*) AS customers,
        COALESCE(SUM(ev.prior_events), 0) AS prior_events,
        COALESCE(SUM(ev.current_events), 0) AS current_events,
        COALESCE(SUM(ev.current_events), 0)
          - COALESCE(SUM(ev.prior_events), 0) AS abs_change
    FROM cls
    LEFT JOIN ev ON ev.customer_id = cls.customer_id
    GROUP BY 1
    ORDER BY abs_change
    """

    segment_sql = """
    WITH stable AS (
        SELECT customer_id, region, subscription_tier
        FROM stable_customers
    ),
    ev AS (
        SELECT customer_id,
            COALESCE(SUM(event_count)
                FILTER (WHERE period = 'prior'), 0) AS prior_events,
            COALESCE(SUM(event_count)
                FILTER (WHERE period = 'current'), 0) AS current_events
        FROM events_tagged
        GROUP BY 1
    ),
    a AS (
        SELECT s.region, s.subscription_tier,
            COUNT(*) AS customers,
            SUM(COALESCE(e.prior_events, 0)) AS prior_events,
            SUM(COALESCE(e.current_events, 0)) AS current_events
        FROM stable s
        LEFT JOIN ev e ON e.customer_id = s.customer_id
        GROUP BY 1, 2
    )
    SELECT *,
        current_events - prior_events AS abs_change,
        ROUND(
            100.0 * (current_events * 1.0 / NULLIF(prior_events, 0) - 1),
            1
        ) AS pct_change
    FROM a
    ORDER BY abs_change
    """

    return {
        "customer_bridge": _records(con, bridge_sql),
        "stable_customer_segments": _records(con, segment_sql)
    }


def analyse_feature_usage(con):
    """Compare feature adoption and event volume across periods."""

    adoption_sql = """
    SELECT *
    FROM mart_feature_performance
    ORDER BY feature_name, period DESC
    """

    impact_sql = """
    WITH a AS (
        SELECT feature_name,
            CASE
                WHEN region = 'Europe'
                  AND subscription_tier IN ('Basic', 'Pro')
                THEN 'Europe Basic/Pro'
                ELSE 'Rest of base'
            END AS segment,
            COALESCE(SUM(event_count)
                FILTER (WHERE period = 'prior'), 0) AS prior_events,
            COALESCE(SUM(event_count)
                FILTER (WHERE period = 'current'), 0) AS current_events
        FROM events_tagged
        WHERE customer_id IN (
            SELECT customer_id FROM stable_customers
        )
        GROUP BY 1, 2
    )
    SELECT *,
        current_events - prior_events AS abs_change,
        ROUND(
            100.0 * (current_events * 1.0 / NULLIF(prior_events, 0) - 1),
            1
        ) AS pct_change
    FROM a
    ORDER BY feature_name, segment
    """

    return {
        "feature_adoption": _records(con, adoption_sql),
        "stable_customer_feature_impact": _records(con, impact_sql)
    }


def get_customer_risk(con):
    """Return usage-reduction buckets and associated latest-month MRR."""

    sql = """
    WITH cust AS (
        SELECT customer_id,
            CASE
                WHEN region = 'Europe'
                  AND subscription_tier = 'Basic' THEN 'Europe Basic'
                WHEN region = 'Europe'
                  AND subscription_tier = 'Pro' THEN 'Europe Pro'
                ELSE 'Rest of base'
            END AS segment,
            COALESCE(SUM(event_count)
                FILTER (WHERE period = 'prior'), 0) AS prior_e,
            COALESCE(SUM(event_count)
                FILTER (WHERE period = 'current'), 0) AS cur_e
        FROM events_tagged
        WHERE customer_id IN (
            SELECT customer_id FROM stable_customers
        )
        GROUP BY 1, 2
    ),
    mrr AS (
        SELECT customer_id, mrr_usd
        FROM fact_subscriptions
        WHERE month_start = (
            SELECT MAX(month_start) FROM fact_subscriptions
        )
    )
    SELECT cust.segment,
        CASE
            WHEN cur_e < 0.35 * prior_e
                THEN '1 sharply reduced (<35% of prior)'
            WHEN cur_e < 0.7 * prior_e
                THEN '2 reduced (35-70%)'
            WHEN cur_e <= 1.3 * prior_e
                THEN '3 steady (70-130%)'
            ELSE '4 growing (>130%)'
        END AS usage_bucket,
        COUNT(*) AS customers,
        SUM(mrr.mrr_usd) AS latest_mrr_usd
    FROM cust
    JOIN mrr ON mrr.customer_id = cust.customer_id
    WHERE prior_e > 0
    GROUP BY 1, 2
    ORDER BY 1, 2
    """

    return _records(con, sql)


def analyse_decline_timing(con):
    """Track Reports and Dashboards activity in rolling seven-day blocks."""

    sql = """
    WITH b AS (
        SELECT MAX(event_date) AS end_d
        FROM fact_platform_events
    ),
    w AS (
        SELECT
            b.end_d - CAST(
                7 * ((b.end_d - e.event_date) // 7) AS INTEGER
            ) AS block_end,
            CASE
                WHEN c.region = 'Europe'
                  AND c.subscription_tier IN ('Basic', 'Pro')
                THEN 'europe_basic_pro'
                ELSE 'rest_of_base'
            END AS segment,
            SUM(e.event_count) AS events
        FROM fact_platform_events e
        CROSS JOIN b
        JOIN dim_customers c ON c.customer_id = e.customer_id
        WHERE e.feature_id IN ('F03', 'F04')
          AND e.customer_id IN (
              SELECT customer_id FROM stable_customers
          )
          AND e.event_date > b.end_d - 70
        GROUP BY 1, 2
    )
    SELECT block_end, segment, events,
        ROUND(
            100.0 * events /
            NULLIF(
                FIRST_VALUE(events) OVER (
                    PARTITION BY segment ORDER BY block_end
                ), 0
            ),
            0
        ) AS index_vs_first
    FROM w
    ORDER BY segment, block_end
    """

    return _records(con, sql)


def get_data_quality(con):
    """Run the same quality checks used by the dashboard."""

    results, latest = run_checks(con=con)

    failures = [
        result for result in results
        if result["violations"] > 0
    ]

    return {
        "checks": results,
        "failed_checks": failures,
        "total_checks": len(results),
        "failed_count": len(failures),
        "latest_event_date": latest
    }


METRIC_DEFINITIONS = {
    "dau": (
        "Distinct customers with recorded platform events on a given day."
    ),
    "wau": (
        "Distinct customers with recorded events in the seven-day rolling "
        "window ending on the report date."
    ),
    "mau": (
        "Distinct customers with recorded events in the 30-day rolling "
        "window ending on the report date."
    ),
    "active_customers": (
        "Distinct customers with at least one recorded platform event "
        "during the reporting period."
    ),
    "events_per_active_customer": (
        "Total recorded events divided by distinct active customers "
        "in the same reporting period."
    ),
    "feature_adoption": (
        "Distinct feature adopters divided by eligible customers. "
        "Eligibility is based on signup date, churn date and minimum tier."
    ),
    "logo_retention": (
        "Customers active in the previous subscription month who remain "
        "active in the following month, divided by opening customers."
    ),
    "mrr": (
        "Monthly recurring revenue recorded in fact_subscriptions."
    ),
    "arpa": (
        "Monthly recurring revenue divided by paying customers."
    ),
    "usage_risk": (
        "Stable customers classified by current-period event volume "
        "relative to their prior-period event volume."
    )
}


def get_metric_definition(metric=None):
    """Return metric definitions or all available definitions."""

    if metric is None:
        return METRIC_DEFINITIONS

    key = metric.strip().lower()

    if key not in METRIC_DEFINITIONS:
        return {
            "error": f"Unknown metric: {metric}",
            "available_metrics": list(METRIC_DEFINITIONS.keys())
        }

    return {key: METRIC_DEFINITIONS[key]}


# Explicit allowlist for the agent. No arbitrary SQL execution.
TOOL_FUNCTIONS = {
    "get_platform_overview": get_platform_overview,
    "compare_segments": compare_segments,
    "analyse_feature_usage": analyse_feature_usage,
    "get_customer_risk": get_customer_risk,
    "analyse_decline_timing": analyse_decline_timing,
    "get_data_quality": get_data_quality,
    "get_metric_definition": get_metric_definition
}
