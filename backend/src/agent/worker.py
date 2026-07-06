"""Standalone Redis Streams worker process."""

import asyncio

from loguru import logger

from agent.logger import setup_logger
from agent.runtime_config import validate_runtime_config
from agent.task_queue import start_worker


def main() -> None:
    """Run the worker until the process receives an interrupt."""
    setup_logger()
    validate_runtime_config("worker")
    try:
        asyncio.run(start_worker())
    except KeyboardInterrupt:
        logger.info("[TaskQueue] worker 已停止")


if __name__ == "__main__":
    main()
