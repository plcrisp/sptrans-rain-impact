import argparse
from pathlib import Path
import sys
import time
from typing import Callable, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.utils.logger import get_logger

logger = get_logger("pipeline")


def stage_cemaden_ingest() -> int:
    """Ingest raw CEMADEN rainfall data for the SPTrans collection window."""
    from src.pipelines.bronze import ingest_cemaden

    return ingest_cemaden.main([])


def stage_sptrans_silver() -> int:
    """Clean SPTrans Bronze GPS telemetry and export Silver dataset."""
    from src.pipelines.silver import build_sptrans_silver

    return build_sptrans_silver.run_pipeline()


def stage_cemaden_silver() -> int:
    """Clean CEMADEN Bronze rainfall CSVs and export Silver dataset."""
    from src.pipelines.silver import build_cemaden_silver

    return build_cemaden_silver.main([])


def stage_silver_enrichment() -> int:
    """Join SPTrans Silver and CEMADEN Silver into sptrans_rain_enriched.parquet."""
    from src.pipelines.silver import enrich_sptrans_rain

    return enrich_sptrans_rain.run_enrichment()


def stage_gold_load() -> int:
    """Load Silver datasets into DuckDB silver schema."""
    from src.pipelines.gold import load_duckdb

    return load_duckdb.run_load()


def stage_gold_dbt() -> int:
    """Build Gold Star Schema models declaratively via dbt."""
    from src.pipelines.gold import run_dbt

    return run_dbt.run_dbt()


StageFn = Optional[Callable[[], Optional[int]]]

STAGES: List[Tuple[str, StageFn]] = [
    ("cemaden_ingest", stage_cemaden_ingest),
    ("sptrans_silver", stage_sptrans_silver),
    ("cemaden_silver", stage_cemaden_silver),
    ("silver_enrichment", stage_silver_enrichment),
    ("gold_load", stage_gold_load),
    ("gold_dbt", stage_gold_dbt),
]


def _execute_stage(name: str, fn: Callable[[], Optional[int]]) -> None:
    """Run one pipeline stage with timing and explicit failure logging."""
    logger.info("Etapa %s: iniciada", name)
    t0 = time.monotonic()
    try:
        result = fn()
        if isinstance(result, int) and result != 0:
            raise RuntimeError(f"Stage '{name}' exited with non-zero code: {result}")
    except Exception:
        logger.exception("Etapa %s: falhou", name)
        raise
    logger.info("Etapa %s: concluída em %.2fs", name, time.monotonic() - t0)


def run_pipeline(
    start_from: Optional[str] = None,
    stages: Optional[List[Tuple[str, StageFn]]] = None,
) -> int:
    """Execute pipeline stages sequentially, optionally resuming from start_from."""
    stage_list = stages if stages is not None else STAGES
    stage_names = [name for name, _ in stage_list]

    if start_from is not None and start_from not in stage_names:
        raise ValueError(
            f"Unknown stage '{start_from}'. Available stages: {', '.join(stage_names)}"
        )

    started = start_from is None
    t_total = time.monotonic()
    logger.info(
        "Pipeline iniciado (início=%s)",
        start_from if start_from is not None else "do começo",
    )

    for name, fn in stage_list:
        if name == start_from:
            started = True

        if not started or fn is None:
            reason = "pendente de implementação" if fn is None else f"anterior a '{start_from}'"
            logger.info("Etapa %s: pulada (%s)", name, reason)
            continue

        _execute_stage(name, fn)

    logger.info("Pipeline concluído em %.2fs", time.monotonic() - t_total)
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unified batch pipeline orchestrator (CEMADEN Bronze -> Silver -> Gold)."
    )
    parser.add_argument(
        "stage",
        nargs="?",
        default=None,
        help="Optional stage name to resume from (e.g., sptrans_silver, silver_enrichment).",
    )
    parser.add_argument(
        "--start-from",
        dest="start_from",
        default=None,
        help="Stage name to resume from (alternative to positional argument).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all declared pipeline stages and their implementation status.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.list:
        for name, fn in STAGES:
            status = "ready" if fn is not None else "pending (None)"
            logger.info("Stage %-20s | %s", name, status)
        return 0

    start_from = args.start_from or args.stage
    return run_pipeline(start_from=start_from)


if __name__ == "__main__":
    sys.exit(main())
