import json
from typing import Any, Dict, List, Optional, Tuple
import geopandas as gpd
import numpy as np
import pandas as pd

from src.utils.geo_utils import (
    DEFAULT_MAX_DIST_M,
    DEFAULT_STEP_M,
    nearest_station,
    sample_linestring,
)
from src.utils.logger import get_logger
from src.utils.text_utils import text_similarity

logger = get_logger("line_selection")

# Constantes de scoring e amostragem
WEIGHT_COVERAGE = 0.5
WEIGHT_FREQUENCY = 0.3
WEIGHT_LENGTH = 0.2

DEFAULT_MAX_PEAK_HEADWAY_MIN = 15.0
DEFAULT_MIN_COVERAGE = 0.90
DEFAULT_MIN_STATION_DIST_M = 3000.0


def load_active_stations(path: str) -> gpd.GeoDataFrame:
    """Carrega estações pluviométricas ativas do arquivo JSON do CEMADEN.

    Filtra estritamente por status == 'Active' e station_type == 'Pluviométrica'.
    """
    with open(path, "r", encoding="utf-8") as f:
        stations_raw = json.load(f)

    total_raw = len(stations_raw)
    df = pd.DataFrame(stations_raw)

    # Filtro de atividade e tipo
    active_mask = (df["status"] == "Active") & (df["station_type"] == "Pluviométrica")
    df_active = df[active_mask].copy()
    total_active = len(df_active)

    logger.info(
        f"Estações carregadas de {path}: {total_raw} no total -> {total_active} ativas pluviométricas."
    )

    gdf = gpd.GeoDataFrame(
        df_active,
        geometry=gpd.points_from_xy(
            df_active["longitude"].astype(float),
            df_active["latitude"].astype(float),
        ),
        crs="EPSG:4326",
    ).to_crs(epsg=31983)

    return gdf


def load_routes(path: str) -> gpd.GeoDataFrame:
    """Carrega routes_geometry.geojson, tipa as propriedades e aplica filtros de elegibilidade.

    Filtros aplicados:
    1. scheduled_speed_suspect == False
    2. weekday_trips > 0
    3. Geometria válida e não vazia
    4. Um único shape por (route_id, direction_id), mantendo o de maior weekday_trips
       (desempate determinístico por shape_id crescente).
    """
    gdf = gpd.read_file(path).to_crs(epsg=31983)
    total_initial = len(gdf)

    # Tipagem explícita das colunas
    gdf["direction_id"] = gdf["direction_id"].astype(int)
    gdf["weekday_trips"] = gdf["weekday_trips"].astype(int)
    gdf["saturday_trips"] = gdf["saturday_trips"].astype(int)
    gdf["sunday_trips"] = gdf["sunday_trips"].astype(int)
    gdf["n_stops"] = gdf["n_stops"].astype(int)
    gdf["length_km"] = gdf["length_km"].astype(float)
    gdf["scheduled_speed_suspect"] = gdf["scheduled_speed_suspect"].astype(bool)

    # 1. Filtro de velocidade suspeita
    suspect_removed = (gdf["scheduled_speed_suspect"] == True).sum()
    gdf_filtered = gdf[~gdf["scheduled_speed_suspect"]].copy()

    # 2. Filtro de viagens de dia útil > 0
    zero_trips_removed = (gdf_filtered["weekday_trips"] <= 0).sum()
    gdf_filtered = gdf_filtered[gdf_filtered["weekday_trips"] > 0].copy()

    # 3. Filtro de geometria válida e não vazia
    invalid_geom_removed = (~gdf_filtered.geometry.is_valid | gdf_filtered.geometry.is_empty).sum()
    gdf_filtered = gdf_filtered[gdf_filtered.geometry.is_valid & ~gdf_filtered.geometry.is_empty].copy()

    logger.info(
        f"Filtros de elegibilidade das rotas ({total_initial} features iniciais): "
        f"removidas {suspect_removed} velocidades suspeitas, {zero_trips_removed} viagens zeradas, "
        f"{invalid_geom_removed} geometrias inválidas. Restantes: {len(gdf_filtered)}."
    )

    # 4. Um shape por (route_id, direction_id)
    before_dedup = len(gdf_filtered)
    gdf_filtered = (
        gdf_filtered.sort_values(
            ["route_id", "direction_id", "weekday_trips", "shape_id"],
            ascending=[True, True, False, True],
        )
        .groupby(["route_id", "direction_id"])
        .first()
        .reset_index()
    )
    gdf_filtered = gpd.GeoDataFrame(gdf_filtered, geometry="geometry", crs="EPSG:31983")
    shapes_dedup_removed = before_dedup - len(gdf_filtered)

    logger.info(
        f"Deduplicação de shapes por (route_id, direction_id): "
        f"{shapes_dedup_removed} shapes redundantes descartados. Total final de traçados: {len(gdf_filtered)}."
    )

    return gdf_filtered


def compute_network_metrics(
    routes_gdf: gpd.GeoDataFrame,
    stations_gdf: gpd.GeoDataFrame,
    step_m: float = DEFAULT_STEP_M,
    max_dist_m: float = DEFAULT_MAX_DIST_M,
) -> Tuple[pd.DataFrame, pd.DataFrame, gpd.GeoDataFrame]:
    """Calcula amostragem espacial, cobertura e métricas para toda a rede de ônibus.

    Retorna:
    - df_routes: métricas agregadas por route_id (com coverage_pct_min, trips, headways, etc.).
    - df_directions: métricas detalhadas por (route_id, direction_id).
    - joined_points: GeoDataFrame completo de todos os pontos amostrados com estação mais próxima.
    """
    all_points = []

    for idx, row in routes_gdf.iterrows():
        geom = row.geometry
        distances, pts = sample_linestring(geom, step_m=step_m)
        n_pts = len(distances)

        df_line = pd.DataFrame(
            {
                "route_id": [row["route_id"]] * n_pts,
                "direction_id": [row["direction_id"]] * n_pts,
                "shape_id": [row["shape_id"]] * n_pts,
                "point_idx": np.arange(n_pts, dtype=np.int32),
                "dist_along_m": distances,
            }
        )
        df_line["geometry"] = pts
        all_points.append(df_line)

    df_all_pts = pd.concat(all_points, ignore_index=True)
    gdf_all_pts = gpd.GeoDataFrame(df_all_pts, geometry="geometry", crs="EPSG:31983")

    # Spatial join com estações
    joined_points = nearest_station(gdf_all_pts, stations_gdf, max_dist_m=max_dist_m)

    # Métricas por (route_id, direction_id)
    dir_metrics = []
    for (r_id, d_id), grp in joined_points.groupby(["route_id", "direction_id"]):
        n_pts = len(grp)
        covered_pts = grp[grp["covered"]]
        cov_pct = len(covered_pts) / n_pts if n_pts > 0 else 0.0

        if len(covered_pts) > 0:
            st_counts = covered_pts["station_id"].value_counts()
            dom_st = st_counts.index[0]
            dom_share = float(st_counts.iloc[0] / len(covered_pts))
            n_st_distinct = int(len(st_counts))
        else:
            dom_st = None
            dom_share = 0.0
            n_st_distinct = 0

        dir_metrics.append(
            {
                "route_id": r_id,
                "direction_id": d_id,
                "coverage_pct": cov_pct,
                "dominant_station_id": dom_st,
                "dominant_share": dom_share,
                "n_stations_distinct": n_st_distinct,
            }
        )

    df_dir_metrics = pd.DataFrame(dir_metrics)
    routes_merged = routes_gdf.merge(df_dir_metrics, on=["route_id", "direction_id"], how="left")

    # Checagem de divergência de length_km vs recalculado
    recalc_km = routes_merged.geometry.length / 1000.0
    diff_pct = (recalc_km - routes_merged["length_km"]).abs() / routes_merged["length_km"]
    divergent_count = (diff_pct > 0.01).sum()
    if divergent_count > 0:
        logger.warning(
            f"{divergent_count} traçados apresentaram divergência > 1% entre length_km e geometria."
        )

    # Agregação por route_id
    route_agg = []
    for r_id, grp in routes_merged.groupby("route_id"):
        cov_min = float(grp["coverage_pct"].min())
        len_mean = float(grp["length_km"].mean())
        w_trips_sum = int(grp["weekday_trips"].sum())
        peak_hw_med = float(grp["peak_headway_min"].median())
        offpeak_hw_med = float(grp["offpeak_headway_min"].median())
        speed_mean = float(grp["scheduled_speed_kmh"].mean())
        n_dirs = len(grp)

        # Estação dominante somando ambos os sentidos
        pts_r = joined_points[joined_points["route_id"] == r_id]
        covered_r = pts_r[pts_r["covered"]]
        if len(covered_r) > 0:
            dom_st_route = covered_r["station_id"].value_counts().index[0]
        else:
            dom_st_route = None

        # Identificação de potencial circular
        dir0 = grp[grp["direction_id"] == 0]
        orig_0 = dir0["origin_stop_id"].values[0] if len(dir0) > 0 else ""
        dest_0 = dir0["dest_stop_id"].values[0] if len(dir0) > 0 else ""
        is_circular_gtfs = (n_dirs == 1) or (orig_0 != "" and orig_0 == dest_0)

        route_agg.append(
            {
                "route_id": r_id,
                "route_short_name": grp["route_short_name"].iloc[0],
                "route_long_name": grp["route_long_name"].iloc[0],
                "coverage_pct_min": cov_min,
                "length_km_mean": len_mean,
                "weekday_trips": w_trips_sum,
                "peak_headway_min": peak_hw_med,
                "offpeak_headway_min": offpeak_hw_med,
                "scheduled_speed_kmh": speed_mean,
                "n_directions": n_dirs,
                "dominant_station_id": dom_st_route,
                "is_circular_gtfs": is_circular_gtfs,
            }
        )

    df_routes = pd.DataFrame(route_agg)
    return df_routes, routes_merged, joined_points


def calc_length_norm(l: float) -> float:
    """Normalização do comprimento da linha para o score.

    Vale 1.0 entre 8 km e 30 km (faixa ideal para linhas operacionais troncais/alimentadoras).
    Abaixo de 8 km decai linearmente até 0.0 (linhas excessivamente curtas).
    Acima de 30 km decai linearmente até 0.0 em 50 km (linhas excessivamente longas).
    """
    if 8.0 <= l <= 30.0:
        return 1.0
    elif l < 8.0:
        return max(0.0, l / 8.0)
    else:
        return max(0.0, 1.0 - (l - 30.0) / 20.0)


def score_routes(
    df_routes: pd.DataFrame,
    max_peak_headway_min: float = DEFAULT_MAX_PEAK_HEADWAY_MIN,
) -> pd.DataFrame:
    """Calcula score multi-critério para ranquear as rotas candidatas.

    Score = 0.5 * coverage_norm + 0.3 * freq_norm + 0.2 * length_norm.
    - coverage_norm: coverage_pct_min (0 a 1).
    - freq_norm: percentil de weekday_trips (0 a 1).
    - length_norm: pontuação de comprimento ótimo (8 a 30 km = 1.0).
    """
    df = df_routes.copy()

    df["length_norm"] = df["length_km_mean"].apply(calc_length_norm)
    df["coverage_norm"] = df["coverage_pct_min"]
    df["freq_norm"] = df["weekday_trips"].rank(pct=True)

    df["score"] = (
        WEIGHT_COVERAGE * df["coverage_norm"]
        + WEIGHT_FREQUENCY * df["freq_norm"]
        + WEIGHT_LENGTH * df["length_norm"]
    )

    # Identificar motivos de exclusão preliminares
    exclusion_reasons = []
    for idx, row in df.iterrows():
        reasons = []
        if row["coverage_pct_min"] < DEFAULT_MIN_COVERAGE:
            reasons.append(f"cobertura_baixa_{row['coverage_pct_min']:.2%}")
        if row["peak_headway_min"] > max_peak_headway_min:
            reasons.append(f"headway_pico_alto_{row['peak_headway_min']:.1f}min")
        if row["n_directions"] < 2 and not row["is_circular_gtfs"]:
            reasons.append("apenas_um_sentido_nao_circular")
        exclusion_reasons.append("; ".join(reasons) if reasons else "")

    df["exclusion_reason"] = exclusion_reasons

    # Ordenação estável decrescente por score e route_id
    df = df.sort_values(["score", "route_id"], ascending=[False, True]).reset_index(drop=True)
    return df


def match_directions(
    gtfs_directions: List[Dict[str, Any]],
    api_lines: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """Associa cada direction_id do GTFS a um item correspondente da API Olho Vivo.

    Retorna lista de dicionários enriquecidos com codigoLinha, sl, tp, ts,
    ou None se a correspondência for ambígua ou incompleta.
    """
    if len(gtfs_directions) == 1:
        # Linha circular ou sentido único
        g_dir = gtfs_directions[0]
        circ_api = [l for l in api_lines if l.get("lc") is True]
        chosen_api = circ_api[0] if circ_api else api_lines[0]
        return [
            {
                **g_dir,
                "codigoLinha": int(chosen_api["cl"]),
                "sl": int(chosen_api["sl"]),
                "tp": chosen_api.get("tp", ""),
                "ts": chosen_api.get("ts", ""),
            }
        ]

    if len(gtfs_directions) != 2 or len(api_lines) != 2:
        logger.warning(
            f"Incompatibilidade de contagem de sentidos: GTFS={len(gtfs_directions)}, API={len(api_lines)}."
        )
        return None

    d0 = gtfs_directions[0]
    d1 = gtfs_directions[1]

    def eval_pair(g_dir: Dict[str, Any], api_line: Dict[str, Any]) -> float:
        # sl=1 vai de tp para ts (destino ts)
        # sl=2 vai de ts para tp (destino tp)
        api_dest = api_line["ts"] if api_line["sl"] == 1 else api_line["tp"]
        api_orig = api_line["tp"] if api_line["sl"] == 1 else api_line["ts"]
        dest_score = text_similarity(g_dir.get("trip_headsign", ""), api_dest)
        orig_score = text_similarity(g_dir.get("origin_stop_name", ""), api_orig)
        return 0.7 * dest_score + 0.3 * orig_score

    score_A = eval_pair(d0, api_lines[0]) + eval_pair(d1, api_lines[1])
    score_B = eval_pair(d0, api_lines[1]) + eval_pair(d1, api_lines[0])

    diff = abs(score_A - score_B)
    if diff < 0.15 and max(score_A, score_B) < 0.5:
        logger.warning(
            f"Associação de sentidos ambígua (score_A={score_A:.2f}, score_B={score_B:.2f}, diff={diff:.2f})."
        )
        return None

    if score_A >= score_B:
        match0, match1 = api_lines[0], api_lines[1]
    else:
        match0, match1 = api_lines[1], api_lines[0]

    return [
        {
            **d0,
            "codigoLinha": int(match0["cl"]),
            "sl": int(match0["sl"]),
            "tp": match0.get("tp", ""),
            "ts": match0.get("ts", ""),
        },
        {
            **d1,
            "codigoLinha": int(match1["cl"]),
            "sl": int(match1["sl"]),
            "tp": match1.get("tp", ""),
            "ts": match1.get("ts", ""),
        },
    ]
