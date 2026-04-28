from __future__ import annotations

import json
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image

from .comfy_client import ComfyClient
from .config import settings
from .convex_client import ClaimedJob, ConvexBridge, ConvexConfig
from .workflow import build_workflow, load_workflow_template


class ConvexPullWorker:
    _THUMB_MAX_SIZE = 600

    def __init__(self) -> None:
        self._convex = ConvexBridge(
            ConvexConfig(
                convex_url=settings.convex_url,
                auth_token=settings.convex_auth_token,
                admin_key=settings.convex_admin_key,
            )
        )
        self._comfy = ComfyClient(settings.comfy_base_url, settings.output_dir)
        template_paths = self._build_workflow_paths()
        self._templates = {
            key: load_workflow_template(path)
            for key, path in template_paths.items()
        }
        self._default_workflow_key = settings.workflow_default_key
        if self._default_workflow_key not in self._templates:
            self._default_workflow_key = next(iter(self._templates))
        self._running = False

    def _build_workflow_paths(self) -> dict[str, str]:
        if settings.workflow_templates_json.strip():
            configured = json.loads(settings.workflow_templates_json)
            if not isinstance(configured, dict) or not configured:
                raise RuntimeError("WORKFLOW_TEMPLATES_JSON must be a non-empty JSON object")
            return {str(k): str(v) for k, v in configured.items()}

        workflows_dir = Path(settings.workflow_templates_dir)
        estuches_defaults = {
            "estuches_stage1_resize_image_mask_node": "estuches_stage1_resize_image_mask_node.json",
            "estuches_stage2_crop_fullres": "estuches_stage2_crop_fullres.json",
            "estuches_stage3_mask_composite": "estuches_stage3_mask_composite.json",
            "estuches_stage4_reimplant_feather": "estuches_stage4_reimplant_feather.json",
            "estuches_stage5_remove_bg_template": "estuches_stage5_remove_bg_template.json",
        }
        mapped = {
            key: str(workflows_dir / filename)
            for key, filename in estuches_defaults.items()
            if (workflows_dir / filename).exists()
        }
        if mapped:
            return mapped

        return {"default": settings.workflow_template_path}

    def _select_template(self, job: ClaimedJob) -> tuple[str, dict[str, Any]]:
        workflow_key = job.workflow_key or self._default_workflow_key
        template = self._templates.get(workflow_key)
        if not template:
            available = ", ".join(sorted(self._templates.keys()))
            raise RuntimeError(
                f"Unknown workflow key '{workflow_key}'. Available keys: {available}"
            )
        return workflow_key, template

    @staticmethod
    def _validate_positive_int(name: str, value: int) -> None:
        if value <= 0:
            raise RuntimeError(f"Invalid {name}: expected a positive integer, got {value}")

    def _resolve_crop_region(self, job: ClaimedJob, width: int, height: int) -> tuple[int, int, int, int]:
        crop_x = 0 if job.crop_x is None else job.crop_x
        crop_y = 0 if job.crop_y is None else job.crop_y
        crop_width = width if job.crop_width is None else job.crop_width
        crop_height = height if job.crop_height is None else job.crop_height

        if crop_x < 0:
            crop_x = 0
        if crop_y < 0:
            crop_y = 0

        self._validate_positive_int("cropWidth", crop_width)
        self._validate_positive_int("cropHeight", crop_height)

        max_x = max(width - 1, 0)
        max_y = max(height - 1, 0)
        if crop_x > max_x:
            crop_x = max_x
        if crop_y > max_y:
            crop_y = max_y

        crop_width = min(crop_width, width - crop_x)
        crop_height = min(crop_height, height - crop_y)

        self._validate_positive_int("cropWidth", crop_width)
        self._validate_positive_int("cropHeight", crop_height)

        return crop_x, crop_y, crop_width, crop_height

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(round(value))
        return None

    def _resolve_stage2_crop_from_thumbnail_space(
        self,
        job: ClaimedJob,
        source_width: int,
        source_height: int,
    ) -> tuple[int, int, int, int] | None:
        region = job.crop_region if isinstance(job.crop_region, dict) else None
        if not region:
            return None

        tx = self._safe_int(region.get("thumbnailX"))
        ty = self._safe_int(region.get("thumbnailY"))
        tw = self._safe_int(region.get("thumbnailWidth"))
        th = self._safe_int(region.get("thumbnailHeight"))
        thumb_w = self._safe_int(region.get("thumbnailCanvasWidth"))
        thumb_h = self._safe_int(region.get("thumbnailCanvasHeight"))

        if tx is None or ty is None or tw is None or th is None:
            return None

        # If canvas size is missing (legacy jobs), fallback to width/height from
        # job payload. Final clamp always uses real source dimensions.
        if not thumb_w or thumb_w <= 0:
            thumb_w = job.width if job.width and job.width > 0 else None
        if not thumb_h or thumb_h <= 0:
            thumb_h = job.height if job.height and job.height > 0 else None

        if not thumb_w or not thumb_h:
            return None

        ratio_x = source_width / thumb_w
        ratio_y = source_height / thumb_h

        crop_x = int(round(tx * ratio_x))
        crop_y = int(round(ty * ratio_y))
        crop_w = int(round(tw * ratio_x))
        crop_h = int(round(th * ratio_y))

        temp_job = ClaimedJob(
            job_id=job.job_id,
            source_image_url=job.source_image_url,
            width=source_width,
            height=source_height,
            crop=job.crop,
            crop_x=crop_x,
            crop_y=crop_y,
            crop_width=crop_w,
            crop_height=crop_h,
            workflow_key=job.workflow_key,
            request_id=job.request_id,
            crop_region=job.crop_region,
        )
        return self._resolve_crop_region(temp_job, source_width, source_height)

    def run_forever(self) -> None:
        if not self._convex.enabled:
            raise RuntimeError("Convex is not configured. Set CONVEX_URL and auth/admin key.")

        self._running = True
        print(
            "[worker] started",
            {
                "workerId": settings.worker_id,
                "pollIntervalSeconds": settings.worker_poll_interval_seconds,
                "claimMutation": settings.convex_claim_job_mutation,
            },
        )

        while self._running:
            try:
                claimed = self._convex.claim_next_pending_job(
                    settings.convex_claim_job_mutation,
                    worker_id=settings.worker_id,
                )
                if not claimed:
                    time.sleep(settings.worker_poll_interval_seconds)
                    continue

                self._process_claimed_job(claimed)

            except KeyboardInterrupt:
                self._running = False
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] loop error: {exc}")
                time.sleep(max(settings.worker_poll_interval_seconds, 1.0))

    def stop(self) -> None:
        self._running = False

    def _emit_event(self, job_id: str, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["timestamp"] = time.time()
        try:
            self._convex.append_job_event(settings.convex_append_event_mutation, job_id, payload)
        except Exception as exc:  # noqa: BLE001
            # Event failures should not kill the run.
            print(f"[worker] append_event failed for {job_id}: {exc}")

    def _process_claimed_job(self, job: ClaimedJob) -> None:
        self._emit_event(
            job.job_id,
            {
                "type": "job_claimed",
                "workerId": settings.worker_id,
                "requestId": job.request_id,
            },
        )

        try:
            response = requests.get(job.source_image_url, timeout=120)
            response.raise_for_status()
            input_bytes = response.content

            source_width: int | None = None
            source_height: int | None = None
            try:
                with Image.open(BytesIO(input_bytes)) as src_img:
                    source_width = int(src_img.width)
                    source_height = int(src_img.height)
            except Exception as dim_exc:  # noqa: BLE001
                print(f"[worker] could not read source dimensions for {job.job_id}: {dim_exc}")

            self._emit_event(
                job.job_id,
                {
                    "type": "input_downloaded",
                    "contentLength": len(input_bytes),
                    "source": job.source_image_url,
                    "sourceWidth": source_width,
                    "sourceHeight": source_height,
                },
            )

            uploaded = self._comfy.upload_image_bytes(input_bytes, f"{job.job_id}.png")
            self._emit_event(job.job_id, {"type": "comfy_input_uploaded", "data": uploaded})

            workflow_key, template = self._select_template(job)
            width = job.width if job.width is not None else settings.default_width
            height = job.height if job.height is not None else settings.default_height

            if (
                workflow_key == "estuches_stage2_crop_fullres"
                and source_width is not None
                and source_height is not None
            ):
                # Stage 2 must use real source dimensions to avoid metadata drift.
                width = source_width
                height = source_height

            self._validate_positive_int("width", width)
            self._validate_positive_int("height", height)

            crop_region_from_thumb = None
            if workflow_key == "estuches_stage2_crop_fullres" and source_width and source_height:
                crop_region_from_thumb = self._resolve_stage2_crop_from_thumbnail_space(
                    job,
                    source_width,
                    source_height,
                )

            if crop_region_from_thumb is not None:
                crop_x, crop_y, crop_width, crop_height = crop_region_from_thumb
            else:
                crop_x, crop_y, crop_width, crop_height = self._resolve_crop_region(job, width, height)

            if workflow_key == "estuches_stage2_crop_fullres":
                self._validate_positive_int("width", width)
                self._validate_positive_int("height", height)

            self._emit_event(
                job.job_id,
                {
                    "type": "workflow_selected",
                    "workflowKey": workflow_key,
                    "resolvedWidth": width,
                    "resolvedHeight": height,
                    "crop": job.crop,
                    "cropRegion": {
                        "x": crop_x,
                        "y": crop_y,
                        "width": crop_width,
                        "height": crop_height,
                    },
                    "cropComputedFrom": (
                        "thumbnail_space" if crop_region_from_thumb is not None else "scaled_region"
                    ),
                },
            )

            workflow = build_workflow(
                template=template,
                input_filename=uploaded["name"],
                width=width,
                height=height,
                filename_prefix=f"thumb_{job.job_id}",
                crop_mode=job.crop,
                crop_x=crop_x,
                crop_y=crop_y,
                crop_width=crop_width,
                crop_height=crop_height,
            )

            def relay_event(event: dict[str, Any]) -> None:
                # This callback is called from blocking Comfy execution context.
                self._emit_event(job.job_id, event)

            comfy_result = self._comfy.run_prompt_and_get_first_image(
                workflow,
                event_callback=relay_event,
            )

            upload = self._convex.upload_file_to_convex(
                file_path=comfy_result.output_file_path,
                content_type="image/png",
                generate_upload_url_mutation=settings.convex_generate_upload_url_mutation,
            )

            thumb_upload = None
            thumb_path = self._create_thumbnail_file(
                source_path=comfy_result.output_file_path,
                job_id=job.job_id,
            )
            if thumb_path:
                thumb_upload = self._convex.upload_file_to_convex(
                    file_path=str(thumb_path),
                    content_type="image/jpeg",
                    generate_upload_url_mutation=settings.convex_generate_upload_url_mutation,
                )
                self._emit_event(
                    job.job_id,
                    {
                        "type": "thumbnail_uploaded",
                        "thumbnailStorageId": thumb_upload["storageId"],
                    },
                )
                try:
                    thumb_path.unlink(missing_ok=True)
                except Exception as cleanup_exc:  # noqa: BLE001
                    print(f"[worker] thumbnail cleanup failed for {job.job_id}: {cleanup_exc}")

            result_payload = {
                "promptId": comfy_result.prompt_id,
                "filename": comfy_result.output_filename,
                "subfolder": comfy_result.output_subfolder,
                "type": comfy_result.output_type,
                "workerId": settings.worker_id,
                "workflowKey": workflow_key,
                "thumbnailStorageId": thumb_upload["storageId"] if thumb_upload else None,
            }

            self._convex.mark_job_completed(
                settings.convex_mark_completed_mutation,
                job_id=job.job_id,
                result_storage_id=upload["storageId"],
                result=result_payload,
            )
            self._emit_event(job.job_id, {"type": "job_completed", "resultStorageId": upload["storageId"]})

        except Exception as exc:  # noqa: BLE001
            err = {
                "message": str(exc),
                "type": exc.__class__.__name__,
                "workerId": settings.worker_id,
            }
            try:
                self._convex.mark_job_failed(settings.convex_mark_failed_mutation, job.job_id, err)
            finally:
                self._emit_event(job.job_id, {"type": "job_failed", "error": err})

    def _create_thumbnail_file(self, source_path: str, job_id: str) -> Path | None:
        try:
            src = Path(source_path)
            thumb_path = Path(settings.output_dir) / f"{job_id}_thumb.jpg"
            with Image.open(src) as img:
                converted = img.convert("RGB")
                converted.thumbnail((self._THUMB_MAX_SIZE, self._THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
                converted.save(thumb_path, format="JPEG", quality=85, optimize=True)
            return thumb_path
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] thumbnail generation failed for {job_id}: {exc}")
            return None
