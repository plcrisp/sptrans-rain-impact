import argparse
from pathlib import Path
import sys
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parents[3]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.core.config import Config
from src.utils.logger import get_logger
from src.utils.provenance import record_provenance, to_relative_path
from src.utils.silver_enrichment import enrich_sptrans_with_rain, load_silver_table

logger = get_logger("enrich_sptrans_rain")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich SPTrans Silver telemetry with CEMADEN hourly rainfall."
    )
    return parser.parse_args(argv)


def run_enrichment() -> int:
    """Join SPTrans Silver and CEMADEN Silver into sptrans_rain_enriched.parquet."""
    Config.ensure_dirs()
    logger.info("Iniciando enriquecimento Silver (SPTrans x CEMADEN)...")

    sptrans_df, sptrans_src = load_silver_table(
        Path(Config.SPTRANS_SILVER_PARQUET_PATH),
        Path(Config.SPTRANS_SILVER_DIR) / "sptrans_silver_export.csv",
    )
    rain_df, rain_src = load_silver_table(
        Path(Config.CEMADEN_SILVER_PARQUET_PATH),
        Path(Config.CEMADEN_SILVER_DIR) / "rain_silver_export.csv",
    )

    enriched_df, stats = enrich_sptrans_with_rain(sptrans_df, rain_df)

    dest_path = Path(Config.ENRICHED_SILVER_PARQUET_PATH)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    enriched_df.to_parquet(dest_path, engine="pyarrow", compression="snappy", index=False)

    logger.info("Dataset Silver enriquecido salvo em: %s (%s linhas).", to_relative_path(dest_path), len(enriched_df))

    record_provenance(
        target_dir=dest_path.parent,
        stage="enrich_sptrans_rain",
        source=[to_relative_path(sptrans_src), to_relative_path(rain_src)],
        output=dest_path.name,
        records=len(enriched_df),
        extra=stats,
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parse_args(argv)
    return run_enrichment()


if __name__ == "__main__":
    sys.exit(main())
