import streamlit as st

import queries

st.set_page_config(
    page_title="SPTrans Rain Impact Dashboard",
    layout="wide",
)

st.title("SPTrans Rain Impact on Bus Speed")
st.markdown(
    "Analytical dashboard built on the Gold layer (`data/gold/project.duckdb`) "
    "combining SPTrans Olho Vivo GPS telemetry with CEMADEN rainfall stations."
)

db_path = queries.get_db_path()
if not db_path.exists():
    st.warning(
        f"Gold database not found at `{db_path}`. "
        "Run `python src/pipeline.py` first to build the Silver and Gold layers."
    )
    st.stop()

options = queries.get_filter_options()

st.sidebar.header("Filters")
selected_routes = st.sidebar.multiselect(
    "Routes",
    options=options["routes"],
    default=options["routes"],
)
peak_choice = st.sidebar.radio(
    "Time Window",
    options=["all", "peak", "off_peak"],
    format_func=lambda x: {"all": "All hours", "peak": "Peak hours only", "off_peak": "Off-peak only"}[x],
)
selected_intensities = st.sidebar.multiselect(
    "Rain Intensity",
    options=options["rain_intensities"],
    default=options["rain_intensities"],
)

kpis = queries.get_overview_metrics(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    rain_intensities=selected_intensities,
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Valid Speed Readings", f"{kpis['total_ok_readings']:,}")
c2.metric(
    "Dry Avg Speed",
    f"{kpis['dry_avg_speed_kmh']:.2f} km/h" if kpis["dry_avg_speed_kmh"] is not None else "N/A",
)
c3.metric(
    "Rain Avg Speed",
    f"{kpis['rain_avg_speed_kmh']:.2f} km/h" if kpis["rain_avg_speed_kmh"] is not None else "N/A",
)
c4.metric(
    "Rain vs. Dry Delta",
    f"{kpis['rain_avg_speed_diff_kmh']:+.2f} km/h"
    if kpis["rain_avg_speed_diff_kmh"] is not None
    else "N/A",
)

col_left, col_right = st.columns(2)

intensity_df = queries.get_speed_by_rain_intensity(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    rain_intensities=selected_intensities,
)
with col_left:
    st.subheader("Average Speed by Rain Intensity")
    if not intensity_df.empty:
        st.bar_chart(intensity_df, x="rain_intensity", y="avg_speed_kmh")
        st.dataframe(intensity_df, hide_index=True, use_container_width=True)
    else:
        st.info("No valid speed readings match the selected filters.")

route_df = queries.get_speed_by_route(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    rain_intensities=selected_intensities,
)
with col_right:
    st.subheader("Average Speed Comparison by Route")
    if not route_df.empty:
        st.dataframe(route_df, hide_index=True, use_container_width=True)
    else:
        st.info("No route breakdown available for the selected filters.")

st.subheader("Recent Enriched Readings")
sample_df = queries.get_readings_sample(
    route_ids=selected_routes,
    peak_mode=peak_choice,
    rain_intensities=selected_intensities,
    limit=200,
)
st.dataframe(sample_df, hide_index=True, use_container_width=True)

prov = queries.get_provenance_summary()
if prov:
    st.caption(
        f"Source: SPTrans Olho Vivo API & CEMADEN | Stage: `{prov.get('stage')}` | "
        f"Gold Fact Records: {prov.get('records')} | Updated at: {prov.get('executed_at')}"
    )
else:
    st.caption("Source: SPTrans Olho Vivo API & CEMADEN (data/gold/project.duckdb).")
