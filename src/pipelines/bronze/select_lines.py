import argparse
from datetime import datetime, timezone
import json
import os
import re
import sys
import time
from typing import Any, Dict, List

# Garante que a raiz do repositório esteja no sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import numpy as np
import pandas as pd

from src.clients.sptrans_client import SPTransClient
from src.core.config import Config
from src.utils.file_utils import compute_file_sha256
from src.utils.line_selection import (
    DEFAULT_MAX_DIST_M,
    DEFAULT_MAX_PEAK_HEADWAY_MIN,
    DEFAULT_MIN_COVERAGE,
    DEFAULT_MIN_STATION_DIST_M,
    DEFAULT_STEP_M,
    WEIGHT_COVERAGE,
    WEIGHT_FREQUENCY,
    WEIGHT_LENGTH,
    compute_network_metrics,
    load_active_stations,
    load_routes,
    match_directions,
    score_routes,
)
from src.utils.logger import get_logger

logger = get_logger("select_lines")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seleção de linhas e mapeamento de estações SPTrans/CEMADEN.")
    parser.add_argument("--n-lines", type=int, default=8, help="Número de linhas a selecionar (default: 8).")
    parser.add_argument("--max-dist-m", type=float, default=DEFAULT_MAX_DIST_M, help="Distância máxima da estação em metros (default: 2000).")
    parser.add_argument("--step-m", type=float, default=DEFAULT_STEP_M, help="Passo de amostragem em metros (default: 100).")
    parser.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE, help="Cobertura mínima requerida (default: 0.90).")
    parser.add_argument("--top-candidates", type=int, default=40, help="Número de candidatas a avaliar com a API (default: 40).")
    parser.add_argument("--max-peak-headway-min", type=float, default=DEFAULT_MAX_PEAK_HEADWAY_MIN, help="Headway máximo no pico (default: 15).")
    parser.add_argument("--skip-api", action="store_true", help="Gera apenas o ranking offline sem validar com a API Olho Vivo.")
    args = parser.parse_args()

    t_start = time.time()
    Config.ensure_dirs()

    # 1. Carregar dados
    stations_path = os.path.join(Config.STATIONS_DATA_DIR, "cemaden_sp_stations.json")
    routes_path = os.path.join(Config.GTFS_DATA_DIR, "routes_geometry.geojson")

    stations_gdf = load_active_stations(stations_path)
    routes_gdf = load_routes(routes_path)

    # 2. Calcular amostragem e métricas espaciais
    logger.info("Calculando amostragem espacial e cobertura para a rede...")
    df_routes, routes_enriched, joined_points = compute_network_metrics(
        routes_gdf,
        stations_gdf,
        step_m=args.step_m,
        max_dist_m=args.max_dist_m,
    )

    # 3. Ranquear rotas
    df_ranked = score_routes(df_routes, max_peak_headway_min=args.max_peak_headway_min)

    # Salvar ranking completo simplificado
    ranking_csv_path = Config.LINE_RANKING_PATH
    ranking_cols = [
        "route_id",
        "route_long_name",
        "coverage_pct_min",
        "length_km_mean",
        "weekday_trips",
        "peak_headway_min",
        "dominant_station_id",
        "score",
        "exclusion_reason",
    ]
    df_ranked[ranking_cols].to_csv(ranking_csv_path, index=False, encoding="utf-8")
    logger.info(f"Ranking completo de rotas salvo em: {ranking_csv_path}")

    # Exibir top-15
    print("\n" + "=" * 80)
    print("TOP 15 LINHAS CANDIDATAS (RANKING MULTI-CRITÉRIO)")
    print("=" * 80)
    top_15 = df_ranked.head(15)[
        ["route_id", "coverage_pct_min", "weekday_trips", "peak_headway_min", "dominant_station_id", "score"]
    ]
    print(top_15.to_string(index=False))
    print("=" * 80 + "\n")

    if args.skip_api:
        logger.info("Modo --skip-api acionado. Processamento concluído com sucesso.")
        return

    # 4. Validação com a API Olho Vivo e seleção diversa
    Config.validate(["SPTRANS_TOKEN"])
    client = SPTransClient()
    client.login()

    chosen_lines_meta: List[Dict[str, Any]] = []
    chosen_stations: List[str] = []
    st_dict = {row["station_id"]: row.geometry for _, row in stations_gdf.iterrows()}

    # Avaliar candidatos do topo
    candidates_pool = df_ranked[
        (df_ranked["coverage_pct_min"] >= args.min_coverage)
        & (df_ranked["peak_headway_min"] <= args.max_peak_headway_min)
        & (df_ranked["n_directions"] >= 2)
    ].head(args.top_candidates)

    logger.info(f"Avaliando até {len(candidates_pool)} candidatas do topo com a API SPTrans Olho Vivo...")

    for idx, row in candidates_pool.iterrows():
        if len(chosen_lines_meta) >= args.n_lines:
            break

        r_id = row["route_id"]
        dom_st = row["dominant_station_id"]

        if not dom_st or dom_st not in st_dict:
            df_ranked.loc[df_ranked["route_id"] == r_id, "exclusion_reason"] = "sem_estacao_dominante"
            continue

        if dom_st in chosen_stations:
            df_ranked.loc[df_ranked["route_id"] == r_id, "exclusion_reason"] = f"estacao_dominante_repetida_{dom_st}"
            continue

        # Verificar distância mínima entre estações dominantes (3 km)
        st_geom = st_dict[dom_st]
        too_close = False
        conflict_st = None
        for prev_st in chosen_stations:
            d = st_geom.distance(st_dict[prev_st])
            if d < DEFAULT_MIN_STATION_DIST_M:
                too_close = True
                conflict_st = prev_st
                break

        if too_close:
            df_ranked.loc[df_ranked["route_id"] == r_id, "exclusion_reason"] = f"estacao_proxima_{conflict_st}_{d:.0f}m"
            continue

        # Consulta à API Olho Vivo
        m = re.match(r"^([A-Za-z0-9]+)-([0-9]+)$", r_id)
        if not m:
            continue
        letreiro, sufixo = m.group(1), int(m.group(2))

        try:
            api_search = client.search_lines(letreiro)
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"Erro na busca da linha {r_id} na API: {e}")
            continue

        api_matched = [
            l for l in api_search if str(l.get("lt")).upper() == letreiro.upper() and int(l.get("tl", -1)) == sufixo
        ]

        if not api_matched:
            df_ranked.loc[df_ranked["route_id"] == r_id, "exclusion_reason"] = "nao_encontrada_na_api"
            continue

        # Direções no GTFS
        gtfs_dirs = routes_enriched[routes_enriched["route_id"] == r_id].to_dict(orient="records")
        matched_dirs = match_directions(gtfs_dirs, api_matched)

        if not matched_dirs:
            df_ranked.loc[df_ranked["route_id"] == r_id, "exclusion_reason"] = "direction_match_ambiguous"
            continue

        # Validar veículos ativos (vs >= 1)
        valid_vehicles = True
        dirs_final = []
        for d in matched_dirs:
            cl = d["codigoLinha"]
            try:
                pos = client.get_positions(cl)
                time.sleep(0.3)
                vs = pos.get("vs", [])
                n_vs = len(vs)
            except Exception as e:
                logger.warning(f"Erro ao consultar posições cl={cl} da linha {r_id}: {e}")
                n_vs = 0

            if n_vs < 1:
                logger.warning(
                    f"Linha {r_id} sentido cl={cl} sem veículos ativos no momento ({n_vs} veículos)."
                )
                valid_vehicles = False
                break

            dirs_final.append(
                {
                    "direction_id": int(d["direction_id"]),
                    "shape_id": str(d["shape_id"]),
                    "codigoLinha": int(cl),
                    "sl": int(d["sl"]),
                    "headsign": str(d.get("trip_headsign", "")),
                    "tp": str(d.get("tp", "")),
                    "ts": str(d.get("ts", "")),
                    "length_km": round(float(d["length_km"]), 2),
                    "peak_headway_min": round(float(d["peak_headway_min"]), 1),
                    "offpeak_headway_min": round(float(d["offpeak_headway_min"]), 1),
                    "scheduled_speed_kmh": round(float(d["scheduled_speed_kmh"]), 2) if pd.notna(d["scheduled_speed_kmh"]) else None,
                    "coverage_pct": round(float(d["coverage_pct"]), 4),
                    "dominant_station_id": str(d.get("dominant_station_id", "")),
                    "dominant_share": round(float(d.get("dominant_share", 0.0)), 4),
                    "n_vehicles_seen": int(n_vs),
                }
            )

        if not valid_vehicles:
            df_ranked.loc[df_ranked["route_id"] == r_id, "exclusion_reason"] = "sem_veiculos_ativos"
            continue

        # Linha aprovada!
        line_entry = {
            "route_id": r_id,
            "letreiro": letreiro,
            "tl": sufixo,
            "route_long_name": str(row["route_long_name"]),
            "dominant_station_id": dom_st,
            "coverage_pct_min": round(float(row["coverage_pct_min"]), 4),
            "weekday_trips": int(row["weekday_trips"]),
            "directions": dirs_final,
        }

        chosen_lines_meta.append(line_entry)
        chosen_stations.append(dom_st)
        df_ranked.loc[df_ranked["route_id"] == r_id, "exclusion_reason"] = "selecionada"
        logger.info(
            f"Linha {r_id} selecionada com sucesso ({len(chosen_lines_meta)}/{args.n_lines}) | "
            f"Estação Dominante: {dom_st} | Cobertura Min: {row['coverage_pct_min']:.2%}"
        )

    # Atualizar CSV com motivos finais
    df_ranked.to_csv(ranking_csv_path, index=False, encoding="utf-8")

    if len(chosen_lines_meta) < args.n_lines:
        logger.warning(
            f"Apenas {len(chosen_lines_meta)} linhas atenderam a todos os critérios (alvo: {args.n_lines})."
        )

    # 5. Gerar selected_lines.json em data/reference/lines
    chosen_route_ids = {line["route_id"] for line in chosen_lines_meta}
    chosen_points = joined_points[joined_points["route_id"].isin(chosen_route_ids)].copy()

    # Estações necessárias (todas as cobertas pelos pontos das linhas escolhidas)
    covered_chosen_pts = chosen_points[chosen_points["covered"]]
    stations_needed = sorted(list(covered_chosen_pts["station_id"].dropna().unique()))

    lines_payload = []
    for line in chosen_lines_meta:
        dirs = []
        for d in line["directions"]:
            dirs.append({
                "direction_id": int(d["direction_id"]),
                "codigoLinha": int(d["codigoLinha"]),
                "sl": int(d.get("sl", 1)),
            })
        lines_payload.append({
            "route_id": line["route_id"],
            "route_long_name": line["route_long_name"],
            "directions": dirs,
        })

    selected_lines_payload = {
        "stations_needed": stations_needed,
        "lines": lines_payload,
    }

    selected_lines_json_path = Config.SELECTED_LINES_PATH
    with open(selected_lines_json_path, "w", encoding="utf-8") as f:
        json.dump(selected_lines_payload, f, indent=2, ensure_ascii=False)
    logger.info(f"Arquivo de linhas selecionadas salvo em: {selected_lines_json_path}")

    # 6. Gerar line_station_mapping.parquet em data/reference/lines
    # Mapeamento do codigoLinha para os pontos
    cl_mapping = {}
    for line in chosen_lines_meta:
        for d in line["directions"]:
            cl_mapping[(line["route_id"], d["direction_id"])] = d["codigoLinha"]

    chosen_points["codigoLinha"] = chosen_points.apply(
        lambda r: cl_mapping.get((r["route_id"], r["direction_id"]), 0), axis=1
    ).astype(np.int32)

    # Reprojetar pontos para WGS84 para coordenadas de latitude e longitude
    pts_wgs84 = chosen_points.to_crs(epsg=4326)
    chosen_points["lat"] = pts_wgs84.geometry.y.astype(np.float64)
    chosen_points["lon"] = pts_wgs84.geometry.x.astype(np.float64)

    # Colunas essenciais requeridas
    parquet_cols = [
        "route_id",
        "direction_id",
        "codigoLinha",
        "lat",
        "lon",
        "station_id",
        "dist_to_station_m",
    ]
    df_parquet = chosen_points[parquet_cols].copy()
    df_parquet["direction_id"] = df_parquet["direction_id"].astype(np.int32)
    df_parquet["codigoLinha"] = df_parquet["codigoLinha"].astype(np.int32)
    df_parquet["dist_to_station_m"] = df_parquet["dist_to_station_m"].astype(np.float32)

    parquet_path = Config.LINE_STATION_MAPPING_PATH
    df_parquet.to_parquet(parquet_path, engine="pyarrow", compression="snappy", index=False)
    logger.info(f"Mapeamento trajeto-estação salvo em: {parquet_path}")

    t_end = time.time()
    logger.info(f"Pipeline select_lines concluído com sucesso em {t_end - t_start:.2f}s!")


if __name__ == "__main__":
    main()
