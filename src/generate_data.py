"""Synthetic data generator for FleetPulse Intelligence (seeded, reproducible)."""
from pathlib import Path
import numpy as np
import pandas as pd

SEED = 42
END = pd.Timestamp("2026-10-31")
N_DAYS = 150
START = END - pd.Timedelta(days=N_DAYS - 1)
DECLINE_START = END - pd.Timedelta(days=29)   # last 30 days
OUT = Path("data/raw")

REGIONS = ["North America", "Europe", "Middle East & Africa", "Asia Pacific"]
INDUSTRIES = ["Logistics", "Construction", "Utilities", "Field Services",
              "Public Sector", "Retail & Distribution"]
TIERS = ["Basic", "Pro", "Enterprise"]
TIER_RANK = {"Basic": 1, "Pro": 2, "Enterprise": 3}
PRICE = {"Basic": 12, "Pro": 20, "Enterprise": 30}          # USD per vehicle per month
VEH_RANGE = {"Basic": (5, 20), "Pro": (15, 45), "Enterprise": (40, 100)}
VEHICLE_TYPES = ["Van", "Light Truck", "Heavy Truck", "Passenger Car"]

# feature_id, name, category, min_tier, adoption_prob, base_daily_rate
FEATURES = [
    ("F01", "Live Map", "Tracking", "Basic", 0.95, 10),
    ("F02", "Trip History", "Tracking", "Basic", 0.85, 6),
    ("F03", "Reports", "Analytics", "Basic", 0.80, 7),
    ("F04", "Dashboards", "Analytics", "Basic", 0.75, 8),
    ("F05", "Driver Safety Scoring", "Safety", "Pro", 0.70, 4),
    ("F06", "Fuel Analytics", "Efficiency", "Pro", 0.60, 3),
    ("F07", "Maintenance Alerts", "Maintenance", "Pro", 0.55, 2),
    ("F08", "API & Marketplace Integrations", "Integrations", "Enterprise", 0.65, 3),
]


def make_customers(rng, n=240):
    tier = rng.choice(TIERS, n, p=[0.5, 0.35, 0.15])
    df = pd.DataFrame({
        "customer_id": [f"C{i:04d}" for i in range(1, n + 1)],
        "industry": rng.choice(INDUSTRIES, n),
        "region": rng.choice(REGIONS, n, p=[0.3, 0.3, 0.2, 0.2]),
        "subscription_tier": tier,
        "signup_date": START - pd.to_timedelta(rng.integers(-60, 540, n), unit="D"),
    })
    # ~7% of established customers churn at some point in the window
    churn_flag = (rng.random(n) < 0.07) & (df.signup_date < START)
    churn_dates = pd.Series(START + pd.to_timedelta(rng.integers(20, N_DAYS - 10, n), unit="D"),
                            index=df.index)
    df["churn_date"] = churn_dates.where(churn_flag)
    # Hidden ground truth for the seeded decline (NOT written to disk)
    df["_dormant"] = (df.region == "Europe") & (df.subscription_tier == "Basic") & (rng.random(n) < 0.15)
    return df


def make_vehicles(rng, cust):
    parts = []
    for c in cust.itertuples():
        lo, hi = VEH_RANGE[c.subscription_tier]
        n = int(rng.integers(lo, hi + 1))
        parts.append(pd.DataFrame({
            "customer_id": c.customer_id,
            "vehicle_type": rng.choice(VEHICLE_TYPES, n, p=[0.4, 0.3, 0.15, 0.15]),
            "activation_date": c.signup_date + pd.to_timedelta(rng.integers(0, 60, n), unit="D"),
        }))
    v = pd.concat(parts, ignore_index=True)
    v.insert(0, "vehicle_id", [f"V{i:05d}" for i in range(1, len(v) + 1)])
    return v


def make_vehicle_activity(rng, veh, cust):
    dates = pd.DataFrame({"activity_date": pd.date_range(START, END)})
    g = veh.merge(dates, how="cross")
    g = g[g.activity_date >= g.activation_date]
    g = g.merge(cust[["customer_id", "churn_date"]], on="customer_id")
    g = g[g.churn_date.isna() | (g.activity_date <= g.churn_date)].copy()
    n = len(g)
    g["is_connected"] = rng.random(n) < 0.93
    km = rng.gamma(4, 25, n) * np.where(g.activity_date.dt.dayofweek >= 5, 0.4, 1.0)
    g["distance_km"] = np.where(g.is_connected, km.round(1), 0.0)
    return g[["vehicle_id", "activity_date", "is_connected", "distance_km"]]


def make_events(rng, cust, veh):
    feats = pd.DataFrame(FEATURES, columns=["feature_id", "feature_name", "category",
                                            "min_tier", "adoption_prob", "base_rate"])
    feats["min_rank"] = feats.min_tier.map(TIER_RANK)
    n_veh = veh.groupby("customer_id").size().rename("n_vehicles").reset_index()
    c = cust.assign(tier_rank=cust.subscription_tier.map(TIER_RANK)).merge(n_veh, on="customer_id")

    pairs = c.merge(feats, how="cross")
    pairs = pairs[pairs.tier_rank >= pairs.min_rank]
    pairs = pairs[rng.random(len(pairs)) < pairs.adoption_prob.values].copy()   # adopters only
    pairs["first_use"] = pairs.signup_date + pd.to_timedelta(rng.integers(0, 45, len(pairs)), unit="D")

    g = pairs.merge(pd.DataFrame({"event_date": pd.date_range(START, END)}), how="cross")
    g = g[(g.event_date >= g.first_use) & (g.churn_date.isna() | (g.event_date <= g.churn_date))].copy()

    lam = (g.base_rate * np.sqrt(g.n_vehicles) / 3).to_numpy()
    lam = lam * np.where(g.event_date.dt.dayofweek >= 5, 0.35, 1.0)

    in_decline = (g.event_date >= DECLINE_START).to_numpy()
    eu_segment = ((g.region == "Europe") & g.subscription_tier.isin(["Basic", "Pro"])).to_numpy()
    hit = in_decline & eu_segment & g.feature_id.isin(["F03", "F04"]).to_numpy()
    lam = np.where(hit, lam * 0.5, lam)
    dormant = (g.event_date >= DECLINE_START + pd.Timedelta(days=5)).to_numpy() & g["_dormant"].to_numpy()
    lam = np.where(dormant, lam * 0.05, lam)

    g["event_count"] = rng.poisson(lam)
    g["active_users"] = np.minimum(g.event_count, 1 + rng.poisson(np.sqrt(g.n_vehicles) / 2))
    g = g[g.event_count > 0]
    return g[["customer_id", "feature_id", "event_date", "event_count", "active_users"]], \
        feats[["feature_id", "feature_name", "category", "min_tier"]]


def make_subscriptions(cust, veh):
    months = pd.date_range(START.replace(day=1), END, freq="MS")
    acts = {k: g.activation_date.to_numpy() for k, g in veh.groupby("customer_id")}
    rows = []
    for c in cust.itertuples():
        for m in months:
            m_end = m + pd.offsets.MonthEnd(0)
            if c.signup_date > m_end:
                continue
            if pd.notna(c.churn_date) and c.churn_date < m:
                continue
            churned = pd.notna(c.churn_date) and c.churn_date <= m_end
            cutoff = min(m_end, END).to_datetime64()
            n_veh = int((acts[c.customer_id] <= cutoff).sum())
            rows.append({
                "customer_id": c.customer_id,
                "month_start": m,
                "status": "churned" if churned else "active",
                "billable_vehicles": n_veh,
                "mrr_usd": 0 if churned else n_veh * PRICE[c.subscription_tier],
            })
    return pd.DataFrame(rows)


def main():
    rng = np.random.default_rng(SEED)
    OUT.mkdir(parents=True, exist_ok=True)

    cust = make_customers(rng)
    veh = make_vehicles(rng, cust)
    activity = make_vehicle_activity(rng, veh, cust)
    events, features = make_events(rng, cust, veh)
    subs = make_subscriptions(cust, veh)

    cust.drop(columns=["_dormant"]).to_csv(OUT / "customers.csv", index=False)
    veh.to_csv(OUT / "vehicles.csv", index=False)
    activity.to_csv(OUT / "vehicle_activity.csv", index=False)
    events.to_csv(OUT / "platform_events.csv", index=False)
    features.to_csv(OUT / "features.csv", index=False)
    subs.to_csv(OUT / "subscriptions.csv", index=False)

    # Sanity check: did the decline get seeded?
    daily = events.groupby("event_date").event_count.sum()
    cur = daily[daily.index >= DECLINE_START].sum()
    prev = daily[(daily.index < DECLINE_START) & (daily.index >= DECLINE_START - pd.Timedelta(days=30))].sum()
    print(f"customers={len(cust)} vehicles={len(veh)} activity_rows={len(activity):,} "
          f"event_rows={len(events):,} sub_rows={len(subs)}")
    print(f"Events last 30d: {cur:,} | prior 30d: {prev:,} | change: {cur / prev - 1:+.1%}")


if __name__ == "__main__":
    main()