from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("silver_enrichment")


def load_silver_table(parquet_path: Path, csv_fallback: Path) -> Tuple[pd.DataFrame, Path]:
    """Load a Silver table from Parquet, falling back to CSV when needed."""
    if parquet_path.exists():
        return pd.read_parquet(parquet_path), parquet_path
    if csv_fallback.exists():
        return pd.read_csv(csv_fallback), csv_fallback
    raise FileNotFoundError(
        f"Silver input not found at {parquet_path} or {csv_fallback}."
    )


def _normalize_timestamp_sp(series: pd.Series) -> pd.Series:
    """Normalize timestamp_sp values to tz-aware America/Sao_Paulo datetimes."""
    dt = pd.to_datetime(series, errors="coerce")
    if getattr(dt.dt, "tz", None) is None:
        return dt.dt.tz_localize(
            Config.TZ_LOCAL, ambiguous="infer", nonexistent="shift_forward"
        )
    return dt.dt.tz_convert(Config.TZ_LOCAL)


def prepare_sptrans_for_join(df_sptrans: pd.DataFrame) -> pd.DataFrame:
    """Normalize join keys (station_id, hour_sp) on SPTrans Silver readings."""
    df = df_sptrans.copy()
    if df.empty:
        df["station_id"] = pd.Series(dtype="object")
        df["timestamp_sp"] = pd.Series(dtype=f"datetime64[ns, {Config.TZ_LOCAL}]")
        df["hour_sp"] = pd.Series(dtype=f"datetime64[ns, {Config.TZ_LOCAL}]")
        return df

    df["station_id"] = (
        df["station_id"].where(df["station_id"].notna(), None).astype(object)
    )
    df["station_id"] = df["station_id"].apply(
        lambda v: str(v).strip() if v is not None and str(v).strip() else None
    )
    df["timestamp_sp"] = _normalize_timestamp_sp(df["timestamp_sp"])
    df["hour_sp"] = df["timestamp_sp"].dt.floor("h")
    return df


def aggregate_cemaden_hourly(df_rain: pd.DataFrame) -> pd.DataFrame:
    """Aggregate CEMADEN Silver readings to unique (station_id, hour_sp) grain."""
    cols = ["station_id", "hour_sp", "rain_mm", "rain_readings_in_hour", "rain_is_missing"]
    if df_rain.empty:
        return pd.DataFrame(columns=cols)

    df = df_rain.copy()
    df["station_id"] = df["station_id"].astype(str).str.strip()
    df["timestamp_sp"] = _normalize_timestamp_sp(df["timestamp_sp"])
    df["hour_sp"] = df["timestamp_sp"].dt.floor("h")
    df["rain_mm"] = pd.to_numeric(df["rain_mm"], errors="coerce").astype("float64")
    df["is_missing"] = df["is_missing"].astype(bool)
    df["_is_valid_reading"] = (~df["is_missing"]) & df["rain_mm"].notna()

    grouped = (
        df.groupby(["station_id", "hour_sp"], as_index=False)
        .agg(
            rain_mm=("rain_mm", lambda s: round(float(s.sum(min_count=1)), 2) if s.notna().any() else np.nan),
            rain_readings_in_hour=("_is_valid_reading", "sum"),
        )
    )
    grouped["rain_readings_in_hour"] = grouped["rain_readings_in_hour"].astype("int32")
    grouped["rain_is_missing"] = (grouped["rain_readings_in_hour"] == 0) | grouped["rain_mm"].isna()
    return grouped[cols]


def classify_rain_intensity(rain_mm: Any) -> str:
    """Classify hourly precipitation (mm/h) into standard intensity buckets."""
    if pd.isna(rain_mm):
        return "unknown"
    val = float(rain_mm)
    if val <= 0.0:
        return "dry"
    if val <= 2.5:
        return "light"
    if val <= 10.0:
        return "moderate"
    return "heavy"


def enrich_sptrans_with_rain(
    df_sptrans: pd.DataFrame,
    df_rain: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Left-join SPTrans readings with hourly CEMADEN rainfall and compute derived metrics."""
    left_df = prepare_sptrans_for_join(df_sptrans)
    right_df = aggregate_cemaden_hourly(df_rain)

    left_keys = set(
        zip(left_df["station_id"].dropna(), left_df.loc[left_df["station_id"].notna(), "hour_sp"])
    )
    right_keys = set(zip(right_df["station_id"], right_df["hour_sp"]))

    logger.info(
        f"Pre-join key check | only_sptrans={len(left_keys - right_keys)} | "
        f"only_cemaden={len(right_keys - left_keys)} | matched_keys={len(left_keys & right_keys)}"
    )

    rows_before = len(left_df)
    merged = left_df.merge(
        right_df,
        on=["station_id", "hour_sp"],
        how="left",
        validate="many_to_one",
        indicator="_join_status",
    )
    rows_after = len(merged)

    if rows_before != rows_after:
        raise ValueError(
            f"Join changed row count unexpectedly: before={rows_before}, after={rows_after}"
        )

    unmatched_key_rows = int((merged["_join_status"] == "left_only").sum())
    merged["rain_matched"] = merged["_join_status"] == "both"
    merged["rain_is_missing"] = merged["rain_is_missing"].fillna(True).astype(bool)
    merged["rain_readings_in_hour"] = merged["rain_readings_in_hour"].fillna(0).astype("int32")
    merged = merged.drop(columns=["_join_status"])

    # Post-join derived attributes
    merged["rain_intensity"] = merged["rain_mm"].apply(classify_rain_intensity)
    merged["is_raining"] = np.where(
        merged["rain_mm"].isna(),
        pd.NA,
        merged["rain_mm"] > 0.0,
    )

    ok_dry = merged[(merged["speed_flag"] == "ok") & (merged["rain_intensity"] == "dry")]
    if not ok_dry.empty:
        baseline_levels = [
            (["route_id", "direction_id", "station_id", "is_peak"], "_base_station_peak"),
            (["route_id", "direction_id", "station_id"], "_base_station"),
            (["route_id", "direction_id", "is_peak"], "_base_route_peak"),
            (["route_id", "direction_id"], "_base_route"),
        ]
        baseline_series = pd.Series(np.nan, index=merged.index, dtype="float64")
        for group_cols, col_name in baseline_levels:
            agg = (
                ok_dry.groupby(group_cols, as_index=False, dropna=False)["speed_kmh"]
                .mean()
                .rename(columns={"speed_kmh": col_name})
            )
            merged = merged.merge(agg, on=group_cols, how="left")
            baseline_series = baseline_series.fillna(merged[col_name])
            merged = merged.drop(columns=[col_name])

        merged["dry_baseline_speed_kmh"] = baseline_series.round(2)
    else:
        merged["dry_baseline_speed_kmh"] = np.nan

    merged["speed_diff_from_dry_avg"] = np.where(
        (merged["speed_flag"] == "ok") & merged["dry_baseline_speed_kmh"].notna(),
        (merged["speed_kmh"] - merged["dry_baseline_speed_kmh"]).round(2),
        np.nan,
    )

    missing_rain_rows = int((merged["rain_matched"] & merged["rain_is_missing"]).sum())
    matched_with_rain_rows = int((merged["rain_matched"] & (~merged["rain_is_missing"])).sum())
    rain_intensity_counts = {
        str(k): int(v) for k, v in merged["rain_intensity"].value_counts(dropna=False).items()
    }

    stats: Dict[str, Any] = {
        "left_rows": rows_before,
        "result_rows": rows_after,
        "only_sptrans_keys": len(left_keys - right_keys),
        "only_cemaden_keys": len(right_keys - left_keys),
        "matched_keys": len(left_keys & right_keys),
        "matched_with_rain_rows": matched_with_rain_rows,
        "unmatched_key_rows": unmatched_key_rows,
        "missing_rain_rows": missing_rain_rows,
        "rain_intensity_counts": rain_intensity_counts,
    }
    logger.info(
        f"Enrichment validation | left={rows_before} | result={rows_after} | "
        f"with_rain={matched_with_rain_rows} | unmatched_key={unmatched_key_rows} | "
        f"missing_sensor={missing_rain_rows}"
    )
    return merged, stats
