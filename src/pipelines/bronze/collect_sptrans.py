import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional
import uuid

# Garante que a raiz do repositório esteja no sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.clients.sptrans_client import PositionResponse, SPTransAuthError, SPTransClient, SPTransError
from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("collect_sptrans")

# Variável de controle para interrupção graciosa
_stop_requested = False


def _signal_handler(signum, frame):
    """Trata sinais de interrupção (SIGINT, SIGTERM, SIGBREAK) de forma graciosa."""
    global _stop_requested
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.info(f"Sinal de interrupção recebido ({sig_name}). Encerrando ciclo de forma limpa...")
    _stop_requested = True


def register_signal_handlers():
    """Registra tratadores de sinal compatíveis com a plataforma."""
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)


def set_keep_awake(enable: bool) -> bool:
    """Evita que o sistema operacional entre em suspensão durante a coleta (Windows apenas).

    Totalmente isolado e seguro para execução em Linux.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        if enable:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            logger.info("Modo keep-awake ativado (SetThreadExecutionState: ES_SYSTEM_REQUIRED).")
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            logger.info("Modo keep-awake desativado.")
        return True
    except Exception as e:
        logger.warning(f"Não foi possível configurar SetThreadExecutionState: {e}")
        return False


def compute_payload_hash(payload: Dict[str, Any]) -> str:
    """Calcula hash SHA-256 determinístico para detecção de respostas inalteradas."""
    raw_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest()


def build_partition_path(collected_at: datetime, base_dir: str = Config.SPTRANS_BRONZE_DIR) -> str:
    """Constrói o caminho de partição date=YYYY-MM-DD/hour=HH baseado em UTC."""
    date_str = collected_at.strftime("%Y-%m-%d")
    hour_str = collected_at.strftime("%H")
    return os.path.join(base_dir, f"date={date_str}", f"hour={hour_str}")


def build_filename(codigo_linha: int, collected_at: datetime) -> str:
    """Gera o nome de arquivo imutável com timestamp compacto em UTC e microssegundos."""
    compact_ts = collected_at.strftime("%Y%m%dT%H%M%S%fZ")
    return f"posicao_{codigo_linha}_{compact_ts}.json"


def save_bronze_record(
    codigo_linha: int,
    target: Dict[str, Any],
    pos_resp: PositionResponse,
    cycle_id: int,
    run_id: str,
    collected_at: datetime,
    base_dir: str = Config.SPTRANS_BRONZE_DIR,
) -> str:
    """Grava o registro de forma atômica e imutável na camada bronze."""
    target_dir = build_partition_path(collected_at, base_dir=base_dir)
    os.makedirs(target_dir, exist_ok=True)

    filename = build_filename(codigo_linha, collected_at)
    final_path = os.path.join(target_dir, filename)
    tmp_path = os.path.join(target_dir, f"{filename}.tmp")

    if os.path.exists(final_path):
        raise FileExistsError(f"Arquivo já existe e não pode ser sobrescrito: {final_path}")

    vs = pos_resp.payload.get("vs", [])
    n_vehicles = len(vs) if isinstance(vs, list) else 0

    envelope = {
        "schema_version": 1,
        "collected_at_utc": collected_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "codigoLinha": codigo_linha,
        "route_id": target["route_id"],
        "direction_id": target["direction_id"],
        "http_status": pos_resp.http_status,
        "latency_ms": pos_resp.latency_ms,
        "attempts": pos_resp.attempts,
        "relogin": pos_resp.relogin,
        "n_vehicles": n_vehicles,
        "cycle_id": cycle_id,
        "run_id": run_id,
        "request_url": f"{Config.SPTRANS_BASE_URL}/Posicao/Linha?codigoLinha={codigo_linha}",
    }

    record = {
        "envelope": envelope,
        "payload": pos_resp.payload,
    }

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(record, f, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp_path, final_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise e

    return final_path


def seed_last_hashes_and_cleanup(base_dir: str = Config.SPTRANS_BRONZE_DIR) -> tuple[Dict[int, str], int]:
    """Lê os arquivos bronze mais recentes para semear os hashes e limpa arquivos .tmp órfãos."""
    last_hashes: Dict[int, str] = {}
    latest_files: Dict[int, tuple[str, str]] = {}  # codigoLinha -> (filename, fullpath)
    orphan_tmp_count = 0

    if not os.path.exists(base_dir):
        return last_hashes, orphan_tmp_count

    for root, _, files in os.walk(base_dir):
        for f in files:
            full_path = os.path.join(root, f)
            if f.endswith(".tmp"):
                orphan_tmp_count += 1
                try:
                    os.remove(full_path)
                except OSError:
                    pass
                continue

            if f.startswith("posicao_") and f.endswith(".json"):
                parts = f[len("posicao_") : -len(".json")].split("_")
                if len(parts) >= 2:
                    try:
                        cl = int(parts[0])
                        ts = parts[1]
                        if cl not in latest_files or ts > latest_files[cl][0]:
                            latest_files[cl] = (ts, full_path)
                    except ValueError:
                        continue

    # Carrega payload dos arquivos mais recentes para semear o hash
    for cl, (_, path) in latest_files.items():
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if "payload" in data:
                    last_hashes[cl] = compute_payload_hash(data["payload"])
        except Exception as e:
            logger.warning(f"Erro ao semear hash anterior de {path}: {e}")

    return last_hashes, orphan_tmp_count


def load_targets(lines_file: str) -> List[Dict[str, Any]]:
    """Carrega selected_lines.json e monta a lista plana de alvos."""
    with open(lines_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    targets = []
    for line in data.get("lines", []):
        r_id = line["route_id"]
        for d in line.get("directions", []):
            targets.append(
                {
                    "route_id": r_id,
                    "direction_id": int(d["direction_id"]),
                    "codigoLinha": int(d["codigoLinha"]),
                    "sl": int(d.get("sl", 1)),
                }
            )
    return targets


def run_collection(
    interval_s: int,
    lines_file: str,
    duration_min: Optional[float] = None,
    max_cycles: Optional[int] = None,
    once: bool = False,
    keep_awake: bool = True,
    simulate_logout_at_cycle: Optional[int] = None,
    client: Optional[SPTransClient] = None,
    base_dir: str = Config.SPTRANS_BRONZE_DIR,
) -> int:
    """Loop principal de coleta contínua da SPTrans."""
    global _stop_requested
    register_signal_handlers()

    Config.ensure_dirs()
    Config.validate(["SPTRANS_TOKEN"])

    targets = load_targets(lines_file)
    logger.info(f"Coletor iniciado com {len(targets)} alvos carregados de {lines_file}.")

    # Semear hashes e limpar arquivos temporários órfãos
    last_hashes, orphan_tmps = seed_last_hashes_and_cleanup(base_dir=base_dir)
    if orphan_tmps > 0:
        logger.info(f"{orphan_tmps} arquivos .tmp órfãos foram removidos ao inicializar.")
    if last_hashes:
        logger.info(f"{len(last_hashes)} linhas tiveram seu último estado semeado com sucesso.")

    run_id = str(uuid.uuid4())
    cycle_id = 0
    consecutive_failed_cycles = 0
    recovery_alert_needed = False

    total_records_saved = 0
    total_cycles_executed = 0
    total_errors_encountered = 0

    start_monotonic = time.monotonic()
    run_start_time = time.time()

    should_keep_awake = keep_awake and (sys.platform == "win32")
    if should_keep_awake:
        set_keep_awake(True)

    client_provided = client is not None
    sptrans = client or SPTransClient()

    try:
        while not _stop_requested:
            cycle_id += 1
            total_cycles_executed += 1
            cycle_start_utc = datetime.now(timezone.utc)
            cycle_start_mono = time.monotonic()

            # Simulação de logout para teste de aceite
            if simulate_logout_at_cycle and cycle_id == simulate_logout_at_cycle:
                logger.info(
                    f"[TESTE] Simulando logout no ciclo {cycle_id} (limpando cookies da sessão)..."
                )
                sptrans.session.cookies.clear()

            saved_count = 0
            unchanged_count = 0
            empty_count = 0
            errors_count = 0
            total_vehicles_seen = 0
            latencies: List[float] = []
            relogins_in_cycle = 0

            for target in targets:
                if _stop_requested:
                    break

                cl = target["codigoLinha"]
                r_id = target["route_id"]
                dir_id = target["direction_id"]

                try:
                    pos_resp = sptrans.get_positions(cl)
                    latencies.append(pos_resp.latency_ms)
                    if pos_resp.relogin:
                        relogins_in_cycle += 1

                    vs = pos_resp.payload.get("vs", [])
                    if not vs:
                        empty_count += 1
                        continue

                    total_vehicles_seen += len(vs)
                    payload_hash = compute_payload_hash(pos_resp.payload)

                    # Verifica se o payload é idêntico ao último gravado
                    if last_hashes.get(cl) == payload_hash:
                        unchanged_count += 1
                        continue

                    # Grava arquivo na camada bronze
                    now_utc = datetime.now(timezone.utc)
                    save_bronze_record(
                        codigo_linha=cl,
                        target=target,
                        pos_resp=pos_resp,
                        cycle_id=cycle_id,
                        run_id=run_id,
                        collected_at=now_utc,
                        base_dir=base_dir,
                    )
                    last_hashes[cl] = payload_hash
                    saved_count += 1
                    total_records_saved += 1

                except Exception as e:
                    errors_count += 1
                    total_errors_encountered += 1
                    logger.warning(
                        f"Falha ao consultar linha {r_id} (dir={dir_id}, cl={cl}): {type(e).__name__} - {e}"
                    )

                # Pausa configurável entre chamadas
                if Config.COLLECT_INTER_CALL_DELAY_SECONDS > 0:
                    time.sleep(Config.COLLECT_INTER_CALL_DELAY_SECONDS)

            # Métricas agregadas do ciclo
            avg_lat = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
            max_lat = round(max(latencies), 2) if latencies else 0.0

            # Log resumido do ciclo no logger
            logger.info(
                f"Ciclo {cycle_id} concluído | Alvos: {len(targets)} | Gravados: {saved_count} | "
                f"Inalterados: {unchanged_count} | Vazios: {empty_count} | Erros: {errors_count} | "
                f"Veículos: {total_vehicles_seen} | Latência Média: {avg_lat}ms (Máx: {max_lat}ms) | "
                f"Reautenticações: {relogins_in_cycle}"
            )


            # Detecção de falhas consecutivas
            if errors_count == len(targets):
                consecutive_failed_cycles += 1
                if consecutive_failed_cycles >= Config.COLLECT_MAX_CONSECUTIVE_FAILED_CYCLES:
                    logger.error(
                        f"ALERTA CRÍTICO: Todos os {len(targets)} alvos falharam por {consecutive_failed_cycles} ciclos consecutivos! Mantendo tentativas..."
                    )
                    recovery_alert_needed = True
            else:
                if recovery_alert_needed:
                    logger.info("RECUPERAÇÃO: Conexão restabelecida após falhas consecutivas.")
                    recovery_alert_needed = False
                consecutive_failed_cycles = 0

            # Condições de parada
            if once:
                break
            if max_cycles and cycle_id >= max_cycles:
                logger.info(f"Limite de ciclos atingido ({max_cycles}).")
                break
            if duration_min and (time.time() - run_start_time) >= (duration_min * 60):
                logger.info(f"Duração máxima atingida ({duration_min} minutos).")
                break
            if _stop_requested:
                break

            # Agendamento de taxa fixa (drift-free scheduling)
            target_next_mono = start_monotonic + (cycle_id * interval_s)
            now_mono = time.monotonic()
            sleep_time = target_next_mono - now_mono

            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                logger.warning(
                    f"Ciclo {cycle_id} demorou mais que o intervalo (atraso de {-sleep_time:.2f}s). Iniciando próximo ciclo imediatamente."
                )
                if sleep_time < -interval_s:
                    start_monotonic = now_mono - (cycle_id * interval_s)

    finally:
        if not client_provided:
            sptrans.close()
        if should_keep_awake:
            set_keep_awake(False)

        total_elapsed = round(time.time() - run_start_time, 2)
        logger.info(
            f"=== Resumo Final do Coletor ===\n"
            f"- Ciclos executados: {total_cycles_executed}\n"
            f"- Arquivos bronze gravados: {total_records_saved}\n"
            f"- Total de erros: {total_errors_encountered}\n"
            f"- Duração total: {total_elapsed}s\n"
            f"================================"
        )

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Job de coleta contínua da API Olho Vivo SPTrans (Camada Bronze)."
    )
    parser.add_argument(
        "--interval-s",
        type=int,
        default=Config.COLLECT_INTERVAL_SECONDS,
        help=f"Intervalo entre ciclos em segundos (default: {Config.COLLECT_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--lines-file",
        type=str,
        default=Config.SELECTED_LINES_PATH,
        help=f"Caminho do arquivo selected_lines.json (default: {Config.SELECTED_LINES_PATH}).",
    )
    parser.add_argument(
        "--duration-min",
        type=float,
        default=None,
        help="Duração máxima da execução em minutos.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Número máximo de ciclos antes de encerrar.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Executa um único ciclo e encerra (smoke test).",
    )
    parser.add_argument(
        "--keep-awake",
        dest="keep_awake",
        action="store_true",
        default=True,
        help="Evita suspensão do sistema no Windows (default: True).",
    )
    parser.add_argument(
        "--no-keep-awake",
        dest="keep_awake",
        action="store_false",
        help="Desativa a prevenção de suspensão do sistema.",
    )
    parser.add_argument(
        "--simulate-logout-at-cycle",
        type=int,
        default=None,
        help="[TESTE] Limpa cookies da sessão no ciclo N para testar reautenticação.",
    )

    args = parser.parse_args()

    exit_code = run_collection(
        interval_s=args.interval_s,
        lines_file=args.lines_file,
        duration_min=args.duration_min,
        max_cycles=args.max_cycles,
        once=args.once,
        keep_awake=args.keep_awake,
        simulate_logout_at_cycle=args.simulate_logout_at_cycle,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
