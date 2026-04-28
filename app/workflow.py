from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def load_workflow_template(path: str) -> dict[str, Any]:
    content = Path(path).read_text(encoding="utf-8")
    return json.loads(content)


def _replace_tokens(value: Any, mapping: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: _replace_tokens(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_tokens(v, mapping) for v in value]
    if isinstance(value, str) and value in mapping:
        return mapping[value]
    return value


def build_workflow(
    template: dict[str, Any],
    input_filename: str,
    width: int,
    height: int,
    filename_prefix: str,
    crop_mode: str,
    crop_x: int | None = None,
    crop_y: int | None = None,
    crop_width: int | None = None,
    crop_height: int | None = None,
) -> dict[str, Any]:
    resolved_crop_x = 0 if crop_x is None else crop_x
    resolved_crop_y = 0 if crop_y is None else crop_y
    resolved_crop_width = width if crop_width is None else crop_width
    resolved_crop_height = height if crop_height is None else crop_height

    mapping = {
        "__INPUT_IMAGE__": input_filename,
        "__WIDTH__": width,
        "__HEIGHT__": height,
        "__FILENAME_PREFIX__": filename_prefix,
        "__CROP_MODE__": crop_mode,
        "__CROP_X__": resolved_crop_x,
        "__CROP_Y__": resolved_crop_y,
        "__CROP_WIDTH__": resolved_crop_width,
        "__CROP_HEIGHT__": resolved_crop_height,
    }
    return _replace_tokens(copy.deepcopy(template), mapping)
