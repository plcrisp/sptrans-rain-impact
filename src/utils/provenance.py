from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlsplit

from src.core.config import Config


def sanitize_url(url: Optional[str]) -> str:
    """Strip query parameters, fragments, and embedded credentials from a URL."""
    if not url:
        return ""

    parts = urlsplit(url.strip())
    if parts.hostname:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        host_port = f"{host}:{parts.port}" if parts.port is not None else host
    else:
        host_port = ""

    if parts.scheme:
        return f"{parts.scheme}://{host_port}{parts.path}"
    if host_port:
        return f"//{host_port}{parts.path}"
    return parts.path


def to_relative_path(path: Union[str, Path]) -> str:
    """Return a POSIX path relative to Config.BASE_DIR when inside the repository."""
    base_dir = Path(Config.BASE_DIR).resolve()
    try:
        return Path(path).resolve().relative_to(base_dir).as_posix()
    except ValueError:
        return str(Path(path)).replace("\\", "/")


def record_provenance(
    target_dir: Union[str, Path],
    stage: str,
    source: Union[str, List[str]],
    output: Union[str, List[str]],
    records: int,
    extra: Optional[Dict[str, Any]] = None,
    global_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """Append an English provenance entry to local and global provenance.jsonl files."""
    dir_path = Path(target_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    info: Dict[str, Any] = {
        "stage": stage,
        "source": source,
        "output": output,
        "records": int(records),
        "executed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if extra:
        info.update(extra)

    local_path = dir_path / "provenance.jsonl"
    with local_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(info, ensure_ascii=False) + "\n")

    root_dir = Path(global_dir) if global_dir is not None else Path(Config.DATA_DIR)
    if root_dir.resolve() != dir_path.resolve():
        root_dir.mkdir(parents=True, exist_ok=True)
        global_info = dict(info)
        global_info["directory"] = to_relative_path(dir_path)
        global_path = root_dir / "provenance.jsonl"
        with global_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(global_info, ensure_ascii=False) + "\n")

    return local_path
