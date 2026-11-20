import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "gold" / "project.duckdb"
DEFAULT_GOLD_PROVENANCE = BASE_DIR / "data" / "gold" / "provenance.jsonl"


def get_db_path() -> Path:
    """Resolve the DuckDB database path from GOLD_DB_PATH or default location."""
    return Path(os.getenv("GOLD_DB_PATH", str(DEFAULT_DB_PATH)))


def _query(sql: str, params: Optional[List[Any]] = None) -> pd.DataFrame:
    """Run a parameterized read-only query against project.duckdb."""
    db_path = get_db_path()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def _build_where_clause(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    rain_intensities: Optional[List[str]] = None,
    require_ok_speed: bool = False,
) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []

    if require_ok_speed:
        clauses.append("speed_flag = 'ok'")

    if route_ids:
        placeholders = ", ".join(["?"] * len(route_ids))
        clauses.append(f"route_id in ({placeholders})")
        params.extend(route_ids)

    if peak_mode == "peak":
        clauses.append("is_peak = true")
    elif peak_mode == "off_peak":
        clauses.append("is_peak = false")

    if rain_intensities:
        placeholders = ", ".join(["?"] * len(rain_intensities))
        clauses.append(f"rain_intensity in ({placeholders})")
        params.extend(rain_intensities)

    if not clauses:
        return "", params
    return "where " + " and ".join(clauses), params


def get_filter_options() -> Dict[str, Any]:
    """Fetch distinct routes and rain intensity categories for sidebar filters."""
    routes_df = _query(
        """
        select distinct route_id, route_name
        from gold.dim_lines
        order by route_id
        """
    )
    intensities_df = _query(
        """
        select distinct rain_intensity
        from gold.vw_bus_rain_impact
        order by rain_intensity
        """
    )
    return {
        "routes": routes_df["route_id"].tolist() if not routes_df.empty else [],
        "route_labels": dict(zip(routes_df["route_id"], routes_df["route_name"]))
        if not routes_df.empty
        else {},
        "rain_intensities": intensities_df["rain_intensity"].tolist()
        if not intensities_df.empty
        else [],
    }


def get_overview_metrics(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    rain_intensities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compute summary KPIs for valid bus speed readings."""
    where_sql, params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        rain_intensities=rain_intensities,
        require_ok_speed=True,
    )
    df = _query(
        f"""
        select
            count(*) as total_ok_readings,
            round(avg(case when rain_intensity = 'dry' then speed_kmh end), 2) as dry_avg_speed_kmh,
            round(avg(case when is_raining = true then speed_kmh end), 2) as rain_avg_speed_kmh,
            round(avg(case when is_raining = true then speed_diff_from_dry_avg end), 2) as rain_avg_speed_diff_kmh
        from gold.vw_bus_rain_impact
        {where_sql}
        """,
        params,
    )
    if df.empty:
        return {
            "total_ok_readings": 0,
            "dry_avg_speed_kmh": None,
            "rain_avg_speed_kmh": None,
            "rain_avg_speed_diff_kmh": None,
        }
    row = df.iloc[0]
    return {
        "total_ok_readings": int(row["total_ok_readings"]),
        "dry_avg_speed_kmh": None if pd.isna(row["dry_avg_speed_kmh"]) else float(row["dry_avg_speed_kmh"]),
        "rain_avg_speed_kmh": None if pd.isna(row["rain_avg_speed_kmh"]) else float(row["rain_avg_speed_kmh"]),
        "rain_avg_speed_diff_kmh": None
        if pd.isna(row["rain_avg_speed_diff_kmh"])
        else float(row["rain_avg_speed_diff_kmh"]),
    }


def get_speed_by_rain_intensity(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    rain_intensities: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Aggregate speed metrics by rainfall intensity bucket."""
    where_sql, params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        rain_intensities=rain_intensities,
        require_ok_speed=True,
    )
    return _query(
        f"""
        select
            rain_intensity,
            count(*) as readings,
            round(avg(speed_kmh), 2) as avg_speed_kmh,
            round(median(speed_kmh), 2) as median_speed_kmh,
            round(avg(speed_diff_from_dry_avg), 2) as avg_diff_from_dry_kmh
        from gold.vw_bus_rain_impact
        {where_sql}
        group by rain_intensity
        order by case rain_intensity
            when 'dry' then 1
            when 'light' then 2
            when 'moderate' then 3
            when 'heavy' then 4
            else 5
        end
        """,
        params,
    )


def get_speed_by_route(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    rain_intensities: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Compare dry vs. rainy average speed by bus route."""
    where_sql, params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        rain_intensities=rain_intensities,
        require_ok_speed=True,
    )
    return _query(
        f"""
        select
            route_id,
            route_name,
            count(*) as readings,
            round(avg(case when rain_intensity = 'dry' then speed_kmh end), 2) as dry_avg_speed_kmh,
            round(avg(case when is_raining = true then speed_kmh end), 2) as rain_avg_speed_kmh,
            round(avg(speed_diff_from_dry_avg), 2) as avg_diff_from_dry_kmh
        from gold.vw_bus_rain_impact
        {where_sql}
        group by route_id, route_name
        order by route_id
        """,
        params,
    )


def get_readings_sample(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    rain_intensities: Optional[List[str]] = None,
    limit: int = 200,
) -> pd.DataFrame:
    """Return recent enriched readings from Gold view."""
    where_sql, params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        rain_intensities=rain_intensities,
        require_ok_speed=False,
    )
    params.append(int(limit))
    return _query(
        f"""
        select
            observed_at,
            route_id,
            direction_id,
            vehicle_id,
            speed_kmh,
            speed_flag,
            station_id,
            station_name,
            precipitation_mm,
            rain_intensity,
            speed_diff_from_dry_avg,
            is_peak
        from gold.vw_bus_rain_impact
        {where_sql}
        order by observed_at desc
        limit ?
        """,
        params,
    )


def get_provenance_summary(provenance_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Read the latest Gold provenance record for dashboard footer attribution."""
    path = provenance_path or DEFAULT_GOLD_PROVENANCE
    if not path.exists():
        return None
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])
