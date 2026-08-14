"""Loopback-first HTTP API for task submission, status, answers, and execution."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from .api_models import HumanAnswers, RunRequest, TaskSubmit
from .runtime import CortexRuntime, RuntimeErrorState
from .settings import Settings


def create_app(
    runtime: CortexRuntime | None = None, *, settings: Settings | None = None
) -> FastAPI:
    configured = settings or (runtime.settings if runtime else Settings.from_env())
    controller = runtime or CortexRuntime(configured)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = controller
        app.state.background_tasks = set()
        yield
        pending = list(app.state.background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await controller.close()

    app = FastAPI(
        title="Cortex V5",
        version="0.1.0",
        description="Independent local-first mechanical task runtime",
        lifespan=lifespan,
    )

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        expected = configured.http_bearer
        if not expected:
            return
        supplied = ""
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:]
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    def schedule(request: Request, task_id: str) -> None:
        async def execute() -> None:
            try:
                await controller.run(task_id)
            except Exception as exc:  # preserve a visible, resumable failure state
                await controller.record_unexpected_failure(task_id, exc)

        background = asyncio.create_task(execute(), name=f"cortex-v5:{task_id}")
        request.app.state.background_tasks.add(background)
        background.add_done_callback(request.app.state.background_tasks.discard)

    @app.get("/healthz")
    async def health(_: None = Depends(authorize)) -> dict[str, Any]:
        return {"status": "ok", "runtime": "cortex-v5", **configured.public()}

    @app.post("/v1/tasks", status_code=status.HTTP_202_ACCEPTED)
    async def submit_task(
        body: TaskSubmit, request: Request, _: None = Depends(authorize)
    ) -> dict[str, Any]:
        try:
            task = await controller.submit(body.model_dump(mode="json"))
        except (ValueError, RuntimeErrorState, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if body.autostart and task["status"] == "ready":
            schedule(request, task["task_id"])
        return task

    @app.get("/v1/tasks/{task_id}")
    async def task_status(task_id: str, _: None = Depends(authorize)) -> dict[str, Any]:
        try:
            return controller.task_snapshot(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found or expired") from exc

    @app.post("/v1/tasks/{task_id}/answers", status_code=status.HTTP_202_ACCEPTED)
    async def human_answers(
        task_id: str,
        body: HumanAnswers,
        request: Request,
        _: None = Depends(authorize),
    ) -> dict[str, Any]:
        try:
            task = await controller.answer(task_id, body.answers)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found or expired") from exc
        except RuntimeErrorState as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if body.autostart and task["status"] == "ready":
            schedule(request, task_id)
        return task

    @app.post("/v1/tasks/{task_id}/run", status_code=status.HTTP_202_ACCEPTED)
    async def run_task(
        task_id: str,
        body: RunRequest,
        request: Request,
        _: None = Depends(authorize),
    ) -> dict[str, Any]:
        del body
        try:
            task = controller.task_snapshot(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found or expired") from exc
        if task["status"] == "waiting_for_human":
            raise HTTPException(status_code=409, detail="human answers are required")
        schedule(request, task_id)
        return task

    return app
