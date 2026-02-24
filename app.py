import io
import json
import os
import hashlib
from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st

# ---------------- CONFIG ----------------
st.set_page_config(page_title="InventoryFlow (🟢🟡🔴)", layout="wide")

# ---------------- ACCESS GATE ----------------
# Expected in Streamlit Cloud Secrets, e.g.:
# ACCESS_CODES='["brand-7d","brand-14d","brand-30d"]'
if "ACCESS_CODES" not in st.secrets:
    st.error("Missing ACCESS_CODES in Streamlit Secrets.")
    st.info(
        "Go to Streamlit Cloud → Manage app → Settings → Secrets and add:\n\n"
        'ACCESS_CODES=\'["brand-14d","brand-30d"]\''
    )
    st.stop()

ACCESS_CODES = st.secrets["ACCESS_CODES"]

# ---------------- APP TITLE + LOGIN ----------------
st.title("InventoryFlow (Beta)")

code = st.text_input("Enter access code", type="password")

if code not in ACCESS_CODES:
    st.warning("Access denied. Please enter a valid beta code.")
    st.stop()

st.success("Access granted ✅")

# ---------------- EMAIL (FOR BETA FOLLOW-UP) ----------------
st.markdown("### ✅ Beta info")
user_email = st.text_input("Your email (so I can send your beta link + follow up for feedback)", placeholder="you@brand.com")

consent = st.checkbox(
    "I agree InventoryFlow stores only usage metadata (SKU count + 🟢🟡🔴 counts) for product improvement. No CSV is stored.",
    value=True,
)

if not user_email or "@" not in user_email:
    st.info("Enter your email above to continue (used only for beta follow-up).")
    st.stop()

if not consent:
    st.warning("Consent is required for beta access in this build (since we log only metadata).")
    st.stop()

# ---------------- APP HEADER ----------------
st.markdown("## 🟢🟡🔴 InventoryFlow")
st.caption(
    "**Inventory clarity for Shopify brands.** "
    "Upload a CSV or Excel file and get instant 🟢🟡🔴 stock risk signals "
    "based on sales velocity — with optional financial impact (in your currency)."
)

# ✅ Pro CSS: Streamlit metrics + our “big metric cards” (prevents number wrapping)
st.markdown(
    """
<style>
/* Streamlit metrics: no ellipsis, no clipping */
div[data-testid="stMetricValue"]{
    white-space: nowrap !important;
    overflow: visible !important;
    text-overflow: clip !important;
    font-size: 28px !important;
    line-height: 1.15 !important;
}
div[data-testid="stMetric"]{
    min-width: 0 !important;
}

/* “Pro” big-number cards */
.if-card{
    border: 1px solid rgba(49, 51, 63, 0.12);
    border-radius: 12px;
    padding: 12px 14px;
    background: rgba(255,255,255,0.7);
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.if-label{
    font-size: 14px;
    opacity: 0.8;
    margin-bottom: 6px;
    font-weight: 700;
}
.if-value{
    font-size: 28px;
    font-weight: 800;
    line-height: 1.1;

    /* Important: allow wrapping, but not mid-number */
    white-space: normal;
    overflow-wrap: normal;
    word-break: normal;

    font-variant-numeric: tabular-nums;
}
.if-num{ white-space: nowrap; }
.if-unit{ white-space: nowrap; opacity: 0.9; }
.if-sub{
    margin-top: 6px;
    font-size: 12px;
    opacity: 0.75;
    line-height: 1.3;
}
</style>
""",
    unsafe_allow_html=True,
)

# bump when logic changes (forces cache refresh)
CACHE_VERSION = 25  # <- bump due to beta logging + email gate

# ---------------- HELPERS ----------------
def read_file(file) -> pd.DataFrame:
    name = file.name.lower()
    if name.endswith(".csv"):
        raw = file.getvalue()
        try:
            return pd.read_csv(io.BytesIO(raw))
        except Exception:
            return pd.read_csv(io.BytesIO(raw), sep=";")
    elif name.endswith(".xlsx"):
        return pd.read_excel(file)
    else:
        raise ValueError("Unknown file type")


def fmt_kr(x) -> str:
    """Kept for compatibility with your current formatting style.
    If you later want true multi-currency formatting, we can rename this to fmt_money."""
    try:
        v = float(x)
    except Exception:
        return ""
    if not np.isfinite(v):
        return ""
    return f"{v:,.0f}".replace(",", ".") + " kr"


def _safe_num_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("float64")


def short_action(action: str) -> str:
    a = (action or "").lower()
    if "fix data" in a or "correct data" in a:
        return "fix data"
    if "phase-out plan" in a or "phase out" in a or "liquidation" in a:
        return "phase out"
    if "reorder" in a and ("pause" in a or "stop" in a):
        return "pause reorders"
    if "price" in a or "campaign" in a or "bundle" in a or "markdown" in a:
        return "pricing/promo"
    if "stop purchasing" in a or "stop buying" in a:
        return "stop purchasing"
    return "next step"


def df_for_display(df: pd.DataFrame) -> pd.DataFrame:
    disp = df.copy()
    text_cols = ["Status", "Product", "Why", "Recommended action", "Priority (why)"]
    if "Why (details)" in disp.columns:
        text_cols.append("Why (details)")
    for c in text_cols:
        if c in disp.columns:
            disp[c] = disp[c].replace({None: ""}).fillna("")
    return disp


def render_big_metric(label: str, value: str, sub: str = ""):
    v = (value or "").strip()

    num = v
    unit = ""
    if v.endswith(" kr"):
        num = v[:-3].strip()
        unit = "kr"

    sub_html = f"<div class='if-sub'>{sub}</div>" if sub else ""
    unit_html = f" <span class='if-unit'>{unit}</span>" if unit else ""

    st.markdown(
        f"""
<div class="if-card">
  <div class="if-label">{label}</div>
  <div class="if-value"><span class="if-num">{num}</span>{unit_html}</div>
  {sub_html}
</div>
""",
        unsafe_allow_html=True,
    )


def _file_fingerprint(uploaded_file) -> str:
    """Stable fingerprint so we can log only once per upload."""
    raw = uploaded_file.getvalue()
    h = hashlib.md5(raw).hexdigest()
    return f"{uploaded_file.name}:{len(raw)}:{h}"


def log_beta_metadata_once(result_df: pd.DataFrame, email: str, uploaded_file, rules: dict):
    """
    Stores ONLY metadata:
    - timestamp, email
    - file fingerprint (not content)
    - SKU count + 🟢🟡🔴 counts
    - rule settings (green/yellow/deadstock)
    No CSV is stored.
    """
    try:
        fp = _file_fingerprint(uploaded_file)
        if "logged_fingerprints" not in st.session_state:
            st.session_state.logged_fingerprints = set()

        if fp in st.session_state.logged_fingerprints:
            return  # already logged this upload

        s = result_df["Status"].astype(str)
        red = int(s.str.startswith("🔴").sum())
        yellow = int(s.str.startswith("🟡").sum())
        green = int(s.str.startswith("🟢").sum())
        total = int(len(result_df))

        log_row = {
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "email": email.strip().lower(),
            "file_fp": fp,  # fingerprint only
            "total_skus": total,
            "green": green,
            "yellow": yellow,
            "red": red,
            "green_max_days": rules.get("green_max"),
            "yellow_max_days": rules.get("yellow_max"),
            "dead_stock_days": rules.get("dead_stock_days"),
            "details_on": bool(rules.get("show_details")),
        }

        log_df = pd.DataFrame([log_row])
        file_path = "beta_log.csv"

        if os.path.exists(file_path):
            log_df.to_csv(file_path, mode="a", header=False, index=False)
        else:
            log_df.to_csv(file_path, index=False)

        st.session_state.logged_fingerprints.add(fp)
    except Exception:
        # We intentionally fail silently so beta users don't get blocked by logging issues
        pass


# ---------------- COMPUTE ----------------
def _compute_flow_core(
    df: pd.DataFrame,
    green_max_val: int,
    yellow_max_val: int,
    dead_stock_days_val: int,
    show_details_val: bool,
) -> pd.DataFrame:
    required = {"product_name", "stock_qty", "sales_30d"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")

    out = df.copy()

    # Optional unit cost
    has_cost = "unit_cost" in out.columns
    if has_cost:
        out["unit_cost"] = pd.to_numeric(out["unit_cost"], errors="coerce")

    # Parse numerics
    out["stock_qty"] = pd.to_numeric(out["stock_qty"], errors="coerce")
    out["sales_30d"] = pd.to_numeric(out["sales_30d"], errors="coerce")

    # Validity
    stock_missing = out["stock_qty"].isna() | ~np.isfinite(out["stock_qty"])
    sales30_missing = out["sales_30d"].isna() | ~np.isfinite(out["sales_30d"])
    stock_invalid = stock_missing | (out["stock_qty"] < 0)

    # Dead stock window (requires e.g. sales_90d, sales_180d, etc.)
    window_col = f"sales_{int(dead_stock_days_val)}d"
    has_window = window_col in out.columns

    dead_stock = np.zeros(len(out), dtype=bool)
    slow_mover = np.zeros(len(out), dtype=bool)

    if has_window:
        out[window_col] = pd.to_numeric(out[window_col], errors="coerce")
        sales_for_dead = out[window_col]

        dead_stock = (~sales_for_dead.isna()) & np.isfinite(sales_for_dead) & (sales_for_dead <= 0)

        is_zero_30 = (~sales30_missing) & np.isfinite(out["sales_30d"]) & (out["sales_30d"] <= 0)
        slow_mover = is_zero_30 & (~dead_stock) & (sales_for_dead > 0)
    else:
        is_zero_30 = (~sales30_missing) & np.isfinite(out["sales_30d"]) & (out["sales_30d"] <= 0)
        if dead_stock_days_val <= 30:
            dead_stock = is_zero_30
        else:
            slow_mover = is_zero_30

    # Flow calc only when sales_30d > 0
    valid_flow = (~stock_invalid) & (~sales30_missing) & (out["sales_30d"] > 0)
    data_issue = (~valid_flow) & (~dead_stock) & (~slow_mover)

    # Metrics
    out["stock_days"] = np.nan
    out.loc[valid_flow, "stock_days"] = (out.loc[valid_flow, "stock_qty"] * 30.0) / out.loc[valid_flow, "sales_30d"]

    finite_days = out["stock_days"].notna() & np.isfinite(out["stock_days"]) & (out["stock_days"] >= 0)
    out.loc[~finite_days, "stock_days"] = np.nan

    out["per_day"] = np.nan
    out.loc[finite_days, "per_day"] = out.loc[finite_days, "sales_30d"] / 30.0

    # Masks
    mask_green = finite_days & (out["stock_days"] <= green_max_val)
    mask_yellow = finite_days & (out["stock_days"] > green_max_val) & (out["stock_days"] <= yellow_max_val)
    mask_red = finite_days & (out["stock_days"] > yellow_max_val)

    # ✅ Status (English)
    out["Status"] = "🟢 Healthy"
    out.loc[mask_yellow, "Status"] = "🟡 At risk"
    out.loc[mask_red, "Status"] = "🔴 Critical"

    out.loc[slow_mover, "Status"] = "🟡 Slow mover"
    out.loc[dead_stock, "Status"] = "🔴 Dead stock"
    out.loc[data_issue, "Status"] = "🔴 Data issue"

    # Why (short + clean)
    out["Why"] = ""
    out["Why (details)"] = ""

    out.loc[data_issue & stock_missing & sales30_missing, "Why"] = "Missing stock + sales"
    out.loc[data_issue & stock_missing & ~sales30_missing, "Why"] = "Missing stock"
    out.loc[data_issue & ~stock_missing & sales30_missing, "Why"] = "Missing sales"
    out.loc[data_issue & (out["stock_qty"] < 0), "Why"] = "Negative stock"

    if has_window:
        out.loc[dead_stock, "Why"] = f"0 sales in the last {int(dead_stock_days_val)} days"
        out.loc[slow_mover, "Why"] = "0 sales in the last 30 days"
    else:
        out.loc[dead_stock if dead_stock_days_val <= 30 else slow_mover, "Why"] = "0 sales in the last 30 days"

    out.loc[mask_green, "Why"] = f"≤ {int(green_max_val)} days of stock"
    out.loc[mask_yellow, "Why"] = f"Over {int(green_max_val)} days of stock"
    out.loc[mask_red, "Why"] = f"Over {int(yellow_max_val)} days of stock"

    if show_details_val:
        out.loc[mask_green, "Why (details)"] = (
            out.loc[mask_green, "stock_days"].round(0).astype(int).astype(str)
            + " stock days · "
            + out.loc[mask_green, "per_day"].round(2).astype(str)
            + " sold/day"
        )
        out.loc[mask_yellow, "Why (details)"] = (
            out.loc[mask_yellow, "stock_days"].round(0).astype(int).astype(str)
            + " stock days · "
            + out.loc[mask_yellow, "per_day"].round(2).astype(str)
            + " sold/day"
        )
        out.loc[mask_red, "Why (details)"] = (
            out.loc[mask_red, "stock_days"].round(0).astype(int).astype(str)
            + " stock days · "
            + out.loc[mask_red, "per_day"].round(2).astype(str)
            + " sold/day"
        )

        if has_window:
            out.loc[dead_stock, "Why (details)"] = f"{window_col} = 0"
            out.loc[slow_mover, "Why (details)"] = f"sales_30d = 0 but {window_col} > 0"
        else:
            if dead_stock_days_val <= 30:
                out.loc[dead_stock, "Why (details)"] = "sales_30d = 0 (threshold ≤ 30)"
            else:
                out.loc[slow_mover, "Why (details)"] = "Only sales_30d provided → treated as slow mover"

    out.loc[out["Why"].eq(""), "Why"] = "Check input data"

    # ✅ Recommended action (English, Shopify-operator tone)
    out["Recommended action"] = ""
    out.loc[data_issue, "Recommended action"] = "Fix input data first (stock/sales)"
    out.loc[dead_stock, "Recommended action"] = "Stop reorders + run a liquidation plan (bundles/markdown/phase-out)"
    out.loc[slow_mover, "Recommended action"] = "Check seasonality/one-off demand. If not → liquidation plan"
    out.loc[mask_green, "Recommended action"] = "Healthy — maintain"
    out.loc[mask_yellow, "Recommended action"] = "Pause reorders + sanity-check demand"
    out.loc[mask_red, "Recommended action"] = "Stop reorders + reduce inventory (promo/bundles/price moves)"

    # Output
    out["Product"] = out["product_name"].astype(str)
    out["Stock (units)"] = out["stock_qty"].round(0)

    out["Days of stock"] = np.nan
    out.loc[finite_days, "Days of stock"] = out.loc[finite_days, "stock_days"].round(0)

    out["If you stop reordering: OOS in"] = np.nan
    out.loc[finite_days, "If you stop reordering: OOS in"] = out.loc[finite_days, "stock_days"].round(0)

    out["Capital tied up"] = np.nan
    out["Excess stock (value)"] = np.nan

    # econ helpers
    out["_econ_over"] = 0.0
    out["_econ_cap"] = 0.0

    if has_cost:
        cost_ok = out["unit_cost"].notna() & np.isfinite(out["unit_cost"]) & (out["unit_cost"] >= 0)
        cap_mask = (~stock_invalid) & cost_ok
        out.loc[cap_mask, "Capital tied up"] = (out.loc[cap_mask, "stock_qty"] * out.loc[cap_mask, "unit_cost"]).astype(float)

        target_units_green = pd.Series(np.nan, index=out.index, dtype="float64")
        target_units_green.loc[finite_days] = out.loc[finite_days, "per_day"] * float(green_max_val)

        excess_units = pd.Series(np.nan, index=out.index, dtype="float64")
        excess_units.loc[finite_days] = np.maximum(out.loc[finite_days, "stock_qty"] - target_units_green.loc[finite_days], 0.0)

        overstock_mask = finite_days & cap_mask
        out.loc[overstock_mask, "Excess stock (value)"] = excess_units.loc[overstock_mask] * out.loc[overstock_mask, "unit_cost"]

        out["_econ_over"] = _safe_num_series(out["Excess stock (value)"]).fillna(0.0)
        out["_econ_cap"] = _safe_num_series(out["Capital tied up"]).fillna(0.0)

    # Ranking buckets
    out["_bucket"] = 0
    out.loc[mask_green, "_bucket"] = 10
    out.loc[slow_mover, "_bucket"] = 20
    out.loc[mask_yellow, "_bucket"] = 30
    out.loc[mask_red, "_bucket"] = 40
    out.loc[data_issue, "_bucket"] = 90
    out.loc[dead_stock, "_bucket"] = 100

    out["_days_over"] = 0.0
    out.loc[mask_yellow, "_days_over"] = out.loc[mask_yellow, "stock_days"] - float(green_max_val)
    out.loc[mask_red, "_days_over"] = out.loc[mask_red, "stock_days"] - float(yellow_max_val)

    out["_econ_effect"] = np.maximum(out["_econ_over"], out["_econ_cap"])

    # Priority (why) (English)
    def _status_part(s: str) -> str:
        s = str(s)
        if s.startswith("🔴 Dead stock"):
            return "Status: Dead stock"
        if s.startswith("🔴 Data issue"):
            return "Status: Data issue"
        if s.startswith("🔴"):
            return "Status: Critical"
        if s.startswith("🟡 Slow mover"):
            return "Status: Slow mover"
        if s.startswith("🟡"):
            return "Status: At risk"
        return "Status: Healthy"

    status_txt = out["Status"].astype(str).apply(_status_part)
    boost_txt = np.where(dead_stock, "Boost: dead stock", np.where(data_issue, "Boost: data", "Boost: -"))

    over_v = _safe_num_series(out.get("Excess stock (value)", pd.Series(index=out.index, dtype=float))).fillna(0.0)
    cap_v = _safe_num_series(out.get("Capital tied up", pd.Series(index=out.index, dtype=float))).fillna(0.0)

    econ_txt = np.where(
        over_v > 0,
        "Value impact: " + over_v.round(0).astype(int).astype(str) + " excess",
        np.where(
            cap_v > 0,
            "Value impact: " + cap_v.round(0).astype(int).astype(str) + " tied up",
            "Value impact: -",
        ),
    )

    days_txt = np.where(
        mask_red | mask_yellow,
        "Days over: +" + out["_days_over"].round(0).astype(int).astype(str),
        "Days over: -",
    )
    out["Priority (why)"] = status_txt + " · " + boost_txt + " · " + days_txt + " · " + econ_txt

    # Sort
    out = out.sort_values(
        ["_bucket", "_days_over", "_econ_effect", "Product"],
        ascending=[False, False, False, True],
        kind="mergesort",
    ).reset_index(drop=True)

    out["Priority"] = np.arange(1, len(out) + 1)

    cols = [
        "Priority",
        "Status",
        "Product",
        "Stock (units)",
        "Days of stock",
        "Capital tied up",
        "Excess stock (value)",
        "Why",
        "Recommended action",
    ]

    if show_details_val:
        cols.insert(cols.index("Status") + 1, "Priority (why)")
        cols.insert(cols.index("Days of stock") + 1, "If you stop reordering: OOS in")
        cols.insert(cols.index("Why") + 1, "Why (details)")

    return out[cols]


@st.cache_data(show_spinner=False)
def compute_flow_cached(
    df: pd.DataFrame,
    green_max_val: int,
    yellow_max_val: int,
    dead_stock_days_val: int,
    show_details_val: bool,
    cache_version: int,
) -> pd.DataFrame:
    return _compute_flow_core(df.copy(), green_max_val, yellow_max_val, dead_stock_days_val, show_details_val)


# ---------------- UI PIECES ----------------
def column_config(show_details_val: bool, unit: str):
    stock_title = f"Stock ({unit})" if unit else "Stock (units)"
    cap_help = (
        f"Total inventory value (stock × unit cost per {unit})." if unit else "Total inventory value (stock × unit cost)."
    )
    over_help = (
        f"Value above healthy flow (above 🟢 threshold) — in your currency ({unit}-based)."
        if unit
        else "Value above healthy flow (above 🟢 threshold)."
    )

    cfg = {
        "Stock (units)": st.column_config.NumberColumn(stock_title, format="%.0f"),
        "Days of stock": st.column_config.NumberColumn("Days of stock", format="%.0f"),
        "Capital tied up": st.column_config.NumberColumn("Capital tied up", format="%.0f", help=cap_help),
        "Excess stock (value)": st.column_config.NumberColumn("Excess stock (value)", format="%.0f", help=over_help),
        "If you stop reordering: OOS in": st.column_config.NumberColumn("If you stop reordering: OOS in", format="%.0f days"),
        "Priority": st.column_config.NumberColumn(
            "Priority",
            format="%.0f",
            help="Sorting: worst status first + boost (dead stock/data) + days beyond threshold + value impact.",
        ),
        "Why": st.column_config.TextColumn("Why", help="One-line diagnosis (no actions)."),
    }
    if show_details_val:
        cfg["Priority (why)"] = st.column_config.TextColumn(
            "Priority (why)",
            help="Mini breakdown: status weight + boost + days beyond threshold + value impact.",
            width="large",
        )
    return cfg


def plan_for_flow_health(df_result: pd.DataFrame, target: float) -> str:
    total = len(df_result)
    if total <= 0:
        return ""
    is_green = df_result["Status"].astype(str).str.startswith("🟢")
    green = int(is_green.sum())
    required_green = int(np.ceil(target * total))
    needed = max(required_green - green, 0)
    if needed <= 0:
        return "✅ You’re on target — keep 🟢 stable (avoid new 🔴/🟡)."

    candidates = df_result[df_result["Status"].astype(str).str.startswith(("🔴", "🟡"))].copy()
    if len(candidates) == 0:
        return f"To reach {target*100:.0f}% green you need ~{needed} products — but there are no 🔴/🟡 items to move. (Check data/labels.)"

    if "Priority" in candidates.columns:
        candidates = candidates.sort_values("Priority", ascending=True, kind="mergesort")
    top = candidates.head(needed)

    h = top.get("Recommended action", pd.Series([""] * len(top))).fillna("").astype(str).str.lower()
    data_fix = int(h.str.contains("fix input data|fix data").sum())
    stop_buy = int(h.str.contains("stop reorders|stop purchasing|pause reorders").sum())
    pricing = int(h.str.contains("reduce inventory|promo|bundle|price|markdown|liquidation").sum())
    phase_out = int(h.str.contains("phase out|liquidation plan|phase-out plan").sum())

    buckets = []
    if pricing > 0:
        buckets.append(f"pricing/promo for {pricing}")
    if stop_buy > 0:
        buckets.append(f"pause/stop reorders for {stop_buy}")
    if phase_out > 0:
        buckets.append(f"phase-out for {phase_out}")
    if data_fix > 0:
        buckets.append(f"data fixes for {data_fix}")

    buckets_txt = "mainly via " + " + ".join(buckets) if buckets else "mainly via the highest-priority 🔴/🟡 items"
    return f"To reach {target*100:.0f}% green, ~{needed} products must move from 🔴/🟡 → 🟢 ({buckets_txt})."


def top3_focus_lines(df_result: pd.DataFrame) -> list[str]:
    if len(df_result) == 0:
        return []

    d = df_result.copy()
    d = d[d["Status"].astype(str).str.startswith(("🔴", "🟡"))].copy()
    if len(d) == 0:
        return []

    over = pd.to_numeric(d.get("Excess stock (value)", pd.Series(index=d.index, dtype=float)), errors="coerce").fillna(0.0)
    cap = pd.to_numeric(d.get("Capital tied up", pd.Series(index=d.index, dtype=float)), errors="coerce").fillna(0.0)

    d["_over"] = over
    d["_cap"] = cap
    d["_is_dead"] = d["Status"].astype(str).str.contains("Dead stock", case=False, na=False)

    chosen_idx = []

    if (d["_over"] > 0).any():
        top_over = d.sort_values("_over", ascending=False, kind="mergesort").head(2)
        chosen_idx.extend(list(top_over.index))

    dead_candidates = d[d["_is_dead"] & ~d.index.isin(chosen_idx)].copy()
    if len(dead_candidates) > 0:
        dead_pick = dead_candidates.sort_values("_cap", ascending=False, kind="mergesort").head(1)
        chosen_idx.extend(list(dead_pick.index))

    if len(chosen_idx) < 3:
        remaining = d[~d.index.isin(chosen_idx)].copy()
        if "Priority" in remaining.columns:
            remaining = remaining.sort_values("Priority", ascending=True, kind="mergesort")
        fill = remaining.head(3 - len(chosen_idx))
        chosen_idx.extend(list(fill.index))

    picked = d.loc[chosen_idx].copy()

    lines = []
    for i, (_, row) in enumerate(picked.iterrows(), start=1):
        prod = str(row.get("Product", "")).strip()
        act = short_action(str(row.get("Recommended action", "")))

        if bool(row.get("_is_dead", False)):
            lines.append(f"{i}) {prod}: dead stock → {act}")
        else:
            if float(row.get("_over", 0.0)) > 0:
                lines.append(f"{i}) {prod}: {fmt_kr(row.get('_over'))} excess → {act}")
            else:
                if float(row.get("_cap", 0.0)) > 0:
                    lines.append(f"{i}) {prod}: {fmt_kr(row.get('_cap'))} tied up → {act}")
                else:
                    lines.append(f"{i}) {prod}: → {act}")

    return lines[:3]


def render_overview_header(df_result: pd.DataFrame, target: float, unit: str):
    total = len(df_result)
    if total == 0:
        st.info("No rows to display.")
        return

    s = df_result["Status"].astype(str)
    red = int(s.str.startswith("🔴").sum())
    yellow = int(s.str.startswith("🟡").sum())
    green = int(s.str.startswith("🟢").sum())

    flow_health = green / total
    gap = max(target - flow_health, 0.0)
    badge = "✅ On target" if flow_health >= target else "⚠ Below target"

    cap_total = float(pd.to_numeric(df_result.get("Capital tied up", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
    over_total = float(pd.to_numeric(df_result.get("Excess stock (value)", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())

    c1, c2, c3, c4, c5, c6 = st.columns([1, 1, 1, 1.6, 1.6, 2.2])
    with c1:
        st.metric("🔴 Red", red)
    with c2:
        st.metric("🟡 Yellow", yellow)
    with c3:
        st.metric("🟢 Green", green)

    with c4:
        render_big_metric(
            "💰 Capital tied up (total)",
            fmt_kr(cap_total),
            f"Total inventory value (stock × unit cost per {unit})." if unit else "Total inventory value (stock × unit cost).",
        )
    with c5:
        render_big_metric(
            "🔥 Excess stock (total)",
            fmt_kr(over_total),
            "Value above healthy flow (above 🟢 threshold).",
        )

    with c6:
        st.write("🟢 Flow health")
        st.progress(flow_health)
        st.caption(f"{badge} · {flow_health*100:.1f}% green · target {target*100:.0f}% · gap {gap*100:.1f} pts")
        plan_line = plan_for_flow_health(df_result, target)
        if plan_line:
            st.caption("🧭 " + plan_line)

    if unit:
        st.caption(f"📏 Unit in this file: **{unit}** (stock + sales must use the same unit)")

    focus = top3_focus_lines(df_result)
    if focus:
        st.markdown("**🎯 Top 3 focus (what should you do today?)**")
        for line in focus:
            st.markdown(f"- {line}")


def render_actions(df_result: pd.DataFrame, show_details_val: bool, unit: str):
    st.subheader("⚡ Actions (items that require attention)")
    st.caption("Default view shows 🔴 + 🟡 only.")

    show_green = st.toggle("Include 🟢 items (show all)", value=False, key="actions_toggle_show_green")
    if show_green:
        actions = df_result.copy()
    else:
        actions = df_result[df_result["Status"].astype(str).str.startswith(("🔴", "🟡"))].copy()

    if len(actions) == 0:
        st.success("No 🔴/🟡 items — looks healthy 😄")
        return

    limit = st.selectbox("Rows to show", [20, 50, 100, "All"], index=0, key="actions_row_limit")
    if limit != "All":
        actions = actions.head(int(limit))

    st.data_editor(
        df_for_display(actions),
        hide_index=True,
        use_container_width=True,
        disabled=True,
        column_config=column_config(show_details_val, unit),
        key="data_editor_actions",
    )


def render_all_products(df_result: pd.DataFrame, show_details_val: bool, unit: str):
    st.subheader("📦 All products (worst → best)")
    st.data_editor(
        df_for_display(df_result),
        hide_index=True,
        use_container_width=True,
        disabled=True,
        column_config=column_config(show_details_val, unit),
        key="data_editor_all",
    )


def download_section(df_result: pd.DataFrame):
    st.subheader("⬇️ Download results")
    st.caption("Downloads contain raw numbers (Excel-friendly).")

    csv_bytes = df_result.to_csv(index=False, sep=";").encode("utf-8")
    st.download_button(
        label="⬇️ Download CSV (Excel-friendly)",
        data=csv_bytes,
        file_name="inventoryflow_result.csv",
        mime="text/csv",
        key="download_csv_btn",
    )

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_result.to_excel(writer, index=False, sheet_name="Flow")
    xlsx_bytes = output.getvalue()

    st.download_button(
        label="⬇️ Download Excel (.xlsx)",
        data=xlsx_bytes,
        file_name="inventoryflow_result.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="download_xlsx_btn",
    )


# ---------------- TOP: UPLOAD + HELP ----------------
with st.expander("✅ Required file format", expanded=True):
    st.markdown(
        """
**Your file must include these columns:**
- `product_name` (text)
- `stock_qty` (number) – current stock on hand (same unit as sales)
- `sales_30d` (number) – units sold in the last 30 days (same unit as stock)

**Optional (to calculate financial impact):**
- `unit_cost` (number) – cost per unit (e.g. currency/unit)

**Bonus (for dead stock windows):**
- `sales_90d`, `sales_180d`, etc. (must match the slider value if the column exists)
"""
    )

st.subheader("Upload file")
uploaded = st.file_uploader("Choose a CSV or Excel file", type=["csv", "xlsx"], accept_multiple_files=False)

# ---------------- TABS ----------------
tab_overview, tab_all, tab_settings = st.tabs(["📊 Overview", "📦 All products", "⚙️ Settings + Download"])

with tab_settings:
    st.subheader("🔧 Tune rules")
    st.caption("Tip: Keep this tab closed for most users. This is for fine-tuning.")

    col_a, col_b, col_c, col_d = st.columns([1, 1, 1, 1.2])
    with col_a:
        green_max = st.number_input("🟢 Green up to (days of stock)", min_value=1, max_value=2000, value=120, step=10, key="set_green")
    with col_b:
        yellow_max = st.number_input(
            "🟡 Yellow up to (days of stock)",
            min_value=int(green_max) + 1,
            max_value=3000,
            value=240,
            step=10,
            key="set_yellow",
        )
    with col_c:
        st.markdown("**🔴 Red** is anything above the 🟡 threshold")

    with col_d:
        unit = st.selectbox(
            "📏 Unit",
            options=["pcs", "kg", "l", "m", "case", "pallet", "other"],
            index=0,
            help="Select the unit used in `stock_qty` and `sales_30d`. Stock and sales must use the same unit.",
            key="set_unit",
        )
        if unit == "other":
            unit = st.text_input("Custom unit (e.g. 'ton', 'meter', 'pack')", value="unit", key="set_unit_custom").strip() or "unit"

    st.divider()

    dead_stock_days = st.slider(
        "⏳ Dead stock if no sales in (days)",
        min_value=30,
        max_value=365,
        value=90,
        step=15,
        key="set_dead_stock_days",
    )
    st.caption(
        "If your file only includes `sales_30d`, we can’t prove “no sales in 90 days.” "
        "So when the threshold is > 30, items with 0 sales in 30 days are treated as 🟡 Slow mover (not 🔴 Dead stock). "
        "If you also upload `sales_90d` (or the matching column), it becomes exact."
    )

    st.divider()

    flow_target_pct = st.slider(
        "🎯 Flow health target (% green)",
        min_value=50,
        max_value=100,
        value=85,
        step=1,
        key="set_flow_target",
    )
    flow_target = flow_target_pct / 100.0

    st.divider()

    show_details = st.toggle("🔎 Show details (extra context + OOS estimate)", value=False, key="set_details")
    st.caption("Details OFF by default. Turn ON when you need to explain decisions.")

# Defaults (before upload) to avoid NameError
if "green_max" not in globals():
    green_max = 120
    yellow_max = 240
    dead_stock_days = 90
    flow_target = 0.85
    show_details = False
    unit = "pcs"

# ---------------- RUN ----------------
if uploaded:
    try:
        df_in = read_file(uploaded)
        result = compute_flow_cached(
            df_in,
            int(green_max),
            int(yellow_max),
            int(dead_stock_days),
            bool(show_details),
            CACHE_VERSION,
        )

        # ✅ LOG ONLY METADATA (ONCE PER UPLOAD)
        log_beta_metadata_once(
            result_df=result,
            email=user_email,
            uploaded_file=uploaded,
            rules={
                "green_max": int(green_max),
                "yellow_max": int(yellow_max),
                "dead_stock_days": int(dead_stock_days),
                "show_details": bool(show_details),
            },
        )

        with tab_overview:
            st.success("Risk signals + action plan + optional financial impact (worst → best)")
            render_overview_header(result, flow_target, unit)
            st.divider()
            render_actions(result, show_details, unit)

        with tab_all:
            render_all_products(result, show_details, unit)

        with tab_settings:
            st.divider()
            download_section(result)

    except Exception as e:
        st.error(f"Could not read/compute the file: {e}")
else:
    with tab_overview:
        st.info("Upload a file to see Overview + Actions.")
    with tab_all:
        st.info("Upload a file to see all products.")
    with tab_settings:
        st.info("Upload a file to enable downloads.")
