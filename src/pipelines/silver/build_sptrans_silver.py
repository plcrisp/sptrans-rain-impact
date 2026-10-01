import argparse
from pathlib import Path
import sys
from typing import List, Optional
import pandas as pd

from src.core.config import Config
from src.utils.logger import get_logger
from src.utils.sptrans_silver import (
    add_station,
    add_time_columns,
    clean_readings,
    compute_speed,
    load_bronze,
)

logger = get_logger("build_sptrans_silver")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Geração do dataset limpo da camada Silver da SPTrans (sptrans_silver_export.csv)."
    )
    return parser.parse_args(argv)


def run_pipeline() -> int:
    """Executa o pipeline Silver da SPTrans gerando estritamente o sptrans_silver_export.csv."""
    Config.ensure_dirs()
    logger.info("Iniciando pipeline Silver da SPTrans...")

    bronze_dir = Path(Config.SPTRANS_BRONZE_DIR)
    if not bronze_dir.exists():
        logger.error(f"Diretório bronze SPTrans não encontrado: {bronze_dir}")
        return 1

    # 1. Carrega dados brutos do bronze
    df_raw, stats = load_bronze(bronze_dir)
    logger.info(f"Bronze carregado: {stats['files_read']} arquivos lidos, {len(df_raw)} leituras brutas.")

    # 2. Limpeza estrutural e deduplicação
    df_clean, discard_counts = clean_readings(df_raw)
    logger.info(f"Leituras limpas: {len(df_clean)} mantidas, descartadas: {discard_counts}.")

    # 3. Cálculo de velocidade e flags
    df_speed = compute_speed(df_clean)

    # 4. Derivação de colunas temporais
    df_timed = add_time_columns(df_speed)

    # 5. Enriquecimento espacial com estações
    mapping_path = Path(Config.LINE_STATION_MAPPING_PATH)
    if not mapping_path.exists():
        logger.error(f"Mapeamento line_station_mapping não encontrado: {mapping_path}")
        return 1
    mapping_df = pd.read_parquet(mapping_path)
    df_enriched = add_station(df_timed, mapping_df)

    # 6. Ordenação final e seleção estrita das colunas solicitadas
    df_final = df_enriched.sort_values(by=["vehicle_id", "ta_utc"]).reset_index(drop=True)

    df_final["speed_kmh"] = df_final["speed_kmh"].round(2)
    df_final["dist_to_station_m"] = df_final["dist_to_station_m"].round(2)

    export_cols = [
        "vehicle_id",
        "codigoLinha",
        "route_id",
        "direction_id",
        "timestamp_sp",
        "lat",
        "lon",
        "speed_kmh",
        "speed_flag",
        "is_peak",
        "station_id",
        "dist_to_station_m",
    ]

    silver_dir = Path(Config.SPTRANS_SILVER_DIR)
    silver_dir.mkdir(parents=True, exist_ok=True)
    csv_dest = silver_dir / "sptrans_silver_export.csv"

    df_final[export_cols].to_csv(csv_dest, index=False)
    logger.info(f"Arquivo Silver gerado com sucesso: {csv_dest} ({len(df_final)} linhas).")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parse_args(argv)
    return run_pipeline()


if __name__ == "__main__":
    sys.exit(main())
