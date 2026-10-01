import argparse
import datetime
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from src.clients.cemaden_client import CemadenClient
from src.core.config import Config
from src.utils.cemaden_bronze import (
    append_manifest_event,
    chunk_date_range,
    compute_uncovered_chunks,
    expand_window_with_margin,
    format_dt_param,
    get_sptrans_date_range,
    inspect_and_extract_zip,
    load_manifest,
    save_atomic_bronze_csv,
)
from src.utils.logger import get_logger

logger = get_logger("ingest_cemaden")

_interrupted = False


def _signal_handler(signum, frame):
    global _interrupted
    logger.warning("Interrupção solicitada. Finalizando ciclo com segurança...")
    _interrupted = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingestão de dados brutos de chuva do CEMADEN (Camada Bronze)."
    )
    parser.add_argument("--start", type=str, default=None, help="Data inicial YYYY-MM-DD.")
    parser.add_argument("--end", type=str, default=None, help="Data final YYYY-MM-DD.")
    parser.add_argument(
        "--margin-days",
        type=int,
        default=1,
        help="Dias de margem antes e depois da janela SPTrans (padrão: 1).",
    )
    parser.add_argument(
        "--stations",
        type=str,
        default=None,
        help="Lista de station_ids separados por vírgula para sobrescrever stations_needed.",
    )
    parser.add_argument(
        "--max-span-days",
        type=int,
        default=14,
        help="Tamanho máximo em dias de cada job agendado (padrão: 14).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Força o reagendamento e download de todos os blocos, ignorando o manifest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas exibe o plano de execução sem chamar a API de dados.",
    )
    return parser.parse_args(argv)


def load_target_stations() -> Tuple[List[str], Dict[str, str]]:
    """Carrega stations_needed e o mapeamento de estado/UF."""
    sel_path = Path(Config.SELECTED_LINES_PATH)
    if not sel_path.exists():
        raise FileNotFoundError(f"Arquivo de linhas selecionadas não encontrado: {sel_path}")

    with open(sel_path, "r", encoding="utf-8") as f:
        stations_needed = json.load(f).get("stations_needed", [])

    stations_path = Path(Config.STATIONS_DATA_DIR) / "cemaden_sp_stations.json"
    station_uf_map: Dict[str, str] = {}
    if stations_path.exists():
        with open(stations_path, "r", encoding="utf-8") as f:
            for item in json.load(f):
                sid = str(item.get("station_id") or item.get("codestacao") or "")
                uf = item.get("state") or item.get("uf") or "SP"
                if sid:
                    station_uf_map[sid] = uf

    return [str(s) for s in stations_needed], station_uf_map


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    Config.ensure_dirs()
    run_id = f"cemaden_{uuid.uuid4().hex[:6]}"

    logger.info("Iniciando ingestão bronze do CEMADEN...")

    # 1. Carregar estações
    stations_needed, station_uf_map = load_target_stations()
    if args.stations:
        target_station_ids = [s.strip() for s in args.stations.split(",") if s.strip()]
    else:
        target_station_ids = stations_needed

    # 2. Determinar janela temporal
    if args.start and args.end:
        start_d = datetime.date.fromisoformat(args.start)
        end_d = datetime.date.fromisoformat(args.end)
    else:
        sp_start, sp_end = get_sptrans_date_range()
        start_d, end_d = expand_window_with_margin(sp_start, sp_end, margin_days=args.margin_days)

    target_chunks = chunk_date_range(start_d, end_d, max_span_days=args.max_span_days)
    manifest_events = load_manifest()

    station_plan: Dict[str, List[Tuple[datetime.date, datetime.date]]] = {}
    today = datetime.date.today()
    for sid in target_station_ids:
        station_plan[sid] = compute_uncovered_chunks(
            station_id=sid,
            target_chunks=target_chunks,
            manifest_events=manifest_events,
            force=args.force,
            reference_date=today,
        )

    total_chunks = sum(len(c) for c in station_plan.values())

    if args.dry_run:
        logger.info(f"Dry-run: {len(target_station_ids)} estações, {total_chunks} blocos pendentes de download.")
        return 0

    if total_chunks == 0 and not any(ev.get("status") == "pending" for ev in manifest_events):
        logger.info("Todos os blocos da janela já estão cobertos no bronze.")
        return 0

    client = CemadenClient()

    # Recuperar jobs pendentes anteriores
    pending_jobs: Dict[int, Dict[str, Any]] = {}
    for ev in manifest_events:
        jid = ev.get("job_id")
        if jid is not None and ev.get("status") == "pending":
            pending_jobs[int(jid)] = {
                "station_id": ev.get("station_id"),
                "uf": ev.get("uf", "SP"),
                "start_dt": ev.get("start_str"),
                "end_dt": ev.get("end_str"),
                "scheduled_at": time.time(),
            }

    # Fila de novas requisições
    queue: List[Dict[str, Any]] = []
    for sid in target_station_ids:
        uf = station_uf_map.get(sid, "SP")
        for c_start, c_end in station_plan[sid]:
            queue.append(
                {
                    "station_id": sid,
                    "uf": uf,
                    "start_dt": format_dt_param(c_start, is_end=False),
                    "end_dt": format_dt_param(c_end, is_end=True),
                }
            )

    max_pending = Config.CEMADEN_MAX_PENDING_JOBS
    poll_interval = Config.CEMADEN_POLL_INTERVAL_SECONDS
    job_timeout = Config.CEMADEN_JOB_TIMEOUT_SECONDS

    q_idx = 0
    total_to_schedule = len(queue)

    while (q_idx < total_to_schedule or pending_jobs) and not _interrupted:
        while len(pending_jobs) < max_pending and q_idx < total_to_schedule and not _interrupted:
            req = queue[q_idx]
            q_idx += 1
            sid = req["station_id"]

            try:
                job_id, _, _ = client.schedule(
                    station_id=sid,
                    uf=req["uf"],
                    start_dt=req["start_dt"],
                    end_dt=req["end_dt"],
                )
                now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
                append_manifest_event(
                    {
                        "ts": now_utc,
                        "station_id": sid,
                        "job_id": job_id,
                        "status": "pending",
                        "start_str": req["start_dt"],
                        "end_str": req["end_dt"],
                        "uf": req["uf"],
                    }
                )
                pending_jobs[job_id] = {**req, "scheduled_at": time.time()}
                time.sleep(1.5)
            except Exception as e:
                logger.error(f"Erro ao agendar estação {sid}: {e}")
                now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
                append_manifest_event(
                    {
                        "ts": now_utc,
                        "station_id": sid,
                        "job_id": None,
                        "status": "error",
                        "start_str": req["start_dt"],
                        "end_str": req["end_dt"],
                        "error": str(e),
                    }
                )

        if not pending_jobs:
            break

        time.sleep(poll_interval)
        try:
            jobs_list, _, _ = client.list_jobs()
            jobs_by_id = {int(j.get("id")): j for j in jobs_list if j.get("id") is not None}
        except Exception:
            jobs_by_id = {}

        now_time = time.time()
        finished = []

        for jid, job_info in list(pending_jobs.items()):
            sid = job_info["station_id"]
            item = jobs_by_id.get(jid)
            desc = item.get("status", {}).get("description") if item else None
            link = item.get("link") if item else None

            if now_time - job_info["scheduled_at"] > job_timeout and desc != "CONCLUIDA":
                append_manifest_event({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "station_id": sid, "job_id": jid, "status": "timeout"})
                finished.append(jid)
                continue

            if desc == "CONCLUIDA" and link:
                try:
                    zip_bytes, _, _ = client.download(link)
                    _, csv_bytes, n_rows, reason = inspect_and_extract_zip(zip_bytes)

                    if csv_bytes is not None:
                        csv_path = save_atomic_bronze_csv(
                            station_id=sid,
                            start_dt=job_info["start_dt"],
                            end_dt=job_info["end_dt"],
                            csv_bytes=csv_bytes,
                        )
                        rel_path = str(csv_path.relative_to(Config.BASE_DIR)).replace("\\", "/")
                        append_manifest_event(
                            {
                                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                "station_id": sid,
                                "job_id": jid,
                                "status": "ok",
                                "start_str": job_info["start_dt"],
                                "end_str": job_info["end_dt"],
                                "file_path": rel_path,
                                "n_rows": n_rows,
                            }
                        )
                        logger.info(f"Dados baixados para {sid}: {csv_path.name} ({n_rows} linhas).")
                    else:
                        append_manifest_event(
                            {
                                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                "station_id": sid,
                                "job_id": jid,
                                "status": "no_data",
                                "start_str": job_info["start_dt"],
                                "end_str": job_info["end_dt"],
                                "reason": reason or "no_csv",
                            }
                        )
                        logger.warning(f"Estação {sid} concluída sem dados ({reason}).")

                    finished.append(jid)
                except Exception as e:
                    logger.error(f"Erro no download do job {jid} ({sid}): {e}")
                    finished.append(jid)

            elif desc in ("REJEITADA", "EXPIRADA"):
                append_manifest_event(
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "station_id": sid,
                        "job_id": jid,
                        "status": desc.lower(),
                        "start_str": job_info["start_dt"],
                        "end_str": job_info["end_dt"],
                    }
                )
                finished.append(jid)

        for fid in finished:
            pending_jobs.pop(fid, None)

    logger.info("Ingestão bronze do CEMADEN concluída com sucesso.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
