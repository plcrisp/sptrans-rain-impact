from concurrent.futures import ThreadPoolExecutor
import json
import os
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


def _read_json_batch(
    batch: List[Tuple[str, str]],
) -> Tuple[List[Tuple[Any, ...]], int, List[Tuple[str, str]]]:
    """Lê um lote de arquivos JSON do bronze da SPTrans e retorna tuplas leves."""
    rows: List[Tuple[Any, ...]] = []
    files_read = 0
    errors: List[Tuple[str, str]] = []

    for full_path, rel_path in batch:
        try:
            with open(full_path, "rb") as fp:
                data = json.loads(fp.read())
            files_read += 1
        except Exception as e:
            errors.append((os.path.basename(full_path), str(e)))
            continue

        env = data.get("envelope") or {}
        payload = data.get("payload") or {}
        vs = payload.get("vs") or []
        if not vs:
            continue

        c_at = env.get("collected_at_utc")
        cod = env.get("codigoLinha")
        r_id = env.get("route_id")
        dir_id = env.get("direction_id")

        for v in vs:
            p_val = v.get("p")
            rows.append(
                (
                    rel_path,
                    c_at,
                    cod,
                    r_id,
                    dir_id,
                    str(p_val) if p_val is not None else None,
                    v.get("py"),
                    v.get("px"),
                    v.get("ta"),
                )
            )

    return rows, files_read, errors


BRONZE_COLS = [
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


def _scan_json_files(
    scan_root: Path, base_dir: Path
) -> Tuple[List[Tuple[str, str]], int, float]:
    """Varre um diretório com os.walk e retorna (arquivos_json, ignorados, max_mtime)."""
    base_prefix = str(base_dir) + os.sep
    files_ignored = 0
    file_entries: List[Tuple[str, str]] = []
    max_mtime = 0.0

    for dirpath, _, filenames in os.walk(scan_root):
        if dirpath.startswith(base_prefix):
            rel_dir = dirpath[len(base_prefix) :].replace("\\", "/")
        elif dirpath == str(base_dir):
            rel_dir = ""
        else:
            rel_dir = dirpath.replace("\\", "/")

        for fname in filenames:
            if fname == "provenance.jsonl":
                continue
            if not fname.lower().endswith(".json"):
                files_ignored += 1
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                mtime = os.path.getmtime(full_path)
                if mtime > max_mtime:
                    max_mtime = mtime
            except OSError:
                pass
            rel_path = f"{rel_dir}/{fname}" if rel_dir else fname
            file_entries.append((full_path, rel_path))

    return file_entries, files_ignored, max_mtime


def _read_entries_parallel(
    file_entries: List[Tuple[str, str]], label: str = "Bronze SPTrans"
) -> Tuple[pd.DataFrame, int, int]:
    """Lê uma lista de arquivos JSON em paralelo e retorna (DataFrame, lidos, corrompidos)."""
    total_files = len(file_entries)
    if total_files == 0:
        return pd.DataFrame(columns=BRONZE_COLS), 0, 0

    files_read = 0
    files_corrupted = 0
    readings: List[Tuple[Any, ...]] = []

    batch_size = 250
    batches = [
        file_entries[i : i + batch_size]
        for i in range(0, total_files, batch_size)
    ]
    max_workers = min(16, max(4, (os.cpu_count() or 4) * 2))
    next_log_at = 10_000

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for batch_rows, batch_read, batch_errors in executor.map(_read_json_batch, batches):
            readings.extend(batch_rows)
            files_read += batch_read
            if batch_errors:
                files_corrupted += len(batch_errors)
                for err_name, err_msg in batch_errors:
                    logger.warning(f"Arquivo corrompido ou JSON inválido ignorado ({err_name}): {err_msg}")
            processed = files_read + files_corrupted
            if processed >= next_log_at or processed == total_files:
                logger.info(
                    f"Progresso leitura {label}: {processed}/{total_files} arquivos "
                    f"({len(readings)} leituras)..."
                )
                next_log_at += 10_000

    df = pd.DataFrame.from_records(readings, columns=BRONZE_COLS) if readings else pd.DataFrame(columns=BRONZE_COLS)
    return df, files_read, files_corrupted


def load_bronze(
    root: Union[Path, str],
    cache_dir: Optional[Union[Path, str]] = None,
) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """Percorre os arquivos JSON do bronze da SPTrans em paralelo e achata em uma linha por veículo.

    Quando `cache_dir` é informado e `root` contém partições `date=YYYY-MM-DD`, mantém um cache
    Parquet incremental por data (invalidado automaticamente se a contagem ou mtime mudar).
    Retorna tupla com (DataFrame bruto, contagens de arquivos lidos, corrompidos e ignorados).
    """
    root_path = Path(root).resolve()
    base_dir = Path(Config.BASE_DIR).resolve()

    date_dirs = sorted(
        d for d in root_path.iterdir() if d.is_dir() and d.name.startswith("date=")
    ) if root_path.exists() else []

    # Caminho incremental por partição de data quando cache_dir é fornecido
    if cache_dir is not None and date_dirs:
        cache_path = Path(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)
        meta_path = cache_path / "_cache_meta.json"
        meta: Dict[str, Dict[str, Any]] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

        part_dfs: List[pd.DataFrame] = []
        total_read = 0
        total_corrupted = 0
        total_ignored = 0
        cache_hits = 0

        for d in date_dirs:
            part_name = d.name
            file_entries, ignored, max_mtime = _scan_json_files(d, base_dir)
            total_ignored += ignored
            parquet_file = cache_path / f"{part_name}.parquet"
            cached_info = meta.get(part_name)

            is_valid_cache = (
                parquet_file.exists()
                and cached_info is not None
                and cached_info.get("file_count") == len(file_entries)
                and cached_info.get("max_mtime") == max_mtime
            )

            if is_valid_cache:
                part_df = pd.read_parquet(parquet_file)
                part_dfs.append(part_df)
                total_read += int(cached_info.get("files_read", len(file_entries)))
                total_corrupted += int(cached_info.get("files_corrupted", 0))
                cache_hits += 1
                logger.info(
                    f"Cache hit {part_name}: {len(part_df)} leituras ({len(file_entries)} arquivos)."
                )
            else:
                logger.info(f"Lendo partição {part_name} ({len(file_entries)} arquivos JSON)...")
                part_df, f_read, f_corr = _read_entries_parallel(file_entries, label=part_name)
                part_df.to_parquet(parquet_file, engine="pyarrow", compression="snappy", index=False)
                meta[part_name] = {
                    "file_count": len(file_entries),
                    "max_mtime": max_mtime,
                    "files_read": f_read,
                    "files_corrupted": f_corr,
                }
                meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                part_dfs.append(part_df)
                total_read += f_read
                total_corrupted += f_corr

        logger.info(
            f"Leitura Bronze concluída: {cache_hits}/{len(date_dirs)} partições carregadas do cache Parquet."
        )
        df = (
            pd.concat(part_dfs, ignore_index=True)
            if part_dfs
            else pd.DataFrame(columns=BRONZE_COLS)
        )
        return df, {
            "files_read": total_read,
            "files_corrupted": total_corrupted,
            "files_ignored": total_ignored,
        }

    # Caminho direto (sem cache ou diretório sem partições date=)
    file_entries, files_ignored, _ = _scan_json_files(root_path, base_dir)
    logger.info(
        f"Encontrados {len(file_entries)} arquivos JSON na Bronze SPTrans. Iniciando leitura paralela..."
    )
    df, files_read, files_corrupted = _read_entries_parallel(file_entries)
    return df, {
        "files_read": files_read,
        "files_corrupted": files_corrupted,
        "files_ignored": files_ignored,
    }


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
