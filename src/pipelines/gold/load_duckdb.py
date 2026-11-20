import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[3]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.core.config import Config
from src.utils.logger import get_logger
from src.utils.provenance import record_provenance, to_relative_path
from src.utils.station_parser import get_sp_stations

logger = get_logger("load_duckdb")


def connect_db(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open (or create) the DuckDB analytical database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def create_schemas(con: duckdb.DuckDBPyConnection) -> None:
    """Create silver and gold schemas if they do not exist."""
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")


def _load_selected_lines_df(selected_lines_path: Path) -> pd.DataFrame:
    data = json.loads(selected_lines_path.read_text(encoding="utf-8"))
    rows = []
    for line in data.get("lines", []):
        for direction in line.get("directions", []):
            rows.append(
                {
                    "route_code": int(direction["codigoLinha"]),
                    "route_id": str(line["route_id"]),
                    "route_name": str(line["route_long_name"]),
                    "direction_id": int(direction["direction_id"]),
                    "service_direction_code": int(direction.get("sl", 1)),
                }
            )
    return pd.DataFrame(rows)


def _load_stations_df(stations_path: Path) -> pd.DataFrame:
    data = json.loads(stations_path.read_text(encoding="utf-8"))
    rows = []
    for st in data:
        rows.append(
            {
                "station_id": str(st.get("station_id", "")),
                "station_name": str(st.get("station_name", "")),
                "city": str(st.get("city", "")),
                "state": str(st.get("state", "")),
                "latitude": float(st["latitude"]) if st.get("latitude") is not None else None,
                "longitude": float(st["longitude"]) if st.get("longitude") is not None else None,
                "status": str(st.get("status", "")),
                "station_type": str(st.get("station_type", "")),
            }
        )
    return pd.DataFrame(rows)


def load_silver_into_db(
    con: duckdb.DuckDBPyConnection,
    enriched_parquet: Path,
    selected_lines_path: Path,
    stations_path: Path,
) -> None:
    """Load Silver Parquet and reference catalogs into DuckDB silver schema."""
    if not enriched_parquet.exists():
        raise FileNotFoundError(f"Enriched Silver parquet not found: {enriched_parquet}")
    if not selected_lines_path.exists():
        raise FileNotFoundError(f"Selected lines file not found: {selected_lines_path}")
    if not stations_path.exists():
        logger.info("Stations catalog not found at %s; fetching from CEMADEN...", stations_path)
        get_sp_stations(str(stations_path.parent))
    if not stations_path.exists():
        raise FileNotFoundError(f"Stations catalog not found: {stations_path}")

    con.execute(
        f"""
        CREATE OR REPLACE TABLE silver.sptrans_rain_enriched AS
        SELECT * FROM read_parquet('{enriched_parquet.as_posix()}')
        """
    )

    df_lines = _load_selected_lines_df(selected_lines_path)
    con.register("_tmp_selected_lines", df_lines)
    con.execute("CREATE OR REPLACE TABLE silver.selected_lines AS SELECT * FROM _tmp_selected_lines")
    con.unregister("_tmp_selected_lines")

    df_stations = _load_stations_df(stations_path)
    con.register("_tmp_stations_catalog", df_stations)
    con.execute("CREATE OR REPLACE TABLE silver.stations_catalog AS SELECT * FROM _tmp_stations_catalog")
    con.unregister("_tmp_stations_catalog")


def build_gold_star_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Build Gold dimension tables, fact table, and analytical view in SQL."""
    con.execute(
        """
        CREATE OR REPLACE TABLE gold.dim_lines AS
        SELECT DISTINCT
            route_code,
            route_id,
            route_name,
            direction_id,
            service_direction_code
        FROM silver.selected_lines
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE gold.dim_stations AS
        SELECT DISTINCT
            station_id,
            station_name,
            city,
            state,
            latitude,
            longitude,
            status,
            station_type
        FROM silver.stations_catalog
        WHERE station_id IS NOT NULL AND station_id <> ''
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE gold.dim_time AS
        SELECT DISTINCT
            hour_sp AS hour_key,
            CAST(hour_sp AS DATE) AS date_sp,
            EXTRACT('hour' FROM hour_sp)::INTEGER AS hour_of_day,
            EXTRACT('isodow' FROM hour_sp)::INTEGER AS day_of_week,
            (EXTRACT('isodow' FROM hour_sp) >= 6)::BOOLEAN AS is_weekend,
            (
                EXTRACT('isodow' FROM hour_sp) <= 5
                AND (
                    (EXTRACT('hour' FROM hour_sp) >= 6 AND EXTRACT('hour' FROM hour_sp) < 9)
                    OR (EXTRACT('hour' FROM hour_sp) >= 17 AND EXTRACT('hour' FROM hour_sp) < 20)
                )
            )::BOOLEAN AS is_peak
        FROM silver.sptrans_rain_enriched
        WHERE hour_sp IS NOT NULL
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE gold.fact_bus_readings AS
        SELECT
            CAST(vehicle_id AS VARCHAR) AS vehicle_id,
            CAST(codigoLinha AS INTEGER) AS route_code,
            CAST(station_id AS VARCHAR) AS station_id,
            hour_sp AS hour_key,
            timestamp_sp AS observed_at,
            CAST(lat AS DOUBLE) AS latitude,
            CAST(lon AS DOUBLE) AS longitude,
            CAST(speed_kmh AS DOUBLE) AS speed_kmh,
            CAST(speed_flag AS VARCHAR) AS speed_flag,
            CAST(dist_to_station_m AS DOUBLE) AS distance_to_station_m,
            CAST(rain_mm AS DOUBLE) AS precipitation_mm,
            CAST(rain_readings_in_hour AS INTEGER) AS rain_readings_in_hour,
            CAST(is_raining AS BOOLEAN) AS is_raining,
            CAST(rain_is_missing AS BOOLEAN) AS rain_is_missing,
            CAST(rain_intensity AS VARCHAR) AS rain_intensity,
            CAST(dry_baseline_speed_kmh AS DOUBLE) AS dry_baseline_speed_kmh,
            CAST(speed_diff_from_dry_avg AS DOUBLE) AS speed_diff_from_dry_avg
        FROM silver.sptrans_rain_enriched
        WHERE station_id IS NOT NULL
        """
    )

    con.execute(
        """
        CREATE OR REPLACE VIEW gold.vw_bus_rain_impact AS
        SELECT
            f.vehicle_id,
            f.route_code,
            l.route_id,
            l.route_name,
            l.direction_id,
            f.station_id,
            s.station_name,
            s.status AS station_status,
            f.hour_key,
            t.date_sp,
            t.hour_of_day,
            t.day_of_week,
            t.is_weekend,
            t.is_peak,
            f.observed_at,
            f.latitude,
            f.longitude,
            f.speed_kmh,
            f.speed_flag,
            f.distance_to_station_m,
            f.precipitation_mm,
            f.rain_readings_in_hour,
            f.is_raining,
            f.rain_is_missing,
            f.rain_intensity,
            f.dry_baseline_speed_kmh,
            f.speed_diff_from_dry_avg
        FROM gold.fact_bus_readings f
        JOIN gold.dim_lines l USING (route_code)
        JOIN gold.dim_stations s USING (station_id)
        JOIN gold.dim_time t USING (hour_key)
        """
    )


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


def verify_gold_model(con: duckdb.DuckDBPyConnection) -> Dict[str, int]:
    """Check composite grain uniqueness and absence of orphan foreign keys."""
    duplicate_grain = _scalar(
        con,
        """
        SELECT COUNT(*) FROM (
            SELECT vehicle_id, route_code, observed_at
            FROM gold.fact_bus_readings
            GROUP BY vehicle_id, route_code, observed_at
            HAVING COUNT(*) > 1
        )
        """,
    )
    orphan_lines = _scalar(
        con,
        """
        SELECT COUNT(*) FROM gold.fact_bus_readings f
        LEFT JOIN gold.dim_lines d USING (route_code)
        WHERE d.route_code IS NULL
        """,
    )
    orphan_stations = _scalar(
        con,
        """
        SELECT COUNT(*) FROM gold.fact_bus_readings f
        LEFT JOIN gold.dim_stations d USING (station_id)
        WHERE d.station_id IS NULL
        """,
    )
    orphan_time = _scalar(
        con,
        """
        SELECT COUNT(*) FROM gold.fact_bus_readings f
        LEFT JOIN gold.dim_time d USING (hour_key)
        WHERE d.hour_key IS NULL
        """,
    )

    counts = {
        "fact_rows": _scalar(con, "SELECT COUNT(*) FROM gold.fact_bus_readings"),
        "dim_lines_rows": _scalar(con, "SELECT COUNT(*) FROM gold.dim_lines"),
        "dim_stations_rows": _scalar(con, "SELECT COUNT(*) FROM gold.dim_stations"),
        "dim_time_rows": _scalar(con, "SELECT COUNT(*) FROM gold.dim_time"),
        "view_rows": _scalar(con, "SELECT COUNT(*) FROM gold.vw_bus_rain_impact"),
        "duplicate_grain_rows": duplicate_grain,
        "orphan_lines_rows": orphan_lines,
        "orphan_stations_rows": orphan_stations,
        "orphan_time_rows": orphan_time,
    }
    logger.info(
        "Gold integrity check | fact=%s | dim_lines=%s | dim_stations=%s | dim_time=%s | "
        "view=%s | dup_grain=%s | orphan_lines=%s | orphan_stations=%s | orphan_time=%s",
        counts["fact_rows"],
        counts["dim_lines_rows"],
        counts["dim_stations_rows"],
        counts["dim_time_rows"],
        counts["view_rows"],
        duplicate_grain,
        orphan_lines,
        orphan_stations,
        orphan_time,
    )

    if any(
        v > 0
        for v in (duplicate_grain, orphan_lines, orphan_stations, orphan_time)
    ):
        raise RuntimeError(f"Gold integrity verification failed: {counts}")

    return counts


def run_load() -> int:
    """Load Silver datasets into DuckDB silver schema (Gold models are built by dbt)."""
    Config.ensure_dirs()
    db_path = Path(Config.GOLD_DB_PATH)
    enriched_parquet = Path(Config.ENRICHED_SILVER_PARQUET_PATH)
    selected_lines_path = Path(Config.SELECTED_LINES_PATH)
    stations_path = Path(Config.STATIONS_DATA_DIR) / "cemaden_sp_stations.json"

    logger.info("Loading Silver data into DuckDB at %s...", db_path)
    con = connect_db(db_path)
    try:
        create_schemas(con)
        load_silver_into_db(con, enriched_parquet, selected_lines_path, stations_path)
        # build_gold_star_schema(con)  # Replaced by declarative dbt models (Stage 4)
        silver_rows = _scalar(con, "SELECT COUNT(*) FROM silver.sptrans_rain_enriched")
    finally:
        con.close()

    record_provenance(
        target_dir=db_path.parent,
        stage="load_gold_duckdb",
        source=[
            to_relative_path(enriched_parquet),
            to_relative_path(selected_lines_path),
            to_relative_path(stations_path),
        ],
        output=db_path.name,
        records=silver_rows,
        extra={"silver_enriched_rows": silver_rows},
    )
    logger.info("Silver DuckDB load completed successfully (%s rows).", silver_rows)
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load Silver Parquet into DuckDB and build the Gold Star Schema."
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    parse_args(argv)
    return run_load()


if __name__ == "__main__":
    sys.exit(main())
