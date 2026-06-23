"Logger setup via loguru"

from __future__ import annotations

import sys
from loguru import logger

from src.utils.config import load_config


def setup_logging() -> None:
    config = load_config()
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.logging.level,
        format=config.logging.format,
        colorize=True,
    )


__all__ = ["logger", "setup_logging"]
