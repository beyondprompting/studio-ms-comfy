from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

from .comfy_client import ComfyClient
from .config import settings
from .convex_client import ConvexBridge, ConvexConfig
from .events import EventBus
from .models import JobState, JobStatus, ThumbnailRequest
from .workflow import build_workflow, load_workflow_template


@dataclass
class Job:
    id: str
    request: ThumbnailRequest
    status: JobStatus
    created_at: float
    updated_at: float
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class JobManager:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._event_bus = EventBus()
        self._worker_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._template = load_workflow_template(settings.workflow_template_path)
        self._comfy = ComfyClient(settings.comfy_base_url, settings.output_dir)
        self._convex = ConvexBridge(
            ConvexConfig(
                convex_url=settings.convex_url,
                auth_token=settings.convex_auth_token,
                admin_key=settings.convex_admin_key,
            )
        )

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None

    async def create_job(self, req: ThumbnailRequest) -> str:
        job_id = str(uuid.uuid4())
        now = time.time()
        job = Job(
            id=job_id,
            request=req,
            status=JobStatus.queued,
            created_at=now,
            updated_at=now,
        )

        async with self._lock:
            self._jobs[job_id] = job

        await self._publish(job_id, {"type": "job_created", "job_id": job_id, "status": JobStatus.queued.value})
        await self._queue.put(job_id)
        return job_id

    async def get_job_state(self, job_id: str) -> JobState | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return JobState(
                job_id=job.id,
                status=job.status,
                created_at=job.created_at,
                updated_at=job.updated_at,
                request=job.request.model_dump(),
                result=job.result,
                error=job.error,
                events=list(job.events),
            )

    async def health(self) -> dict[str, Any]:
        comfy = self._comfy.health()
        return {
            "ok": True,
            "comfy": {"connected": True, "system_stats": comfy},
            "convex": {"configured": self._convex.enabled},
        }

    def subscribe_events(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        return self._event_bus.subscribe(job_id)

    def unsubscribe_events(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._event_bus.unsubscribe(job_id, queue)

    async def upload_result_to_convex(
        self,
        job_id: str,
        generate_upload_url_mutation: str,
        save_metadata_mutation: str | None,
        save_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        state = await self.get_job_state(job_id)
        if state is None:
            raise KeyError(f"Job not found: {job_id}")
        if state.status != JobStatus.completed:
            raise RuntimeError("Job must be completed before uploading result to Convex")

        local_file_path = state.result["local_file_path"]
        result = await asyncio.to_thread(
            self._convex.upload_file_and_record,
            local_file_path,
            "image/png",
            generate_upload_url_mutation,
            save_metadata_mutation,
            save_payload,
        )

        await self._publish(job_id, {"type": "convex_uploaded", "data": result})

        return result

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run_job(job_id)
            finally:
                self._queue.task_done()

    async def _run_job(self, job_id: str) -> None:
        job = await self._set_status(job_id, JobStatus.running)
        if job is None:
            return

        try:
            await self._publish(job_id, {"type": "job_running", "job_id": job_id})

            response = requests.get(str(job.request.image_url), timeout=120)
            response.raise_for_status()
            input_bytes = response.content
            await self._publish(
                job_id,
                {
                    "type": "input_downloaded",
                    "content_length": len(input_bytes),
                    "source": str(job.request.image_url),
                },
            )

            uploaded = await asyncio.to_thread(
                self._comfy.upload_image_bytes,
                input_bytes,
                f"{job_id}.png",
            )
            await self._publish(job_id, {"type": "comfy_input_uploaded", "data": uploaded})

            input_filename = uploaded["name"]
            workflow = build_workflow(
                template=self._template,
                input_filename=input_filename,
                width=job.request.width,
                height=job.request.height,
                filename_prefix=f"thumb_{job_id}",
                crop_mode=job.request.crop,
            )

            def relay_event(event: dict[str, Any]) -> None:
                if self._loop is None:
                    return
                asyncio.run_coroutine_threadsafe(self._publish(job_id, event), self._loop)

            comfy_result = await asyncio.to_thread(
                self._comfy.run_prompt_and_get_first_image,
                workflow,
                relay_event,
            )

            await self._set_result(
                job_id,
                {
                    "prompt_id": comfy_result.prompt_id,
                    "local_file_path": comfy_result.output_file_path,
                    "filename": comfy_result.output_filename,
                    "subfolder": comfy_result.output_subfolder,
                    "type": comfy_result.output_type,
                },
            )
            await self._set_status(job_id, JobStatus.completed)
            await self._publish(job_id, {"type": "job_completed", "job_id": job_id})

        except Exception as exc:  # noqa: BLE001
            await self._set_error(job_id, {"message": str(exc), "type": exc.__class__.__name__})
            await self._set_status(job_id, JobStatus.failed)
            await self._publish(job_id, {"type": "job_failed", "job_id": job_id, "error": str(exc)})

    async def _set_status(self, job_id: str, status: JobStatus) -> Job | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.status = status
            job.updated_at = time.time()
            return job

    async def _set_result(self, job_id: str, result: dict[str, Any]) -> None:
        async with self._lock:
            job = self._jobs[job_id]
            job.result = result
            job.updated_at = time.time()

    async def _set_error(self, job_id: str, error: dict[str, Any]) -> None:
        async with self._lock:
            job = self._jobs[job_id]
            job.error = error
            job.updated_at = time.time()

    async def _publish(self, job_id: str, event: dict[str, Any]) -> None:
        enriched = dict(event)
        enriched["timestamp"] = time.time()

        async with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.events.append(enriched)
                if len(job.events) > 500:
                    job.events = job.events[-500:]

        await self._event_bus.publish(job_id, enriched)
