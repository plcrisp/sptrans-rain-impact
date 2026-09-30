import logging
from logging.handlers import RotatingFileHandler
import os
import time

from src.core.config import Config


def get_logger(name: str) -> logging.Logger:
    """Configura e retorna uma instância idempotente de logging.Logger.

    Garante envio de logs para console (StreamHandler) e arquivo rotativo
    (RotatingFileHandler) em formato padronizado com timestamp UTC e sufixo Z.
    """
    logger = logging.getLogger(name)

    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    # Assegura que o diretório de logs exista
    os.makedirs(Config.LOGS_DIR, exist_ok=True)

    # Formatter em UTC com sufixo Z
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%SZ",
    )
    formatter.converter = time.gmtime

    # Handler do console
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Handler do arquivo rotativo
    log_file_path = os.path.join(Config.LOGS_DIR, "pipeline.log")
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
