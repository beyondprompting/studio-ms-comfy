# Comfy Convex Worker + Microservice

Proyecto Python para procesar imagenes en ComfyUI con dos modos:

1. `worker pull` (recomendado): escucha Convex, toma jobs `pending`, procesa y actualiza estado en Convex.
2. `api debug`: expone endpoints HTTP/SSE/WS para pruebas locales manuales.

## Arquitectura recomendada

La app principal (Convex + Next) NO llama directo al microservicio.
En su lugar:

1. App principal crea job en tabla Convex.
2. Worker Python hace `claim` del job.
3. Worker procesa con ComfyUI.
4. Worker sube resultado a Convex Storage.
5. Worker marca job `completed/failed`.
6. Next escucha cambios en Convex en tiempo real.

## Requisitos

- Python 3.10+
- ComfyUI corriendo (ej. `http://127.0.0.1:8188`)
- Convex configurado (para worker pull)

## Instalacion

```bash
cd /home/lucas/clients/avatar/estuches/comfy-convex
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Variables de entorno

Basicas:

- `COMFY_BASE_URL` (default `http://127.0.0.1:8188`)
- `OUTPUT_DIR` (default `./output`)
- `WORKFLOW_TEMPLATES_DIR` (default `./workflows`)
- `WORKFLOW_DEFAULT_KEY` (default `estuches_stage1_resize_image_mask_node`)
- `WORKFLOW_TEMPLATES_JSON` (opcional, mapa JSON key->path)
- `WORKFLOW_TEMPLATE_PATH` (fallback legacy para single-workflow)

Convex:

- `CONVEX_URL` (requerido para worker pull)
- `CONVEX_ADMIN_KEY` (requerido en tu setup)

Worker pull:

- `WORKER_ID` (default `comfy-worker-1`)
- `WORKER_POLL_INTERVAL_SECONDS` (default `1.0`)
- `CONVEX_CLAIM_JOB_MUTATION` (default `thumbnailJobs:claimNextPendingJob`)
- `CONVEX_APPEND_EVENT_MUTATION` (default `thumbnailJobs:appendEvent`)
- `CONVEX_MARK_COMPLETED_MUTATION` (default `thumbnailJobs:markCompleted`)
- `CONVEX_MARK_FAILED_MUTATION` (default `thumbnailJobs:markFailed`)
- `CONVEX_GENERATE_UPLOAD_URL_MUTATION` (default `files:generateUploadUrl`)

## Ejecutar modo worker pull (recomendado)

```bash
cd /home/lucas/clients/avatar/estuches/comfy-convex
source .venv/bin/activate
python run_worker.py
```

## Ejecutar modo API debug local

```bash
cd /home/lucas/clients/avatar/estuches/comfy-convex
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 9000
```

Si quieres iniciar API + worker en el mismo proceso (opcional):

```bash
export WORKER_ENABLED=true
uvicorn app.main:app --host 127.0.0.1 --port 9000
```

## Contrato esperado del claim en Convex

La mutation `CONVEX_CLAIM_JOB_MUTATION` debe devolver `null` o un objeto con:

```json
{
  "jobId": "...",
  "sourceImageUrl": "https://...",
  "workflowKey": "estuches_stage1_resize_image_mask_node",
  "width": 512,
  "height": 512,
  "crop": "center",
  "cropRegion": { "x": 0, "y": 0, "width": 512, "height": 512 },
  "requestId": "opcional"
}
```

El worker usa:

1. `CONVEX_APPEND_EVENT_MUTATION`
2. `CONVEX_GENERATE_UPLOAD_URL_MUTATION`
3. `CONVEX_MARK_COMPLETED_MUTATION`
4. `CONVEX_MARK_FAILED_MUTATION`

Para `estuches_stage2_crop_fullres`, el worker usa `cropRegion` para inyectar
`__CROP_X__`, `__CROP_Y__`, `__CROP_WIDTH__`, `__CROP_HEIGHT__`.

## JSON de workflow Comfy

Usa export API (`File -> Export (API)`) y placeholders:

- `__INPUT_IMAGE__`
- `__WIDTH__`
- `__HEIGHT__`
- `__FILENAME_PREFIX__`
- `__CROP_MODE__`

Workflows soportados por default en este repo:

- `estuches_stage1_resize_image_mask_node`
- `estuches_stage2_crop_fullres`
- `estuches_stage3_mask_composite`
- `estuches_stage4_reimplant_feather`
- `estuches_stage5_remove_bg_template`

El worker selecciona el workflow por `workflowKey` del job y usa `WORKFLOW_DEFAULT_KEY` si no viene informado.

## Estructura

- `app/convex_pull_worker.py`: worker pull principal
- `app/convex_client.py`: bridge Convex
- `app/comfy_client.py`: cliente ComfyUI
- `app/workflow.py`: placeholders sobre workflow API
- `run_worker.py`: entrypoint worker
- `app/main.py`: API debug local

## Docs

- `docs/main-app-integration-guide.md`: guia principal para integrar con Convex + Next
- `docs/curl-test-playbook.md`: pruebas curl en modo API debug
- `docs/worker-pull-test-playbook.md`: pruebas del flujo final pull worker + Convex
- `docs/implementation-plan.md`: plan historico
