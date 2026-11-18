import logging
import os
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("./logs")


def setup_logger(name: str = "pipeline") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = LOG_DIR / f"{timestamp}_{name}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(fh)

    logger.info(f"=== Session started: {timestamp} ===")
    logger.info(f"Log file: {log_file}")
    logger.info(f"Python: {sys.executable}")
    logger.info(f"PID: {os.getpid()}")
    logger.info(f"Args: {sys.argv}")

    return logger


def get_latest_log() -> Path | None:
    logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None
