import logging
import os
from pathlib import Path
import time
from typing import Dict

_BASE_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_LOG_PATH = _BASE_DIR / "pipeline.log"
_FILE_HANDLERS: Dict[str, logging.FileHandler] = {}


def _get_shared_file_handler(
    log_path: Path, level: int, formatter: logging.Formatter
) -> logging.FileHandler:
    """Return a shared UTF-8 FileHandler for the given log file path."""
    key = str(log_path.resolve())
    handler = _FILE_HANDLERS.get(key)
    if handler is None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(formatter)
        _FILE_HANDLERS[key] = handler
    return handler


def get_logger(name: str) -> logging.Logger:
    """Configure and return a logger writing to both console and pipeline.log."""
    logger = logging.getLogger(name)

    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%SZ",
    )
    formatter.converter = time.gmtime

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_path = Path(os.getenv("PIPELINE_LOG_PATH", str(_DEFAULT_LOG_PATH)))
    file_handler = _get_shared_file_handler(log_path, level, formatter)
    logger.addHandler(file_handler)

    return logger
