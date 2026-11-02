"""FleetPulse Intelligence dashboard. Reads from the DuckDB marts only."""
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "warehouse" / "fleetpulse.duckdb"

st.set_page_config(page_title="FleetPulse Intelligence", layout="wide")

if not DB.exists():
    from src import build
    with st.spinner("First run: building the warehouse..."):
        build.main()


@st.cache_data(show_spinner=False)
def q(sql: str) -> pd.DataFrame:
    con = duckdb.connect(str(DB), read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


STABLE = "customer_id IN (SELECT customer_id FROM stable_customers)"

SEG_SQL = f"""
SELECT region || ' ' || subscription_tier AS segment,
       100.0 * (SUM(event_count) FILTER (WHERE period = 'current') * 1.0
              / SUM(event_count) FILTER (WHERE period = 'prior') - 1) AS pct_change
FROM events_tagged WHERE {STABLE}
GROUP BY region, subscription_tier ORDER BY pct_change"""

FEAT_SQL = f"""
SELECT feature_name,
       CASE WHEN region = 'Europe' AND subscription_tier IN ('Basic', 'Pro')
            THEN 'Europe Basic/Pro' ELSE 'Rest of base' END AS segment,
       100.0 * (SUM(event_count) FILTER (WHERE period = 'current') * 1.0
              / SUM(event_count) FILTER (WHERE period = 'prior') - 1) AS pct_change
FROM events_tagged WHERE {STABLE}
GROUP BY 1, 2 ORDER BY 1, 2"""

TREND_SQL = f"""
WITH b AS (SELECT MAX(event_date) AS end_d FROM fact_platform_events)
SELECT b.end_d - CAST(7 * ((b.end_d - e.event_date) // 7) AS INTEGER) AS block_end,
       CASE WHEN c.region = 'Europe' AND c.subscription_tier IN ('Basic', 'Pro')
            THEN 'Europe Basic/Pro' ELSE 'Rest of base' END AS segment,
       SUM(e.event_count) AS events
FROM fact_platform_events e CROSS JOIN b
JOIN dim_customers c ON c.customer_id = e.customer_id
WHERE e.feature_id IN ('F03', 'F04')
  AND e.{STABLE}
  AND e.event_date > b.end_d - 70
GROUP BY 1, 2 ORDER BY 1, 2"""

RISK_SQL = f"""
WITH cust AS (
  SELECT customer_id,
         CASE WHEN region = 'Europe' AND subscription_tier = 'Basic' THEN 'Europe Basic'
              WHEN region = 'Europe' AND subscription_tier = 'Pro' THEN 'Europe Pro'
              ELSE 'Rest of base' END AS segment,
         COALESCE(SUM(event_count) FILTER (WHERE period = 'prior'), 0) AS prior_e,
         COALESCE(SUM(event_count) FILTER (WHERE period = 'current'), 0) AS cur_e
  FROM events_tagged WHERE {STABLE}
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
GROUP BY 1, 2 ORDER BY 1, 2"""

st.title("FleetPulse Intelligence")
st.caption("Connected fleet product analytics. Synthetic data only. "
           "Independent prototype, not affiliated with Geotab.")
tab1, tab2, tab3, tab4 = st.tabs(["Executive overview", "Engagement", "Investigation", "Data quality"])

# ---------------------------------------------------------------- overview
with tab1:
    rev = q("SELECT * FROM mart_revenue_performance ORDER BY month_start")
    ret = q("SELECT * FROM mart_customer_retention ORDER BY month_start")
    ev = q("""SELECT period, SUM(event_count) AS events, COUNT(DISTINCT customer_id) AS customers
              FROM events_tagged GROUP BY 1""").set_index("period")
    cur, prev = ev.loc["current"], ev.loc["prior"]
    last, before = rev.iloc[-1], rev.iloc[-2]
    mon = last.month_start.strftime("%b")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Events, last 30 days", f"{cur.events:,.0f}", f"{cur.events / prev.events - 1:+.1%}")
    c2.metric("Active customers, 30d", f"{cur.customers:,.0f}", f"{cur.customers - prev.customers:+,.0f}")
    epc_c, epc_p = cur.events / cur.customers, prev.events / prev.customers
    c3.metric("Events per active customer", f"{epc_c:,.0f}", f"{epc_c / epc_p - 1:+.1%}")
    c4.metric(f"MRR ({mon})", f"${last.mrr:,.0f}", f"{last.mrr / before.mrr - 1:+.1%}")
    c5.metric(f"Logo retention ({mon})", f"{ret.iloc[-1].logo_retention_pct:.1f}%")

    a, b = st.columns(2)
    a.plotly_chart(px.line(rev, x="month_start", y="mrr", markers=True, title="MRR (USD)"), width="stretch")
    b.plotly_chart(px.line(ret, x="month_start", y="logo_retention_pct", markers=True,
                           title="Monthly logo retention (%)"), width="stretch")
    st.caption("Event change includes a calendar effect of roughly 2 points (one fewer weekday in the "
               "current window). MRR falls because the synthetic base has no new-customer acquisition "
               "after July, so don't read it as a market signal.")

# -------------------------------------------------------------- engagement
with tab2:
    eng = q("SELECT * FROM mart_customer_engagement WHERE full_30d_window ORDER BY report_date")
    eng["events_7d_avg"] = eng.events.rolling(7).mean()
    st.plotly_chart(px.line(eng, x="report_date", y=["events", "events_7d_avg"],
                            title="Daily platform events (weekends are naturally low)"), width="stretch")
    fig = px.line(eng, x="report_date", y=["dau", "wau", "mau"], title="Active customers: DAU, WAU, MAU")
    fig.update_yaxes(rangemode="tozero")
    st.plotly_chart(fig, width="stretch")
    st.info("Customer counts are flat. The decline is in how much each customer does, not in how many "
            "customers show up, so logins and adoption alone would not catch it.")

# ----------------------------------------------------------- investigation
with tab3:
    seg = q(SEG_SQL)
    fig = px.bar(seg, x="pct_change", y="segment", orientation="h",
                 title="Event change by segment, current vs prior 30 days (stable customers)")
    fig.update_yaxes(autorange="reversed")
    st.plotly_chart(fig, width="stretch")

    l, r = st.columns(2)
    l.plotly_chart(px.bar(q(FEAT_SQL), x="feature_name", y="pct_change", color="segment", barmode="group",
                          title="Event change by feature (%)"), width="stretch")
    trend = q(TREND_SQL)
    trend["index"] = trend.groupby("segment").events.transform(lambda s: 100 * s / s.iloc[0])
    r.plotly_chart(px.line(trend, x="block_end", y="index", color="segment", markers=True,
                           title="Reports + Dashboards, weekly index (first week = 100)"), width="stretch")

    st.subheader("Depth vs breadth by feature")
    fp = q("SELECT * FROM mart_feature_performance")
    pc, pp = fp[fp.period == "current"].set_index("feature_name"), fp[fp.period == "prior"].set_index("feature_name")
    depth = pd.DataFrame({
        "Adoption prior %": pp.adoption_rate_pct, "Adoption current %": pc.adoption_rate_pct,
        "Events/adopter prior": pp.events_per_adopter, "Events/adopter current": pc.events_per_adopter})
    depth["Depth change %"] = ((depth["Events/adopter current"] / depth["Events/adopter prior"] - 1) * 100).round(1)
    st.dataframe(depth.sort_values("Depth change %"), width="stretch")

    st.subheader("Customers by usage change, with MRR exposure")
    st.dataframe(q(RISK_SQL), width="stretch", hide_index=True)

    worst = seg.iloc[0]
    st.subheader("Findings and recommendations")
    st.markdown(f"""
**Finding.** The largest like-for-like drop is **{worst["segment"]} ({worst["pct_change"]:.1f}%)**. Other segments
move within a few points, mostly calendar effect. The drop is concentrated in Reports and Dashboards, begins
as a step change in early October, and shows up as lower usage depth rather than fewer customers.

**Recommended next steps**
1. Find the trigger for the early-October step change in Europe: release notes, packaging or pricing changes, data pipeline changes, regional outages.
2. Review support tickets and customer feedback for Reports and Dashboards in Europe for the same dates.
3. Contact the sharply reduced accounts first. They are the nearest churn risk.
4. Add events per active customer to the executive KPIs, because customer counts missed this.
5. Track the segment weekly. Treat the finding as indicative, since Europe Basic is about 35 customers.
""")

# ------------------------------------------------------------ data quality
with tab4:
    from src.quality import run_checks
    results, latest = run_checks()
    dq = pd.DataFrame(results)
    dq["status"] = dq.violations.map(lambda v: "PASS" if v == 0 else "FAIL")
    st.dataframe(dq, width="stretch", hide_index=True)
    st.caption(f"Latest event date: {latest}. Checks pass largely by construction on synthetic data. "
               "They are written to catch violations on real feeds.")