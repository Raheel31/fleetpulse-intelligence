"""Load raw CSVs into DuckDB: staging -> dimensions/facts."""
from pathlib import Path
import duckdb

RAW = Path("data/raw")
DB = Path("data/warehouse/fleetpulse.duckdb")
SCHEMA = Path("sql/schema.sql")

SOURCES = {
    "customers": "customers.csv",
    "vehicles": "vehicles.csv",
    "features": "features.csv",
    "platform_events": "platform_events.csv",
    "vehicle_activity": "vehicle_activity.csv",
    "subscriptions": "subscriptions.csv",
}


def build():
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    con = duckdb.connect(str(DB))

    # 1. Extract -> staging (raw, untyped-as-inferred copies of the CSVs)
    for name, fname in SOURCES.items():
        path = (RAW / fname).as_posix()
        con.execute(f"CREATE TABLE stg_{name} AS SELECT * FROM read_csv_auto('{path}')")

    # 2. Create the modelled tables
    con.execute(SCHEMA.read_text())

    # 3. Transform + load (dimensions first, then facts)
    con.execute("""
        INSERT INTO dim_date
        SELECT d::DATE, year(d), month(d), date_trunc('month', d)::DATE,
               date_trunc('week', d)::DATE, isodow(d), isodow(d) >= 6
        FROM generate_series(DATE '2024-01-01', DATE '2026-12-31', INTERVAL 1 DAY) t(d)
    """)
    con.execute("""
        INSERT INTO dim_customers
        SELECT customer_id, industry, region, subscription_tier,
               CAST(signup_date AS DATE), CAST(churn_date AS DATE)
        FROM stg_customers
    """)
    con.execute("""
        INSERT INTO dim_vehicles
        SELECT vehicle_id, customer_id, vehicle_type, CAST(activation_date AS DATE)
        FROM stg_vehicles
    """)
    con.execute("INSERT INTO dim_features SELECT feature_id, feature_name, category, min_tier FROM stg_features")
    con.execute("""
        INSERT INTO fact_platform_events
        SELECT customer_id, feature_id, CAST(event_date AS DATE), event_count, active_users
        FROM stg_platform_events
    """)
    con.execute("""
        INSERT INTO fact_vehicle_activity
        SELECT vehicle_id, CAST(activity_date AS DATE), is_connected, distance_km
        FROM stg_vehicle_activity
    """)
    con.execute("""
        INSERT INTO fact_subscriptions
        SELECT customer_id, CAST(month_start AS DATE), status, billable_vehicles, mrr_usd
        FROM stg_subscriptions
    """)

    # 4. Row-count report
    for t in ["dim_date", "dim_customers", "dim_vehicles", "dim_features",
              "fact_platform_events", "fact_vehicle_activity", "fact_subscriptions"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:<24}{n:>10,}")
    con.close()


if __name__ == "__main__":
    build()