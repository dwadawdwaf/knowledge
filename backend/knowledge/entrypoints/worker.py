"""Worker 进程入口。"""

from __future__ import annotations

from knowledge.config import get_settings
from knowledge.logging import get_logger, setup_logging
from knowledge.worker.loop import run_forever

log = get_logger("worker")


def main() -> None:
    setup_logging()
    settings = get_settings()
    log.info(
        "worker_started",
        extra={"poll_seconds": settings.worker_poll_seconds, "lease_seconds": settings.lease_seconds},
    )
    try:
        run_forever(settings)
    except KeyboardInterrupt:
        log.info("worker_stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
