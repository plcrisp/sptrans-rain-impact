import os
from typing import Dict, Optional, Set

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from src.utils.logger import get_logger

logger = get_logger("gtfs_parser")


def resolve_gtfs_file(gtfs_dir: str, name: str) -> str:
    base_name = name.removesuffix(".csv").removesuffix(".txt")
    for ext in [".csv", ".txt"]:
        file_path = os.path.join(gtfs_dir, f"{base_name}{ext}")
        if os.path.exists(file_path):
            return file_path
    raise FileNotFoundError(
        f"Arquivo GTFS '{base_name}' não encontrado (.csv ou .txt) em: {gtfs_dir}"
    )


def time_to_seconds(series: pd.Series) -> pd.Series:
    s = series.astype(str)
    parts = s.str.split(":", expand=True)
    seconds = (
        parts[0].astype(int) * 3600
        + parts[1].astype(int) * 60
        + parts[2].astype(int)
    )
    if series.isna().any():
        seconds = seconds.where(series.notna(), other=np.nan)
    return seconds


def service_ids_by_daytype(calendar_df: pd.DataFrame) -> Dict[str, Set[str]]:
    df = calendar_df.copy()
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for day in days:
        df[day] = df[day].astype(int)

    weekday_mask = (
        (df["monday"] == 1)
        & (df["tuesday"] == 1)
        & (df["wednesday"] == 1)
        & (df["thursday"] == 1)
        & (df["friday"] == 1)
    )
    saturday_mask = df["saturday"] == 1
    sunday_mask = df["sunday"] == 1

    return {
        "weekday": set(df.loc[weekday_mask, "service_id"].dropna()),
        "saturday": set(df.loc[saturday_mask, "service_id"].dropna()),
        "sunday": set(df.loc[sunday_mask, "service_id"].dropna()),
    }


def compute_service_metrics(
    frequencies_df: pd.DataFrame,
    trips_df: pd.DataFrame,
    calendar_df: pd.DataFrame,
) -> pd.DataFrame:
    """Calcula métricas de serviço e frequência por shape_id."""
    day_types = service_ids_by_daytype(calendar_df)
    weekday_services = day_types["weekday"]
    saturday_services = day_types["saturday"]
    sunday_services = day_types["sunday"]

    # Cruzar frequencies com trips para associar service_id e shape_id
    freq = frequencies_df.merge(
        trips_df[["trip_id", "service_id", "shape_id"]],
        on="trip_id",
        how="inner",
    )

    start_sec = time_to_seconds(freq["start_time"])
    end_sec = time_to_seconds(freq["end_time"])
    headway_s = freq["headway_secs"].astype(int)

    invalid_mask = (headway_s <= 0) | (end_sec <= start_sec)
    invalid_count = int(invalid_mask.sum())
    if invalid_count > 0:
        logger.warning(
            f"Ignorando {invalid_count} janelas de frequência com headway <= 0 ou end <= start"
        )

    # Número de viagens na janela
    window_trips = np.where(
        invalid_mask,
        0,
        np.ceil((end_sec - start_sec) / np.where(headway_s <= 0, 1, headway_s)).astype(int),
    )

    is_weekday = freq["service_id"].isin(weekday_services)
    is_saturday = freq["service_id"].isin(saturday_services)
    is_sunday = freq["service_id"].isin(sunday_services)

    freq["weekday_trips"] = np.where(is_weekday, window_trips, 0)
    freq["saturday_trips"] = np.where(is_saturday, window_trips, 0)
    freq["sunday_trips"] = np.where(is_sunday, window_trips, 0)

    # Pico: [06:00, 09:00) ou [17:00, 20:00) em dias úteis
    peak_mask = is_weekday & (
        ((start_sec >= 21600) & (start_sec < 32400))
        | ((start_sec >= 61200) & (start_sec < 72000))
    )
    # Entrepico: [10:00, 16:00) em dias úteis
    offpeak_mask = is_weekday & ((start_sec >= 36000) & (start_sec < 57600))

    headway_min = headway_s / 60.0
    freq["peak_headway_min"] = np.where(peak_mask, headway_min, np.nan)
    freq["offpeak_headway_min"] = np.where(offpeak_mask, headway_min, np.nan)

    # Início da primeira janela e fim da última janela em dia útil
    freq_weekday = freq[is_weekday].copy()
    first_dep = freq_weekday.groupby("shape_id")["start_time"].min().rename("first_departure_weekday")
    last_dep = freq_weekday.groupby("shape_id")["end_time"].max().rename("last_departure_weekday")

    aggregated = (
        freq.groupby("shape_id")
        .agg(
            {
                "weekday_trips": "sum",
                "saturday_trips": "sum",
                "sunday_trips": "sum",
                "peak_headway_min": "median",
                "offpeak_headway_min": "median",
            }
        )
        .join(first_dep)
        .join(last_dep)
    )

    # Identificar shapes sem frequências cadastradas
    all_shapes = set(trips_df["shape_id"].dropna())
    missing_shapes = all_shapes - set(aggregated.index)
    if missing_shapes:
        logger.warning(
            f"{len(missing_shapes)} shapes sem frequências associadas; preenchidos com 0/NaN"
        )
        empty_df = pd.DataFrame(
            {
                "weekday_trips": 0,
                "saturday_trips": 0,
                "sunday_trips": 0,
                "peak_headway_min": np.nan,
                "offpeak_headway_min": np.nan,
                "first_departure_weekday": np.nan,
                "last_departure_weekday": np.nan,
            },
            index=pd.Index(list(missing_shapes), name="shape_id"),
        )
        aggregated = pd.concat([aggregated, empty_df])

    return aggregated


def compute_trip_terminals(
    stop_times_df: pd.DataFrame,
    stops_df: pd.DataFrame,
    trips_df: pd.DataFrame,
    weekday_trips_by_trip: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Calcula paradas terminais (origem e destino) e duração programada por shape_id."""
    trips = trips_df.copy()
    if weekday_trips_by_trip is not None:
        trips = trips.merge(
            weekday_trips_by_trip.rename("w_trips"),
            left_on="trip_id",
            right_index=True,
            how="left",
        ).fillna({"w_trips": 0})
    else:
        trips["w_trips"] = 0

    # Selecionar viagem representativa por shape_id
    rep_trips = (
        trips.sort_values(
            ["shape_id", "w_trips", "trip_id"],
            ascending=[True, False, True],
        )
        .groupby("shape_id")
        .first()
        .reset_index()
    )

    rep_trip_set = set(rep_trips["trip_id"])

    # Filtrar stop_times pelas viagens representativas
    st = stop_times_df[stop_times_df["trip_id"].isin(rep_trip_set)].copy()
    st["stop_sequence"] = st["stop_sequence"].astype(np.int32)
    st = st.sort_values(["trip_id", "stop_sequence"])

    grouped = st.groupby("trip_id")
    orig = grouped.first().reset_index()
    dest = grouped.last().reset_index()
    n_stops = grouped.size().rename("n_stops").reset_index()

    orig_dest = pd.DataFrame(
        {
            "trip_id": orig["trip_id"],
            "origin_stop_id": orig["stop_id"],
            "orig_dep_time": orig["departure_time"],
            "dest_stop_id": dest["stop_id"],
            "dest_arr_time": dest["arrival_time"],
        }
    ).merge(n_stops, on="trip_id")

    orig_dep_s = time_to_seconds(orig_dest["orig_dep_time"])
    dest_arr_s = time_to_seconds(orig_dest["dest_arr_time"])
    duration_min = (dest_arr_s - orig_dep_s) / 60.0

    invalid_dur = duration_min <= 0
    if invalid_dur.any():
        logger.warning(
            f"{int(invalid_dur.sum())} viagens representativas com duração programada <= 0 min ignoradas (NaN)"
        )
        duration_min = duration_min.where(~invalid_dur, other=np.nan)

    orig_dest["scheduled_duration_min"] = duration_min

    # Mesclar informações geográficas das paradas
    stops_info = stops_df[["stop_id", "stop_name", "stop_lat", "stop_lon"]].copy()
    stops_info["stop_lat"] = stops_info["stop_lat"].astype(float)
    stops_info["stop_lon"] = stops_info["stop_lon"].astype(float)

    orig_dest = orig_dest.merge(
        stops_info.rename(
            columns={
                "stop_name": "origin_stop_name",
                "stop_lat": "origin_lat",
                "stop_lon": "origin_lon",
            }
        ),
        left_on="origin_stop_id",
        right_on="stop_id",
        how="left",
    ).drop(columns=["stop_id"])

    orig_dest = orig_dest.merge(
        stops_info.rename(
            columns={
                "stop_name": "dest_stop_name",
                "stop_lat": "dest_lat",
                "stop_lon": "dest_lon",
            }
        ),
        left_on="dest_stop_id",
        right_on="stop_id",
        how="left",
    ).drop(columns=["stop_id"])

    result = rep_trips[["shape_id", "trip_id"]].merge(orig_dest, on="trip_id", how="left")
    result = result.set_index("shape_id")
    cols_to_keep = [
        "origin_stop_id",
        "origin_stop_name",
        "origin_lat",
        "origin_lon",
        "dest_stop_id",
        "dest_stop_name",
        "dest_lat",
        "dest_lon",
        "n_stops",
        "scheduled_duration_min",
    ]
    return result[cols_to_keep]


def load_csv(file_path, usecols):
    return pd.read_csv(file_path, usecols=usecols, dtype=str)


def get_unique_trips(trips_df):
    return trips_df.drop_duplicates(subset=["shape_id"])


def merge_transit_data(shapes, trips, routes):
    unique_trips = get_unique_trips(trips)
    shapes_trips = pd.merge(shapes, unique_trips, on="shape_id", how="left")
    final_dataset = pd.merge(shapes_trips, routes, on="route_id", how="inner")

    final_dataset["shape_pt_lat"] = pd.to_numeric(final_dataset["shape_pt_lat"])
    final_dataset["shape_pt_lon"] = pd.to_numeric(final_dataset["shape_pt_lon"])
    final_dataset["shape_pt_sequence"] = pd.to_numeric(final_dataset["shape_pt_sequence"])

    return final_dataset


def create_geometries(df):
    df = df.sort_values(["shape_id", "shape_pt_sequence"])
    group_cols = [
        "shape_id",
        "route_id",
        "route_short_name",
        "route_long_name",
        "direction_id",
        "trip_headsign",
    ]

    records = []
    for keys, group in df.groupby(group_cols, sort=True):
        if len(group) > 1:
            geom = LineString(zip(group["shape_pt_lon"], group["shape_pt_lat"]))
            rec = dict(zip(group_cols, keys if isinstance(keys, tuple) else (keys,)))
            rec["geometry"] = geom
            records.append(rec)

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
    return gdf


def build_gtfs_dataset(gtfs_dir, output_dir=None):
    shapes_cols = ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"]
    trips_cols = ["route_id", "service_id", "trip_id", "shape_id", "direction_id", "trip_headsign"]
    routes_cols = ["route_id", "route_short_name", "route_long_name", "route_type"]

    shapes_file = resolve_gtfs_file(gtfs_dir, "shapes")
    trips_file = resolve_gtfs_file(gtfs_dir, "trips")
    routes_file = resolve_gtfs_file(gtfs_dir, "routes")

    shapes_df = load_csv(shapes_file, shapes_cols)
    trips_df = load_csv(trips_file, trips_cols)
    routes_df = load_csv(routes_file, routes_cols)

    # Filtrar apenas ônibus (route_type == '3')
    routes_df = routes_df[routes_df["route_type"] == "3"].copy()
    routes_df = routes_df.drop(columns=["route_type"])

    dataset = merge_transit_data(shapes_df, trips_df, routes_df)
    gdf = create_geometries(dataset)

    # Verificar existência dos arquivos auxiliares para enriquecimento
    aux_files = ["calendar", "frequencies", "stop_times", "stops"]
    missing_aux = []
    resolved_paths = {}
    for aux in aux_files:
        try:
            resolved_paths[aux] = resolve_gtfs_file(gtfs_dir, aux)
        except FileNotFoundError:
            missing_aux.append(aux)

    if missing_aux:
        logger.warning(
            f"Arquivos auxiliares ausentes ({', '.join(missing_aux)}). "
            "Gerando GeoDataFrame básico sem métricas enriquecidas."
        )
    else:
        # Carregar arquivos auxiliares
        calendar_df = pd.read_csv(resolved_paths["calendar"], dtype=str)
        frequencies_df = pd.read_csv(resolved_paths["frequencies"], dtype=str)
        stops_df = pd.read_csv(
            resolved_paths["stops"],
            usecols=["stop_id", "stop_name", "stop_lat", "stop_lon"],
            dtype=str,
        )
        stop_times_df = pd.read_csv(
            resolved_paths["stop_times"],
            usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
            dtype={
                "trip_id": str,
                "arrival_time": str,
                "departure_time": str,
                "stop_id": str,
                "stop_sequence": np.int32,
            },
        )

        # 1. Métricas de serviço e frequência
        service_metrics = compute_service_metrics(frequencies_df, trips_df, calendar_df)

        # Calcular viagens de dia útil por trip_id para guiar a seleção da viagem representativa
        day_types = service_ids_by_daytype(calendar_df)
        weekday_services = day_types["weekday"]
        freq_w = frequencies_df.merge(
            trips_df[["trip_id", "service_id"]],
            on="trip_id",
            how="inner",
        )
        is_w = freq_w["service_id"].isin(weekday_services)
        st_s = time_to_seconds(freq_w["start_time"])
        et_s = time_to_seconds(freq_w["end_time"])
        hw_s = freq_w["headway_secs"].astype(int)
        w_trips_arr = np.where(
            (hw_s <= 0) | (et_s <= st_s) | (~is_w),
            0,
            np.ceil((et_s - st_s) / np.where(hw_s <= 0, 1, hw_s)).astype(int),
        )
        freq_w["w_trips"] = w_trips_arr
        weekday_by_trip = freq_w.groupby("trip_id")["w_trips"].sum()

        # 2. Terminais e duração programada
        terminals = compute_trip_terminals(
            stop_times_df, stops_df, trips_df, weekday_trips_by_trip=weekday_by_trip
        )

        # 3. Comprimento da rota projetando para EPSG:31983 (UTM 23S)
        gdf_proj = gdf.to_crs(epsg=31983)
        length_km = gdf_proj.geometry.length / 1000.0

        # Enriquecer o GeoDataFrame
        gdf["length_km"] = length_km.round(2)
        gdf = gdf.merge(service_metrics, on="shape_id", how="left")
        gdf = gdf.merge(terminals, on="shape_id", how="left")

        # 4. Velocidade programada e flag de suspeita
        dur_hours = gdf["scheduled_duration_min"] / 60.0
        raw_speed = gdf["length_km"] / dur_hours
        suspect_mask = (raw_speed < 3) | (raw_speed > 80) | dur_hours.isna() | (dur_hours <= 0)

        gdf["scheduled_speed_kmh"] = np.where(suspect_mask, np.nan, raw_speed).round(2)
        gdf["scheduled_speed_suspect"] = suspect_mask

        # Arredondamentos finais
        gdf["peak_headway_min"] = gdf["peak_headway_min"].round(1)
        gdf["offpeak_headway_min"] = gdf["offpeak_headway_min"].round(1)
        gdf["origin_lat"] = gdf["origin_lat"].round(6)
        gdf["origin_lon"] = gdf["origin_lon"].round(6)
        gdf["dest_lat"] = gdf["dest_lat"].round(6)
        gdf["dest_lon"] = gdf["dest_lon"].round(6)
        gdf["scheduled_duration_min"] = gdf["scheduled_duration_min"].round(2)

        # Tipos inteiros para contagens de viagens e paradas
        for col in ["weekday_trips", "saturday_trips", "sunday_trips", "n_stops"]:
            if col in gdf.columns:
                gdf[col] = gdf[col].fillna(0).astype(int)

    keep_cols = [
        "shape_id", "route_id", "route_short_name", "route_long_name",
        "direction_id", "length_km", "weekday_trips", "peak_headway_min",
        "offpeak_headway_min", "scheduled_speed_kmh", "scheduled_speed_suspect",
        "origin_stop_id", "dest_stop_id", "geometry"
    ]
    gdf = gdf[[c for c in keep_cols if c in gdf.columns]]

    if output_dir:
        output_path = os.path.join(output_dir, "routes_geometry.geojson")
        gdf.to_file(output_path, driver="GeoJSON")
        logger.info(f"Dataset saved successfully at: {output_path}")

    return gdf