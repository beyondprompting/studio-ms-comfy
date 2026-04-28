from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from convex import ConvexClient


@dataclass
class ConvexConfig:
    convex_url: str | None
    auth_token: str | None
    admin_key: str | None


@dataclass
class ClaimedJob:
    job_id: str
    source_image_url: str
    width: int | None
    height: int | None
    crop: str
    crop_x: int | None = None
    crop_y: int | None = None
    crop_width: int | None = None
    crop_height: int | None = None
    workflow_key: str | None = None
    request_id: str | None = None
    crop_region: dict[str, int | float] | None = None
    params: dict[str, Any] | None = None


class ConvexBridge:
    def __init__(self, cfg: ConvexConfig) -> None:
        self._cfg = cfg
        self._client: ConvexClient | None = None

        if not cfg.convex_url:
            return

        client = ConvexClient(cfg.convex_url)
        if cfg.admin_key:
            client.set_admin_auth(cfg.admin_key)
        elif cfg.auth_token:
            client.set_auth(cfg.auth_token)

        self._client = client

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def mutation(self, path: str, args: dict[str, Any]) -> Any:
        if not self._client:
            raise RuntimeError("Convex is not configured. Set CONVEX_URL first.")
        return self._client.mutation(path, args)

    def query(self, path: str, args: dict[str, Any]) -> Any:
        if not self._client:
            raise RuntimeError("Convex is not configured. Set CONVEX_URL first.")
        return self._client.query(path, args)

    def claim_next_pending_job(self, claim_mutation: str, worker_id: str) -> ClaimedJob | None:
        body = self.mutation(claim_mutation, {"workerId": worker_id})
        if not body:
            return None

        request = body.get("request") if isinstance(body.get("request"), dict) else {}
        params = body.get("params") if isinstance(body.get("params"), dict) else None
        if params is None:
            params = request if request else None
        crop_region = body.get("cropRegion") if isinstance(body.get("cropRegion"), dict) else None
        if crop_region is None:
            crop_region = request.get("cropRegion") if isinstance(request.get("cropRegion"), dict) else {}

        job_id = body.get("jobId") or body.get("id") or body.get("runId")
        source_image_url = (
            body.get("sourceImageUrl")
            or body.get("imageUrl")
            or body.get("sourceUrl")
            or body.get("inputUrl")
        )
        width = body.get("width", request.get("width"))
        height = body.get("height", request.get("height"))
        crop = body.get("crop", request.get("crop", "center"))
        crop_x = body.get("cropX", request.get("cropX", crop_region.get("x")))
        crop_y = body.get("cropY", request.get("cropY", crop_region.get("y")))
        crop_width = body.get("cropWidth", request.get("cropWidth", crop_region.get("width")))
        crop_height = body.get("cropHeight", request.get("cropHeight", crop_region.get("height")))
        workflow_key = (
            body.get("workflowKey")
            or body.get("workflow")
            or body.get("workflowName")
            or body.get("jobType")
            or body.get("stage")
            or request.get("workflowKey")
            or request.get("workflow")
            or request.get("workflowName")
            or request.get("jobType")
            or request.get("stage")
        )
        request_id = body.get("requestId") or request.get("requestId")

        if not job_id or not source_image_url:
            raise RuntimeError(
                "Invalid claim response from Convex. Expected keys: "
                "jobId, sourceImageUrl"
            )

        parsed_crop_region: dict[str, int | float] | None = None
        if isinstance(crop_region, dict) and crop_region:
            parsed_crop_region = {
                k: v
                for k, v in crop_region.items()
                if isinstance(v, (int, float))
            }

        return ClaimedJob(
            job_id=str(job_id),
            source_image_url=str(source_image_url),
            width=int(width) if width is not None else None,
            height=int(height) if height is not None else None,
            crop=str(crop),
            crop_x=int(crop_x) if crop_x is not None else None,
            crop_y=int(crop_y) if crop_y is not None else None,
            crop_width=int(crop_width) if crop_width is not None else None,
            crop_height=int(crop_height) if crop_height is not None else None,
            workflow_key=str(workflow_key) if workflow_key is not None else None,
            request_id=str(request_id) if request_id is not None else None,
            crop_region=parsed_crop_region,
            params=params,
        )

    def append_job_event(self, append_event_mutation: str, job_id: str, event: dict[str, Any]) -> None:
        self.mutation(append_event_mutation, {"jobId": job_id, "event": event})

    def mark_job_completed(
        self,
        mark_completed_mutation: str,
        job_id: str,
        result_storage_id: str,
        result: dict[str, Any],
    ) -> Any:
        return self.mutation(
            mark_completed_mutation,
            {
                "jobId": job_id,
                "resultStorageId": result_storage_id,
                "result": result,
            },
        )

    def mark_job_failed(self, mark_failed_mutation: str, job_id: str, error: dict[str, Any]) -> Any:
        return self.mutation(mark_failed_mutation, {"jobId": job_id, "error": error})

    def upload_file_to_convex(
        self,
        file_path: str,
        content_type: str,
        generate_upload_url_mutation: str,
    ) -> dict[str, Any]:
        upload_url = self.mutation(generate_upload_url_mutation, {})
        data = Path(file_path).read_bytes()
        response = requests.post(upload_url, data=data, headers={"Content-Type": content_type}, timeout=120)
        response.raise_for_status()
        body = response.json()
        storage_id = body.get("storageId")
        if not storage_id:
            raise RuntimeError("Convex upload response did not contain storageId")
        return {"storageId": storage_id, "uploadResponse": body}

    def upload_file_and_record(
        self,
        file_path: str,
        content_type: str,
        generate_upload_url_mutation: str,
        save_metadata_mutation: str | None,
        save_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not self._client:
            raise RuntimeError("Convex is not configured. Set CONVEX_URL first.")

        upload = self.upload_file_to_convex(file_path, content_type, generate_upload_url_mutation)
        storage_id = upload["storageId"]
        body = upload["uploadResponse"]

        mutation_result = None
        if save_metadata_mutation:
            payload = dict(save_payload or {})
            payload["storageId"] = storage_id
            mutation_result = self._client.mutation(save_metadata_mutation, payload)

        return {
            "storageId": storage_id,
            "uploadResponse": body,
            "metadataMutationResult": mutation_result,
        }
