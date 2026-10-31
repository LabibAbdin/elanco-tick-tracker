"""Background sync loop. Failures are logged and never crash the web process."""

from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.ingestion.pipeline import IngestionService

log = logging.getLogger(__name__)


async def _safe_sync(service: IngestionService, trigger: str) -> None:
    try:
        await service.sync(trigger=trigger, strategy="auto")
    except RuntimeError as exc:  # already running
        log.info("sync skipped", extra={"reason": str(exc)})
    except Exception:  # noqa: BLE001
        log.exception("scheduled sync crashed")


async def run_scheduler(service: IngestionService, settings: Settings) -> None:
    if settings.sync_on_startup:
        await _safe_sync(service, "startup")
    if settings.sync_interval_minutes <= 0:
        return
    while True:
        await asyncio.sleep(settings.sync_interval_minutes * 60)
        await _safe_sync(service, "schedule")
