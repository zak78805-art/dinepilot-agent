from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import routes
from app.api.routes import router as agent_router
from app.clients.http_pool import close_shared_backend_client, open_shared_backend_client
from app.config import get_settings
from app.observability.context import TraceIdMiddleware


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    settings = get_settings()
    open_shared_backend_client(
        base_url=settings.sky_backend_base_url,
        timeout_seconds=getattr(settings, 'backend_http_timeout_seconds', 10.0),
        max_connections=getattr(settings, 'backend_http_max_connections', 50),
        max_keepalive_connections=getattr(settings, 'backend_http_max_keepalive', 20),
    )
    try:
        yield
    finally:
        for agent_svc in (getattr(routes, 'merchant_agent', None), getattr(routes, 'customer_agent', None)):
            shutdown_fn = getattr(agent_svc, 'shutdown', None)
            if callable(shutdown_fn):
                await shutdown_fn()
            else:
                manager = getattr(agent_svc, 'review_manager', None)
                if manager is not None:
                    await manager.shutdown()
        await close_shared_backend_client()


app = FastAPI(
    title="Sky Agent",
    description="苍穹外卖商家经营 AI Agent 服务",
    version="0.1.0",
    lifespan=_lifespan,
)
app.add_middleware(TraceIdMiddleware)
app.include_router(agent_router)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "sky-agent"}
