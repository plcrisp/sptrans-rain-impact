import datetime
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("cemaden_silver_utils")


def parse_raw_bronze_csv(
    csv_bytes: bytes,
    station_id: str,
    sentinel_values: Tuple[float, ...] = (-999.0, -9999.0, -99.0),
    max_plausible_mm: float = 150.0,
) -> pd.DataFrame:
    """Lê o CSV bruto do bronze, normaliza timestamps em horário de SP e limpa valores numéricos."""
    if not csv_bytes or len(csv_bytes.strip()) == 0:
        return pd.DataFrame(columns=["station_id", "timestamp_sp", "rain_mm"])

    df_raw = pd.read_csv(
        io.BytesIO(csv_bytes),
        dtype=str,
        sep=",",
        encoding="utf-8",
        skip_blank_lines=True,
    )

    if df_raw.empty:
        return pd.DataFrame(columns=["station_id", "timestamp_sp", "rain_mm"])

    df_raw.columns = [c.strip().lower() for c in df_raw.columns]

    # Parsing de datahora no fuso local America/Sao_Paulo (UTC-3)
    raw_dh = df_raw["datahora"].astype(str).str.strip()
    parsed_dt = pd.to_datetime(raw_dh, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    valid_mask = parsed_dt.notna()

    if not valid_mask.any():
        return pd.DataFrame(columns=["station_id", "timestamp_sp", "rain_mm"])

    df_valid = df_raw[valid_mask].copy()
    dt_sp = parsed_dt[valid_mask].dt.tz_localize(Config.CEMADEN_SOURCE_TZ, ambiguous="infer", nonexistent="shift_forward")

    # Tratamento numérico de valor
    raw_val = df_valid["valor"].fillna("").astype(str).str.strip()
    sentinel_set = set(float(s) for s in sentinel_values)

    rain_list: List[Optional[float]] = []
    for v_str in raw_val:
        if not v_str or v_str.lower() in ("nan", "none", "null"):
            rain_list.append(None)
            continue
        try:
            val_f = float(v_str.replace(",", "."))
            if np.isnan(val_f) or val_f in sentinel_set or val_f < 0.0 or val_f > max_plausible_mm:
                rain_list.append(None)
            else:
                rain_list.append(round(val_f, 2))
        except ValueError:
            rain_list.append(None)

    df_res = pd.DataFrame(
        {
            "station_id": station_id,
            "timestamp_sp": dt_sp,
            "rain_mm": rain_list,
        }
    )

    # Remove duplicatas exatas por (station_id, timestamp_sp)
    df_res = df_res.drop_duplicates(subset=["station_id", "timestamp_sp"]).reset_index(drop=True)
    return df_res


def generate_clean_hourly_export(
    df: pd.DataFrame,
    all_station_ids: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Gera o dataset final consolidado para o CSV da Silver do CEMADEN.

    - Registros reais são mantidos com sua granularidade nativa e is_missing = False.
    - Para horas sem leituras, cria-se uma linha artificial com rain_mm nulo e is_missing = True.
    """
    if df.empty and not all_station_ids:
        return pd.DataFrame(columns=["station_id", "timestamp_sp", "rain_mm", "is_missing"])

    if not df.empty:
        df_work = df.copy()
        df_work["hour_sp"] = df_work["timestamp_sp"].dt.floor("h")
        min_hour = df_work["hour_sp"].min()
        max_hour = df_work["hour_sp"].max()
        stations = sorted(list(set(all_station_ids or df_work["station_id"].unique())))
    else:
        now_sp = pd.Timestamp.now(tz=Config.CEMADEN_SOURCE_TZ).floor("h")
        min_hour = now_sp
        max_hour = now_sp
        stations = sorted(list(set(all_station_ids or [])))
        df_work = pd.DataFrame(columns=["station_id", "timestamp_sp", "rain_mm", "hour_sp"])

    all_hours = pd.date_range(min_hour, max_hour, freq="1h")
    records = []

    for sid in stations:
        sub = df_work[df_work["station_id"] == sid] if not df_work.empty else pd.DataFrame()
        hours_with_data = set(sub["hour_sp"].unique()) if not sub.empty else set()

        # 1. Registros reais preservando minutos e valores exatos
        for _, r in sub.iterrows():
            ts_val = r["timestamp_sp"]
            ts_str = ts_val.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts_val, "strftime") else str(ts_val)
            val = r["rain_mm"]
            clean_val = round(float(val), 2) if pd.notna(val) else None

            records.append(
                {
                    "station_id": sid,
                    "timestamp_sp": ts_str,
                    "rain_mm": clean_val,
                    "is_missing": False,
                    "_sort_key": pd.Timestamp(ts_val),
                }
            )

        # 2. Linhas artificiais apenas para horas sem nenhuma leitura
        for h in all_hours:
            if h not in hours_with_data:
                records.append(
                    {
                        "station_id": sid,
                        "timestamp_sp": h.strftime("%Y-%m-%d %H:%M:%S"),
                        "rain_mm": None,
                        "is_missing": True,
                        "_sort_key": pd.Timestamp(h),
                    }
                )

    df_out = pd.DataFrame(records)
    if not df_out.empty:
        df_out = df_out.sort_values(["station_id", "_sort_key"]).drop(columns=["_sort_key"]).reset_index(drop=True)
    else:
        df_out = pd.DataFrame(columns=["station_id", "timestamp_sp", "rain_mm", "is_missing"])

    return df_out
