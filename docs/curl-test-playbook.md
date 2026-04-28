# Curl Test Playbook

Fecha: 2026-04-27
Objetivo: validar el microservicio en modo API debug con curl.

Nota importante:

- La arquitectura final recomendada para integracion con la app principal es `worker pull` contra Convex.
- Este playbook de curl se mantiene para pruebas locales del pipeline ComfyUI/thumbnail.

## 1. Prerrequisitos

1. ComfyUI corriendo local en http://127.0.0.1:8188.
2. Este microservicio instalado en:
   - /home/lucas/clients/avatar/estuches/comfy-convex
3. Dependencias instaladas (incluye soporte WS en uvicorn):

   cd /home/lucas/clients/avatar/estuches/comfy-convex
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

## 2. Que hacer con el JSON exportado de Comfy

## Opcion A (recomendada para primera prueba)

Usar el template ya incluido:
- /home/lucas/clients/avatar/estuches/comfy-convex/workflows/thumbnail_api_template.json

No necesitas cambiar nada para la primera validacion.

## Opcion B (tu propio workflow exportado desde ComfyUI)

1. En ComfyUI arma el workflow que funcione visualmente.
2. Exporta con File -> Export (API).
3. Guarda el JSON en:
   - /home/lucas/clients/avatar/estuches/comfy-convex/workflows/mi_thumbnail.json
4. Reemplaza en tu JSON los valores de entrada por placeholders:
   - __INPUT_IMAGE__
   - __WIDTH__
   - __HEIGHT__
   - __FILENAME_PREFIX__
   - __CROP_MODE__
5. Asegura que exista un nodo de salida de imagen (SaveImage o equivalente).
6. Levanta el servicio con variable de entorno:

   export WORKFLOW_TEMPLATE_PATH=/home/lucas/clients/avatar/estuches/comfy-convex/workflows/mi_thumbnail.json

Notas:
- El microservicio recorre history.outputs y toma la primera imagen encontrada.
- Si tu workflow no usa crop, simplemente ignora __CROP_MODE__.

## 3. Levantar el microservicio

En una terminal:

cd /home/lucas/clients/avatar/estuches/comfy-convex
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 9000

## 4. Pruebas por curl

## 4.1 Health

curl -sS http://127.0.0.1:9000/health

Debes ver:
- ok=true
- comfy.connected=true

## 4.2 Crear job thumbnail

curl -sS -X POST http://127.0.0.1:9000/v1/jobs/thumbnail \
  -H 'Content-Type: application/json' \
  -d '{
    "image_url": "https://picsum.photos/1200/800",
    "width": 256,
    "height": 256,
    "crop": "center"
  }'

Respuesta esperada:
- job_id
- status=queued

Guarda el job_id para los siguientes pasos.

## 4.3 Ver estado del job

curl -sS http://127.0.0.1:9000/v1/jobs/JOB_ID

Estados esperables:
- queued
- running
- completed (o failed)

## 4.4 Ver eventos en tiempo real (SSE)

curl -N http://127.0.0.1:9000/v1/jobs/JOB_ID/events

Eventos tipicos:
- snapshot
- job_running
- input_downloaded
- comfy_input_uploaded
- comfy_ws_connected
- comfy_prompt_queued
- comfy_ws_message (progress/executing/status)
- comfy_image_saved
- job_completed

## 4.5 Descargar resultado

curl -L http://127.0.0.1:9000/v1/jobs/JOB_ID/result --output thumbnail.png

Verifica dimensiones:

python3 - <<'PY'
from PIL import Image
img = Image.open('thumbnail.png')
print(img.size)
PY

## 5. Script rapido de smoke test (solo curl + python stdlib)

cd /home/lucas/clients/avatar/estuches/comfy-convex

resp=$(curl -sS -X POST http://127.0.0.1:9000/v1/jobs/thumbnail \
  -H 'Content-Type: application/json' \
  -d '{"image_url":"https://picsum.photos/1200/800","width":256,"height":256,"crop":"center"}')

echo "$resp"
job_id=$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["job_id"])' <<< "$resp")
echo "JOB_ID=$job_id"

for i in $(seq 1 30); do
  st=$(curl -sS http://127.0.0.1:9000/v1/jobs/$job_id)
  status=$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["status"])' <<< "$st")
  echo "[$i] $status"
  if [[ "$status" == "completed" || "$status" == "failed" ]]; then
    echo "$st"
    break
  fi
  sleep 1
done

curl -L http://127.0.0.1:9000/v1/jobs/$job_id/result --output "thumb_${job_id}.png"

## 6. Endpoint WS listo para siguiente etapa

El servicio expone WS por job en:
- ws://127.0.0.1:9000/ws/jobs/JOB_ID

Para browser/app principal despues, el stream WS ya esta operativo.
Para pruebas puras de terminal, SSE con curl suele ser lo mas simple.

## 7. Troubleshooting

1. WS responde 404 en handshake:
   - Reinstala deps: pip install -r requirements.txt
   - Asegura websockets instalado en la venv activa.
2. Job falla en upload/prompt:
   - Verifica ComfyUI activo en 127.0.0.1:8188
   - Verifica que el workflow API exportado sea valido.
3. Job completed pero no hay imagen:
   - Asegura nodo SaveImage (u output images) en workflow.
4. result devuelve 409:
   - El job aun no termino o fallo; revisa estado/eventos.
