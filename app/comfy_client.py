from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests
import websocket


@dataclass
class ComfyResult:
    prompt_id: str
    output_file_path: str
    output_filename: str
    output_subfolder: str
    output_type: str


class ComfyClient:
    def __init__(self, base_url: str, output_dir: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _http(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _ws(self, path: str) -> str:
        return self._http(path).replace("http://", "ws://").replace("https://", "wss://")

    def health(self) -> dict[str, Any]:
        response = requests.get(self._http("/system_stats"), timeout=20)
        response.raise_for_status()
        return response.json()

    def upload_image_bytes(self, image_bytes: bytes, filename: str, folder_type: str = "input") -> dict[str, Any]:
        files = {"image": (filename, BytesIO(image_bytes), "image/png")}
        data = {"type": folder_type}
        response = requests.post(self._http("/upload/image"), files=files, data=data, timeout=120)
        response.raise_for_status()
        return response.json()

    def queue_prompt(self, prompt: dict[str, Any], client_id: str, prompt_id: str) -> dict[str, Any]:
        payload = {"prompt": prompt, "client_id": client_id, "prompt_id": prompt_id}
        response = requests.post(self._http("/prompt"), json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_history(self, prompt_id: str) -> dict[str, Any]:
        response = requests.get(self._http(f"/history/{prompt_id}"), timeout=30)
        response.raise_for_status()
        return response.json()

    def get_image(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        query = urlencode({"filename": filename, "subfolder": subfolder, "type": folder_type})
        response = requests.get(self._http(f"/view?{query}"), timeout=120)
        response.raise_for_status()
        return response.content

    def run_prompt_and_get_first_image(
        self,
        prompt: dict[str, Any],
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        timeout_seconds: int = 600,
    ) -> ComfyResult:
        client_id = str(uuid.uuid4())
        prompt_id = str(uuid.uuid4())

        ws = websocket.WebSocket()
        ws.settimeout(1.0)
        ws.connect(self._ws(f"/ws?clientId={client_id}"))

        if event_callback:
            event_callback({"type": "comfy_ws_connected", "client_id": client_id})

        queue_response = self.queue_prompt(prompt, client_id=client_id, prompt_id=prompt_id)
        if event_callback:
            event_callback({"type": "comfy_prompt_queued", "prompt_id": prompt_id, "queue_response": queue_response})

        started = time.time()

        try:
            while True:
                if time.time() - started > timeout_seconds:
                    raise TimeoutError(f"Comfy execution timeout after {timeout_seconds}s")
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue

                if not isinstance(raw, str):
                    continue

                message = json.loads(raw)
                msg_type = message.get("type")
                data = message.get("data", {})
                if event_callback:
                    event_callback({"type": "comfy_ws_message", "message_type": msg_type, "data": data})

                if msg_type == "execution_error" and data.get("prompt_id") == prompt_id:
                    raise RuntimeError(f"Comfy execution_error: {data}")

                if msg_type == "executing" and data.get("prompt_id") == prompt_id and data.get("node") is None:
                    break
        finally:
            ws.close()

        history = self.get_history(prompt_id)
        run = history.get(prompt_id)
        if not run:
            raise RuntimeError(f"No history found for prompt_id={prompt_id}")

        outputs = run.get("outputs", {})
        for node_id, node_output in outputs.items():
            images = node_output.get("images")
            if not images:
                continue
            image = images[0]
            filename = image["filename"]
            subfolder = image.get("subfolder", "")
            image_type = image.get("type", "output")
            image_bytes = self.get_image(filename, subfolder, image_type)

            output_path = self.output_dir / f"{prompt_id}_{node_id}_{filename}"
            output_path.write_bytes(image_bytes)

            if event_callback:
                event_callback(
                    {
                        "type": "comfy_image_saved",
                        "prompt_id": prompt_id,
                        "node_id": node_id,
                        "path": str(output_path),
                    }
                )

            return ComfyResult(
                prompt_id=prompt_id,
                output_file_path=str(output_path),
                output_filename=filename,
                output_subfolder=subfolder,
                output_type=image_type,
            )

        raise RuntimeError("Comfy finished but no image outputs were found in history")
