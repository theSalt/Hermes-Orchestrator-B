"""hermes-dispatch 入口：FastAPI app + 空闲回收清扫循环。"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import manager
from .config import settings
from .registry import registry
from .routes import router

logger = logging.getLogger("hermes-dispatch")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


async def _sweep_loop() -> None:
    while True:
        try:
            await manager.flush_active()
            stopped = await manager.sweep_once()
            if stopped:
                logger.info("sweep stopped idle agents: %s", stopped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("idle sweep failed")
        await asyncio.sleep(settings.sweep_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await registry.start()
    task = asyncio.create_task(_sweep_loop())
    logger.info(
        "hermes-dispatch started: image=%s network=%s data=%s idle=%smin "
        "dashboard=%s fwd=%s",
        settings.image,
        settings.network,
        settings.data_dir,
        settings.idle_timeout_minutes,
        settings.dashboard_port,
        settings.forwarder_port,
    )
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await registry.close()


app = FastAPI(title="hermes-dispatch", version="0.1.0", lifespan=lifespan)
app.include_router(router)
