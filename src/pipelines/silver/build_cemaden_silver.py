import argparse
import json
import os
from pathlib import Path
import sys
from typing import List, Optional
import pandas as pd

from src.core.config import Config
from src.utils.cemaden_silver import generate_clean_hourly_export, parse_raw_bronze_csv
from src.utils.logger import get_logger
from src.utils.provenance import record_provenance, to_relative_path

logger = get_logger("build_cemaden_silver")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Geração do dataset limpo da camada Silver do CEMADEN (rain_silver_export.csv)."
    )
    parser.add_argument("--stations", type=str, default=None, help="Filtro opcional de estações.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    Config.ensure_dirs()

    logger.info("Iniciando processamento Silver do CEMADEN...")

    bronze_dir = Path(Config.CEMADEN_BRONZE_DIR)
    if not bronze_dir.exists():
        logger.error(f"Diretório bronze não encontrado: {bronze_dir}")
        return 1

    # Carrega estações necessárias de selected_lines.json se disponível
    sel_path = Path(Config.SELECTED_LINES_PATH)
    all_needed = None
    if sel_path.exists():
        try:
            with open(sel_path, "r", encoding="utf-8") as f:
                all_needed = json.load(f).get("stations_needed")
        except Exception:
            pass

    station_filter = set(args.stations.split(",")) if args.stations else None

    parsed_dfs = []
    bronze_files_read = 0
    # Lê todos os CSVs disponíveis nas pastas station=*
    for st_dir in sorted(bronze_dir.glob("station=*")):
        sid = st_dir.name.replace("station=", "")
        if station_filter and sid not in station_filter:
            continue

        for csv_file in sorted(st_dir.glob("req_*.csv")):
            bronze_files_read += 1
            with open(csv_file, "rb") as f:
                csv_bytes = f.read()
            df_st = parse_raw_bronze_csv(csv_bytes, station_id=sid)
            if not df_st.empty:
                parsed_dfs.append(df_st)

    df_consolidated = pd.concat(parsed_dfs, ignore_index=True) if parsed_dfs else pd.DataFrame()

    # Gera a exportação limpa com preservação de granularidade e preenchimento de horas faltantes
    df_clean = generate_clean_hourly_export(df_consolidated, all_station_ids=all_needed)

    # Grava literalmente só o rain_silver_export.csv
    silver_dir = Path(Config.CEMADEN_SILVER_DIR)
    silver_dir.mkdir(parents=True, exist_ok=True)
    csv_dest = silver_dir / "rain_silver_export.csv"

    df_clean.to_csv(csv_dest, index=False)
    logger.info(f"Arquivo Silver gerado com sucesso: {csv_dest} ({len(df_clean)} linhas).")

    missing_rows = (
        int(df_clean["is_missing"].sum())
        if "is_missing" in df_clean.columns and not df_clean.empty
        else 0
    )
    real_rows = len(df_clean) - missing_rows
    stations_in_output = (
        int(df_clean["station_id"].nunique())
        if "station_id" in df_clean.columns and not df_clean.empty
        else 0
    )
    sources = [to_relative_path(bronze_dir)] + (
        [to_relative_path(sel_path)] if sel_path.exists() else []
    )
    record_provenance(
        target_dir=silver_dir,
        stage="build_cemaden_silver",
        source=sources,
        output=csv_dest.name,
        records=len(df_clean),
        extra={
            "bronze_files_read": bronze_files_read,
            "stations_count": stations_in_output,
            "real_rows": real_rows,
            "missing_rows": missing_rows,
            "station_filter": sorted(station_filter) if station_filter else None,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
