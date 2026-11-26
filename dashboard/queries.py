import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

import duckdb
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "gold" / "project.duckdb"
DEFAULT_GOLD_PROVENANCE = BASE_DIR / "data" / "gold" / "provenance.jsonl"
DEFAULT_GOLD_DB_URL = (
    "https://github.com/plcrisp/sptrans-rain-impact/releases/download/v1.0-data/project.duckdb"
)


def get_db_path() -> Path:
    """Resolve the DuckDB database path from GOLD_DB_PATH or default location."""
    return Path(os.getenv("GOLD_DB_PATH", str(DEFAULT_DB_PATH)))


def ensure_gold_db(url: Optional[str] = None) -> Path:
    """Ensure project.duckdb exists locally, downloading from GitHub Release if missing."""
    db_path = get_db_path()
    if db_path.exists():
        return db_path

    download_url = url or os.getenv("GOLD_DB_URL") or DEFAULT_GOLD_DB_URL
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(".duckdb.tmp")
    try:
        req = urllib.request.Request(download_url, headers={"User-Agent": "sptrans-rain-impact"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp_path, db_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return db_path


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
    days_of_week: Optional[List[int]] = None,
    rain_intensities: Optional[List[str]] = None,
    require_ok_speed: bool = False,
    include_unknown: bool = False,
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

    if days_of_week:
        placeholders = ", ".join(["?"] * len(days_of_week))
        clauses.append(f"day_of_week in ({placeholders})")
        params.extend(int(d) for d in days_of_week)

    if not include_unknown:
        clauses.append("rain_intensity <> 'unknown'")

    if rain_intensities:
        placeholders = ", ".join(["?"] * len(rain_intensities))
        clauses.append(f"rain_intensity in ({placeholders})")
        params.extend(rain_intensities)

    if not clauses:
        return "", params
    return "where " + " and ".join(clauses), params


def get_filter_options(min_readings_for_default: int = 30) -> Dict[str, Any]:
    """Fetch distinct routes, rain intensities, and routes meeting minimum sample in both dry and rain."""
    routes_df = _query(
        """
        select distinct route_id, route_name
        from gold.dim_lines
        order by route_id
        """
    )
    comparable_df = _query(
        """
        select route_id
        from gold.vw_bus_rain_impact
        where speed_flag = 'ok'
        group by route_id
        having sum(case when rain_intensity = 'dry' then 1 else 0 end) >= ?
           and sum(case when is_raining = true then 1 else 0 end) >= ?
        order by route_id
        """,
        [int(min_readings_for_default), int(min_readings_for_default)],
    )
    intensities_df = _query(
        """
        select distinct rain_intensity
        from gold.vw_bus_rain_impact
        order by case rain_intensity
            when 'dry' then 1
            when 'light' then 2
            when 'moderate' then 3
            when 'heavy' then 4
            else 5
        end
        """
    )
    days_df = _query(
        """
        select distinct day_of_week
        from gold.vw_bus_rain_impact
        where day_of_week is not null
        order by day_of_week
        """
    )
    period_df = _query(
        """
        select
            min(date_sp) as min_date,
            max(date_sp) as max_date,
            count(distinct date_sp) as days_count
        from gold.vw_bus_rain_impact
        """
    )
    all_routes = routes_df["route_id"].tolist() if not routes_df.empty else []
    comparable_routes = comparable_df["route_id"].tolist() if not comparable_df.empty else []
    min_date = period_df.iloc[0]["min_date"] if not period_df.empty else None
    max_date = period_df.iloc[0]["max_date"] if not period_df.empty else None
    days_count = int(period_df.iloc[0]["days_count"]) if not period_df.empty else 0
    return {
        "routes": all_routes,
        "default_routes": comparable_routes if comparable_routes else all_routes,
        "route_labels": dict(zip(routes_df["route_id"], routes_df["route_name"]))
        if not routes_df.empty
        else {},
        "rain_intensities": intensities_df["rain_intensity"].tolist()
        if not intensities_df.empty
        else [],
        "days_of_week": [int(d) for d in days_df["day_of_week"].tolist()]
        if not days_df.empty
        else [],
        "min_date": min_date,
        "max_date": max_date,
        "days_count": days_count,
    }


def get_overview_metrics(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    days_of_week: Optional[List[int]] = None,
    rain_intensities: Optional[List[str]] = None,
    include_unknown: bool = False,
) -> Dict[str, Any]:
    """Compute summary KPIs and sample counts for valid bus speed readings."""
    where_sql, params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        days_of_week=days_of_week,
        rain_intensities=rain_intensities,
        require_ok_speed=True,
        include_unknown=include_unknown,
    )
    df = _query(
        f"""
        select
            count(*) as total_ok_readings,
            sum(case when rain_intensity = 'dry' then 1 else 0 end) as dry_readings,
            sum(case when is_raining = true then 1 else 0 end) as rain_readings,
            count(distinct case when is_raining = true then route_id end) as rain_routes_count,
            round(avg(case when is_raining = true and dry_baseline_speed_kmh is not null then dry_baseline_speed_kmh end), 2) as dry_avg_speed_kmh,
            round(avg(case when is_raining = true and dry_baseline_speed_kmh is not null then speed_kmh end), 2) as rain_avg_speed_kmh,
            round(avg(case when is_raining = true and dry_baseline_speed_kmh is not null then speed_diff_from_dry_avg end), 2) as rain_avg_speed_diff_kmh
        from gold.vw_bus_rain_impact
        {where_sql}
        """,
        params,
    )

    unknown_where, unknown_params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        days_of_week=days_of_week,
        rain_intensities=None,
        require_ok_speed=True,
        include_unknown=True,
    )
    unknown_clause = (
        f"{unknown_where} and rain_intensity = 'unknown'"
        if unknown_where
        else "where rain_intensity = 'unknown'"
    )
    df_unknown = _query(
        f"select count(*) as unknown_readings from gold.vw_bus_rain_impact {unknown_clause}",
        unknown_params,
    )
    unknown_count = int(df_unknown.iloc[0]["unknown_readings"]) if not df_unknown.empty else 0

    if df.empty or int(df.iloc[0]["total_ok_readings"]) == 0:
        return {
            "total_ok_readings": 0,
            "dry_readings": 0,
            "rain_readings": 0,
            "rain_routes_count": 0,
            "unknown_readings": unknown_count,
            "dry_avg_speed_kmh": None,
            "rain_avg_speed_kmh": None,
            "rain_avg_speed_diff_kmh": None,
            "rain_avg_speed_diff_pct": None,
        }
    row = df.iloc[0]
    dry_avg = None if pd.isna(row["dry_avg_speed_kmh"]) else float(row["dry_avg_speed_kmh"])
    rain_avg = None if pd.isna(row["rain_avg_speed_kmh"]) else float(row["rain_avg_speed_kmh"])
    diff_avg = None if pd.isna(row["rain_avg_speed_diff_kmh"]) else float(row["rain_avg_speed_diff_kmh"])
    diff_pct = (
        round((diff_avg / dry_avg) * 100.0, 1)
        if diff_avg is not None and dry_avg not in (None, 0.0)
        else None
    )
    return {
        "total_ok_readings": int(row["total_ok_readings"]),
        "dry_readings": int(row["dry_readings"]),
        "rain_readings": int(row["rain_readings"]),
        "rain_routes_count": int(row["rain_routes_count"]),
        "unknown_readings": unknown_count,
        "dry_avg_speed_kmh": dry_avg,
        "rain_avg_speed_kmh": rain_avg,
        "rain_avg_speed_diff_kmh": diff_avg,
        "rain_avg_speed_diff_pct": diff_pct,
    }


def get_speed_by_route(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    days_of_week: Optional[List[int]] = None,
    rain_intensities: Optional[List[str]] = None,
    include_unknown: bool = False,
    min_readings: int = 1,
) -> pd.DataFrame:
    """Compare dry vs. rainy average speed by bus route (enforcing min_readings on both conditions)."""
    where_sql, params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        days_of_week=days_of_week,
        rain_intensities=rain_intensities,
        require_ok_speed=True,
        include_unknown=include_unknown,
    )
    params.extend([int(min_readings), int(min_readings)])
    return _query(
        f"""
        select
            route_id,
            route_name,
            count(*) as readings,
            sum(case when rain_intensity = 'dry' then 1 else 0 end) as dry_readings,
            sum(case when is_raining = true then 1 else 0 end) as rain_readings,
            round(avg(case when is_raining = true and dry_baseline_speed_kmh is not null then dry_baseline_speed_kmh end), 2) as dry_avg_speed_kmh,
            round(avg(case when is_raining = true and dry_baseline_speed_kmh is not null then speed_kmh end), 2) as rain_avg_speed_kmh,
            round(avg(case when is_raining = true and dry_baseline_speed_kmh is not null then speed_diff_from_dry_avg end), 2) as avg_diff_from_dry_kmh,
            round(
                (
                    avg(case when is_raining = true and dry_baseline_speed_kmh is not null then speed_diff_from_dry_avg end)
                    / nullif(avg(case when is_raining = true and dry_baseline_speed_kmh is not null then dry_baseline_speed_kmh end), 0)
                ) * 100.0,
                1
            ) as avg_diff_pct
        from gold.vw_bus_rain_impact
        {where_sql}
        group by route_id, route_name
        having sum(case when rain_intensity = 'dry' then 1 else 0 end) >= ?
           and sum(case when is_raining = true then 1 else 0 end) >= ?
        order by avg_diff_from_dry_kmh asc nulls last, route_id
        """,
        params,
    )


def get_hourly_profile(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    days_of_week: Optional[List[int]] = None,
    min_readings: int = 10,
    hour_start: int = 5,
    hour_end: int = 23,
) -> pd.DataFrame:
    """Return matched dry baseline vs. observed rainy bus speed by hour of day."""
    where_sql, params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        days_of_week=days_of_week,
        require_ok_speed=True,
        include_unknown=False,
    )
    extra = "is_raining = true and dry_baseline_speed_kmh is not null and hour_of_day between ? and ?"
    full_where = f"{where_sql} and {extra}" if where_sql else f"where {extra}"
    params.extend([int(hour_start), int(hour_end), int(min_readings)])
    return _query(
        f"""
        with matched as (
            select
                hour_of_day,
                count(*) as readings,
                round(avg(dry_baseline_speed_kmh), 2) as dry_speed_kmh,
                round(avg(speed_kmh), 2) as rain_speed_kmh
            from gold.vw_bus_rain_impact
            {full_where}
            group by hour_of_day
            having count(*) >= ?
        )
        select hour_of_day, 'Seco (mesmo trecho)' as condition, readings, dry_speed_kmh as avg_speed_kmh
        from matched
        union all
        select hour_of_day, 'Chuva' as condition, readings, rain_speed_kmh as avg_speed_kmh
        from matched
        order by hour_of_day, condition
        """,
        params,
    )


def get_daily_summary(
    route_ids: Optional[List[str]] = None,
    peak_mode: str = "all",
    days_of_week: Optional[List[int]] = None,
    min_rain_readings: int = 100,
) -> pd.DataFrame:
    """Return daily summary of valid readings, max rainfall, and average speed impact."""
    where_sql, params = _build_where_clause(
        route_ids=route_ids,
        peak_mode=peak_mode,
        days_of_week=days_of_week,
        require_ok_speed=True,
        include_unknown=False,
    )
    params.append(int(min_rain_readings))
    return _query(
        f"""
        select
            date_sp,
            sum(case when rain_intensity = 'dry' then 1 else 0 end) as dry_readings,
            sum(case when is_raining = true then 1 else 0 end) as rain_readings,
            round(max(precipitation_mm), 2) as max_precipitation_mm,
            round(avg(case when is_raining = true and dry_baseline_speed_kmh is not null then speed_diff_from_dry_avg end), 2) as avg_diff_from_dry_kmh
        from gold.vw_bus_rain_impact
        {where_sql}
        group by date_sp
        having sum(case when is_raining = true then 1 else 0 end) >= ?
        order by date_sp
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
