"""Data quality checks. Each check returns the number of violating rows (0 = pass)."""
import sys
import duckdb

DB = "data/warehouse/fleetpulse.duckdb"

CHECKS = {
    "events after customer churn": """
        SELECT COUNT(*) FROM fact_platform_events e
        JOIN dim_customers c USING (customer_id)
        WHERE c.churn_date IS NOT NULL AND e.event_date > c.churn_date""",
    "vehicle activity before activation": """
        SELECT COUNT(*) FROM fact_vehicle_activity a
        JOIN dim_vehicles v USING (vehicle_id)
        WHERE a.activity_date < v.activation_date""",
    "active_users greater than event_count": """
        SELECT COUNT(*) FROM fact_platform_events WHERE active_users > event_count""",
    "non-positive event_count": """
        SELECT COUNT(*) FROM fact_platform_events WHERE event_count <= 0""",
    "disconnected vehicle with distance > 0": """
        SELECT COUNT(*) FROM fact_vehicle_activity WHERE NOT is_connected AND distance_km > 0""",
    "feature used below its minimum tier": """
        SELECT COUNT(*) FROM fact_platform_events e
        JOIN dim_customers c USING (customer_id)
        JOIN dim_features f USING (feature_id)
        WHERE CASE c.subscription_tier WHEN 'Basic' THEN 1 WHEN 'Pro' THEN 2 ELSE 3 END
            < CASE f.min_tier WHEN 'Basic' THEN 1 WHEN 'Pro' THEN 2 ELSE 3 END""",
    "churned subscription with MRR > 0": """
        SELECT COUNT(*) FROM fact_subscriptions WHERE status = 'churned' AND mrr_usd > 0""",
    "events outside dim_date range": """
        SELECT COUNT(*) FROM fact_platform_events e
        LEFT JOIN dim_date d ON e.event_date = d.date_key WHERE d.date_key IS NULL""",
    "null customer attributes": """
        SELECT COUNT(*) FROM dim_customers
        WHERE region IS NULL OR industry IS NULL OR subscription_tier IS NULL""",
}


def run_checks(con=None):
    own = con is None
    con = con or duckdb.connect(DB, read_only=True)
    results = [{"check": name, "violations": con.execute(sql).fetchone()[0]}
               for name, sql in CHECKS.items()]
    # Freshness is reported as information, not pass/fail
    latest = con.execute("SELECT MAX(event_date) FROM fact_platform_events").fetchone()[0]
    if own:
        con.close()
    return results, latest


if __name__ == "__main__":
    results, latest = run_checks()
    for r in results:
        print(f"{'PASS' if r['violations'] == 0 else 'FAIL'}  {r['check']:<42} {r['violations']:>8,}")
    print(f"\nLatest event date: {latest}")
    sys.exit(1 if any(r["violations"] for r in results) else 0)