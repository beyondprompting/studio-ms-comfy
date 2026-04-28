from __future__ import annotations

import json
import threading
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse

from .job_manager import JobManager
from .convex_pull_worker import ConvexPullWorker
from .config import settings
from .models import ConvexUploadRequest, JobResponse, ThumbnailRequest

app = FastAPI(title="Comfy Convex Microservice", version="0.1.0")
manager = JobManager()
pull_worker = ConvexPullWorker() if settings.worker_enabled else None
pull_worker_thread: threading.Thread | None = None


@app.on_event("startup")
async def startup_event() -> None:
    await manager.start()
    global pull_worker_thread
    if pull_worker is not None:
        pull_worker_thread = threading.Thread(target=pull_worker.run_forever, daemon=True)
        pull_worker_thread.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if pull_worker is not None:
        pull_worker.stop()
    await manager.stop()


@app.get("/health")
async def health() -> dict[str, Any]:
    return await manager.health()


@app.post("/v1/jobs/thumbnail", response_model=JobResponse)
async def create_thumbnail_job(payload: ThumbnailRequest) -> JobResponse:
    job_id = await manager.create_job(payload)
    return JobResponse(job_id=job_id, status="queued")


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    state = await manager.get_job_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return state.model_dump()


@app.get("/v1/jobs/{job_id}/result")
async def get_job_result_file(job_id: str) -> FileResponse:
    state = await manager.get_job_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if state.status != "completed" or not state.result:
        raise HTTPException(status_code=409, detail="Job is not completed")

    file_path = state.result["local_file_path"]
    return FileResponse(file_path, media_type="image/png", filename=f"thumbnail_{job_id}.png")


@app.post("/v1/jobs/{job_id}/upload-to-convex")
async def upload_result_to_convex(job_id: str, payload: ConvexUploadRequest) -> dict[str, Any]:
    try:
        result = await manager.upload_result_to_convex(
            job_id,
            generate_upload_url_mutation="files:generateUploadUrl",
            save_metadata_mutation="images:createFromPython",
            save_payload=payload.metadata,
        )
        return {"ok": True, "job_id": job_id, "convex": result}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/v1/jobs/{job_id}/events")
async def stream_job_events(job_id: str) -> StreamingResponse:
    state = await manager.get_job_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Job not found")

    queue = manager.subscribe_events(job_id)

    async def event_generator() -> Any:
        try:
            yield f"event: snapshot\ndata: {json.dumps({'events': state.events})}\n\n"
            while True:
                item = await queue.get()
                yield f"event: update\ndata: {json.dumps(item)}\n\n"
        finally:
            manager.unsubscribe_events(job_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.websocket("/ws/jobs/{job_id}")
async def job_ws(websocket: WebSocket, job_id: str) -> None:
    state = await manager.get_job_state(job_id)
    if state is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    queue = manager.subscribe_events(job_id)

    try:
        await websocket.send_json({"type": "snapshot", "events": state.events})
        while True:
            item = await queue.get()
            await websocket.send_json(item)
    except WebSocketDisconnect:
        pass
    finally:
        manager.unsubscribe_events(job_id, queue)
