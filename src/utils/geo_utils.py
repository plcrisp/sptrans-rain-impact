from typing import List, Tuple
import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point

DEFAULT_STEP_M = 100.0
DEFAULT_MAX_DIST_M = 2000.0


def sample_linestring(
    geom: LineString,
    step_m: float = DEFAULT_STEP_M,
) -> Tuple[np.ndarray, List[Point]]:
    """Amostra pontos ao longo de uma LineString a cada step_m em projeção métrica.

    Garante a inclusão exata do ponto inicial (0m) e do ponto final (comprimento total).
    Retorna tupla de (distâncias acumuladas em metros, lista de geometrias Point).
    """
    total_len = geom.length
    if total_len <= 0:
        return np.array([0.0], dtype=np.float32), [geom.interpolate(0.0)]

    distances = np.arange(0, total_len, step_m, dtype=np.float32)
    if len(distances) == 0 or distances[-1] < total_len:
        distances = np.append(distances, np.float32(total_len))

    points = [geom.interpolate(float(d)) for d in distances]
    return distances, points


def nearest_station(
    points_gdf: gpd.GeoDataFrame,
    stations_gdf: gpd.GeoDataFrame,
    max_dist_m: float = DEFAULT_MAX_DIST_M,
) -> gpd.GeoDataFrame:
    """Encontra para cada ponto amostrado a estação pluviométrica ativa mais próxima."""
    if points_gdf.crs is None:
        points_gdf = points_gdf.set_crs(31983)
    if stations_gdf.crs is None:
        stations_gdf = stations_gdf.set_crs(31983)

    joined = gpd.sjoin_nearest(
        points_gdf,
        stations_gdf[["station_id", "station_name", "geometry"]],
        distance_col="dist_to_station_m",
        how="left",
    )
    # Deduplica empates exatos de distância para o mesmo ponto
    if "point_idx" in joined.columns and "route_id" in joined.columns:
        joined = joined.drop_duplicates(subset=["route_id", "direction_id", "point_idx"])

    joined["covered"] = joined["dist_to_station_m"] <= max_dist_m
    return joined
