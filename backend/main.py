"""FastAPI 入口。

基础 URL：http://localhost:5001/api/v1
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api import branches, characters, director, graph, output, projects, scenes
from backend.api.schemas import ApiResponse
from backend.config import settings
from backend.exceptions import PlotSystemError
from backend.utils.db import init_db
from backend.utils.logger import get_logger

logger = get_logger("main")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    await init_db()
    logger.info("PlotSystem 后端启动，数据目录：%s", settings.data_path)
    yield
    logger.info("PlotSystem 后端关闭")


app = FastAPI(
    title="PlotSystem API",
    version="0.1.0",
    description="影视多智能体、多分枝剧情推演系统",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(PlotSystemError)
async def handle_business_error(_: Request, exc: PlotSystemError) -> JSONResponse:
    return JSONResponse(status_code=404, content=ApiResponse.fail(str(exc)).model_dump())


@app.exception_handler(Exception)
async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常")
    return JSONResponse(status_code=500, content=ApiResponse.fail(str(exc)).model_dump())


@app.get("/api/v1/health")
async def health() -> ApiResponse:
    return ApiResponse.ok({"status": "ok"})


# 注册路由
for r in (
    projects.router,
    characters.router,
    scenes.project_router,
    scenes.scene_router,
    director.router,
    branches.project_router,
    branches.snapshot_router,
    output.router,
    graph.router,
):
    app.include_router(r, prefix=API_PREFIX)


def run() -> None:
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.BACKEND_HOST,
        port=settings.BACKEND_PORT,
        reload=settings.DEBUG,
    )


if __name__ == "__main__":
    run()
