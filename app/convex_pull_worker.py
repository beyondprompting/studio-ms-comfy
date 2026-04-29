from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image, ImageFilter

from .comfy_client import ComfyClient
from .config import settings
from .convex_client import ClaimedJob, ConvexBridge, ConvexConfig
from .workflow import build_workflow, load_workflow_template


class ConvexPullWorker:
    _THUMB_MAX_SIZE = 600
    _CRITICAL_COMFY_WS_TYPES = {
        "execution_start",
        "execution_success",
        "execution_error",
        "execution_cached",
        "executing",
        "executed",
    }

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
        self._source_image_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._ws_event_counters: dict[str, int] = {}
        self._running = False

    def _purge_source_cache(self, now: float) -> None:
        ttl = max(settings.worker_source_cache_ttl_seconds, 0)
        if ttl == 0:
            self._source_image_cache.clear()
            return

        expired_keys = [
            key
            for key, (fetched_at, _data) in self._source_image_cache.items()
            if now - fetched_at > ttl
        ]
        for key in expired_keys:
            self._source_image_cache.pop(key, None)

        max_entries = max(settings.worker_source_cache_max_entries, 1)
        while len(self._source_image_cache) > max_entries:
            self._source_image_cache.popitem(last=False)

    def _get_source_image_bytes(self, source_url: str) -> tuple[bytes, bool]:
        if not settings.worker_source_cache_enabled:
            response = requests.get(source_url, timeout=120)
            response.raise_for_status()
            return response.content, False

        now = time.time()
        self._purge_source_cache(now)

        cached = self._source_image_cache.get(source_url)
        if cached is not None:
            fetched_at, cached_bytes = cached
            self._source_image_cache.move_to_end(source_url)
            if now - fetched_at <= max(settings.worker_source_cache_ttl_seconds, 0):
                return cached_bytes, True
            self._source_image_cache.pop(source_url, None)

        response = requests.get(source_url, timeout=120)
        response.raise_for_status()
        data = response.content

        self._source_image_cache[source_url] = (now, data)
        self._source_image_cache.move_to_end(source_url)
        self._purge_source_cache(now)
        return data, False

    def _should_emit_comfy_event(self, job_id: str, event: dict[str, Any]) -> bool:
        if settings.worker_emit_all_comfy_events:
            return True

        event_type = event.get("type")
        if event_type != "comfy_ws_message":
            return True

        message_type = event.get("message_type")
        if message_type in self._CRITICAL_COMFY_WS_TYPES:
            return True

        sample_every = max(settings.worker_ws_event_sample_every, 1)
        counter = self._ws_event_counters.get(job_id, 0) + 1
        self._ws_event_counters[job_id] = counter

        if counter == 1:
            return True
        return counter % sample_every == 0

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def _download_image_rgba(self, url: str, timeout: int = 120) -> Image.Image:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        with Image.open(BytesIO(response.content)) as img:
            return img.convert("RGBA")

    def _process_stage4_reimplant(
        self,
        job: ClaimedJob,
        background_bytes: bytes,
    ) -> tuple[Path, int, int, dict[str, int]]:
        t0 = time.perf_counter()
        params = job.params if isinstance(job.params, dict) else {}
        qwen_result_url = params.get("qwenResultUrl") if isinstance(params.get("qwenResultUrl"), str) else None
        mask_url = params.get("maskUrl") if isinstance(params.get("maskUrl"), str) else None
        thumbnail_max_size = self._safe_int(params.get("thumbnailMaxSize")) or 600

        if not qwen_result_url or not mask_url:
            raise RuntimeError("Stage 4 missing required params: qwenResultUrl or maskUrl")

        with Image.open(BytesIO(background_bytes)) as bg_raw:
            bg_img = bg_raw.convert("RGBA")
        load_background_ms = int(round((time.perf_counter() - t0) * 1000))

        def _timed_download(url: str) -> tuple[Image.Image, int]:
            t_start = time.perf_counter()
            img = self._download_image_rgba(url)
            elapsed_ms = int(round((time.perf_counter() - t_start) * 1000))
            return img, elapsed_ms

        # Download independent inputs in parallel to reduce Stage4 wall time.
        with ThreadPoolExecutor(max_workers=2) as executor:
            qwen_future = executor.submit(_timed_download, qwen_result_url)
            mask_future = executor.submit(_timed_download, mask_url)
            qwen_img, download_qwen_ms = qwen_future.result()
            mask_img, download_mask_ms = mask_future.result()

        crop_region = job.crop_region if isinstance(job.crop_region, dict) else {}
        crop_x_thumb = self._safe_float(crop_region.get("x"))
        crop_y_thumb = self._safe_float(crop_region.get("y"))
        crop_w_thumb = self._safe_float(crop_region.get("width"))
        crop_h_thumb = self._safe_float(crop_region.get("height"))
        thumb_canvas_w = self._safe_float(crop_region.get("thumbnailCanvasWidth"))
        thumb_canvas_h = self._safe_float(crop_region.get("thumbnailCanvasHeight"))
        rotation = self._safe_float(crop_region.get("rotation")) or 0.0

        if (
            crop_x_thumb is None
            or crop_y_thumb is None
            or crop_w_thumb is None
            or crop_h_thumb is None
        ):
            raise RuntimeError("Stage 4 missing cropRegion x/y/width/height")

        bg_w, bg_h = bg_img.width, bg_img.height
        if (
            thumb_canvas_w is not None
            and thumb_canvas_h is not None
            and thumb_canvas_w > 0
            and thumb_canvas_h > 0
        ):
            ratio_x = bg_w / thumb_canvas_w
            ratio_y = bg_h / thumb_canvas_h
        else:
            # Legacy fallback for older jobs that only send thumbnailMaxSize.
            thumb_scale = min(1.0, thumbnail_max_size / bg_w, thumbnail_max_size / bg_h)
            thumb_w = max(1, int(round(bg_w * thumb_scale)))
            thumb_h = max(1, int(round(bg_h * thumb_scale)))
            ratio_x = bg_w / thumb_w
            ratio_y = bg_h / thumb_h

        orig_crop_x = int(round(crop_x_thumb * ratio_x))
        orig_crop_y = int(round(crop_y_thumb * ratio_y))
        orig_crop_w = int(round(crop_w_thumb * ratio_x))
        orig_crop_h = int(round(crop_h_thumb * ratio_y))

        if orig_crop_w <= 0 or orig_crop_h <= 0:
            raise RuntimeError("Stage 4 resolved crop dimensions are invalid")

        # Clamp crop against background bounds to avoid PIL errors.
        orig_crop_x = max(0, min(orig_crop_x, bg_w - 1))
        orig_crop_y = max(0, min(orig_crop_y, bg_h - 1))
        orig_crop_w = max(1, min(orig_crop_w, bg_w - orig_crop_x))
        orig_crop_h = max(1, min(orig_crop_h, bg_h - orig_crop_y))

        t_resize = time.perf_counter()
        qwen_resized = qwen_img.resize((orig_crop_w, orig_crop_h), Image.Resampling.LANCZOS)
        mask_resized_l = mask_img.resize((orig_crop_w, orig_crop_h), Image.Resampling.LANCZOS).convert("L")
        resize_inputs_ms = int(round((time.perf_counter() - t_resize) * 1000))

        feather_radius = min(20, max(3, int(round(min(orig_crop_w, orig_crop_h) * 0.015))))
        t_feather = time.perf_counter()
        feathered = mask_resized_l.filter(ImageFilter.BoxBlur(feather_radius))
        feathered = feathered.filter(ImageFilter.BoxBlur(feather_radius))
        feathered = feathered.filter(ImageFilter.BoxBlur(feather_radius))
        feather_mask_ms = int(round((time.perf_counter() - t_feather) * 1000))

        inv_mask = feathered.point(lambda p: 255 - p)

        t_blend = time.perf_counter()
        if abs(rotation) < 0.001:
            crop_bg = bg_img.crop(
                (orig_crop_x, orig_crop_y, orig_crop_x + orig_crop_w, orig_crop_y + orig_crop_h)
            )
            blended = Image.composite(qwen_resized, crop_bg, inv_mask)
            bg_img.paste(blended, (orig_crop_x, orig_crop_y))
        else:
            # Rotated case: replicate pixel mapping semantics from frontend implementation.
            bg_px = bg_img.load()
            qwen_px = qwen_resized.load()
            feathered_px = feathered.load()

            radians = rotation * math.pi / 180.0
            cos_r = math.cos(radians)
            sin_r = math.sin(radians)
            cx = orig_crop_x + (orig_crop_w / 2.0)
            cy = orig_crop_y + (orig_crop_h / 2.0)

            half_w = orig_crop_w / 2.0
            half_h = orig_crop_h / 2.0

            for py in range(orig_crop_h):
                rel_y = py - half_h
                for px in range(orig_crop_w):
                    mask_brightness = feathered_px[px, py]
                    if mask_brightness >= 255:
                        continue

                    rel_x = px - half_w
                    ox = int(round(cx + rel_x * cos_r - rel_y * sin_r))
                    oy = int(round(cy + rel_x * sin_r + rel_y * cos_r))

                    if ox < 0 or oy < 0 or ox >= bg_w or oy >= bg_h:
                        continue

                    alpha = mask_brightness / 255.0
                    one_minus = 1.0 - alpha
                    br, bgc, bb, _ba = bg_px[ox, oy]
                    qr, qg, qb, _qa = qwen_px[px, py]
                    bg_px[ox, oy] = (
                        int(round(br * alpha + qr * one_minus)),
                        int(round(bgc * alpha + qg * one_minus)),
                        int(round(bb * alpha + qb * one_minus)),
                        255,
                    )
        blend_ms = int(round((time.perf_counter() - t_blend) * 1000))

        output_path = Path(settings.output_dir) / f"{job.job_id}_reimplanted.png"
        t_encode = time.perf_counter()
        compress_level = min(max(settings.worker_stage4_png_compress_level, 0), 9)
        bg_img.convert("RGB").save(
            output_path,
            format="PNG",
            optimize=settings.worker_stage4_png_optimize,
            compress_level=compress_level,
        )
        encode_ms = int(round((time.perf_counter() - t_encode) * 1000))
        output_bytes = output_path.stat().st_size if output_path.exists() else 0

        timings = {
            "loadBackgroundMs": load_background_ms,
            "downloadQwenMs": download_qwen_ms,
            "downloadMaskMs": download_mask_ms,
            "resizeInputsMs": resize_inputs_ms,
            "featherMaskMs": feather_mask_ms,
            "blendMs": blend_ms,
            "encodeMs": encode_ms,
            "totalStage4Ms": int(round((time.perf_counter() - t0) * 1000)),
            "outputBytes": int(output_bytes),
            "cropWidthPx": int(orig_crop_w),
            "cropHeightPx": int(orig_crop_h),
            "pngCompressLevel": int(compress_level),
            "pngOptimize": 1 if settings.worker_stage4_png_optimize else 0,
        }
        return output_path, bg_w, bg_h, timings

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
        self._ws_event_counters[job.job_id] = 0
        self._emit_event(
            job.job_id,
            {
                "type": "job_claimed",
                "workerId": settings.worker_id,
                "requestId": job.request_id,
            },
        )

        try:
            input_bytes, cache_hit = self._get_source_image_bytes(job.source_image_url)

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
                    "sourceCacheHit": cache_hit,
                    "sourceWidth": source_width,
                    "sourceHeight": source_height,
                },
            )

            workflow_key, template = self._select_template(job)

            if workflow_key == "estuches_stage4_reimplant_feather":
                self._emit_event(job.job_id, {"type": "stage4_reimplant_started"})
                result_path, result_w, result_h, stage4_timings = self._process_stage4_reimplant(job, input_bytes)

                if settings.worker_stage4_emit_timing_events:
                    self._emit_event(
                        job.job_id,
                        {
                            "type": "stage4_reimplant_timing",
                            **stage4_timings,
                        },
                    )

                t_upload = time.perf_counter()
                upload = self._convex.upload_file_to_convex(
                    file_path=str(result_path),
                    content_type="image/png",
                    generate_upload_url_mutation=settings.convex_generate_upload_url_mutation,
                )
                upload_ms = int(round((time.perf_counter() - t_upload) * 1000))

                if settings.worker_stage4_emit_timing_events:
                    self._emit_event(
                        job.job_id,
                        {
                            "type": "stage4_reimplant_upload_timing",
                            "uploadMs": upload_ms,
                            "uploadedBytes": stage4_timings.get("outputBytes", 0),
                        },
                    )

                thumb_upload = None
                thumb_path = self._create_thumbnail_file(
                    source_path=str(result_path),
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

                try:
                    result_path.unlink(missing_ok=True)
                except Exception as cleanup_exc:  # noqa: BLE001
                    print(f"[worker] stage4 cleanup failed for {job.job_id}: {cleanup_exc}")

                result_payload = {
                    "workerId": settings.worker_id,
                    "workflowKey": workflow_key,
                    "resultWidth": result_w,
                    "resultHeight": result_h,
                    "thumbnailStorageId": thumb_upload["storageId"] if thumb_upload else None,
                }

                self._convex.mark_job_completed(
                    settings.convex_mark_completed_mutation,
                    job_id=job.job_id,
                    result_storage_id=upload["storageId"],
                    result=result_payload,
                )
                self._emit_event(
                    job.job_id,
                    {
                        "type": "job_completed",
                        "resultStorageId": upload["storageId"],
                        "resultWidth": result_w,
                        "resultHeight": result_h,
                    },
                )
                return

            uploaded = self._comfy.upload_image_bytes(input_bytes, f"{job.job_id}.png")
            self._emit_event(job.job_id, {"type": "comfy_input_uploaded", "data": uploaded})

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
                if not self._should_emit_comfy_event(job.job_id, event):
                    return
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

            if workflow_key == "estuches_stage2_crop_fullres":
                fast_result_payload = {
                    "promptId": comfy_result.prompt_id,
                    "filename": comfy_result.output_filename,
                    "subfolder": comfy_result.output_subfolder,
                    "type": comfy_result.output_type,
                    "workerId": settings.worker_id,
                    "workflowKey": workflow_key,
                    "thumbnailStorageId": None,
                }

                # Stage 2 UX path: unblock frontend as soon as crop PNG is uploaded.
                self._convex.mark_job_completed(
                    settings.convex_mark_completed_mutation,
                    job_id=job.job_id,
                    result_storage_id=upload["storageId"],
                    result=fast_result_payload,
                )
                self._emit_event(job.job_id, {"type": "job_completed", "resultStorageId": upload["storageId"]})

                thumb_path = self._create_thumbnail_file(
                    source_path=comfy_result.output_file_path,
                    job_id=job.job_id,
                )
                if thumb_path:
                    try:
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
                            self._convex.mutation(
                                settings.convex_attach_stage2_thumbnail_mutation,
                                {
                                    "jobId": job.job_id,
                                    "thumbnailStorageId": thumb_upload["storageId"],
                                },
                            )
                        except Exception as attach_exc:  # noqa: BLE001
                            print(f"[worker] stage2 thumbnail attach failed for {job.job_id}: {attach_exc}")
                    finally:
                        try:
                            thumb_path.unlink(missing_ok=True)
                        except Exception as cleanup_exc:  # noqa: BLE001
                            print(f"[worker] thumbnail cleanup failed for {job.job_id}: {cleanup_exc}")

                return

            thumb_upload = None
            # Stage 1 already outputs the real thumbnail image. Creating another
            # thumbnail here duplicates small files in Convex storage.
            if workflow_key != "estuches_stage1_resize_image_mask_node":
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
        finally:
            self._ws_event_counters.pop(job.job_id, None)

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
