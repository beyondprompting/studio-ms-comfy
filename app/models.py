from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class ThumbnailRequest(BaseModel):
    image_url: HttpUrl
    width: int = Field(default=256, ge=32, le=4096)
    height: int = Field(default=256, ge=32, le=4096)
    crop: str = Field(default="center")
    request_id: str | None = None


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobState(BaseModel):
    job_id: str
    status: JobStatus
    created_at: float
    updated_at: float
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    events: list[dict[str, Any]]


class ConvexUploadRequest(BaseModel):
    local_file_path: str
    content_type: str = "image/png"
    metadata: dict[str, Any] = Field(default_factory=dict)
