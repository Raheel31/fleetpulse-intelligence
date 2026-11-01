-- FleetPulse warehouse schema (DuckDB)

CREATE TABLE dim_date (
    date_key     DATE PRIMARY KEY,
    year         INTEGER,
    month        INTEGER,
    month_start  DATE,
    week_start   DATE,          -- Monday
    iso_dow      INTEGER,       -- 1=Mon ... 7=Sun
    is_weekend   BOOLEAN
);

CREATE TABLE dim_customers (
    customer_id        VARCHAR PRIMARY KEY,
    industry           VARCHAR NOT NULL,
    region             VARCHAR NOT NULL,
    subscription_tier  VARCHAR NOT NULL,
    signup_date        DATE NOT NULL,
    churn_date         DATE            -- NULL = still active
);

CREATE TABLE dim_vehicles (
    vehicle_id       VARCHAR PRIMARY KEY,
    customer_id      VARCHAR NOT NULL REFERENCES dim_customers(customer_id),
    vehicle_type     VARCHAR NOT NULL,
    activation_date  DATE NOT NULL
);

CREATE TABLE dim_features (
    feature_id    VARCHAR PRIMARY KEY,
    feature_name  VARCHAR NOT NULL,
    category      VARCHAR NOT NULL,
    min_tier      VARCHAR NOT NULL
);

-- Grain: one row per customer x feature x day (only days with at least one event)
CREATE TABLE fact_platform_events (
    customer_id   VARCHAR NOT NULL REFERENCES dim_customers(customer_id),
    feature_id    VARCHAR NOT NULL REFERENCES dim_features(feature_id),
    event_date    DATE NOT NULL,
    event_count   INTEGER NOT NULL,
    active_users  INTEGER NOT NULL,
    PRIMARY KEY (customer_id, feature_id, event_date)
);

-- Grain: one row per vehicle x day, from activation until customer churn
CREATE TABLE fact_vehicle_activity (
    vehicle_id     VARCHAR NOT NULL REFERENCES dim_vehicles(vehicle_id),
    activity_date  DATE NOT NULL,
    is_connected   BOOLEAN NOT NULL,
    distance_km    DOUBLE NOT NULL,
    PRIMARY KEY (vehicle_id, activity_date)
);

-- Grain: one row per customer x billing month
CREATE TABLE fact_subscriptions (
    customer_id        VARCHAR NOT NULL REFERENCES dim_customers(customer_id),
    month_start        DATE NOT NULL,
    status             VARCHAR NOT NULL,   -- 'active' | 'churned'
    billable_vehicles  INTEGER NOT NULL,
    mrr_usd            DOUBLE NOT NULL,
    PRIMARY KEY (customer_id, month_start)
);