import argparse
import os
from pathlib import Path
import subprocess
import sys
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parents[3]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.core.config import Config
from src.pipelines.gold.load_duckdb import connect_db, verify_gold_model
from src.utils.logger import get_logger
from src.utils.provenance import record_provenance, to_relative_path

logger = get_logger("run_dbt")


def _resolve_dbt_command() -> List[str]:
    """Use the dbt executable in the current Python environment when available."""
    scripts_dir = Path(sys.executable).parent
    for candidate in ("dbt.exe", "dbt"):
        exe_path = scripts_dir / candidate
        if exe_path.exists():
            return [str(exe_path)]
    return ["dbt"]


def _extract_dbt_summary(stdout: str) -> str:
    """Return the final 'Done.' summary line from dbt CLI output, if present."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if "Done." in stripped:
            return stripped
    return "concluído"


def _run_dbt_step(cmd: List[str], label: str, cwd: Path, env: dict) -> None:
    """Run a dbt CLI command, logging only its summary unless it fails."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        if proc.stdout:
            logger.error("Saída do %s:\n%s", label, proc.stdout.strip())
        if proc.stderr:
            logger.error("Erro do %s:\n%s", label, proc.stderr.strip())
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)
    logger.info("%s concluído: %s", label, _extract_dbt_summary(proc.stdout or ""))


def run_dbt(select: Optional[str] = None) -> int:
    """Run dbt models against project.duckdb and verify Gold model integrity."""
    Config.ensure_dirs()
    dbt_dir = Path(Config.DBT_PROJECT_DIR)
    db_path = Path(Config.GOLD_DB_PATH).resolve()

    if not dbt_dir.exists():
        raise FileNotFoundError(f"dbt project directory not found: {dbt_dir}")
    if not db_path.exists():
        raise FileNotFoundError(
            f"DuckDB database not found at {db_path}. Run gold_load first."
        )

    cmd_run = _resolve_dbt_command() + [
        "run",
        "--profiles-dir",
        str(dbt_dir),
    ]
    if select:
        cmd_run.extend(["--select", select])

    cmd_test = _resolve_dbt_command() + [
        "test",
        "--profiles-dir",
        str(dbt_dir),
    ]
    if select:
        cmd_test.extend(["--select", select])

    env = os.environ.copy()
    env["GOLD_DB_PATH"] = db_path.as_posix()

    logger.info("Executando modelos dbt em %s (banco=%s)...", to_relative_path(dbt_dir), to_relative_path(db_path))
    _run_dbt_step(cmd_run, "dbt run", dbt_dir, env)

    logger.info("Executando testes de dados dbt em %s...", to_relative_path(dbt_dir))
    _run_dbt_step(cmd_test, "dbt test", dbt_dir, env)

    con = connect_db(db_path)
    try:
        counts = verify_gold_model(con)
    finally:
        con.close()

    record_provenance(
        target_dir=db_path.parent,
        stage="run_gold_dbt",
        source=[to_relative_path(dbt_dir / "models"), to_relative_path(db_path)],
        output=db_path.name,
        records=counts["fact_rows"],
        extra=counts,
    )
    logger.info("Construção e validação da camada Ouro via dbt concluídas com sucesso.")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Gold Star Schema models declaratively via dbt."
    )
    parser.add_argument(
        "--select",
        type=str,
        default=None,
        help="Optional dbt model selector (e.g., dim_lines+).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    return run_dbt(select=args.select)


if __name__ == "__main__":
    sys.exit(main())
