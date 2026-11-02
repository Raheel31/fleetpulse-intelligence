"""FleetPulse Intelligence dashboard. Reads from the DuckDB marts only."""
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "warehouse" / "fleetpulse.duckdb"

st.set_page_config(page_title="FleetPulse Intelligence", page_icon="🚚", layout="wide")
st.markdown("""<style>
.block-container {padding-top: 2.5rem; max-width: 1400px;}
footer {visibility: hidden;}
div[data-testid="stMetricValue"] {font-size: 2rem;}
</style>""", unsafe_allow_html=True)

if not DB.exists():
    from src import build
    with st.spinner("First run: building the warehouse..."):
        build.main()


@st.cache_data(show_spinner=False, ttl=600)
def q(sql: str) -> pd.DataFrame:
    con = duckdb.connect(str(DB), read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


# ------------------------------------------------------------------ SQL
STABLE = "customer_id IN (SELECT customer_id FROM stable_customers)"

SEG_SQL = f"""
SELECT region || ' ' || subscription_tier AS segment,
       SUM(event_count) FILTER (WHERE period = 'prior') AS prior_events,
       SUM(event_count) FILTER (WHERE period = 'current') AS current_events,
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

WEEKLY_SQL = """
WITH b AS (SELECT MAX(event_date) AS end_d FROM fact_platform_events)
SELECT b.end_d - CAST(7 * ((b.end_d - e.event_date) // 7) AS INTEGER) AS week_end,
       SUM(e.event_count) AS events
FROM fact_platform_events e CROSS JOIN b
WHERE e.event_date > b.end_d - 70
GROUP BY 1 ORDER BY 1"""

STABLE_TOTAL_SQL = f"""
SELECT period, SUM(event_count) AS events
FROM events_tagged WHERE {STABLE} GROUP BY 1"""

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

# One row per like-for-like customer: Reports + Dashboards events in each period
FUNNEL_SQL = """
WITH cust AS (
  SELECT customer_id,
         CASE WHEN region = 'Europe' AND subscription_tier IN ('Basic', 'Pro')
              THEN 'Europe Basic/Pro' ELSE 'Rest of base' END AS segment
  FROM stable_customers),
u AS (
  SELECT customer_id,
         SUM(event_count) FILTER (WHERE period = 'prior') AS prior_e,
         SUM(event_count) FILTER (WHERE period = 'current') AS cur_e
  FROM events_tagged WHERE feature_id IN ('F03', 'F04')
  GROUP BY 1)
SELECT cust.customer_id, cust.segment,
       COALESCE(u.prior_e, 0) AS prior_e, COALESCE(u.cur_e, 0) AS cur_e
FROM cust LEFT JOIN u ON u.customer_id = cust.customer_id"""

# -------------------------------------------------------------- helpers
# Geotab-style approximation, not official brand codes. Swap these to match geotab.com exactly.
PRIMARY = "#2E9BEA"   # bright blue accent
ACCENT = "#7CC8FF"    # lighter blue
ALERT = "#FF6B5B"     # problem highlight
NEUTRAL = "#6F85A0"   # everything that is fine
INK = "#E8EEF5"       # text on dark
GRID = "#1E3350"      # gridlines


def fmt(d):
    return f"{d.day} {d:%b}"


def join_names(xs):
    xs = list(xs)
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    return ", ".join(xs[:-1]) + " and " + xs[-1]


def style(fig, height=340, legend=False):
    fig.update_layout(template="plotly_dark", height=height, showlegend=legend,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(size=13, color=INK),
                      margin=dict(l=10, r=10, t=20, b=10),
                      legend=dict(orientation="h", y=1.08, x=0, title=None))
    fig.update_xaxes(showgrid=False, title=None, linecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, title=None)
    return fig


def show(fig):
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def section(title, takeaway=None):
    st.markdown(f"#### {title}")
    if takeaway:
        st.caption(takeaway)


def mark_start(fig, d):
    x = pd.Timestamp(d).strftime("%Y-%m-%d")
    fig.add_shape(type="line", x0=x, x1=x, y0=0, y1=1, yref="paper",
                  line=dict(color=ALERT, dash="dash", width=1.5))
    fig.add_annotation(x=x, y=1, yref="paper", text=f"Decline starts {fmt(pd.Timestamp(d))}",
                       showarrow=False, xanchor="left", font=dict(color=ALERT, size=12))


def tag(fig, x, y, text, color=INK, yshift=16, xanchor="center"):
    fig.add_annotation(x=x, y=y, text=text, showarrow=False, yshift=yshift,
                       xanchor=xanchor, font=dict(color=color, size=13))


# ----------------------------------------------------------------- data
seg, feat, trend = q(SEG_SQL), q(FEAT_SQL), q(TREND_SQL)
seg[["prior_events", "current_events"]] = seg[["prior_events", "current_events"]].astype(float)
rev = q("SELECT * FROM mart_revenue_performance ORDER BY month_start")
ret = q("SELECT * FROM mart_customer_retention ORDER BY month_start")
ev = q("""SELECT period, SUM(event_count) AS events, COUNT(DISTINCT customer_id) AS customers
          FROM events_tagged GROUP BY 1""").set_index("period")
cur, prev = ev.loc["current"], ev.loc["prior"]
stable = q(STABLE_TOTAL_SQL).set_index("period")["events"].astype(float)
stable_chg = stable["current"] / stable["prior"] - 1

per = q("SELECT * FROM ref_periods").set_index("period")
cur_start, cur_end = pd.Timestamp(per.loc["current", "start_d"]), pd.Timestamp(per.loc["current", "end_d"])
prev_start, prev_end = pd.Timestamp(per.loc["prior", "start_d"]), pd.Timestamp(per.loc["prior", "end_d"])
start_d = cur_start

overall = cur.events / prev.events - 1
epc_c, epc_p = cur.events / cur.customers, prev.events / prev.customers
epc_chg = epc_c / epc_p - 1

worst = seg.iloc[0]
material = seg[seg["pct_change"] < -10]
drop_total = (seg["current_events"] - seg["prior_events"]).sum()
drop_material = (material["current_events"] - material["prior_events"]).sum()
share = drop_material / drop_total if drop_total else 0.0
mat_names = join_names(material["segment"]) or worst["segment"]

eu_feat = feat[feat["segment"] == "Europe Basic/Pro"].nsmallest(2, "pct_change")
feat_names = join_names(eu_feat["feature_name"])

risk = q(RISK_SQL)
risk["usage_bucket"] = risk["usage_bucket"].str[2:]
exposed = risk[risk["usage_bucket"].str.startswith(("sharply", "reduced"))]
n_risk = int(exposed["customers"].sum())
mrr_risk = float(exposed["latest_mrr_usd"].sum())

last, before = rev.iloc[-1], rev.iloc[-2]
mon = last.month_start.strftime("%b")

# ----------------------------------------------------------------- page
st.title("FleetPulse Intelligence")
st.caption("Connected fleet product analytics · Synthetic data only · "
           "Independent prototype, not affiliated with Geotab")
st.caption(f"Reporting period: {fmt(cur_start)} to {fmt(cur_end)} {cur_end:%Y} "
           f"vs {fmt(prev_start)} to {fmt(prev_end)} · Data through {fmt(cur_end)} {cur_end:%Y}")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["Summary", "Engagement", "Root cause", "Funnel", "Retention & cohorts", "Data quality"])

# ---------------------------------------------------------------- summary
with tab1:
    with st.container(border=True):
        st.markdown(f"""#### The headline
Platform usage is **{overall:+.1%}** vs the prior 30 days (**{stable_chg:+.1%}** among accounts present in
both periods). **{share:.0%}** of that like-for-like drop comes from **{mat_names}**, driven by
**{feat_names}**, and it began on **{fmt(start_d)}**. Customers still log in ({cur.customers:.0f} active
vs {prev.customers:.0f}), but each one is doing **{abs(epc_chg):.0%} less**.""")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Platform events, 30d", f"{cur.events:,.0f}", f"{overall:+.1%}",
              help="Total feature interactions in the last 30 days vs the 30 days before.")
    c2.metric("Active customers, 30d", f"{cur.customers:,.0f}", f"{cur.customers - prev.customers:+,.0f}",
              help="Customers with at least one event in the period.")
    c3.metric("Events per customer", f"{epc_c:,.0f}", f"{epc_chg:+.1%}",
              help="Depth of usage. Customer counts alone miss this decline.")
    c4.metric(f"MRR ({mon})", f"${last.mrr:,.0f}", f"{last.mrr / before.mrr - 1:+.1%}",
              help="Monthly recurring revenue: billable vehicles x tier price.")
    c5.metric(f"MRR at risk ({n_risk} accounts)", f"${mrr_risk:,.0f}",
              help="Latest-month MRR of customers whose usage fell below 70% of the prior period.")

    st.write("")
    a, b = st.columns(2)
    wk = q(WEEKLY_SQL)
    wk["week_end"] = pd.to_datetime(wk["week_end"])
    wk["phase"] = (wk["week_end"] - pd.Timedelta(days=6) >= start_d).map(
        {True: "After decline start", False: "Before"})
    with a:
        section("Weekly platform events fell about 10% after early October",
                "All customers, weeks ending on the date shown.")
        f = px.bar(wk, x="week_end", y="events", color="phase",
                   color_discrete_map={"Before": PRIMARY, "After decline start": ALERT})
        f.update_traces(texttemplate="%{y:,.0f}", textposition="outside", cliponaxis=False)
        f.update_xaxes(tickvals=wk["week_end"].tolist(), tickformat="%d %b")
        f.update_yaxes(visible=False, range=[0, wk["events"].max() * 1.15])
        show(style(f, 330))
    with b:
        section("Monthly recurring revenue is drifting down",
                "USD. The base has no new-customer acquisition after July, so this is not a market signal.")
        f = px.bar(rev, x="month_start", y="mrr")
        f.update_traces(texttemplate="$%{y:,.0f}", textposition="outside",
                        marker_color=PRIMARY, cliponaxis=False)
        f.update_xaxes(tickvals=rev["month_start"].tolist(), tickformat="%b")
        f.update_yaxes(visible=False, range=[0, rev["mrr"].max() * 1.15])
        show(style(f, 330))

    st.caption("Headline change includes a calendar effect of roughly 2 points: the current window has one "
               "fewer weekday than the prior one.")

    with st.expander("How to read these numbers"):
        st.markdown("""
- **Event**: one interaction with a platform feature, such as opening a report.
- **Active customer**: a customer with at least one event in the period.
- **Events per customer**: how much each customer does. A fall here means shallower use.
- **Like-for-like**: only customers present throughout both periods, so churned or new accounts do not distort comparisons.
- **MRR**: billable vehicles x price per vehicle for the customer's tier.
- **MRR at risk**: monthly revenue from customers whose usage fell below 70% of the prior period.
""")

# -------------------------------------------------------------- engagement
with tab2:
    eng = q("SELECT * FROM mart_customer_engagement WHERE full_30d_window ORDER BY report_date")
    eng["report_date"] = pd.to_datetime(eng["report_date"])
    eng["events"] = eng["events"].astype(float)
    eng["events_7d"] = eng["events"].rolling(7).mean()
    eng["epc_7d"] = eng["events"].rolling(7).sum() / eng["dau"].rolling(7).sum()
    eng = eng.dropna(subset=["events_7d"]).reset_index(drop=True)
    pre, post = eng[eng["report_date"] < start_d].iloc[-1], eng.iloc[-1]

    section("Platform events are down, even though the customer count is flat",
            "Daily events, 7-day average (smooths the weekday/weekend pattern). Axis is zoomed to show the change.")
    f = px.line(eng, x="report_date", y="events_7d")
    f.update_traces(line=dict(color=PRIMARY, width=3))
    f.update_yaxes(range=[eng["events_7d"].min() * 0.9, eng["events_7d"].max() * 1.07], tickformat=",")
    tag(f, pre["report_date"], pre["events_7d"], f"{pre['events_7d']:,.0f} per day", yshift=20)
    tag(f, post["report_date"], post["events_7d"], f"{post['events_7d']:,.0f} per day",
        yshift=-22, xanchor="right")
    mark_start(f, start_d)
    show(style(f, 340))

    l, r = st.columns(2)
    with l:
        section("Active customers are flat", "Customers with activity in the last 7 days.")
        f = px.line(eng, x="report_date", y="wau")
        f.update_traces(line=dict(color=PRIMARY, width=3))
        f.update_yaxes(range=[0, eng["wau"].max() * 1.25])
        tag(f, post["report_date"], post["wau"], f"{post['wau']:.0f} customers", yshift=16, xanchor="right")
        mark_start(f, start_d)
        show(style(f, 300))
    with r:
        section("Each customer is doing less", "Events per active customer per day, 7-day average.")
        f = px.line(eng, x="report_date", y="epc_7d")
        f.update_traces(line=dict(color=ALERT, width=3))
        f.update_yaxes(range=[eng["epc_7d"].min() * 0.9, eng["epc_7d"].max() * 1.08])
        tag(f, pre["report_date"], pre["epc_7d"], f"{pre['epc_7d']:.1f}", yshift=18)
        tag(f, post["report_date"], post["epc_7d"], f"{post['epc_7d']:.1f}", yshift=-20, xanchor="right")
        mark_start(f, start_d)
        show(style(f, 300))

    st.info("Usage fell but the number of active customers did not. The problem is shallower use, "
            "not lost logins, so login-based metrics would not have flagged it.")

# ------------------------------------------------------------- root cause
with tab3:
    st.caption("Four questions, in order: where, what, when, who. Like-for-like customers only.")
    l, r = st.columns(2)

    with l:
        section(f"1. Where: {mat_names} are the outliers",
                "Fall in events, current vs prior 30 days. Grey segments are within normal range.")
        seg["drop"] = -seg["pct_change"]
        seg["label"] = seg["pct_change"].map(lambda v: f"{v:.1f}%")
        seg["status"] = seg["pct_change"].map(lambda v: "Material decline" if v < -10 else "Normal range")
        f = px.bar(seg, x="drop", y="segment", orientation="h", color="status", text="label",
                   color_discrete_map={"Material decline": ALERT, "Normal range": NEUTRAL})
        f.update_traces(textposition="outside", cliponaxis=False)
        f.update_yaxes(autorange="reversed")
        f.update_xaxes(visible=False, range=[min(0, seg["drop"].min() * 1.2), seg["drop"].max() * 1.25])
        show(style(f, 440))

    with r:
        section(f"2. What: {feat_names} collapsed",
                "Fall in events by feature. API & Marketplace is Enterprise-only, so it has no Basic/Pro bars.")
        eu = feat[feat["segment"] == "Europe Basic/Pro"].sort_values("pct_change")
        order = list(eu["feature_name"]) + [x for x in feat["feature_name"].unique()
                                            if x not in set(eu["feature_name"])]
        ft = feat.copy()
        ft["drop"] = -ft["pct_change"]
        ft["label"] = ft["pct_change"].map(lambda v: f"{v:.0f}%")
        f = px.bar(ft, x="drop", y="feature_name", color="segment", barmode="group", orientation="h",
                   text="label", color_discrete_map={"Europe Basic/Pro": ALERT, "Rest of base": NEUTRAL})
        f.update_traces(textposition="outside", cliponaxis=False)
        f.update_yaxes(categoryorder="array", categoryarray=order, autorange="reversed")
        f.update_xaxes(visible=False, range=[min(0, ft["drop"].min() * 1.2), ft["drop"].max() * 1.2])
        show(style(f, 440, legend=True))

    l, r = st.columns(2)
    with l:
        section("3. When: a sudden drop, not a slow slide",
                f"{feat_names}, weekly index (first week = 100). A step change on one date points to a trigger.")
        trend["block_end"] = pd.to_datetime(trend["block_end"])
        trend["usage_index"] = trend.groupby("segment")["events"].transform(
            lambda s: 100 * s.astype(float) / float(s.iloc[0]))
        f = go.Figure()
        for name, color, labelled in [("Europe Basic/Pro", ALERT, True), ("Rest of base", NEUTRAL, False)]:
            d = trend[trend["segment"] == name]
            vals = d["usage_index"].tolist()
            if labelled:
                text = [f"{v:.0f}" for v in vals]
                pos = ["top center" if v >= 85 else "bottom center" for v in vals]
            else:
                text = [""] * (len(vals) - 1) + [f"{vals[-1]:.0f}"]
                pos = "top center"
            f.add_trace(go.Scatter(x=d["block_end"], y=d["usage_index"], mode="lines+markers+text",
                                   name=name, text=text, textposition=pos,
                                   line=dict(color=color, width=3), marker=dict(size=7)))
        f.update_xaxes(tickvals=sorted(trend["block_end"].unique()), tickformat="%d %b")
        f.update_yaxes(visible=False, range=[0, 125])
        mark_start(f, start_d)
        show(style(f, 380, legend=True))

    with r:
        section("4. Who: accounts at risk",
                "Customers grouped by how their usage changed vs the prior period.")
        st.markdown(f"**{n_risk} accounts** have materially lower usage, representing "
                    f"**\\${mrr_risk:,.0f} of monthly revenue**. No revenue has been lost yet, "
                    "so this is an early warning.")
        risk_view = risk.rename(columns={"segment": "Segment", "usage_bucket": "Usage vs prior period",
                                         "customers": "Customers", "latest_mrr_usd": "MRR (USD)"})
        st.dataframe(risk_view, width="stretch", hide_index=True,
                     column_config={"MRR (USD)": st.column_config.NumberColumn(format="$%d")})

    st.markdown("#### Recommended actions")
    st.markdown(f"""
| Priority | Action | Suggested owner |
|---|---|---|
| 1 | Find the trigger for {fmt(start_d)}: release notes, packaging or pricing changes, regional data or pipeline issues | Product and Engineering |
| 2 | Review support tickets and feedback on {feat_names} in Europe for the same dates | Customer Support |
| 3 | Contact the {n_risk} at-risk accounts, starting with the sharply reduced ones | Customer Success |
| 4 | Add events per customer to the executive KPI set, because customer counts missed this | Product Analytics |
| 5 | Track the segment weekly and confirm recovery once the trigger is fixed | Product Analytics |
""")

    with st.expander("Confidence and caveats"):
        st.markdown("""
- **Small segments.** Europe Basic is about 35 customers and the sharply reduced group is a handful of accounts. Treat the findings as indicative.
- **Calendar effect.** The current 30-day window has one fewer weekday than the prior one, worth roughly 2 points of the headline. Weekly blocks (chart 3) are unaffected.
- **Churn is separated out.** Charts 1 to 4 use customers present throughout both periods. Churned accounts explain a minority of the headline drop.
- **Synthetic data.** The findings illustrate the method, not real customer behaviour.
""")

    with st.expander("Depth vs breadth by feature"):
        fp = q("SELECT * FROM mart_feature_performance")
        pc = fp[fp["period"] == "current"].set_index("feature_name")
        pp = fp[fp["period"] == "prior"].set_index("feature_name")
        depth = pd.DataFrame({
            "Adoption before %": pp["adoption_rate_pct"], "Adoption now %": pc["adoption_rate_pct"],
            "Events per user before": pp["events_per_adopter"], "Events per user now": pc["events_per_adopter"]})
        depth["Depth change %"] = ((depth["Events per user now"] / depth["Events per user before"] - 1) * 100).round(1)
        st.dataframe(depth.sort_values("Depth change %"), width="stretch")

# ----------------------------------------------------------------- funnel
with tab4:
    fn = q(FUNNEL_SQL)
    fn[["prior_e", "cur_e"]] = fn[["prior_e", "cur_e"]].astype(float)

    STAGES = ["All customers", "Used analytics before", "Still using now", "At 70%+ of prior volume"]
    SEGS = {"Europe Basic/Pro": ALERT, "Rest of base": NEUTRAL}

    def funnel_counts(d):
        users = d[d["prior_e"] > 0]
        return [len(d), len(users), int((users["cur_e"] > 0).sum()),
                int((users["cur_e"] >= 0.7 * users["prior_e"]).sum())]

    fc = {s: funnel_counts(fn[fn["segment"] == s]) for s in SEGS}
    eu_c, rest_c = fc["Europe Basic/Pro"], fc["Rest of base"]
    eu_hold, rest_hold = eu_c[3] / eu_c[2], rest_c[3] / rest_c[2]
    eu_stay = eu_c[2] / eu_c[1]

    section("Analytics usage funnel: where customers fall out of healthy usage",
            "Same like-for-like customers followed through four stages. Analytics = Reports and Dashboards. "
            "'At 70%+' means their last-30-day events are at least 70% of their prior-30-day events.")
    cols = st.columns(2)
    for col, (name, color) in zip(cols, SEGS.items()):
        with col:
            st.markdown(f"**{name}**")
            f = go.Figure(go.Funnel(y=STAGES, x=fc[name], textposition="inside",
                                    textinfo="value+percent initial", marker=dict(color=color),
                                    connector=dict(line=dict(color=GRID))))
            show(style(f, 340))

    st.info(f"In Europe Basic/Pro, {eu_stay:.0%} of earlier analytics users are still using them, "
            f"but only {eu_hold:.0%} of those are holding 70% or more of their earlier volume, "
            f"vs {rest_hold:.0%} in the rest of the base. Customers have cut back, not left, "
            "so the loss is in intensity rather than abandonment.")

    section("Step conversion", "Share of customers who make it from one stage to the next.")
    rows = []
    for i, stage in enumerate(STAGES):
        row = {"Stage": stage}
        for name in SEGS:
            row[f"{name}: customers"] = fc[name][i]
            row[f"{name}: % of previous step"] = (100.0 * fc[name][i] / fc[name][i - 1]) if i else None
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 column_config={c: st.column_config.NumberColumn(format="%.0f%%")
                                for c in [f"{n}: % of previous step" for n in SEGS]})

    h = fn[fn["prior_e"] > 0].copy()
    h["ratio"] = (h["cur_e"] / h["prior_e"]).clip(upper=1.6)
    med = h.groupby("segment")["ratio"].median()
    eu_share_below = (h[h["segment"] == "Europe Basic/Pro"]["ratio"] < 0.7).mean()

    section("Deep dive: is it a few accounts or the whole segment?",
            "Each customer's analytics volume now vs before. 100% = unchanged. "
            "Bars show the share of customers in each segment, so the groups compare fairly.")
    f = px.histogram(h, x="ratio", color="segment", nbins=32, histnorm="percent", barmode="overlay",
                     opacity=0.8, color_discrete_map=SEGS)
    f.update_xaxes(tickformat=".0%", range=[0, 1.6])
    f.update_yaxes(ticksuffix="%")
    f.add_vline(x=1.0, line_dash="dot", line_color=INK,
                annotation_text="No change", annotation_font_color=INK)
    f.add_vline(x=0.7, line_dash="dash", line_color=ACCENT,
                annotation_text="70% threshold", annotation_font_color=ACCENT, annotation_position="bottom left")
    show(style(f, 360, legend=True))
    st.markdown(
        f"The typical Europe Basic/Pro customer is at **{med['Europe Basic/Pro']:.0%}** of their earlier "
        f"analytics volume, against **{med['Rest of base']:.0%}** elsewhere, and "
        f"**{eu_share_below:.0%}** of them are below the 70% line. The whole segment shifted together, "
        "which points to a product or packaging change rather than a handful of unhappy accounts.")
    st.caption("Small segments: Europe Basic/Pro is about 50 customers. Treat percentages as indicative.")

# --------------------------------------------------------------- retention
with tab5:
    co = q("SELECT * FROM mart_cohort_retention")
    tiers = [t for t in ["Basic", "Pro", "Enterprise"] if t in set(co["cohort"])]
    sizes = co.drop_duplicates("cohort").set_index("cohort")["cohort_size"]
    piv = co.pivot(index="cohort", columns="months_since_start", values="retention_pct").reindex(tiers)
    first_month = pd.Timestamp(rev["month_start"].iloc[0])
    piv.columns = [f"Month {int(c)}<br>({(first_month + pd.DateOffset(months=int(c))):%b})" for c in piv.columns]
    piv.index = [f"{t} (n={int(sizes[t])})" for t in tiers]
    final = piv.iloc[:, -1]

    l, r = st.columns([3, 2])
    with l:
        section(f"Cohort retention: by the end of the window, {final.min():.0f}% to {final.max():.0f}% of each cohort is still active",
                "Customers active in the first month, by subscription tier. Share still subscribed each month.")
        f = px.imshow(piv, text_auto=".1f", color_continuous_scale=[[0, "#12304F"], [1, PRIMARY]],
                      zmin=75, zmax=100, aspect="auto")
        f.update_coloraxes(showscale=False)
        f.update_xaxes(side="top")
        show(style(f, 280))
    with r:
        section("Month-to-month logo retention", "Share of last month's customers still subscribed.")
        f = px.line(ret, x="month_start", y="logo_retention_pct", markers=True)
        f.update_traces(mode="lines+markers+text", texttemplate="%{y:.1f}%", textposition="top center",
                        line=dict(color=PRIMARY, width=3))
        f.update_xaxes(tickvals=ret["month_start"].tolist(), tickformat="%b")
        f.update_yaxes(visible=False, range=[90, 102])
        show(style(f, 280))

    st.caption("Cohorts are small (n shown in each row label), so differences of a few points between tiers "
               "are not meaningful. Monthly retention improves because churn dates in the synthetic data are "
               "concentrated early in the window, so do not read it as a trend.")

# ------------------------------------------------------------ data quality
with tab6:
    from src.quality import run_checks
    results, latest = run_checks()
    dq = pd.DataFrame(results).rename(columns={"check": "Rule", "violations": "Violations"})
    dq["Status"] = dq["Violations"].map(lambda v: "✅ Pass" if v == 0 else "❌ Fail")

    m1, m2 = st.columns(2)
    m1.metric("Checks passing", f"{(dq['Status'] == '✅ Pass').sum()} of {len(dq)}")
    m2.metric("Latest event date", f"{latest}")
    st.dataframe(dq, width="stretch", hide_index=True, height=35 * (len(dq) + 1) + 3)
    st.caption("Checks pass largely by construction on synthetic data. "
               "They are written to catch violations on real feeds.")

    st.markdown("#### Warehouse tables")
    GRAIN = {
        "dim_date": "Calendar day",
        "dim_customers": "Customer",
        "dim_vehicles": "Vehicle",
        "dim_features": "Platform feature",
        "fact_platform_events": "Customer x feature x day (days with events only)",
        "fact_vehicle_activity": "Vehicle x day",
        "fact_subscriptions": "Customer x billing month",
    }
    inv = pd.DataFrame([{"Table": t, "Grain (one row per)": g,
                         "Rows": f"{int(q(f'SELECT COUNT(*) AS n FROM {t}').iloc[0, 0]):,}"}
                        for t, g in GRAIN.items()])
    st.dataframe(inv, width="stretch", hide_index=True)
    st.caption("Full definitions, keys and assumptions are in docs/data_model.md.")