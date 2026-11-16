"""Review app for the three-tier cell check, running in Streamlit in Snowflake.

Reads MOTHERLODE.REVIEW (loaded by scripts/load_review_data.py) and the announcement texts in
MOTHERLODE.RAW.DOCUMENT_TEXT. Three pages: an overview of the verdicts, the escalated cells, and one case with
each value shown where it is written in the deal's documents. Read-only; no AI function is called.
"""
import html
import re

import pandas as pd
import streamlit as st
from snowflake.snowpark.context import get_active_session

st.set_page_config(page_title="Cell check review", layout="wide")
session = get_active_session()

VERDICT_ORDER = ["confirmed", "calculated", "formula_holds_parts_unconfirmed", "not_in_document", "unsure",
                 "contradicted", "formula_fails"]
FIELD_NAMES = {"cash_payments_value": "Cash payments", "exp_comit_value": "Exploration commitment",
               "shares_amount": "Shares issued", "shares_value": "Shares value", "share_price": "Share price",
               "nsr_percent": "Royalty %", "interest_acquired_percent": "Interest acquired %",
               "project_area": "Project area (ha)", "aggregate_deal_value": "Aggregate deal value",
               "shares_value (shares x price)": "Shares value = shares x price", "drilling_amount": "Metres drilled",
               "shares_value_scraped": "Shares value stated"}


@st.cache_data(ttl=600)
def query(sql: str) -> pd.DataFrame:
    return session.sql(sql).to_pandas()


cells = query("SELECT * FROM MOTHERLODE.REVIEW.V_CELLS")
cases = query("SELECT ROW_ID, CASE_ID, PROJECT, BUYER, TICKER, NEWS_DATE, FILE, FILES, CHOSEN_BY, ANNOUNCEMENT_FOUND, "
              "ANALYST FROM MOTHERLODE.REVIEW.V_CASES")
buyer = query("SELECT * FROM MOTHERLODE.REVIEW.V_BUYER")
cells["FIELD_NAME"] = cells["FIELD"].map(FIELD_NAMES).fillna(cells["FIELD"])
buyer_doubt = set(buyer.loc[buyer["P_BUYER"] < 0.5, "ROW_ID"])

page = st.sidebar.radio("Page", ["Overview", "Escalated cells", "Case"])
st.sidebar.caption("Tier 1: code finds where each value is written. Tier 2: a decision model (Jev) judges each "
                   "passage. Tier 3: a frontier model reads the documents for escalated cells only.")

if page == "Overview":
    st.title("Checking the analyst's deal table against the source announcements")
    counts = cells["VERDICT"].value_counts()
    cols = st.columns(6)
    cols[0].metric("Cases", len(cases))
    cols[1].metric("Cells checked", len(cells))
    cols[2].metric("Confirmed", int(counts.get("confirmed", 0)))
    cols[3].metric("Contradicted", int(counts.get("contradicted", 0)))
    cols[4].metric("Unsure", int(counts.get("unsure", 0)))
    cols[5].metric("Escalated", int(cells["ESCALATE"].sum()))

    st.subheader("Verdicts by field")
    table = (cells.pivot_table(index="FIELD_NAME", columns="VERDICT", values="CASE_ID", aggfunc="count", fill_value=0)
             .reindex(columns=[v for v in VERDICT_ORDER if v in set(cells["VERDICT"])]))
    st.bar_chart(table)
    st.dataframe(table, use_container_width=True)

    st.subheader("The buyer column")
    st.write(f"In **{len(buyer_doubt)} of {len(buyer)}** deals the decision model does not read the analyst's "
             "buyer as the buyer in the deal's documents: often a tenement grant with no deal, or the company "
             "selling or farming out the project. One finding per deal, not one per cell.")

    reviewed = cells[cells["TIER3_ANALYST_RIGHT"].notna() | cells["TIER3_VALUE"].notna()]
    if len(reviewed):
        st.subheader("Tier 3, first run")
        summary = reviewed.groupby("VERDICT")["TIER3_ANALYST_RIGHT"].agg(
            reviewed="size", analyst_right=lambda s: int((s == True).sum()),  # noqa: E712
            analyst_wrong=lambda s: int((s == False).sum()))  # noqa: E712
        st.dataframe(summary, use_container_width=True)

elif page == "Escalated cells":
    st.title("Escalated cells")
    left, middle, right = st.columns(3)
    verdicts = left.multiselect("Verdict", ["contradicted", "unsure", "formula_fails"], ["contradicted", "unsure"])
    fields = middle.multiselect("Field", sorted(cells["FIELD_NAME"].unique()))
    hide_buyer = right.checkbox("Leave out deals whose documents do not show the buyer as buyer", value=True)
    view = cells[cells["ESCALATE"] & cells["VERDICT"].isin(verdicts)]
    if fields:
        view = view[view["FIELD_NAME"].isin(fields)]
    if hide_buyer:
        view = view[~view["ROW_ID"].isin(buyer_doubt)]
    st.caption(f"{len(view)} cells in {view['CASE_ID'].nunique()} cases")
    st.dataframe(view[["CASE_ID", "FIELD_NAME", "VALUE", "VERDICT", "P_CONFIRMED", "PROPOSAL", "P_PROPOSAL",
                       "TIER3_VALUE", "TIER3_ANALYST_RIGHT"]].sort_values(["VERDICT", "CASE_ID"]),
                 use_container_width=True, hide_index=True)

else:
    st.title("One case")
    escalated_per_case = cells[cells["ESCALATE"]].groupby("CASE_ID").size()
    labels = {cid: f"{cid}  ({escalated_per_case.get(cid, 0)} escalated)" for cid in sorted(cases["CASE_ID"])}
    case_id = st.selectbox("Case", list(labels), format_func=labels.get)
    case = cases[cases["CASE_ID"] == case_id].iloc[0]
    files = [f.strip('"[] \n') for f in str(case["FILES"]).split(",") if f.strip('"[] \n')]
    p_buyer = buyer.loc[buyer["ROW_ID"] == case["ROW_ID"], "P_BUYER"]

    a, b, c = st.columns(3)
    a.write(f"**Project:** {case['PROJECT']}  \n**Buyer (analyst):** {case['BUYER']} ({case['TICKER']})  \n"
            f"**News date:** {case['NEWS_DATE']}")
    b.write("**Documents:** " + ", ".join(files) + f"  \n**Chosen by:** {case['CHOSEN_BY']}")
    c.write(f"**P(analyst's buyer is the buyer):** {float(p_buyer.iloc[0]):.2f}" if len(p_buyer) else "")

    mine = cells[cells["CASE_ID"] == case_id]
    st.dataframe(mine[["FIELD_NAME", "VALUE", "VERDICT", "P_CONFIRMED", "PROPOSAL", "TIER3_VALUE",
                       "TIER3_ANALYST_RIGHT"]], use_container_width=True, hide_index=True)

    field = st.selectbox("Show where a value is written", list(mine["FIELD_NAME"]))
    row = mine[mine["FIELD_NAME"] == field].iloc[0]
    value = float(row["VALUE"])
    if row["FIELD"] in ("nsr_percent", "interest_acquired_percent"):
        forms = {f"{value:g}%", f"{value:g} %", f"{value:g} per cent", f"{value:g} percent"}
    elif row["FIELD"] == "project_area":          # stored in hectares; the PDF often gives km²
        km2 = f"{value / 100:g}"          # "40km west" is a distance, not an area: a km² unit is required
        forms = {f"{value:,.0f} ha", f"{value:,.0f}ha", f"{value:,.0f} hectares", f"{km2}km²", f"{km2} km²",
                 f"{km2}km2", f"{km2} km2", f"{km2} sq km", f"{km2} square kilomet"}
    else:
        forms = {f"{value:,.0f}", f"{value:.0f}", f"{value:g}"} if value >= 1 else {f"{value:g}"}
        if value >= 1e5:
            forms |= {f"{value / 1e6:g} million", f"{value / 1e6:g}m"}
    pattern = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    ids = ", ".join("'" + f.replace(".pdf", "").replace("'", "") + "'" for f in files)
    texts = query(f"SELECT DOC_ID, TEXT FROM MOTHERLODE.RAW.DOCUMENT_TEXT WHERE DOC_ID IN ({ids})")
    shown = 0
    for _, doc in texts.iterrows():
        text = " ".join(str(doc["TEXT"]).split())
        for m in list(re.finditer(r"(?<![\d.,])(" + pattern + r")(?![\d])", text, re.I))[:3]:
            before, hit, after = text[max(0, m.start() - 250):m.start()], m.group(0), text[m.end():m.end() + 250]
            st.markdown(f"**{doc['DOC_ID']}.pdf**: …{html.escape(before)}<mark>{html.escape(hit)}</mark>"
                        f"{html.escape(after)}…", unsafe_allow_html=True)
            shown += 1
    if not shown:
        st.info("The value is not written as such in the deal's documents (it may be a sum, a split, or "
                "converted from km²; see the verdict above).")
