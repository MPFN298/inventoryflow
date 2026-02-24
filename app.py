import io
import numpy as np
import pandas as pd
import streamlit as st

# ---------------- CONFIG ----------------
st.set_page_config(page_title="InventoryFlow (🟢🟡🔴)", layout="wide")

st.title("🟢🟡🔴 InventoryFlow")
st.caption(
    "Upload en CSV eller Excel-fil og få et klart 🟢🟡🔴 overblik "
    "over dit lagerflow baseret på salgstempo + (valgfrit) økonomisk impact i kroner."
)

# ✅ Pro CSS: Streamlit metrics + vores egne “big metric cards” (fixer tal-wrap pænt)
st.markdown(
    """
<style>
/* Streamlit metrics: ingen ellipsis, ingen clipping */
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

/* Vores “pro” big-number kort */
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

    /* vigtig: vi må gerne wrappe, men IKKE midt i tal */
    white-space: normal;
    overflow-wrap: normal;
    word-break: normal;

    font-variant-numeric: tabular-nums; /* “dashboard” tal */
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
CACHE_VERSION = 24  # <- bump pga PRO-wrap fix på big metric cards

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
        raise ValueError("Ukendt filtype")


def fmt_kr(x) -> str:
    try:
        v = float(x)
    except Exception:
        return ""
    if not np.isfinite(v):
        return ""
    return f"{v:,.0f}".replace(",", ".") + " kr"


def _safe_num_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("float64")


def kort_handling(action: str) -> str:
    a = (action or "").lower()
    if "ret data" in a or "fix data" in a:
        return "ret data"
    if "afviklingsplan" in a or "udfasning" in a or "afvikling" in a:
        return "afvikling"
    if "genbestilling" in a and ("pause" in a or "stop" in a):
        return "pause genbestilling"
    if "pris" in a or "kampagne" in a or "pakkeløsning" in a or "bundle" in a or "markdown" in a:
        return "pris/kampagne"
    if "stop indkøb" in a or "stop køb" in a:
        return "stop indkøb"
    return "næste skridt"


def df_for_display(df: pd.DataFrame) -> pd.DataFrame:
    disp = df.copy()
    text_cols = ["Status", "Produkt", "Hvorfor", "Anbefalet handling", "Prioritet (forklaring)"]
    if "Hvorfor (detaljer)" in disp.columns:
        text_cols.append("Hvorfor (detaljer)")
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
        raise ValueError(f"Mangler kolonner: {', '.join(sorted(missing))}")

    out = df.copy()

    # Valgfri indkøbspris
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

    # Dødt lager-vindue (kræver fx sales_90d, sales_180d osv.)
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

    # ✅ Status (100% dansk)
    out["Status"] = "🟢 OK"
    out.loc[mask_yellow, "Status"] = "🟡 Risiko"
    out.loc[mask_red, "Status"] = "🔴 Kritisk"

    out.loc[slow_mover, "Status"] = "🟡 Langsomt omsat"
    out.loc[dead_stock, "Status"] = "🔴 Dødt lager"
    out.loc[data_issue, "Status"] = "🔴 Dataproblem"

    # Hvorfor (kort + ren)
    out["Hvorfor"] = ""
    out["Hvorfor (detaljer)"] = ""

    out.loc[data_issue & stock_missing & sales30_missing, "Hvorfor"] = "Mangler lager + salg"
    out.loc[data_issue & stock_missing & ~sales30_missing, "Hvorfor"] = "Mangler lager"
    out.loc[data_issue & ~stock_missing & sales30_missing, "Hvorfor"] = "Mangler salg"
    out.loc[data_issue & (out["stock_qty"] < 0), "Hvorfor"] = "Negativt lager"

    if has_window:
        out.loc[dead_stock, "Hvorfor"] = f"0 salg i {int(dead_stock_days_val)} dage"
        out.loc[slow_mover, "Hvorfor"] = "0 salg i 30 dage"
    else:
        out.loc[dead_stock if dead_stock_days_val <= 30 else slow_mover, "Hvorfor"] = "0 salg i 30 dage"

    out.loc[mask_green, "Hvorfor"] = f"≤ {int(green_max_val)} dage"
    out.loc[mask_yellow, "Hvorfor"] = f"Over {int(green_max_val)} dage"
    out.loc[mask_red, "Hvorfor"] = f"Over {int(yellow_max_val)} dage"

    if show_details_val:
        out.loc[mask_green, "Hvorfor (detaljer)"] = (
            out.loc[mask_green, "stock_days"].round(0).astype(int).astype(str)
            + " lagerdage · "
            + out.loc[mask_green, "per_day"].round(2).astype(str)
            + " solgt/dag"
        )
        out.loc[mask_yellow, "Hvorfor (detaljer)"] = (
            out.loc[mask_yellow, "stock_days"].round(0).astype(int).astype(str)
            + " lagerdage · "
            + out.loc[mask_yellow, "per_day"].round(2).astype(str)
            + " solgt/dag"
        )
        out.loc[mask_red, "Hvorfor (detaljer)"] = (
            out.loc[mask_red, "stock_days"].round(0).astype(int).astype(str)
            + " lagerdage · "
            + out.loc[mask_red, "per_day"].round(2).astype(str)
            + " solgt/dag"
        )

        if has_window:
            out.loc[dead_stock, "Hvorfor (detaljer)"] = f"{window_col} = 0"
            out.loc[slow_mover, "Hvorfor (detaljer)"] = f"sales_30d = 0 men {window_col} > 0"
        else:
            if dead_stock_days_val <= 30:
                out.loc[dead_stock, "Hvorfor (detaljer)"] = "sales_30d = 0 (tærskel ≤ 30)"
            else:
                out.loc[slow_mover, "Hvorfor (detaljer)"] = "Kun sales_30d i filen → langsomt omsat"

    out.loc[out["Hvorfor"].eq(""), "Hvorfor"] = "Tjek data"

    # ✅ Handling (100% dansk)
    out["Anbefalet handling"] = ""
    out.loc[data_issue, "Anbefalet handling"] = "Ret data først (lager/salg)"
    out.loc[dead_stock, "Anbefalet handling"] = "Stop indkøb + lav afviklingsplan (pakkeløsning/pris/udfasning)"
    out.loc[slow_mover, "Anbefalet handling"] = "Vurdér: sæson/rare? Hvis nej → afviklingsplan"
    out.loc[mask_green, "Anbefalet handling"] = "OK – hold niveau"
    out.loc[mask_yellow, "Anbefalet handling"] = "Sæt genbestilling på pause + tjek forecast"
    out.loc[mask_red, "Anbefalet handling"] = "Stop indkøb + reducer lager (kampagne/pakkeløsning/pris)"

    # Output
    out["Produkt"] = out["product_name"].astype(str)
    out["Lager (mængde)"] = out["stock_qty"].round(0)

    out["Lagerdage"] = np.nan
    out.loc[finite_days, "Lagerdage"] = out.loc[finite_days, "stock_days"].round(0)

    out["Stop indkøb → tomt om"] = np.nan
    out.loc[finite_days, "Stop indkøb → tomt om"] = out.loc[finite_days, "stock_days"].round(0)

    out["Bundet kapital"] = np.nan
    out["Overlager (kr)"] = np.nan

    # econ helpers
    out["_econ_over"] = 0.0
    out["_econ_cap"] = 0.0

    if has_cost:
        cost_ok = out["unit_cost"].notna() & np.isfinite(out["unit_cost"]) & (out["unit_cost"] >= 0)
        cap_mask = (~stock_invalid) & cost_ok
        out.loc[cap_mask, "Bundet kapital"] = (out.loc[cap_mask, "stock_qty"] * out.loc[cap_mask, "unit_cost"]).astype(float)

        target_units_green = pd.Series(np.nan, index=out.index, dtype="float64")
        target_units_green.loc[finite_days] = out.loc[finite_days, "per_day"] * float(green_max_val)

        excess_units = pd.Series(np.nan, index=out.index, dtype="float64")
        excess_units.loc[finite_days] = np.maximum(out.loc[finite_days, "stock_qty"] - target_units_green.loc[finite_days], 0.0)

        overlager_mask = finite_days & cap_mask
        out.loc[overlager_mask, "Overlager (kr)"] = excess_units.loc[overlager_mask] * out.loc[overlager_mask, "unit_cost"]

        out["_econ_over"] = _safe_num_series(out["Overlager (kr)"]).fillna(0.0)
        out["_econ_cap"] = _safe_num_series(out["Bundet kapital"]).fillna(0.0)

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

    # Prioritet forklaring (100% dansk)
    def _status_part(s: str) -> str:
        s = str(s)
        if s.startswith("🔴 Dødt lager"):
            return "Status: Dødt lager"
        if s.startswith("🔴 Dataproblem"):
            return "Status: Dataproblem"
        if s.startswith("🔴"):
            return "Status: Kritisk"
        if s.startswith("🟡 Langsomt omsat"):
            return "Status: Langsomt omsat"
        if s.startswith("🟡"):
            return "Status: Risiko"
        return "Status: OK"

    status_txt = out["Status"].astype(str).apply(_status_part)
    boost_txt = np.where(dead_stock, "Boost: dødt lager", np.where(data_issue, "Boost: data", "Boost: -"))

    over_v = _safe_num_series(out.get("Overlager (kr)", pd.Series(index=out.index, dtype=float))).fillna(0.0)
    cap_v = _safe_num_series(out.get("Bundet kapital", pd.Series(index=out.index, dtype=float))).fillna(0.0)

    econ_txt = np.where(
        over_v > 0,
        "Kr-effekt: " + over_v.round(0).astype(int).astype(str) + " overlager",
        np.where(
            cap_v > 0,
            "Kr-effekt: " + cap_v.round(0).astype(int).astype(str) + " bundet",
            "Kr-effekt: -",
        ),
    )

    days_txt = np.where(
        mask_red | mask_yellow,
        "Dage over: +" + out["_days_over"].round(0).astype(int).astype(str),
        "Dage over: -",
    )
    out["Prioritet (forklaring)"] = status_txt + " · " + boost_txt + " · " + days_txt + " · " + econ_txt

    # Sort
    out = out.sort_values(
        ["_bucket", "_days_over", "_econ_effect", "Produkt"],
        ascending=[False, False, False, True],
        kind="mergesort",
    ).reset_index(drop=True)

    out["Prioritet"] = np.arange(1, len(out) + 1)

    cols = [
        "Prioritet",
        "Status",
        "Produkt",
        "Lager (mængde)",
        "Lagerdage",
        "Bundet kapital",
        "Overlager (kr)",
        "Hvorfor",
        "Anbefalet handling",
    ]

    if show_details_val:
        cols.insert(cols.index("Status") + 1, "Prioritet (forklaring)")
        cols.insert(cols.index("Lagerdage") + 1, "Stop indkøb → tomt om")
        cols.insert(cols.index("Hvorfor") + 1, "Hvorfor (detaljer)")

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
def column_config(show_details_val: bool, enhed: str):
    lager_title = f"Lager ({enhed})" if enhed else "Lager (mængde)"
    kost_help = (
        f"Hele lagerets værdi (mængde × indkøbspris pr {enhed})." if enhed else "Hele lagerets værdi (mængde × indkøbspris)."
    )
    over_help = (
        f"Overskud over sundt flow (over 🟢-niveau) i kr. ({enhed}-basis)." if enhed else "Overskud over sundt flow (over 🟢-niveau)."
    )

    cfg = {
        "Lager (mængde)": st.column_config.NumberColumn(lager_title, format="%.0f"),
        "Lagerdage": st.column_config.NumberColumn("Lagerdage", format="%.0f"),
        "Bundet kapital": st.column_config.NumberColumn("Bundet kapital", format="%.0f kr", help=kost_help),
        "Overlager (kr)": st.column_config.NumberColumn("Overlager (kr)", format="%.0f kr", help=over_help),
        "Stop indkøb → tomt om": st.column_config.NumberColumn("Stop indkøb → tomt om", format="%.0f dage"),
        "Prioritet": st.column_config.NumberColumn(
            "Prioritet",
            format="%.0f",
            help="Sortering: status (værst først) + boost (dødt lager/data) + dage over grænse + kr-effekt.",
        ),
        "Hvorfor": st.column_config.TextColumn("Hvorfor", help="Diagnose i én linje (kort + uden handling)."),
    }
    if show_details_val:
        cfg["Prioritet (forklaring)"] = st.column_config.TextColumn(
            "Prioritet (forklaring)",
            help="Mini-breakdown: status-vægt + boost + dage over grænse + kr-effekt.",
            width="large",
        )
    return cfg


def plan_for_flow_sundhed(df_result: pd.DataFrame, target: float) -> str:
    total = len(df_result)
    if total <= 0:
        return ""
    is_green = df_result["Status"].astype(str).str.startswith("🟢")
    green = int(is_green.sum())
    required_green = int(np.ceil(target * total))
    needed = max(required_green - green, 0)
    if needed <= 0:
        return "✅ Du er på mål – fokusér på at holde 🟢 stabil (undgå nye 🔴/🟡)."

    candidates = df_result[df_result["Status"].astype(str).str.startswith(("🔴", "🟡"))].copy()
    if len(candidates) == 0:
        return f"For at nå {target*100:.0f}% grøn mangler der ca. {needed} produkter – men der er ingen 🔴/🟡 at flytte. (Tjek data/labels.)"

    if "Prioritet" in candidates.columns:
        candidates = candidates.sort_values("Prioritet", ascending=True, kind="mergesort")
    top = candidates.head(needed)

    h = top.get("Anbefalet handling", pd.Series([""] * len(top))).fillna("").astype(str).str.lower()
    data_fix = int(h.str.contains("ret data|fix data").sum())
    stop_buy = int(h.str.contains("stop indkøb|stop køb|genbestilling").sum())
    pricing = int(h.str.contains("reducer lager|kampagne|pakkeløsning|pris|bundle|markdown").sum())
    phase_out = int(h.str.contains("afviklingsplan|udfasning|afvikling").sum())

    buckets = []
    if pricing > 0:
        buckets.append(f"pris/kampagne på {pricing}")
    if stop_buy > 0:
        buckets.append(f"pause/stop indkøb på {stop_buy}")
    if phase_out > 0:
        buckets.append(f"afvikling/udfasning på {phase_out}")
    if data_fix > 0:
        buckets.append(f"data-rettelser på {data_fix}")

    buckets_txt = "primært via " + " + ".join(buckets) if buckets else "primært via de højst prioriterede 🔴/🟡-varer"
    return f"For at nå {target*100:.0f}% grøn skal ca. {needed} produkter flyttes fra 🔴/🟡 → 🟢 ({buckets_txt})."


def top3_fokuslinjer(df_result: pd.DataFrame) -> list[str]:
    if len(df_result) == 0:
        return []

    d = df_result.copy()
    d = d[d["Status"].astype(str).str.startswith(("🔴", "🟡"))].copy()
    if len(d) == 0:
        return []

    over = pd.to_numeric(d.get("Overlager (kr)", pd.Series(index=d.index, dtype=float)), errors="coerce").fillna(0.0)
    cap = pd.to_numeric(d.get("Bundet kapital", pd.Series(index=d.index, dtype=float)), errors="coerce").fillna(0.0)

    d["_over"] = over
    d["_cap"] = cap
    d["_is_dead"] = d["Status"].astype(str).str.contains("Dødt lager", case=False, na=False)

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
        if "Prioritet" in remaining.columns:
            remaining = remaining.sort_values("Prioritet", ascending=True, kind="mergesort")
        fill = remaining.head(3 - len(chosen_idx))
        chosen_idx.extend(list(fill.index))

    picked = d.loc[chosen_idx].copy()

    lines = []
    for i, (_, row) in enumerate(picked.iterrows(), start=1):
        prod = str(row.get("Produkt", "")).strip()
        act = kort_handling(str(row.get("Anbefalet handling", "")))

        if bool(row.get("_is_dead", False)):
            lines.append(f"{i}) {prod}: dødt lager → {act}")
        else:
            if float(row.get("_over", 0.0)) > 0:
                lines.append(f"{i}) {prod}: {fmt_kr(row.get('_over'))} overlager → {act}")
            else:
                if float(row.get("_cap", 0.0)) > 0:
                    lines.append(f"{i}) {prod}: {fmt_kr(row.get('_cap'))} bundet → {act}")
                else:
                    lines.append(f"{i}) {prod}: → {act}")

    return lines[:3]


def render_overview_header(df_result: pd.DataFrame, target: float, enhed: str):
    total = len(df_result)
    if total == 0:
        st.info("Ingen rækker at vise.")
        return

    s = df_result["Status"].astype(str)
    red = int(s.str.startswith("🔴").sum())
    yellow = int(s.str.startswith("🟡").sum())
    green = int(s.str.startswith("🟢").sum())

    flow_health = green / total
    gap = max(target - flow_health, 0.0)
    badge = "✅ På mål" if flow_health >= target else "⚠ Under mål"

    cap_total = float(pd.to_numeric(df_result.get("Bundet kapital", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
    over_total = float(pd.to_numeric(df_result.get("Overlager (kr)", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())

    c1, c2, c3, c4, c5, c6 = st.columns([1, 1, 1, 1.6, 1.6, 2.2])
    with c1:
        st.metric("🔴 Rød", red)
    with c2:
        st.metric("🟡 Gul", yellow)
    with c3:
        st.metric("🟢 Grøn", green)

    with c4:
        render_big_metric(
            "💰 Bundet kapital (sum)",
            fmt_kr(cap_total),
            f"Hele lagerets værdi (mængde × indkøbspris pr {enhed})." if enhed else "Hele lagerets værdi (mængde × indkøbspris).",
        )
    with c5:
        render_big_metric(
            "🔥 Overlager (sum)",
            fmt_kr(over_total),
            "Overskud over sundt flow (over 🟢-niveau).",
        )

    with c6:
        st.write("🟢 Flow-sundhed")
        st.progress(flow_health)
        st.caption(f"{badge} · {flow_health*100:.1f}% grøn · mål {target*100:.0f}% · gap {gap*100:.1f} %-point")
        plan_line = plan_for_flow_sundhed(df_result, target)
        if plan_line:
            st.caption("🧭 " + plan_line)

    if enhed:
        st.caption(f"📏 Enhed i denne fil: **{enhed}** (lager + salg skal være samme enhed)")

    fokus = top3_fokuslinjer(df_result)
    if fokus:
        st.markdown("**🎯 Top 3 fokus (hvad gør vi i dag?)**")
        for line in fokus:
            st.markdown(f"- {line}")


def render_handlinger(df_result: pd.DataFrame, show_details_val: bool, enhed: str):
    st.subheader("⚡ Handlinger (det der kræver noget)")
    st.caption("Standard: viser 🔴 + 🟡.")

    show_green = st.toggle("Vis også 🟢 (inkludér alt)", value=False, key="actions_toggle_show_green")
    if show_green:
        actions = df_result.copy()
    else:
        actions = df_result[df_result["Status"].astype(str).str.startswith(("🔴", "🟡"))].copy()

    if len(actions) == 0:
        st.success("Ingen 🔴/🟡 – alt ser sundt ud 😄")
        return

    limit = st.selectbox("Vis antal rækker", [20, 50, 100, "Alle"], index=0, key="actions_row_limit")
    if limit != "Alle":
        actions = actions.head(int(limit))

    st.data_editor(
        df_for_display(actions),
        hide_index=True,
        use_container_width=True,
        disabled=True,
        column_config=column_config(show_details_val, enhed),
        key="data_editor_actions",
    )


def render_alle_produkter(df_result: pd.DataFrame, show_details_val: bool, enhed: str):
    st.subheader("📦 Alle produkter (værst → bedst)")
    st.data_editor(
        df_for_display(df_result),
        hide_index=True,
        use_container_width=True,
        disabled=True,
        column_config=column_config(show_details_val, enhed),
        key="data_editor_all",
    )


def download_section(df_result: pd.DataFrame):
    st.subheader("⬇️ Download resultat")
    st.caption("Download indeholder rigtige tal (Excel-venligt).")

    csv_bytes = df_result.to_csv(index=False, sep=";").encode("utf-8")
    st.download_button(
        label="⬇️ Download CSV (Excel-venlig)",
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
with st.expander("✅ Hvilket format skal filen have?", expanded=True):
    st.markdown(
        """
**Din fil skal have disse kolonner:**
- `product_name` (tekst)
- `stock_qty` (tal) – lager mængde (samme enhed som salg)
- `sales_30d` (tal) – solgt de sidste 30 dage (samme enhed som lager)

**Valgfrit (for kroner/impact):**
- `unit_cost` (tal) – indkøbspris pr enhed (fx kr/stk, kr/kg, kr/l)

**Bonus (for vindue til dødt lager):**
- `sales_90d`, `sales_180d`, osv. (matcher skyderen hvis den findes)
"""
    )

st.subheader("Upload fil")
uploaded = st.file_uploader("Vælg CSV eller Excel", type=["csv", "xlsx"], accept_multiple_files=False)

# ---------------- TABS ----------------
tab_overview, tab_all, tab_settings = st.tabs(["📊 Overblik", "📦 Alle produkter", "⚙️ Indstillinger + Download"])

with tab_settings:
    st.subheader("🔧 Justér regler")
    st.caption("Tip: Hold dette faneblad lukket for de fleste brugere. Det er her man finjusterer.")

    col_a, col_b, col_c, col_d = st.columns([1, 1, 1, 1.2])
    with col_a:
        green_max = st.number_input("🟢 Grøn op til (lagerdage)", min_value=1, max_value=2000, value=120, step=10, key="set_green")
    with col_b:
        yellow_max = st.number_input(
            "🟡 Gul op til (lagerdage)",
            min_value=int(green_max) + 1,
            max_value=3000,
            value=240,
            step=10,
            key="set_yellow",
        )
    with col_c:
        st.markdown("**🔴 Rød** er alt over 🟡-grænsen")

    with col_d:
        enhed = st.selectbox(
            "📏 Enhed",
            options=["stk", "kg", "l", "m", "kasse", "palle", "andet"],
            index=0,
            help="Vælg den enhed som 'stock_qty' og 'sales_30d' er målt i. Lager og salg skal være samme enhed.",
            key="set_enhed",
        )
        if enhed == "andet":
            enhed = st.text_input("Skriv enhed (fx 'ton', 'meter', 'pakke')", value="enhed", key="set_enhed_custom").strip() or "enhed"

    st.divider()

    dead_stock_days = st.slider(
        "⏳ Dødt lager hvis ingen salg i (dage)",
        min_value=30,
        max_value=365,
        value=90,
        step=15,
        key="set_dead_stock_days",
    )
    st.caption(
        "Hvis du kun har `sales_30d` i filen, kan vi ikke bevise 'ingen salg i 90 dage'. "
        "Så ved tærskel > 30 bliver 0 i 30 dage behandlet som 🟡 langsomt omsat (ikke 🔴). "
        "Hvis du også uploader `sales_90d` (eller matchende kolonne), bliver det helt præcist."
    )

    st.divider()

    flow_target_pct = st.slider(
        "🎯 Mål for flow-sundhed (% grøn)",
        min_value=50,
        max_value=100,
        value=85,
        step=1,
        key="set_flow_target",
    )
    flow_target = flow_target_pct / 100.0

    st.divider()

    show_details = st.toggle("🔎 Vis detaljer (ekstra forklaring + tomt-om)", value=False, key="set_details")
    st.caption("Detaljer OFF som standard. Slå ON til når du skal forklare beslutningen.")

# Defaults (før upload) for at undgå NameError
if "green_max" not in globals():
    green_max = 120
    yellow_max = 240
    dead_stock_days = 90
    flow_target = 0.85
    show_details = False
    enhed = "stk"

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

        with tab_overview:
            st.success("Flow-status + plan + økonomisk impact (værst → bedst)")
            render_overview_header(result, flow_target, enhed)
            st.divider()
            render_handlinger(result, show_details, enhed)

        with tab_all:
            render_alle_produkter(result, show_details, enhed)

        with tab_settings:
            st.divider()
            download_section(result)

    except Exception as e:
        st.error(f"Kunne ikke læse/beregne filen: {e}")
else:
    with tab_overview:
        st.info("Upload en fil for at se Overblik + Handlinger.")
    with tab_all:
        st.info("Upload en fil for at se alle produkter.")
    with tab_settings:
        st.info("Upload en fil for at få download-knapper.")
