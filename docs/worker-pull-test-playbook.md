# Worker Pull Test Playbook

Fecha: 2026-04-27
Objetivo: validar el flujo final donde el worker Python toma jobs desde Convex y devuelve resultados a Convex Storage.

## 1. Prerrequisitos

1. ComfyUI levantado en la maquina del worker.
2. Worker repo instalado:

   cd /home/lucas/clients/avatar/estuches/comfy-convex
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

3. Convex deployment accesible (dev o prod).
4. Funciones Convex implementadas:
   - `thumbnailJobs:claimNextPendingJob`
   - `thumbnailJobs:appendEvent`
   - `thumbnailJobs:markCompleted`
   - `thumbnailJobs:markFailed`
   - `files:generateUploadUrl`

## 2. Configurar variables de entorno

Archivos listos creados en este repo:

- `.env.worker.dev`
- `.env.worker.prod`

Edita uno de ellos segun entorno y completa valores reales.

Ejemplo:

export CONVEX_URL="https://TU_DEPLOYMENT.convex.cloud"
export CONVEX_ADMIN_KEY="DEPLOY_KEY"

export COMFY_BASE_URL="http://127.0.0.1:8188"
export WORKFLOW_TEMPLATE_PATH="/home/lucas/clients/avatar/estuches/comfy-convex/workflows/thumbnail_api_template.json"

export WORKER_ID="comfy-worker-1"
export WORKER_POLL_INTERVAL_SECONDS="1.0"

# si tus funciones tienen otros nombres, sobreescribe:
# export CONVEX_CLAIM_JOB_MUTATION="miModulo:claim"
# export CONVEX_APPEND_EVENT_MUTATION="miModulo:appendEvent"
# export CONVEX_MARK_COMPLETED_MUTATION="miModulo:markCompleted"
# export CONVEX_MARK_FAILED_MUTATION="miModulo:markFailed"
# export CONVEX_GENERATE_UPLOAD_URL_MUTATION="files:generateUploadUrl"

Carga recomendada desde archivo (dev):

```bash
cd /home/lucas/clients/avatar/estuches/comfy-convex
source .venv/bin/activate
set -a
source .env.worker.dev
set +a
```

Carga recomendada desde archivo (prod):

```bash
cd /home/lucas/clients/avatar/estuches/comfy-convex
source .venv/bin/activate
set -a
source .env.worker.prod
set +a
```

## 3. Levantar el worker

Despues de cargar el archivo `.env` correspondiente:

python run_worker.py

Esperado en stdout:

- `[worker] started` con workerId y mutation de claim.

## 4. Crear un job en Convex desde tu app principal

Desde tu mutation `enqueueThumbnailJob` (o equivalente), crea un registro `pending` con:

- `sourceImageUrl` o info para que `claim` devuelva una URL descargable.
- `width`, `height`, `crop`.

Nota:

- El contrato de `claim` esta detallado en `docs/main-app-integration-guide.md`.

## 5. Validar procesamiento

Revisar en Convex que el job pase por:

1. `pending`
2. `running`
3. `completed` o `failed`

Revisar que `events` se llenen con:

- `job_claimed`
- `input_downloaded`
- `comfy_input_uploaded`
- eventos `comfy_ws_message`
- `job_completed` o `job_failed`

## 6. Validar resultado final

Cuando `completed`:

1. Verificar `resultStorageId` en la tabla.
2. Obtener URL de storage desde query Convex (`ctx.storage.getUrl`).
3. Abrir imagen y confirmar thumbnail correcto.

## 7. Troubleshooting rapido

1. El worker no toma jobs:
   - revisar `CONVEX_URL`/auth.
   - revisar nombre de mutation de claim.
   - revisar que existan jobs `pending`.
2. Falla en descarga de imagen:
   - `sourceImageUrl` no accesible desde la maquina del worker.
3. Falla en Comfy:
   - validar workflow API exportado y nodos de salida.
4. Falla al cerrar como completed:
   - revisar `files:generateUploadUrl` y permisos de mutation.
