import logging
import os
import time


def get_logger(name: str) -> logging.Logger:
    """Configura e retorna uma instância de logging.Logger direcionada ao console."""
    logger = logging.getLogger(name)

    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

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

    return logger
