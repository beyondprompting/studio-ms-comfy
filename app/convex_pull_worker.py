from __future__ import annotations

import json
import math
import copy
import random
import time
import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import requests
from PIL import Image, ImageFilter

from .comfy_client import ComfyClient, ComfyPromptCanceled
from .config import settings
from .convex_client import ClaimedJob, ConvexBridge, ConvexConfig
from .workflow import build_workflow, load_workflow_template

DEFAULT_QWEN_COMFY_MODEL_NAME = "Qwen-Image-Edit-2509-Q4_K_M.gguf"
DEFAULT_QWEN_COMFY_RESOLUTION_MEGAPIXELS = 1.0
DEFAULT_QWEN_COMFY_SAMPLING_SHIFT = 3.5
DEFAULT_QWEN_COMFY_MULTI_ANGLES_LORA_SCALE = 0.9
DEFAULT_QWEN_COMFY_ESTUCHES_LORA_SCALE = 0.9
DEFAULT_QWEN_COMFY_LIGHTNING_LORA_SCALE = 1.0
QWEN_COMFY_MODEL_NAMES = {
    DEFAULT_QWEN_COMFY_MODEL_NAME,
    "Qwen-Image-Edit-2509-Q5_K_M.gguf",
    "qwen-image-edit-2511-Q4_K_M.gguf",
    "qwen-image-edit-2511-Q5_K_M.gguf",
}
QWEN_COMFY_MULTI_ANGLES_LORA_NAME = "Qwen-Edit-2509-Multi-Angle-Lighting.safetensors"
QWEN_COMFY_2509_ESTUCHES_LORA_NAME = "estuches/estuches_003-Qwen2509-5000.safetensors"
QWEN_COMFY_2509_LIGHTNING_LORA_NAME = "Qwen-Image-Edit-2509-Lightning-8steps-V1.0-bf16.safetensors"
QWEN_COMFY_2511_ESTUCHES_LORA_NAME = "estuches/estuches_003-Qwen2511_5000.safetensors"
QWEN_COMFY_2511_LIGHTNING_LORA_NAME = "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors"


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
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_lock = threading.Lock()
        self._current_job_id: str | None = None
        self._current_workflow_key: str | None = None

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

    def _upload_files_to_convex_parallel(
        self,
        files: dict[str, tuple[Path, str]],
        *,
        max_workers: int = 4,
    ) -> dict[str, dict[str, Any]]:
        upload_jobs: dict[str, tuple[str, bytes, str]] = {}
        for key, (path, content_type) in files.items():
            upload_url = self._convex.mutation(settings.convex_generate_upload_url_mutation, {})
            upload_jobs[key] = (upload_url, path.read_bytes(), content_type)

        def _post_upload(job_data: tuple[str, bytes, str]) -> dict[str, Any]:
            upload_url, data, content_type = job_data
            response = requests.post(
                upload_url,
                data=data,
                headers={"Content-Type": content_type},
                timeout=120,
            )
            response.raise_for_status()
            body = response.json()
            storage_id = body.get("storageId")
            if not storage_id:
                raise RuntimeError("Convex upload response did not contain storageId")
            return {"storageId": storage_id, "uploadResponse": body}

        if not upload_jobs:
            return {}

        worker_count = max(1, min(max_workers, len(upload_jobs)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                key: executor.submit(_post_upload, job_data)
                for key, job_data in upload_jobs.items()
            }
            return {key: future.result() for key, future in futures.items()}

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
        original_crop_region = (
            params.get("originalCropRegion")
            if isinstance(params.get("originalCropRegion"), dict)
            else None
        )

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

        crop_computed_from = "thumbnail_space"
        orig_x_param = self._safe_float(original_crop_region.get("x")) if original_crop_region else None
        orig_y_param = self._safe_float(original_crop_region.get("y")) if original_crop_region else None
        orig_w_param = self._safe_float(original_crop_region.get("width")) if original_crop_region else None
        orig_h_param = self._safe_float(original_crop_region.get("height")) if original_crop_region else None

        if (
            orig_x_param is not None
            and orig_y_param is not None
            and orig_w_param is not None
            and orig_h_param is not None
        ):
            orig_crop_x = int(round(orig_x_param))
            orig_crop_y = int(round(orig_y_param))
            orig_crop_w = int(round(orig_w_param))
            orig_crop_h = int(round(orig_h_param))
            crop_computed_from = "original_crop_region"
        else:
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
        target_crop_size = (orig_crop_w, orig_crop_h)
        if qwen_img.size == target_crop_size:
            qwen_resized = qwen_img
        else:
            qwen_resized = qwen_img.resize(target_crop_size, Image.Resampling.LANCZOS)
        if mask_img.size == target_crop_size:
            mask_resized_l = mask_img.convert("L")
        else:
            mask_resized_l = mask_img.resize(target_crop_size, Image.Resampling.LANCZOS).convert("L")
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
            "cropComputedFrom": 1 if crop_computed_from == "original_crop_region" else 0,
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
            "estuches_qwen_comfy": "Estuches-Qwen-Worker.v1.2.0.json",
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

    @staticmethod
    def _encode_image(
        image: Image.Image,
        output_path: Path,
        *,
        image_format: str,
        quality: int | None = None,
    ) -> None:
        save_kwargs: dict[str, Any] = {"format": image_format}
        if image_format == "JPEG":
            save_kwargs.update({"quality": quality or 85, "optimize": True})
        elif image_format == "PNG":
            save_kwargs.update({"optimize": False, "compress_level": 1})
        image.save(output_path, **save_kwargs)

    def _create_thumbnail_file_from_image(self, image: Image.Image, job_id: str) -> Path:
        thumb_path = Path(settings.output_dir) / f"{job_id}_thumb.jpg"
        thumbnail = image.convert("RGB")
        thumbnail.thumbnail((self._THUMB_MAX_SIZE, self._THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
        self._encode_image(thumbnail, thumb_path, image_format="JPEG", quality=85)
        return thumb_path

    def _process_stage1_thumbnail_pil(
        self,
        job: ClaimedJob,
        input_bytes: bytes,
    ) -> tuple[Path, int, int, dict[str, int]]:
        t0 = time.perf_counter()
        with Image.open(BytesIO(input_bytes)) as src:
            image = src.convert("RGB")
        load_ms = int(round((time.perf_counter() - t0) * 1000))

        t_resize = time.perf_counter()
        image.thumbnail((self._THUMB_MAX_SIZE, self._THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
        resize_ms = int(round((time.perf_counter() - t_resize) * 1000))

        output_path = Path(settings.output_dir) / f"{job.job_id}_stage1_thumb.jpg"
        t_encode = time.perf_counter()
        self._encode_image(image, output_path, image_format="JPEG", quality=85)
        encode_ms = int(round((time.perf_counter() - t_encode) * 1000))

        timings = {
            "loadMs": load_ms,
            "resizeMs": resize_ms,
            "encodeMs": encode_ms,
            "totalMs": int(round((time.perf_counter() - t0) * 1000)),
            "outputBytes": int(output_path.stat().st_size if output_path.exists() else 0),
            "resultWidth": int(image.width),
            "resultHeight": int(image.height),
        }
        return output_path, image.width, image.height, timings

    def _download_image_bytes(self, url: str, timeout: int = 120) -> bytes:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return response.content

    @staticmethod
    def _patch_load_image_node(
        workflow: dict[str, Any],
        node_id: str,
        image_name: str,
    ) -> None:
        node = workflow.get(node_id)
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise RuntimeError(f"Comfy workflow missing LoadImage node {node_id}")
        node["inputs"]["image"] = image_name

    @staticmethod
    def _get_qwen_comfy_lora_names(comfy_model_name: str) -> dict[str, str]:
        if "2511" in comfy_model_name:
            return {
                "multiAngles": QWEN_COMFY_MULTI_ANGLES_LORA_NAME,
                "estuches": QWEN_COMFY_2511_ESTUCHES_LORA_NAME,
                "lightning": QWEN_COMFY_2511_LIGHTNING_LORA_NAME,
            }
        return {
            "multiAngles": QWEN_COMFY_MULTI_ANGLES_LORA_NAME,
            "estuches": QWEN_COMFY_2509_ESTUCHES_LORA_NAME,
            "lightning": QWEN_COMFY_2509_LIGHTNING_LORA_NAME,
        }

    @staticmethod
    def _patch_qwen_comfy_loras(
        workflow: dict[str, Any],
        *,
        lora_names: dict[str, str],
        multi_angles_scale: float,
        estuches_scale: float,
        lightning_scale: float,
    ) -> None:
        stack_node = workflow.get("563")
        if not isinstance(stack_node, dict) or not isinstance(stack_node.get("inputs"), dict):
            raise RuntimeError("Qwen Comfy v1.2 workflow missing LoRA stack node 563")
        stack_inputs = stack_node["inputs"]
        stack_inputs["lora_name_1"] = lora_names["multiAngles"]
        stack_inputs["model_weight_1"] = multi_angles_scale
        stack_inputs["lora_name_2"] = lora_names["estuches"]
        stack_inputs["model_weight_2"] = estuches_scale

        lightning_node = workflow.get("548")
        if not isinstance(lightning_node, dict) or not isinstance(lightning_node.get("inputs"), dict):
            raise RuntimeError("Qwen Comfy v1.2 workflow missing Lightning LoRA node 548")
        lightning_inputs = lightning_node["inputs"]
        lightning_inputs["lora_name"] = lora_names["lightning"]
        lightning_inputs["strength_model"] = lightning_scale

    def _build_qwen_comfy_workflow(
        self,
        job: ClaimedJob,
        composite_bytes: bytes,
    ) -> tuple[dict[str, Any], dict[str, int | float]]:
        params = job.params if isinstance(job.params, dict) else {}
        product_url = params.get("productUrl") if isinstance(params.get("productUrl"), str) else None
        qwen_crop_url = params.get("qwenCropUrl") if isinstance(params.get("qwenCropUrl"), str) else None
        mask_url = params.get("maskUrl") if isinstance(params.get("maskUrl"), str) else None

        if not product_url or not qwen_crop_url or not mask_url:
            raise RuntimeError("Qwen Comfy job missing productUrl, qwenCropUrl, or maskUrl")

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=3) as executor:
            product_future = executor.submit(self._download_image_bytes, product_url)
            crop_future = executor.submit(self._download_image_bytes, qwen_crop_url)
            mask_future = executor.submit(self._download_image_bytes, mask_url)
            product_bytes = product_future.result()
            crop_bytes = crop_future.result()
            mask_bytes = mask_future.result()
        download_inputs_ms = int(round((time.perf_counter() - t0) * 1000))

        t_upload = time.perf_counter()
        uploaded_composite = self._comfy.upload_image_bytes(
            composite_bytes,
            f"{job.job_id}_qwen_composite.png",
        )
        uploaded_product = self._comfy.upload_image_bytes(
            product_bytes,
            f"{job.job_id}_product.png",
        )
        uploaded_crop = self._comfy.upload_image_bytes(
            crop_bytes,
            f"{job.job_id}_crop.png",
        )
        uploaded_mask = self._comfy.upload_image_bytes(
            mask_bytes,
            f"{job.job_id}_mask.png",
        )
        upload_inputs_ms = int(round((time.perf_counter() - t_upload) * 1000))

        _, template = self._select_template(job)
        workflow = copy.deepcopy(template)

        self._patch_load_image_node(workflow, "523", uploaded_composite["name"])
        self._patch_load_image_node(workflow, "524", uploaded_product["name"])
        self._patch_load_image_node(workflow, "525", uploaded_crop["name"])
        self._patch_load_image_node(workflow, "526", uploaded_mask["name"])

        prompt = params.get("prompt") if isinstance(params.get("prompt"), str) else ""
        negative_prompt = (
            params.get("negativePrompt")
            if isinstance(params.get("negativePrompt"), str)
            else ""
        )
        seed = self._safe_int(params.get("seed"))
        steps = self._safe_int(params.get("steps")) or 10
        cfg = self._safe_float(params.get("cfg")) or 1.0
        sampler_name = (
            params.get("samplerName")
            if params.get("samplerName") in {"dpmpp_2m_sde_gpu", "dpmpp_2m", "euler"}
            else "dpmpp_2m_sde_gpu"
        )
        scheduler = (
            params.get("scheduler")
            if params.get("scheduler") in {"beta", "karras"}
            else "beta"
        )
        comfy_model_name = (
            params.get("comfyModelName")
            if params.get("comfyModelName") in QWEN_COMFY_MODEL_NAMES
            else DEFAULT_QWEN_COMFY_MODEL_NAME
        )
        resolution_megapixels = self._safe_float(params.get("resolutionMegapixels"))
        if resolution_megapixels is None or resolution_megapixels <= 0:
            resolution_megapixels = DEFAULT_QWEN_COMFY_RESOLUTION_MEGAPIXELS
        sampling_shift = self._safe_float(params.get("samplingShift"))
        if sampling_shift is None or sampling_shift <= 0:
            sampling_shift = DEFAULT_QWEN_COMFY_SAMPLING_SHIFT

        workflow["28"]["inputs"]["prompt"] = prompt
        workflow["12"]["inputs"]["prompt"] = negative_prompt
        workflow["35"]["inputs"]["unet_name"] = comfy_model_name
        workflow["150"]["inputs"]["noise_seed"] = (
            seed if seed is not None else random.randint(0, 2**31 - 1)
        )
        workflow["150"]["inputs"]["cfg"] = cfg
        workflow["23"]["inputs"]["steps"] = steps
        workflow["23"]["inputs"]["scheduler"] = scheduler
        workflow["24"]["inputs"]["sampler_name"] = sampler_name
        workflow["543"]["inputs"]["value"] = resolution_megapixels
        workflow["5"]["inputs"]["shift"] = sampling_shift
        workflow["522"]["inputs"]["filename_prefix"] = f"Estuches-Qwen-{job.job_id}"

        estuches_lora_scale = self._safe_float(params.get("estuchesLoraScale"))
        if estuches_lora_scale is None:
            estuches_lora_scale = DEFAULT_QWEN_COMFY_ESTUCHES_LORA_SCALE
        multi_angles_lora_scale = self._safe_float(params.get("multiAnglesLoraScale"))
        if multi_angles_lora_scale is None:
            multi_angles_lora_scale = DEFAULT_QWEN_COMFY_MULTI_ANGLES_LORA_SCALE
        lightning_lora_scale = self._safe_float(params.get("lightningLoraScale"))
        if lightning_lora_scale is None:
            lightning_lora_scale = DEFAULT_QWEN_COMFY_LIGHTNING_LORA_SCALE

        lora_names = self._get_qwen_comfy_lora_names(comfy_model_name)
        self._patch_qwen_comfy_loras(
            workflow,
            lora_names=lora_names,
            multi_angles_scale=multi_angles_lora_scale,
            estuches_scale=estuches_lora_scale,
            lightning_scale=lightning_lora_scale,
        )

        timings = {
            "downloadInputsMs": download_inputs_ms,
            "uploadInputsToComfyMs": upload_inputs_ms,
            "promptLength": len(prompt),
            "negativePromptLength": len(negative_prompt),
            "steps": steps,
            "cfg": cfg,
            "samplerName": sampler_name,
            "scheduler": scheduler,
            "comfyModelName": comfy_model_name,
            "resolutionMegapixels": resolution_megapixels,
            "samplingShift": sampling_shift,
            "loraMultiAnglesName": lora_names["multiAngles"],
            "loraEstuchesName": lora_names["estuches"],
            "loraLightningName": lora_names["lightning"],
            "loraMultiAnglesScale": multi_angles_lora_scale,
            "loraEstuchesScale": estuches_lora_scale,
            "loraLightningScale": lightning_lora_scale,
        }
        return workflow, timings

    def _process_stage2_crop_pil(
        self,
        job: ClaimedJob,
        input_bytes: bytes,
        source_width: int | None,
        source_height: int | None,
    ) -> tuple[Path, Path, int, int, dict[str, int | str]]:
        t0 = time.perf_counter()
        with Image.open(BytesIO(input_bytes)) as src_raw:
            source = src_raw.convert("RGBA")
        load_ms = int(round((time.perf_counter() - t0) * 1000))

        real_source_w = source_width or source.width
        real_source_h = source_height or source.height

        crop_region_from_thumb = self._resolve_stage2_crop_from_thumbnail_space(
            job,
            real_source_w,
            real_source_h,
        )
        crop_computed_from = "thumbnail_space"
        if crop_region_from_thumb is not None:
            crop_x, crop_y, crop_width, crop_height = crop_region_from_thumb
        else:
            crop_computed_from = "scaled_region"
            crop_x, crop_y, crop_width, crop_height = self._resolve_crop_region(
                job,
                real_source_w,
                real_source_h,
            )

        t_crop = time.perf_counter()
        crop = source.crop((crop_x, crop_y, crop_x + crop_width, crop_y + crop_height))
        crop_ms = int(round((time.perf_counter() - t_crop) * 1000))

        output_path = Path(settings.output_dir) / f"{job.job_id}_crop.png"
        t_encode_crop = time.perf_counter()
        self._encode_image(crop, output_path, image_format="PNG")
        encode_crop_ms = int(round((time.perf_counter() - t_encode_crop) * 1000))

        t_thumb = time.perf_counter()
        thumb_path = self._create_thumbnail_file_from_image(crop, job.job_id)
        thumbnail_ms = int(round((time.perf_counter() - t_thumb) * 1000))

        timings = {
            "loadMs": load_ms,
            "cropMs": crop_ms,
            "encodeCropMs": encode_crop_ms,
            "thumbnailMs": thumbnail_ms,
            "totalMs": int(round((time.perf_counter() - t0) * 1000)),
            "outputBytes": int(output_path.stat().st_size if output_path.exists() else 0),
            "thumbnailBytes": int(thumb_path.stat().st_size if thumb_path.exists() else 0),
            "sourceWidth": int(real_source_w),
            "sourceHeight": int(real_source_h),
            "cropX": int(crop_x),
            "cropY": int(crop_y),
            "cropWidth": int(crop_width),
            "cropHeight": int(crop_height),
            "resultWidth": int(crop.width),
            "resultHeight": int(crop.height),
            "cropComputedFrom": crop_computed_from,
        }
        thumb_scale = min(
            1.0,
            self._THUMB_MAX_SIZE / max(crop.width, 1),
            self._THUMB_MAX_SIZE / max(crop.height, 1),
        )
        timings["thumbnailWidth"] = int(round(crop.width * thumb_scale))
        timings["thumbnailHeight"] = int(round(crop.height * thumb_scale))
        return output_path, thumb_path, crop.width, crop.height, timings

    @staticmethod
    def _decode_data_url_image(data_url: str) -> Image.Image:
        if "," in data_url and data_url.strip().startswith("data:"):
            _, encoded = data_url.split(",", 1)
        else:
            encoded = data_url
        raw = base64.b64decode(encoded)
        with Image.open(BytesIO(raw)) as img:
            return img.convert("RGBA")

    @staticmethod
    def _resize_to_fit(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
        resized = image.copy()
        if resized.width <= max_width and resized.height <= max_height:
            return resized
        ratio = min(max_width / resized.width, max_height / resized.height)
        next_size = (
            max(1, int(round(resized.width * ratio))),
            max(1, int(round(resized.height * ratio))),
        )
        return resized.resize(next_size, Image.Resampling.LANCZOS)

    @staticmethod
    def _white_silhouette(product: Image.Image) -> Image.Image:
        silhouette = Image.new("RGBA", product.size, (255, 255, 255, 0))
        alpha = product.getchannel("A")
        solid_alpha = alpha.point(lambda p: 255 if p > 128 else 0)
        silhouette.putalpha(solid_alpha)
        return silhouette

    @staticmethod
    def _paste_center_alpha(
        base: Image.Image,
        overlay: Image.Image,
        center_x: float,
        center_y: float,
        width: int,
        height: int,
    ) -> None:
        if width <= 0 or height <= 0:
            return
        resized = overlay.resize((width, height), Image.Resampling.LANCZOS)
        x = int(round(center_x - width / 2))
        y = int(round(center_y - height / 2))
        base.alpha_composite(resized, (x, y))

    @staticmethod
    def _draw_product_on_mask(
        mask_l: Image.Image,
        product: Image.Image,
        center_x: float,
        center_y: float,
        width: int,
        height: int,
    ) -> None:
        if width <= 0 or height <= 0:
            return
        alpha = product.getchannel("A").resize((width, height), Image.Resampling.LANCZOS)
        black_shape = Image.new("L", (width, height), 0)
        x = int(round(center_x - width / 2))
        y = int(round(center_y - height / 2))
        mask_l.paste(black_shape, (x, y), alpha)

    @staticmethod
    def _mask_bounds(mask_l: Image.Image) -> dict[str, int]:
        min_x = mask_l.width
        min_y = mask_l.height
        max_x = 0
        max_y = 0
        found = False
        px = mask_l.load()
        for y in range(mask_l.height):
            for x in range(mask_l.width):
                if px[x, y] < 128:
                    found = True
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
        if not found:
            return {"x": 0, "y": 0, "width": mask_l.width, "height": mask_l.height}
        return {
            "x": int(min_x),
            "y": int(min_y),
            "width": int(max_x - min_x + 1),
            "height": int(max_y - min_y + 1),
        }

    def _process_stage3_mask_composite(
        self,
        job: ClaimedJob,
        input_bytes: bytes,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        params = job.params if isinstance(job.params, dict) else {}
        product_url = params.get("productUrl") if isinstance(params.get("productUrl"), str) else None
        mask_data_url = (
            params.get("combinedMaskDataUrl")
            if isinstance(params.get("combinedMaskDataUrl"), str)
            else None
        )
        if not product_url or not mask_data_url:
            raise RuntimeError("Stage 3 missing required params: productUrl or combinedMaskDataUrl")

        product_position = params.get("productPosition") if isinstance(params.get("productPosition"), dict) else {}
        product_x = self._safe_float(product_position.get("x")) or 0.0
        product_y = self._safe_float(product_position.get("y")) or 0.0
        product_scale = self._safe_float(params.get("productScale")) or 1.0
        thumb_w = self._safe_float(params.get("thumbnailCanvasWidth"))
        thumb_h = self._safe_float(params.get("thumbnailCanvasHeight"))
        min_qwen_pixels = self._safe_int(params.get("qwenMinPixels")) or 1_000_000
        composition_fingerprint = (
            params.get("compositionFingerprint")
            if isinstance(params.get("compositionFingerprint"), str)
            else None
        )
        reusable_qwen_composite_image_id = (
            params.get("reusableQwenCompositeImageId")
            if isinstance(params.get("reusableQwenCompositeImageId"), str)
            else None
        )
        reusable_qwen_crop_image_id = (
            params.get("reusableQwenCropImageId")
            if isinstance(params.get("reusableQwenCropImageId"), str)
            else None
        )

        with Image.open(BytesIO(input_bytes)) as crop_raw:
            crop = crop_raw.convert("RGBA")
        mask_thumb = self._decode_data_url_image(mask_data_url).convert("L")

        if not thumb_w or thumb_w <= 0:
            thumb_w = mask_thumb.width
        if not thumb_h or thumb_h <= 0:
            thumb_h = mask_thumb.height

        ratio_x = crop.width / thumb_w
        ratio_y = crop.height / thumb_h
        mask_full = mask_thumb.resize((crop.width, crop.height), Image.Resampling.LANCZOS).convert("L")

        t_download_product = time.perf_counter()
        product = self._download_image_rgba(product_url)
        download_product_ms = int(round((time.perf_counter() - t_download_product) * 1000))

        max_prod_w = max(1, int(round(thumb_w * 0.6)))
        max_prod_h = max(1, int(round(thumb_h * 0.6)))
        product_thumb = self._resize_to_fit(product, max_prod_w, max_prod_h)
        product_full_w = max(1, int(round(product_thumb.width * product_scale * ratio_x)))
        product_full_h = max(1, int(round(product_thumb.height * product_scale * ratio_y)))
        product_center_x = product_x * ratio_x
        product_center_y = product_y * ratio_y

        self._draw_product_on_mask(
            mask_full,
            product_thumb,
            product_center_x,
            product_center_y,
            product_full_w,
            product_full_h,
        )
        mask_bounds = self._mask_bounds(mask_full)

        should_reuse_qwen_composite = bool(reusable_qwen_composite_image_id)

        qwen_scale = 1.0
        crop_pixels = crop.width * crop.height
        if crop_pixels < min_qwen_pixels:
            qwen_scale = math.sqrt(min_qwen_pixels / max(crop_pixels, 1))
        qwen_w = max(1, int(math.ceil(crop.width * qwen_scale)))
        qwen_h = max(1, int(math.ceil(crop.height * qwen_scale)))

        if qwen_scale > 1.0001:
            qwen_crop = crop.resize((qwen_w, qwen_h), Image.Resampling.LANCZOS)
            qwen_mask = mask_full.resize((qwen_w, qwen_h), Image.Resampling.LANCZOS)
        else:
            qwen_crop = crop
            qwen_mask = mask_full

        output_dir = Path(settings.output_dir)
        paths = {
            "mask": output_dir / f"{job.job_id}_stage3_mask.png",
        }
        if not should_reuse_qwen_composite:
            silhouette_composite = crop.copy()
            self._paste_center_alpha(
                silhouette_composite,
                self._white_silhouette(product_thumb),
                product_center_x,
                product_center_y,
                product_full_w,
                product_full_h,
            )
            if qwen_scale > 1.0001:
                qwen_composite = silhouette_composite.resize((qwen_w, qwen_h), Image.Resampling.LANCZOS)
            else:
                qwen_composite = silhouette_composite
            paths["qwenComposite"] = output_dir / f"{job.job_id}_stage3_qwen_composite.png"
        if qwen_scale > 1.0001 and not reusable_qwen_crop_image_id:
            paths["qwenCrop"] = output_dir / f"{job.job_id}_stage3_qwen_crop.png"
        mask_rgba = Image.merge("RGBA", (qwen_mask, qwen_mask, qwen_mask, Image.new("L", qwen_mask.size, 255)))
        self._encode_image(mask_rgba, paths["mask"], image_format="PNG")
        if "qwenComposite" in paths:
            self._encode_image(qwen_composite.convert("RGBA"), paths["qwenComposite"], image_format="PNG")
        if "qwenCrop" in paths:
            self._encode_image(qwen_crop.convert("RGBA"), paths["qwenCrop"], image_format="PNG")

        thumb_paths = {}
        if "qwenComposite" in paths:
            thumb_paths["qwenComposite"] = self._create_thumbnail_file(
                str(paths["qwenComposite"]),
                f"{job.job_id}_qwenComposite",
            )

        timings = {
            "downloadProductMs": download_product_ms,
            "totalMs": int(round((time.perf_counter() - t0) * 1000)),
            "cropWidth": int(crop.width),
            "cropHeight": int(crop.height),
            "qwenWidth": int(qwen_w),
            "qwenHeight": int(qwen_h),
            "qwenScale": float(qwen_scale),
            "reusedSourceCrop": 0 if "qwenCrop" in paths else 1,
            "productWidth": int(product_full_w),
            "productHeight": int(product_full_h),
            "thumbnailCanvasWidth": int(round(thumb_w)),
            "thumbnailCanvasHeight": int(round(thumb_h)),
            "reusedQwenComposite": 1 if should_reuse_qwen_composite else 0,
            "reusedQwenCrop": 1 if reusable_qwen_crop_image_id else 0,
        }
        return {
            "paths": paths,
            "thumbPaths": thumb_paths,
            "maskBounds": mask_bounds,
            "timings": timings,
            "reusedSourceCrop": "qwenCrop" not in paths,
            "qwenCompositeSourceImageId": reusable_qwen_composite_image_id,
            "qwenCropSourceImageId": reusable_qwen_crop_image_id,
            "compositionFingerprint": composition_fingerprint,
        }

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
        self._start_heartbeat_thread()
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

        self._send_heartbeat(status="offline", force=True)

    def stop(self) -> None:
        self._running = False
        self._send_heartbeat(status="offline", force=True)

    def _start_heartbeat_thread(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"{settings.worker_id}-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        self._send_heartbeat(force=True)
        interval = max(settings.worker_heartbeat_interval_seconds, 1.0)
        while self._running:
            time.sleep(interval)
            self._send_heartbeat()

    def _send_heartbeat(self, status: str | None = None, force: bool = False) -> None:
        if not self._convex.enabled:
            return
        with self._heartbeat_lock:
            current_job_id = self._current_job_id
            current_workflow_key = self._current_workflow_key

        resolved_status = status or ("running" if current_job_id else "idle")
        try:
            self._convex.heartbeat_worker(
                settings.convex_worker_heartbeat_mutation,
                worker_id=settings.worker_id,
                status=resolved_status,
                current_job_id=current_job_id if resolved_status == "running" else None,
                current_workflow_key=current_workflow_key if resolved_status == "running" else None,
                metadata={
                    "comfyBaseUrl": settings.comfy_base_url,
                    "workflowDefaultKey": self._default_workflow_key,
                    "heartbeatIntervalSeconds": settings.worker_heartbeat_interval_seconds,
                    "force": force,
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] heartbeat failed: {exc}")

    def _emit_event(self, job_id: str, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["timestamp"] = time.time()
        try:
            self._convex.append_job_event(settings.convex_append_event_mutation, job_id, payload)
        except Exception as exc:  # noqa: BLE001
            # Event failures should not kill the run.
            print(f"[worker] append_event failed for {job_id}: {exc}")

    def _is_job_canceled(self, job: ClaimedJob) -> bool:
        try:
            status = self._convex.query(
                settings.convex_get_job_status_query,
                {"jobId": job.job_id},
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] could not check cancellation for {job.job_id}: {exc}")
            return False
        return isinstance(status, dict) and status.get("status") == "canceled"

    def _process_claimed_job(self, job: ClaimedJob) -> None:
        with self._heartbeat_lock:
            self._current_job_id = job.job_id
            self._current_workflow_key = job.workflow_key or self._default_workflow_key
        self._send_heartbeat(force=True)
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

            workflow_key = job.workflow_key or self._default_workflow_key

            if workflow_key == "estuches_qwen_comfy":
                self._emit_event(job.job_id, {"type": "qwen_comfy_started"})
                t_build = time.perf_counter()
                workflow, qwen_timings = self._build_qwen_comfy_workflow(job, input_bytes)
                qwen_timings["buildWorkflowMs"] = int(round((time.perf_counter() - t_build) * 1000))

                def relay_event(event: dict[str, Any]) -> None:
                    if event.get("type") == "comfy_ws_message" and event.get("message_type") == "progress":
                        data = event.get("data") if isinstance(event.get("data"), dict) else {}
                        value = data.get("value")
                        maximum = data.get("max")
                        prompt_id = data.get("prompt_id")
                        if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
                            bounded_value = max(0, min(float(value), float(maximum)))
                            self._emit_event(
                                job.job_id,
                                {
                                    "type": "qwen_comfy_progress",
                                    "prompt_id": prompt_id,
                                    "value": bounded_value,
                                    "max": float(maximum),
                                    "percent": round((bounded_value / float(maximum)) * 100),
                                },
                            )
                        return

                    if not self._should_emit_comfy_event(job.job_id, event):
                        return
                    self._emit_event(job.job_id, event)

                t_comfy = time.perf_counter()
                comfy_result = self._comfy.run_prompt_and_get_first_image(
                    workflow,
                    event_callback=relay_event,
                    cancel_check=lambda: self._is_job_canceled(job),
                    timeout_seconds=900,
                )
                qwen_timings["comfyMs"] = int(round((time.perf_counter() - t_comfy) * 1000))

                if self._is_job_canceled(job):
                    self._emit_event(
                        job.job_id,
                        {"type": "qwen_comfy_canceled_after_comfy"},
                    )
                    try:
                        Path(comfy_result.output_file_path).unlink(missing_ok=True)
                    except Exception as cleanup_exc:  # noqa: BLE001
                        print(f"[worker] qwen comfy canceled output cleanup failed for {job.job_id}: {cleanup_exc}")
                    return

                result_w: int | None = None
                result_h: int | None = None
                try:
                    with Image.open(comfy_result.output_file_path) as result_img:
                        result_w = int(result_img.width)
                        result_h = int(result_img.height)
                except Exception as dim_exc:  # noqa: BLE001
                    print(f"[worker] could not read qwen comfy result dimensions for {job.job_id}: {dim_exc}")

                t_upload = time.perf_counter()
                upload = self._convex.upload_file_to_convex(
                    file_path=comfy_result.output_file_path,
                    content_type="image/png",
                    generate_upload_url_mutation=settings.convex_generate_upload_url_mutation,
                )
                qwen_timings["uploadResultMs"] = int(round((time.perf_counter() - t_upload) * 1000))

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
                        print(f"[worker] qwen comfy thumbnail cleanup failed for {job.job_id}: {cleanup_exc}")

                try:
                    Path(comfy_result.output_file_path).unlink(missing_ok=True)
                except Exception as cleanup_exc:  # noqa: BLE001
                    print(f"[worker] qwen comfy result cleanup failed for {job.job_id}: {cleanup_exc}")

                params = job.params if isinstance(job.params, dict) else {}
                result_payload = {
                    "promptId": comfy_result.prompt_id,
                    "filename": comfy_result.output_filename,
                    "subfolder": comfy_result.output_subfolder,
                    "type": comfy_result.output_type,
                    "workerId": settings.worker_id,
                    "workflowKey": workflow_key,
                    "processor": "comfy",
                    "provider": "comfy",
                    "resultWidth": result_w,
                    "resultHeight": result_h,
                    "thumbnailStorageId": thumb_upload["storageId"] if thumb_upload else None,
                    "model": params.get("model"),
                    "comfyModelName": qwen_timings.get("comfyModelName"),
                    "prompt": params.get("prompt"),
                    "seed": params.get("seed"),
                    "timings": qwen_timings,
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
                        "processor": "comfy",
                    },
                )
                return

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

            if workflow_key == "estuches_stage1_resize_image_mask_node":
                self._emit_event(job.job_id, {"type": "stage1_thumbnail_pil_started"})
                result_path, result_w, result_h, stage1_timings = self._process_stage1_thumbnail_pil(
                    job,
                    input_bytes,
                )
                self._emit_event(
                    job.job_id,
                    {
                        "type": "stage1_thumbnail_pil_timing",
                        **stage1_timings,
                    },
                )

                upload = self._convex.upload_file_to_convex(
                    file_path=str(result_path),
                    content_type="image/jpeg",
                    generate_upload_url_mutation=settings.convex_generate_upload_url_mutation,
                )

                try:
                    result_path.unlink(missing_ok=True)
                except Exception as cleanup_exc:  # noqa: BLE001
                    print(f"[worker] stage1 cleanup failed for {job.job_id}: {cleanup_exc}")

                result_payload = {
                    "workerId": settings.worker_id,
                    "workflowKey": workflow_key,
                    "processor": "pil",
                    "mimeType": "image/jpeg",
                    "resultWidth": result_w,
                    "resultHeight": result_h,
                    "timings": stage1_timings,
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
                        "processor": "pil",
                    },
                )
                return

            if workflow_key == "estuches_stage2_crop_fullres":
                self._emit_event(job.job_id, {"type": "stage2_crop_pil_started"})
                result_path, thumb_path, result_w, result_h, stage2_timings = self._process_stage2_crop_pil(
                    job,
                    input_bytes,
                    source_width,
                    source_height,
                )
                self._emit_event(
                    job.job_id,
                    {
                        "type": "stage2_crop_pil_timing",
                        **stage2_timings,
                    },
                )

                upload = self._convex.upload_file_to_convex(
                    file_path=str(result_path),
                    content_type="image/png",
                    generate_upload_url_mutation=settings.convex_generate_upload_url_mutation,
                )
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

                for cleanup_path in (result_path, thumb_path):
                    try:
                        cleanup_path.unlink(missing_ok=True)
                    except Exception as cleanup_exc:  # noqa: BLE001
                        print(f"[worker] stage2 cleanup failed for {job.job_id}: {cleanup_exc}")

                result_payload = {
                    "workerId": settings.worker_id,
                    "workflowKey": workflow_key,
                    "processor": "pil",
                    "resultWidth": result_w,
                    "resultHeight": result_h,
                    "thumbnailStorageId": thumb_upload["storageId"],
                    "originalCropRegion": {
                        "x": stage2_timings["cropX"],
                        "y": stage2_timings["cropY"],
                        "width": stage2_timings["cropWidth"],
                        "height": stage2_timings["cropHeight"],
                    },
                    "timings": stage2_timings,
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
                        "thumbnailStorageId": thumb_upload["storageId"],
                        "processor": "pil",
                    },
                )
                return

            if workflow_key == "estuches_stage3_mask_composite":
                self._emit_event(job.job_id, {"type": "stage3_mask_composite_pil_started"})
                stage3_result = self._process_stage3_mask_composite(job, input_bytes)
                self._emit_event(
                    job.job_id,
                    {
                        "type": "stage3_mask_composite_pil_timing",
                        **stage3_result["timings"],
                    },
                )

                t_upload = time.perf_counter()
                upload_inputs = {
                    key: (path, "image/png")
                    for key, path in stage3_result["paths"].items()
                }
                thumbnail_upload_inputs = {
                    key: (path, "image/jpeg")
                    for key, path in stage3_result["thumbPaths"].items()
                    if path is not None
                }
                uploads = self._upload_files_to_convex_parallel(upload_inputs, max_workers=4)
                thumbnail_uploads = self._upload_files_to_convex_parallel(
                    thumbnail_upload_inputs,
                    max_workers=4,
                )
                upload_ms = int(round((time.perf_counter() - t_upload) * 1000))
                self._emit_event(
                    job.job_id,
                    {
                        "type": "stage3_upload_timing",
                        "uploadMs": upload_ms,
                        "fileCount": len(upload_inputs) + len(thumbnail_upload_inputs),
                    },
                )

                for path in list(stage3_result["paths"].values()) + [
                    p for p in stage3_result["thumbPaths"].values() if p is not None
                ]:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception as cleanup_exc:  # noqa: BLE001
                        print(f"[worker] stage3 cleanup failed for {job.job_id}: {cleanup_exc}")

                result_payload = {
                    "workerId": settings.worker_id,
                    "workflowKey": workflow_key,
                    "processor": "pil",
                    "maskStorageId": uploads["mask"]["storageId"],
                    "qwenCompositeStorageId": (
                        uploads["qwenComposite"]["storageId"] if "qwenComposite" in uploads else None
                    ),
                    "qwenCompositeSourceImageId": stage3_result.get("qwenCompositeSourceImageId"),
                    "qwenCropStorageId": uploads["qwenCrop"]["storageId"] if "qwenCrop" in uploads else None,
                    "qwenCropSourceImageId": (
                        stage3_result.get("qwenCropSourceImageId")
                        or (job.source_image_id if stage3_result.get("reusedSourceCrop") else None)
                    ),
                    "qwenCompositeThumbnailStorageId": (
                        thumbnail_uploads["qwenComposite"]["storageId"]
                        if thumbnail_uploads.get("qwenComposite")
                        else None
                    ),
                    "qwenCropThumbnailStorageId": (
                        thumbnail_uploads["qwenCrop"]["storageId"] if thumbnail_uploads.get("qwenCrop") else None
                    ),
                    "maskBounds": stage3_result["maskBounds"],
                    "timings": stage3_result["timings"],
                    "compositionFingerprint": stage3_result.get("compositionFingerprint"),
                }
                result_storage_id = (
                    uploads["qwenComposite"]["storageId"]
                    if "qwenComposite" in uploads
                    else uploads["mask"]["storageId"]
                )

                self._convex.mark_job_completed(
                    settings.convex_mark_completed_mutation,
                    job_id=job.job_id,
                    result_storage_id=result_storage_id,
                    result=result_payload,
                )
                self._emit_event(
                    job.job_id,
                    {
                        "type": "job_completed",
                        "resultStorageId": result_storage_id,
                        "processor": "pil",
                    },
                )
                return

            workflow_key, template = self._select_template(job)

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

        except ComfyPromptCanceled as exc:
            self._emit_event(
                job.job_id,
                {
                    "type": "comfy_prompt_canceled",
                    "promptId": exc.prompt_id,
                    "mode": exc.mode,
                },
            )
            return
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
            with self._heartbeat_lock:
                if self._current_job_id == job.job_id:
                    self._current_job_id = None
                    self._current_workflow_key = None
            self._send_heartbeat(force=True)
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
