# Plan de implementacion: microservicio Python (Convex client) + ComfyUI

Fecha: 2026-04-27
Alcance inicial: generar thumbnail de una imagen usando ComfyUI local y devolver resultado a la app principal (Convex + Next.js).

Actualizacion:

- Esta version refleja el plan inicial.
- La arquitectura vigente para integracion es `worker pull` (ver `docs/main-app-integration-guide.md`).

## 1) Objetivo

Construir un microservicio Python en esta carpeta que:

1. Reciba una solicitud desde la app principal con referencia de imagen (preferido: URL de Convex Storage).
2. Procese la imagen en ComfyUI (local) con un workflow API exportado.
3. Recupere el resultado (thumbnail u otra transformacion).
4. Guarde el resultado nuevamente en Convex Storage.
5. Actualice metadata/estado en Convex para que Next.js se actualice en tiempo real.

## 2) Hallazgos clave de investigacion (internet)

## Convex (oficial)

- El cliente Python oficial es `convex` (repo oficial: `get-convex/convex-py`).
- El `ConvexClient` soporta `query`, `mutation`, `action`, `subscribe`, `set_auth`, `set_admin_auth`, `clear_auth`.
- Existe cliente HTTP legacy (`ConvexHttpClient`), pero el cliente principal es WebSocket-based y soporta suscripciones.
- File Storage recomendado para archivos grandes:
  1. mutation -> `storage.generateUploadUrl()`
  2. `POST` binario al upload URL
  3. guardar `storageId` en DB con mutation
- Para archivos generados en backend, Convex recomienda `storage.store(...)` desde action/http action, y/o flujo de upload URL si sube un servicio externo.
- `storage.getUrl(storageId)` devuelve URL para servir imagenes.
- Scheduled functions: ideal para flujo async durable (mutation encola trabajo, worker procesa, mutation finaliza). Auth no se propaga automaticamente al scheduled function.

## Convex HTTP / Auth

- Las funciones Convex tambien pueden llamarse por HTTP (`/api/query`, `/api/mutation`, `/api/action`, `/api/run/...`).
- Auth por header `Authorization: Bearer <token>` para contexto usuario; `Authorization: Convex <deploy-key>` para permisos admin.
- HTTP Actions en `.convex.site` son utiles para webhooks/public API, pero tienen limite de request/response de 20MB.

## ComfyUI (oficial)

- Rutas relevantes documentadas:
  - `POST /upload/image`
  - `POST /prompt`
  - `GET /history/{prompt_id}`
  - `GET /view?filename=...&subfolder=...&type=...`
  - `GET/POST /queue`, `POST /interrupt`
  - `GET /ws` WebSocket para progreso
- Comfy envia eventos WS como `execution_start`, `executing`, `progress`, `execution_success`, `execution_error`, `status`.
- Patron robusto recomendado por ejemplos oficiales:
  1. abrir WS con `clientId`
  2. enviar `prompt` por HTTP
  3. esperar mensaje `executing` con `node = null` y `prompt_id` correspondiente
  4. leer outputs en `/history/{prompt_id}`
  5. descargar imagenes por `/view`
- Existen ejemplos con `SaveImageWebsocket` para recibir bytes por WS, pero para primera version es mas estable el camino `/history + /view`.

## 3) Decision arquitectonica recomendada

## Recomendacion principal (v1)

Usar flujo async orientado a jobs con URL de imagen (no base64) y Convex Storage como fuente de verdad.

Razon:

- Evita payloads gigantes entre Next -> Python.
- Simplifica reintentos e idempotencia.
- Aprovecha tiempo real de Convex (`useQuery`) para reflejar estados.
- Escala mejor a mas workflows (thumbnail, upscale, inpaint, etc.).

## Flujo end-to-end recomendado

1. Next sube imagen original a Convex Storage (o ya existe).
2. Next invoca mutation `comfyJobs:create` con `inputStorageId` y parametros (`workflow`, `targetWidth`, etc.).
3. Esa mutation guarda job `queued` y devuelve `jobId`.
4. Worker Python (este proyecto) detecta jobs pendientes (polling query/subscription) o recibe webhook interno.
5. Worker obtiene URL temporal de entrada (query/mutation Convex que derive `storageId -> url`).
6. Worker descarga imagen y la envia a ComfyUI.
7. Worker espera completion (WS preferido; polling de respaldo).
8. Worker descarga imagen resultado.
9. Worker pide `generateUploadUrl` por mutation y sube bytes resultado -> obtiene `outputStorageId`.
10. Worker marca job `completed` con metadata (duracion, prompt_id, nodo salida, etc.).
11. Next observa estado del job y renderiza thumbnail con `storage.getUrl`.

## 4) Contratos API propuestos

## 4.1 App principal (Convex) -> Python microservicio

Endpoint interno del microservicio (si decides push desde Convex/Next):

`POST /v1/jobs/thumbnail`

Payload:

```json
{
  "jobId": "<convex-id>",
  "inputStorageId": "<storage-id>",
  "workflow": "thumbnail-v1",
  "transform": {
    "width": 256,
    "height": 256,
    "fit": "cover"
  },
  "requestId": "uuid-para-idempotencia"
}
```

Headers:

- `Authorization: Bearer <service-token-o-hmac>`
- `X-Request-Id: <uuid>`

Alternativa pull (recomendada al inicio):

- Python no recibe jobs por HTTP.
- Python consulta Convex (`comfyJobs:listPending`) y procesa.

## 4.2 Python -> Convex

Funciones sugeridas en Convex:

- `comfyJobs:create` (mutation publica)
- `comfyJobs:listPending` (query interna, con auth de servicio)
- `comfyJobs:markRunning` (mutation interna)
- `files:generateUploadUrl` (mutation)
- `comfyJobs:markCompleted` (mutation interna)
- `comfyJobs:markFailed` (mutation interna)
- `images:createFromComfy` (mutation interna opcional si separas tabla)

Notas de auth:

- En Python usar `ConvexClient.set_auth(token)` cuando quieras contexto de usuario/servicio con reglas de negocio.
- Si necesitas funciones internas administrativas, evaluar `set_admin_auth(deployKey)` con extremo cuidado (solo backend protegido).

## 4.3 Python -> ComfyUI

Secuencia v1:

1. `POST /upload/image` (multipart/form-data) para imagen de entrada.
2. `POST /prompt` con workflow API JSON exportado (`File -> Export (API)` en ComfyUI).
3. Esperar fin por WS (`/ws?clientId=...`) escuchando `executing` con `node=null` para ese `prompt_id`.
4. `GET /history/{prompt_id}` para localizar outputs.
5. `GET /view?...` para descargar imagen final.

Fallback:

- Si WS falla, usar polling a `/history/{prompt_id}` cada N segundos con timeout global.

## 5) Modelo de datos recomendado en Convex

Tabla `comfyJobs`:

- `_id`
- `status`: `queued | running | completed | failed | canceled`
- `kind`: `thumbnail`
- `inputStorageId`: `Id<"_storage">`
- `outputStorageId?`: `Id<"_storage">`
- `params`: `{ width, height, fit, workflowVersion }`
- `promptId?`: `string`
- `error?`: `{ code, message, retryable }`
- `attempt`: `number`
- `createdBy`: `Id<"users"> | "service"`
- `createdAt`, `startedAt?`, `finishedAt?`
- `requestId`: `string` (idempotencia)

Indice sugerido:

- por `status`
- por `requestId` unico (evita duplicados)
- por `createdAt`

## 6) Estrategia de workflows Comfy

## v1 thumbnail estable

Crear un workflow simple de resize/thumbnail con nodos de carga -> resize -> save output.

Requisitos:

- Exportar JSON API del workflow y versionarlo (`thumbnail-v1.json`).
- Parametrizar width/height en nodos destino.
- Definir claramente el nodo de salida esperado para poder leerlo en `history.outputs`.

## Versionado

- `thumbnail-v1`, `thumbnail-v2` ...
- Guardar version en `comfyJobs.params.workflowVersion`.
- Evitar cambios breaking sin migracion.

## 7) Confiabilidad, reintentos e idempotencia

- Cada job debe tener `requestId` unico generado por la app.
- Reintentos del worker solo para errores transitorios:
  - timeout Comfy
  - conexion rechazada
  - error 5xx
- No reintentar automaticamente errores de validacion de workflow o parametros.
- Definir `maxAttempts` (ej. 3).
- Implementar lock de procesamiento (`markRunning` atomico con compare de estado).
- Guardar `prompt_id` de Comfy para trazabilidad.

## 8) Seguridad

- No exponer ComfyUI a internet; mantenerlo local/VPN/red privada.
- Microservicio con token de servicio (o firma HMAC) para llamadas entrantes.
- Si usas `set_admin_auth`, almacenar deploy key en entorno seguro y restringir funciones internas.
- Validar tipo/tamano MIME al descargar/subir imagenes.
- Sanitizar parametros de workflow; no aceptar JSON arbitrario desde cliente final en v1.

## 9) Observabilidad

- Correlation IDs: `requestId`, `jobId`, `prompt_id` en todos los logs.
- Metricas minimas:
  - jobs por estado
  - latencia total por job
  - latencia Comfy
  - tasa de error por tipo
- Health checks del microservicio:
  - `GET /healthz` (app)
  - `GET /readyz` (app + conectividad Convex + Comfy opcional)

## 10) Plan por fases

## Fase 0: Alineacion y contratos

1. Definir schema Convex (`comfyJobs`, `images` si aplica).
2. Definir funciones Convex minimas (create, listPending, markRunning, markCompleted, markFailed, generateUploadUrl).
3. Cerrar contrato de payload `thumbnail`.

Criterio de salida: contratos congelados y validados por frontend + backend.

## Fase 1: Worker Python basico (pull)

1. Inicializar servicio Python con `convex` + `requests` + `websocket-client` (o `websockets`).
2. Implementar loop:
   - leer job pendiente
   - marcar running
   - descargar input
   - ejecutar Comfy
   - subir output a Convex
   - marcar completed/failed
3. Manejo de errores y timeouts.

Criterio de salida: procesa 1 job thumbnail end-to-end de forma estable.

## Fase 2: Robustez

1. Reintentos con backoff.
2. Idempotencia por `requestId`.
3. Locks atomicos para evitar doble procesamiento.
4. Logs estructurados + metricas basicas.

Criterio de salida: tolera fallos transitorios sin duplicar resultados.

## Fase 3: Integracion UX en Next/Convex

1. Pantalla/lista de jobs con estado realtime.
2. Render de thumbnail al completar.
3. Mensajes de error recuperables.

Criterio de salida: experiencia de usuario no bloqueante.

## Fase 4: Escalado funcional

1. Multiples workflows (upscale, crop inteligente, etc.).
2. Control de concurrencia por worker.
3. Priorizacion de cola.

Criterio de salida: pipeline multiproposito mantenible.

## 11) Pruebas recomendadas

## Unitarias

- Mapping de payload -> workflow params.
- Parser de `history` de Comfy.
- Clasificador de errores retryable/non-retryable.

## Integracion local

- Convex dev + Comfy local levantados.
- Caso feliz thumbnail.
- Caso timeout Comfy.
- Caso imagen invalida.

## End-to-end

- Next crea job -> worker procesa -> Next muestra thumbnail.
- Reintento controlado tras falla transitoria.

## 12) Riesgos y mitigaciones

- Workflow Comfy cambia y rompe parsing:
  - Mitigar con versionado estricto de workflow y tests snapshot de `history`.
- Jobs duplicados por reintentos:
  - Mitigar con `requestId` unico + transiciones atomicas de estado.
- Bloqueos por latencia alta de inferencia:
  - Mitigar con async jobs y polling/suscripcion en UI.
- Uso excesivo de privilegios Convex:
  - Mitigar separando funciones publicas/internas y minimizando uso de admin key.

## 13) Recomendacion final para tu caso

Para este proyecto (`comfy-convex`) empieza con:

1. Flujo async con tabla `comfyJobs`.
2. Input por `inputStorageId` (no base64 desde frontend).
3. Worker Python pull de jobs pendientes.
4. Integracion Comfy por `/upload/image` + `/prompt` + WS + `/history` + `/view`.
5. Output siempre a Convex Storage y retorno de `outputStorageId`.

Con eso obtienes una base limpia para microservicio, buena UX en Next y un camino claro para escalar a mas transformaciones.

## 14) Fuentes investigadas (internet)

- Convex docs: Python quickstart, client/python, file storage (upload/store/serve/metadata), functions/actions, HTTP API, HTTP actions, scheduled functions, auth.
- Convex repo oficial Python: `https://github.com/get-convex/convex-py` + `README` y API expuesta (`ConvexClient`, `ConvexHttpClient`).
- ComfyUI docs server: `comms_overview`, `comms_messages`, `comms_routes`.
- ComfyUI ejemplos oficiales (raw):
  - `script_examples/basic_api_example.py`
  - `script_examples/websockets_api_example.py`
  - `script_examples/websockets_api_example_ws_images.py`
- ComfyUI server routes en `server.py` (referencia de endpoints y comportamiento).
