#!/usr/bin/env bash
# Sobe o serviço de rostos para o systemd.
#
# Existe porque o serviço precisa de um venv próprio (OpenCV com YuNet/SFace
# não entra no venv do robô nem no da voz) e dos dois modelos ONNX do
# opencv_zoo. O script garante os dois — uma vez — e termina em `exec` para o
# Python herdar o PID da unidade.
#
# Nenhuma imagem passa pela rede além do loopback: o servidor escuta só em
# 127.0.0.1:8127 e é a ponte (:8126, com token) que o robô alcança.
set -euo pipefail

BASE="${GARRA_BASE:-$HOME/Documents/Projetos/Reachy-Mini-Control}"
APP="$BASE/garra_reachy_mini"
VENV="$BASE/rosto_env"
PY="$VENV/bin/python"
MODELOS="${GARRA_ROSTOS_MODELOS:-$HOME/.local/share/garra-reachy/modelos}"
ZOO="https://github.com/opencv/opencv_zoo/raw/main/models"

[ -f "$APP/tools/servidor_rostos.py" ] || { echo "servidor_rostos.py não encontrado" >&2; exit 1; }

if [ ! -x "$PY" ]; then
  echo "criando $VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" >/dev/null
    uv pip install --python "$PY" -q opencv-python-headless numpy cryptography fastapi uvicorn
  else
    python3 -m venv "$VENV"
    "$PY" -m pip install -q opencv-python-headless numpy cryptography fastapi uvicorn
  fi
fi

mkdir -p "$MODELOS"
baixar() {  # nome-do-arquivo caminho-no-zoo tamanho-mínimo-em-bytes
  local alvo="$MODELOS/$1"
  if [ -s "$alvo" ] && [ "$(stat -c %s "$alvo")" -ge "$3" ]; then
    return 0
  fi
  echo "baixando $1"
  curl -fsSL --retry 3 -o "$alvo.parte" "$ZOO/$2/$1"
  mv "$alvo.parte" "$alvo"
}
baixar face_detection_yunet_2023mar.onnx face_detection_yunet 200000
baixar face_recognition_sface_2021dec.onnx face_recognition_sface 38000000

exec "$PY" "$APP/tools/servidor_rostos.py"
