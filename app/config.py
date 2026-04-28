from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    comfy_base_url: str = os.getenv("COMFY_BASE_URL", "http://127.0.0.1:8188")
    output_dir: str = os.getenv("OUTPUT_DIR", "./output")
    workflow_template_path: str = os.getenv(
        "WORKFLOW_TEMPLATE_PATH", "./workflows/thumbnail_api_template.json"
    )
    workflow_templates_dir: str = os.getenv("WORKFLOW_TEMPLATES_DIR", "./workflows")
    workflow_default_key: str = os.getenv(
        "WORKFLOW_DEFAULT_KEY", "estuches_stage1_resize_image_mask_node"
    )
    workflow_templates_json: str = os.getenv("WORKFLOW_TEMPLATES_JSON", "")
    default_width: int = int(os.getenv("DEFAULT_THUMB_WIDTH", "256"))
    default_height: int = int(os.getenv("DEFAULT_THUMB_HEIGHT", "256"))
    convex_url: str | None = os.getenv("CONVEX_URL")
    convex_auth_token: str | None = os.getenv("CONVEX_AUTH_TOKEN")
    convex_admin_key: str | None = os.getenv("CONVEX_ADMIN_KEY")
    worker_enabled: bool = os.getenv("WORKER_ENABLED", "false").lower() == "true"
    worker_poll_interval_seconds: float = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "1.0"))
    worker_id: str = os.getenv("WORKER_ID", "comfy-worker-1")

    convex_claim_job_mutation: str = os.getenv(
        "CONVEX_CLAIM_JOB_MUTATION", "thumbnailJobs:claimNextPendingJob"
    )
    convex_append_event_mutation: str = os.getenv(
        "CONVEX_APPEND_EVENT_MUTATION", "thumbnailJobs:appendEvent"
    )
    convex_mark_completed_mutation: str = os.getenv(
        "CONVEX_MARK_COMPLETED_MUTATION", "thumbnailJobs:markCompleted"
    )
    convex_mark_failed_mutation: str = os.getenv(
        "CONVEX_MARK_FAILED_MUTATION", "thumbnailJobs:markFailed"
    )
    convex_generate_upload_url_mutation: str = os.getenv(
        "CONVEX_GENERATE_UPLOAD_URL_MUTATION", "files:generateUploadUrl"
    )


settings = Settings()
