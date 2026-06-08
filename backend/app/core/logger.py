"""Centralized logging configuration."""

import logging
import sys
from pathlib import Path

_LOG_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)-8s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def setup_logger(name: str = "traffic_monitor") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.INFO)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s – %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(stdout_handler)

    log_dir = Path(__file__).resolve().parents[2] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "app.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(_LOG_FMT)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()
