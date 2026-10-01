import datetime
import io
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import zipfile

from src.core.config import Config
from src.utils.logger import get_logger

logger = get_logger("cemaden_bronze_utils")


def get_sptrans_date_range(sptrans_dir: Optional[str | Path] = None) -> Tuple[datetime.date, datetime.date]:
    """Descobre o menor e maior date=YYYY-MM-DD presente nas partições de bronze da SPTrans."""
    base_p = Path(sptrans_dir or Config.SPTRANS_BRONZE_DIR)
    if not base_p.exists():
        raise FileNotFoundError(
            f"Diretório SPTrans bronze não encontrado: {base_p}. Forneça --start e --end explicitamente."
        )

    dates: List[datetime.date] = []
    for d in base_p.glob("date=*"):
        if d.is_dir():
            val = d.name.replace("date=", "")
            try:
                dt = datetime.date.fromisoformat(val)
                dates.append(dt)
            except ValueError:
                continue

    if not dates:
        raise ValueError(
            f"Nenhuma partição date=YYYY-MM-DD encontrada em {base_p}. Forneça --start e --end explicitamente."
        )

    dates.sort()
    return dates[0], dates[-1]


def expand_window_with_margin(
    start_date: datetime.date, end_date: datetime.date, margin_days: int = 1
) -> Tuple[datetime.date, datetime.date]:
    """Aplica margem em dias antes e depois da janela de datas."""
    expanded_start = start_date - datetime.timedelta(days=margin_days)
    expanded_end = end_date + datetime.timedelta(days=margin_days)
    return expanded_start, expanded_end


def chunk_date_range(
    start_date: datetime.date, end_date: datetime.date, max_span_days: int = 14
) -> List[Tuple[datetime.date, datetime.date]]:
    """Divide um intervalo de datas em blocos de no máximo max_span_days."""
    if start_date > end_date:
        raise ValueError(f"start_date ({start_date}) não pode ser posterior a end_date ({end_date}).")

    chunks: List[Tuple[datetime.date, datetime.date]] = []
    curr = start_date
    delta = datetime.timedelta(days=max_span_days - 1)

    while curr <= end_date:
        chunk_end = min(curr + delta, end_date)
        chunks.append((curr, chunk_end))
        curr = chunk_end + datetime.timedelta(days=1)

    return chunks


def format_dt_param(d: datetime.date, is_end: bool = False) -> str:
    """Formata data no padrão exigido pela API do CEMADEN: YYYYMMDD0000 ou YYYYMMDD2359."""
    suffix = "2359" if is_end else "0000"
    return d.strftime("%Y%m%d") + suffix


def format_date_ddmmaaaa(d: datetime.date | str) -> str:
    """Formata data no padrão ddmmaaaa para nomes de arquivo claros."""
    if isinstance(d, str):
        # se vier como YYYYMMDD... ou YYYY-MM-DD
        d_clean = d.replace("-", "")[:8]
        dt = datetime.datetime.strptime(d_clean, "%Y%m%d").date()
    else:
        dt = d
    return dt.strftime("%d%m%Y")


def load_manifest(manifest_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """Lê todas as linhas do manifest JSONL existente de forma segura."""
    p = Path(manifest_path or Config.CEMADEN_MANIFEST_PATH)
    if not p.exists():
        return []

    events = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if line_str:
                try:
                    events.append(json.loads(line_str))
                except json.JSONDecodeError:
                    continue
    return events


def append_manifest_event(
    event: Dict[str, Any], manifest_path: Optional[str | Path] = None
) -> None:
    """Acrescenta um evento no arquivo manifest JSONL."""
    p = Path(manifest_path or Config.CEMADEN_MANIFEST_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()


def compute_uncovered_chunks(
    station_id: str,
    target_chunks: List[Tuple[datetime.date, datetime.date]],
    manifest_events: List[Dict[str, Any]],
    force: bool = False,
    reference_date: Optional[datetime.date] = None,
) -> List[Tuple[datetime.date, datetime.date]]:
    """Calcula quais blocos de janela ainda precisam ser requisitados para a estação."""
    if force:
        return list(target_chunks)

    ref_d = reference_date or datetime.date.today()
    recollect_threshold = ref_d - datetime.timedelta(days=1)

    covered_pairs: Set[Tuple[str, str]] = set()
    for ev in manifest_events:
        if ev.get("station_id") == station_id and ev.get("status") in ("ok", "no_data"):
            s = ev.get("start_str")
            e = ev.get("end_str")
            if s and e:
                covered_pairs.add((s, e))

    needed_chunks: List[Tuple[datetime.date, datetime.date]] = []
    for c_start, c_end in target_chunks:
        if c_end >= recollect_threshold:
            needed_chunks.append((c_start, c_end))
            continue

        s_str = format_dt_param(c_start, is_end=False)
        e_str = format_dt_param(c_end, is_end=True)

        if (s_str, e_str) not in covered_pairs:
            needed_chunks.append((c_start, c_end))

    return needed_chunks


def inspect_and_extract_zip(
    zip_bytes: bytes,
) -> Tuple[Optional[str], Optional[bytes], int, Optional[str]]:
    """Analisa o arquivo ZIP recebido e extrai o CSV byte a byte."""
    if not zip_bytes or len(zip_bytes) == 0:
        return None, None, 0, "empty_zip_bytes"

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            namelist = z.namelist()
            csv_files = [n for n in namelist if n.lower().endswith(".csv")]

            if not csv_files:
                return None, None, 0, "no_csv_in_zip"

            inner_name = csv_files[0]
            raw_csv = z.read(inner_name)
    except zipfile.BadZipFile:
        return None, None, 0, "corrupted_zip_file"

    if not raw_csv or len(raw_csv.strip()) == 0:
        return inner_name, None, 0, "empty_csv"

    lines = [l for l in raw_csv.split(b"\n") if l.strip()]
    if len(lines) <= 1:
        return inner_name, None, 0, "header_only"

    n_rows = len(lines) - 1
    return inner_name, raw_csv, n_rows, None


def save_atomic_bronze_csv(
    station_id: str,
    start_dt: str,
    end_dt: str,
    csv_bytes: bytes,
    bronze_dir: Optional[str | Path] = None,
) -> Path:
    """Grava o CSV bruto na pasta da estação como req_datainicio_datafinal.csv (em ddmmaaaa)."""
    base_dir = Path(bronze_dir or Config.CEMADEN_BRONZE_DIR)
    station_dir = base_dir / f"station={station_id}"
    station_dir.mkdir(parents=True, exist_ok=True)

    start_ddmm = format_date_ddmmaaaa(start_dt)
    end_ddmm = format_date_ddmmaaaa(end_dt)

    csv_filename = f"req_{start_ddmm}_{end_ddmm}.csv"
    csv_dest = station_dir / csv_filename

    tmp_csv = station_dir / f"{csv_filename}.tmp"
    try:
        with open(tmp_csv, "wb") as f:
            f.write(csv_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_csv, csv_dest)
    except Exception:
        if tmp_csv.exists():
            tmp_csv.unlink(missing_ok=True)
        raise

    return csv_dest
