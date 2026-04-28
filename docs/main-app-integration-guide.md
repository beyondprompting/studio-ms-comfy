# Guia de integracion oficial (app principal Convex + Next -> worker Python pull)

Fecha: 2026-04-27
Objetivo: integrar el procesamiento de thumbnails sin que Convex cloud tenga que llamar al microservicio por red directa.
Audiencia: otro LLM sin contexto previo del proyecto.

## 1. Resumen ejecutivo

La arquitectura correcta para este proyecto es `pull worker`:

1. La app principal en Convex crea jobs en una tabla (`thumbnailJobs`) con estado `pending`.
2. El worker Python (este repo) consulta Convex constantemente y hace `claim` atomico del siguiente job pendiente.
3. El worker procesa en ComfyUI local/remoto.
4. El worker sube la imagen final a Convex Storage.
5. El worker actualiza el estado del job en Convex (`completed` o `failed`) y agrega eventos de progreso.
6. Next.js solo escucha Convex en tiempo real (queries), no el microservicio.

Esto elimina el problema de conectividad Convex cloud -> VPN privada.

## 2. Topologia de red y por que funciona

Escenario real:

1. Tu app principal (Next) corre en tu computadora.
2. Convex corre en la nube.
3. Worker Python + ComfyUI corren en maquina remota por VPN.

Por que este modelo evita bloqueos de red:

- Tu worker SI puede salir a internet para hablar con Convex (`CONVEX_URL`).
- Convex NO necesita entrar a tu VPN para ejecutar jobs.
- Tu frontend tampoco necesita hablar con la maquina remota.

## 3. Responsabilidades por capa

## 3.1 App principal (Convex + Next)

- Encola jobs (`pending`).
- Muestra estado/progreso en UI consultando Convex.
- Nunca llama al worker por HTTP en la arquitectura final.

## 3.4 Datos exactos que debe enviar la app principal

La app principal (Next -> mutation Convex) debe encolar un job con estos datos minimos:

```ts
{
  sourceStorageId: Id<"_storage">,
  workflowKey?: string,
  width: number,
  height: number,
  crop: string, // ej: "center" o "disabled"
  cropRegion?: {
    x: number,
    y: number,
    width: number,
    height: number,
  },
  requestId?: string // recomendado para idempotencia
}
```

Ejemplo recomendado para preview manteniendo proporcion:

```ts
{
  sourceStorageId,
  workflowKey: "estuches_stage1_resize_image_mask_node",
  width: 512,
  height: 512,
  crop: "disabled",
}
```

Nota importante:

- El worker no usa `sourceStorageId` directamente.
- Tu mutation de `claim` debe convertir ese `sourceStorageId` a `sourceImageUrl` (con `ctx.storage.getUrl`) para devolver el contrato que el worker espera.

## 3.2 Worker Python (este proyecto)

- Hace claim atomico de jobs pendientes.
- Descarga imagen fuente.
- Ejecuta workflow en ComfyUI.
- Sube resultado a Convex Storage.
- Marca `completed/failed` y emite eventos.

## 3.3 ComfyUI

- Ejecuta workflow API.
- Produce imagen final.

## 4. Contrato Convex requerido para que el worker funcione

El worker usa mutations configurables por variables de entorno. Defaults actuales:

1. `thumbnailJobs:claimNextPendingJob`
2. `thumbnailJobs:appendEvent`
3. `thumbnailJobs:markCompleted`
4. `thumbnailJobs:markFailed`
5. `files:generateUploadUrl`

## 4.1 Contrato de claim (OBLIGATORIO)

`claimNextPendingJob(workerId)` debe devolver:

- `null` si no hay trabajo.
- o un objeto con:

```json
{
  "jobId": "<id-job-convex>",
  "sourceImageUrl": "https://...",
  "workflowKey": "estuches_stage1_resize_image_mask_node",
  "width": 512,
  "height": 512,
  "crop": "center",
  "cropRegion": {"x": 10, "y": 20, "width": 300, "height": 300},
  "requestId": "opcional"
}
```

Notas:

- `sourceImageUrl` debe ser descargable por el worker.
- `claim` debe ser atomico para evitar doble procesamiento.
- Para `estuches_stage2_crop_fullres`, enviar `cropRegion` evita recortes fijos en `0,0`.

## 4.5 Documento minimo recomendado en `thumbnailJobs`

Al encolar desde app principal, el documento inicial recomendado es:

```json
{
  "status": "pending",
  "sourceStorageId": "<id_storage>",
  "request": {
    "width": 512,
    "height": 512,
    "crop": "disabled",
    "requestId": "uuid-opcional"
  },
  "events": [],
  "attempt": 0,
  "maxAttempts": 3,
  "createdAt": 1714220000000,
  "updatedAt": 1714220000000
}
```

## 4.2 Contrato de append event

`appendEvent({ jobId, event })`

`event` es JSON libre. Se recomienda incluir:

- `type`
- `timestamp`
- `message` opcional
- `data` opcional

## 4.3 Contrato de markCompleted

`markCompleted({ jobId, resultStorageId, result })`

- `resultStorageId`: id de `_storage` en Convex.
- `result`: metadata (promptId, filename, etc).

## 4.4 Contrato de markFailed

`markFailed({ jobId, error })`

`error` recomendado:

```json
{
  "message": "...",
  "type": "ExceptionClass",
  "workerId": "..."
}
```

## 5. Schema recomendado para tabla thumbnailJobs

Campos recomendados:

1. `status`: `pending | running | completed | failed | canceled`
2. `sourceStorageId` (o referencias equivalentes)
3. `resultStorageId` opcional
4. `request`: `{ width, height, crop, requestId }`
5. `workerId` opcional
6. `events`: array de eventos (ultimo N)
7. `error` opcional
8. `attempt`, `maxAttempts` opcional
9. `createdAt`, `updatedAt`, `startedAt`, `finishedAt`

Indices recomendados:

1. por `status`
2. por `createdAt`
3. por `request.requestId` (idempotencia)

## 6. Flujo de estados recomendado

Transiciones validas:

1. `pending -> running`
2. `running -> completed`
3. `running -> failed`
4. `pending/running -> canceled` (si aplica)

Regla:

- Nunca volver de estado terminal (`completed/failed/canceled`) a no terminal.

## 7. Variables de entorno del worker

Basicas:

1. `CONVEX_URL`
2. `CONVEX_ADMIN_KEY`
3. `COMFY_BASE_URL` (ej. `http://127.0.0.1:8188`)
4. `WORKFLOW_TEMPLATES_DIR`
5. `WORKFLOW_DEFAULT_KEY`
6. `WORKFLOW_TEMPLATES_JSON` (opcional)
7. `WORKER_ID`
8. `WORKER_POLL_INTERVAL_SECONDS`

Workflow keys disponibles hoy:

1. `estuches_stage1_resize_image_mask_node`
2. `estuches_stage2_crop_fullres`
3. `estuches_stage3_mask_composite`
4. `estuches_stage4_reimplant_feather`
5. `estuches_stage5_remove_bg_template`

Paths de funciones Convex (si cambian nombres):

1. `CONVEX_CLAIM_JOB_MUTATION`
2. `CONVEX_APPEND_EVENT_MUTATION`
3. `CONVEX_MARK_COMPLETED_MUTATION`
4. `CONVEX_MARK_FAILED_MUTATION`
5. `CONVEX_GENERATE_UPLOAD_URL_MUTATION`

## 8. Como levantar worker en produccion/dev

Comando recomendado:

```bash
cd /home/lucas/clients/avatar/estuches/comfy-convex
source .venv/bin/activate
python run_worker.py
```

Opcional para debug HTTP local:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 9000
```

## 9. Integracion en Next.js (frontend)

Frontend debe hacer solo esto:

1. Usuario hace click en "generar thumbnail".
2. Llamar mutation Convex `enqueueThumbnailJob(...)`.
3. Suscribirse/query al job (`getJob(jobId)`).
4. Mostrar progreso segun `status` y `events`.
5. Cuando `status=completed`, renderizar `resultUrl` (derivada de `resultStorageId`).

No consumir SSE/WS del microservicio desde browser para flujo final.

## 10. Checklist para otro LLM (orden sugerido)

1. Crear/ajustar tabla `thumbnailJobs` + indices.
2. Implementar `claimNextPendingJob` atomico.
3. Implementar `appendEvent`, `markCompleted`, `markFailed`.
4. Implementar mutation publica `enqueueThumbnailJob`.
5. Exponer query `getJob` y `listJobs` para UI.
6. Configurar env vars del worker.
7. Levantar worker y validar con 1 job real.
8. Conectar UI Next al estado Convex.
9. Agregar retries/lease si hace falta.

## 11. Criterios de aceptacion

Integracion correcta cuando:

1. Al encolar job en Convex, el worker lo toma automaticamente.
2. Convex refleja eventos de progreso mientras corre Comfy.
3. La imagen final termina en Convex Storage.
4. Job queda `completed` con metadata de resultado.
5. Next renderiza preview final sin contactar directo al worker.

## 11.1 Que deberias esperar al crear el documento en la tabla

Cuando la app principal crea un nuevo job en `thumbnailJobs`, el comportamiento esperado es:

1. Estado inicial: `pending`.
2. En 1-3 segundos (segun poll interval), el worker hace `claim` y pasa a `running`.
3. Se agregan eventos: `job_claimed`, `input_downloaded`, `comfy_input_uploaded`, `comfy_ws_message...`.
4. Si todo sale bien:
  - se sube resultado a Convex Storage,
  - se guarda `resultStorageId`,
  - estado final `completed`.
5. Si algo falla:
  - estado final `failed`,
  - campo `error` con causa,
  - evento `job_failed`.

Timeout orientativo de una corrida:

- thumbnails simples: normalmente segundos.
- si supera varios minutos, revisar eventos y conectividad Comfy.

## 12. Errores comunes

1. `claim` no atomico -> doble procesamiento.
2. `sourceImageUrl` no accesible desde worker.
3. Workflow Comfy exportado en formato no API.
4. No guardar `resultStorageId` en `markCompleted`.
5. Frontend intentando hablar directo con microservicio en vez de Convex.
