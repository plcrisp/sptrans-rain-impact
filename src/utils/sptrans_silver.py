import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import geopandas as gpd
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("sptrans_silver")

EARTH_RADIUS_M = 6371000.0


def haversine_distance(
    lat1: Union[float, np.ndarray, pd.Series],
    lon1: Union[float, np.ndarray, pd.Series],
    lat2: Union[float, np.ndarray, pd.Series],
    lon2: Union[float, np.ndarray, pd.Series],
) -> Union[float, np.ndarray, pd.Series]:
    """Calcula a distância ortodrômica em metros entre coordenadas geográficas."""
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = phi2 - phi1
    dlambda = np.radians(lon2 - lon1)

    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    c = 2.0 * np.arcsin(np.clip(np.sqrt(a), 0.0, 1.0))
    return EARTH_RADIUS_M * c


def load_bronze(root: Union[Path, str]) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """Percorre os arquivos JSON do bronze da SPTrans e achata em uma linha por veículo.

    Retorna tupla com (DataFrame bruto, contagens de arquivos lidos, corrompidos e ignorados).
    """
    root_path = Path(root)
    base_dir = Path(Config.BASE_DIR).resolve()

    files_read = 0
    files_corrupted = 0
    files_ignored = 0
    readings: List[Dict[str, Any]] = []

    # Procura recursivamente por arquivos na estrutura particionada ou direta
    for f in root_path.glob("**/*"):
        if f.is_dir():
            continue
        if f.suffix.lower() != ".json":
            files_ignored += 1
            continue

        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            files_read += 1
        except Exception as e:
            files_corrupted += 1
            logger.warning(f"Arquivo corrompido ou JSON inválido ignorado ({f.name}): {e}")
            continue

        env = data.get("envelope", {})
        payload = data.get("payload", {})
        vs = payload.get("vs", [])

        try:
            rel_path = str(f.resolve().relative_to(base_dir)).replace("\\", "/")
        except ValueError:
            rel_path = str(f).replace("\\", "/")

        c_at = env.get("collected_at_utc")
        cod = env.get("codigoLinha")
        r_id = env.get("route_id")
        dir_id = env.get("direction_id")

        for v in vs:
            readings.append(
                {
                    "source_file": rel_path,
                    "collected_at_utc": c_at,
                    "codigoLinha": cod,
                    "route_id": r_id,
                    "direction_id": dir_id,
                    "vehicle_id": str(v.get("p")) if v.get("p") is not None else None,
                    "lat": v.get("py"),
                    "lon": v.get("px"),
                    "ta": v.get("ta"),
                }
            )

    stats = {
        "files_read": files_read,
        "files_corrupted": files_corrupted,
        "files_ignored": files_ignored,
    }
    cols = [
        "source_file",
        "collected_at_utc",
        "codigoLinha",
        "route_id",
        "direction_id",
        "vehicle_id",
        "lat",
        "lon",
        "ta",
    ]
    df = pd.DataFrame(readings, columns=cols) if readings else pd.DataFrame(columns=cols)
    return df, stats


def clean_readings(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """Aplica as regras de limpeza estrutural e deduplicação na ordem estrita de negócio.

    Garante fechamento contábil: len(df) == len(df_clean) + sum(discard_counts.values()).
    """
    if df.empty:
        return df.copy(), {
            "null_or_invalid": 0,
            "outside_bbox": 0,
            "stale_or_future": 0,
            "duplicate": 0,
        }

    # a) nulos em vehicle_id, lat, lon ou ta, ou ta não parseável, ou coordenadas não numéricas
    cond_null = (
        df["vehicle_id"].isna()
        | (df["vehicle_id"].astype(str).str.strip() == "")
        | df["lat"].isna()
        | df["lon"].isna()
        | df["ta"].isna()
    )
    lat_num = pd.to_numeric(df["lat"], errors="coerce")
    lon_num = pd.to_numeric(df["lon"], errors="coerce")
    ta_dt = pd.to_datetime(df["ta"], utc=True, errors="coerce")
    cond_null = cond_null | lat_num.isna() | lon_num.isna() | ta_dt.isna()

    discard_null = int(cond_null.sum())
    df_step1 = df[~cond_null].copy()
    df_step1["lat"] = lat_num[~cond_null].astype(np.float64)
    df_step1["lon"] = lon_num[~cond_null].astype(np.float64)
    df_step1["ta_dt"] = ta_dt[~cond_null]

    # b) fora de Config.SP_BBOX
    bbox = Config.SP_BBOX
    cond_outside = (
        (df_step1["lat"] < bbox["lat_min"])
        | (df_step1["lat"] > bbox["lat_max"])
        | (df_step1["lon"] < bbox["lon_min"])
        | (df_step1["lon"] > bbox["lon_max"])
    )
    discard_bbox = int(cond_outside.sum())
    df_step2 = df_step1[~cond_outside].copy()

    # c) defasagem collected_at_utc - ta > STALE_MAX_MINUTES ou negativa (ta no futuro)
    coll_dt = pd.to_datetime(df_step2["collected_at_utc"], utc=True, errors="coerce")
    lag_s = (coll_dt - df_step2["ta_dt"]).dt.total_seconds()
    max_lag_s = Config.STALE_MAX_MINUTES * 60.0
    cond_stale = coll_dt.isna() | (lag_s < 0.0) | (lag_s > max_lag_s)

    discard_stale = int(cond_stale.sum())
    df_step3 = df_step2[~cond_stale].copy()
    df_step3["coll_dt"] = coll_dt[~cond_stale]

    # d) duplicatas por (vehicle_id, ta): manter a primeira por collected_at_utc
    df_step3 = df_step3.sort_values(by="coll_dt")
    cond_dup = df_step3.duplicated(subset=["vehicle_id", "ta"], keep="first")
    discard_dup = int(cond_dup.sum())
    df_clean = df_step3[~cond_dup].drop(columns=["coll_dt"]).copy()

    discard_counts = {
        "null_or_invalid": discard_null,
        "outside_bbox": discard_bbox,
        "stale_or_future": discard_stale,
        "duplicate": discard_dup,
    }
    return df_clean, discard_counts


def compute_speed(df: pd.DataFrame) -> pd.DataFrame:
    """Calcula dt, distância e velocidade entre leituras consecutivas do mesmo veículo na mesma linha."""
    if df.empty:
        df_out = df.copy()
        df_out["dt_s"] = pd.Series(dtype=np.float32)
        df_out["dist_m"] = pd.Series(dtype=np.float32)
        df_out["speed_kmh"] = pd.Series(dtype=np.float32)
        df_out["speed_flag"] = pd.Series(dtype=str)
        return df_out

    # Assegura presença de ta_dt para cálculos
    if "ta_dt" not in df.columns:
        df = df.copy()
        df["ta_dt"] = pd.to_datetime(df["ta"], utc=True)

    df_sorted = df.sort_values(by=["vehicle_id", "codigoLinha", "ta_dt"]).reset_index(drop=True)

    prev_v = df_sorted["vehicle_id"].shift(1)
    prev_c = df_sorted["codigoLinha"].shift(1)
    prev_ta = df_sorted["ta_dt"].shift(1)
    prev_lat = df_sorted["lat"].shift(1)
    prev_lon = df_sorted["lon"].shift(1)

    is_first = (df_sorted["vehicle_id"] != prev_v) | (df_sorted["codigoLinha"] != prev_c) | prev_ta.isna()

    dt_calc = (df_sorted["ta_dt"] - prev_ta).dt.total_seconds().astype(np.float32)
    dist_calc = haversine_distance(prev_lat, prev_lon, df_sorted["lat"], df_sorted["lon"]).astype(np.float32)

    dt_s = np.where(is_first, np.nan, dt_calc)
    dist_m = np.where(is_first, np.nan, dist_calc)
    raw_speed = np.where((~is_first) & (dt_s > 0), (dist_m / dt_s) * 3.6, np.nan)

    df_sorted["dt_s"] = dt_s.astype(np.float32)
    df_sorted["dist_m"] = dist_m.astype(np.float32)

    # Identificação de leituras "parked": sequência contínua de leituras com dist_m < STOP_DIST_M com duração >= PARKED_MIN_MINUTES
    is_stop_step = (
        (~is_first)
        & (dist_m < Config.STOP_DIST_M)
        & (dt_s >= Config.DT_MIN_S)
        & (dt_s <= Config.DT_MAX_S)
    )
    block_id = ((~is_stop_step) | is_first).cumsum()
    block_dur = df_sorted["dt_s"].fillna(0.0).groupby(block_id).transform("sum")
    is_parked = is_stop_step & (block_dur >= Config.PARKED_MIN_MINUTES * 60.0)

    # Precedência estrita de speed_flag
    cond_first = is_first | np.isnan(dt_s)
    cond_dt = (~cond_first) & ((dt_s < Config.DT_MIN_S) | (dt_s > Config.DT_MAX_S))
    cond_parked = (~cond_first) & (~cond_dt) & is_parked
    cond_fast = (~cond_first) & (~cond_dt) & (~cond_parked) & (raw_speed > Config.SPEED_MAX_KMH)

    speed_flag = np.select(
        [cond_first, cond_dt, cond_parked, cond_fast],
        ["first_reading", "dt_out_of_range", "parked", "too_fast"],
        default="ok",
    )
    df_sorted["speed_flag"] = speed_flag
    df_sorted["speed_kmh"] = np.where(speed_flag == "ok", raw_speed.astype(np.float32), np.nan)

    return df_sorted


def add_time_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Adiciona colunas temporais derivadas de ta_utc no fuso local America/Sao_Paulo."""
    df_out = df.copy()
    if "ta_dt" in df_out.columns:
        ta_utc = df_out["ta_dt"]
    else:
        ta_utc = pd.to_datetime(df_out["ta"], utc=True)

    df_out["ta_utc"] = ta_utc
    ts_sp = ta_utc.dt.tz_convert("America/Sao_Paulo")
    df_out["timestamp_sp"] = ts_sp
    df_out["date_sp"] = ts_sp.dt.date
    df_out["hour"] = ts_sp.dt.hour.astype(np.int8)
    df_out["weekday"] = ts_sp.dt.weekday.astype(np.int8)
    df_out["is_weekend"] = df_out["weekday"] >= 5

    # Validação de janelas de pico em dias úteis
    min_of_day = ts_sp.dt.hour * 60 + ts_sp.dt.minute
    in_peak_window = pd.Series(False, index=df_out.index)
    for start_str, end_str in Config.PEAK_WINDOWS_SP:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        s_min = sh * 60 + sm
        e_min = eh * 60 + em
        in_peak_window = in_peak_window | ((min_of_day >= s_min) & (min_of_day < e_min))

    df_out["is_peak"] = (~df_out["is_weekend"]) & in_peak_window
    return df_out


def add_station(df: pd.DataFrame, mapping_df: pd.DataFrame) -> pd.DataFrame:
    """Associa cada leitura ao ponto amostrado mais próximo da mesma linha em line_station_mapping."""
    if df.empty:
        df_out = df.copy()
        df_out["station_id"] = pd.Series(dtype=str)
        df_out["dist_to_station_m"] = pd.Series(dtype=np.float32)
        df_out["dist_to_route_m"] = pd.Series(dtype=np.float32)
        df_out["is_off_route"] = pd.Series(dtype=bool)
        return df_out

    # Projeção métrica EPSG:31983 para cálculo exato de distâncias euclidianas em metros
    df_gdf = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["lon"], df["lat"]),
        crs="EPSG:4326",
    ).to_crs(epsg=31983)

    mapping_gdf = gpd.GeoDataFrame(
        mapping_df[["codigoLinha", "station_id", "dist_to_station_m"]],
        geometry=gpd.points_from_xy(mapping_df["lon"], mapping_df["lat"]),
        crs="EPSG:4326",
    ).to_crs(epsg=31983)

    joined_list = []
    mapping_cods = set(mapping_gdf["codigoLinha"].unique())

    for cod, grp in df_gdf.groupby("codigoLinha"):
        if cod not in mapping_cods:
            logger.warning(f"codigoLinha {cod} não encontrado no mapeamento de estações.")
            sub_grp = grp.copy()
            sub_grp["station_id"] = None
            sub_grp["dist_to_station_m"] = np.nan
            sub_grp["dist_to_route_m"] = np.nan
            joined_list.append(sub_grp)
            continue

        m_sub = mapping_gdf[mapping_gdf["codigoLinha"] == cod]
        joined = gpd.sjoin_nearest(
            grp,
            m_sub[["station_id", "dist_to_station_m", "geometry"]],
            how="left",
            distance_col="dist_to_route_m",
        )
        # Desempate estrito por índice original mantendo a 1ª correspondência
        joined = joined.loc[~joined.index.duplicated(keep="first")]
        joined_list.append(joined)

    res = pd.concat(joined_list).reindex(df.index)
    res_df = pd.DataFrame(res.drop(columns=["geometry", "index_right"], errors="ignore"))

    res_df["station_id"] = res_df["station_id"].astype(object).where(res_df["station_id"].notna(), None)
    res_df["dist_to_station_m"] = res_df["dist_to_station_m"].astype(np.float32)
    res_df["dist_to_route_m"] = res_df["dist_to_route_m"].astype(np.float32)
    res_df["is_off_route"] = res_df["dist_to_route_m"] > Config.OFF_ROUTE_M
    return res_df
