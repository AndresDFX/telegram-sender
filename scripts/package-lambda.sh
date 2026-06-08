#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/.build"
ZIP_PATH="${BUILD_DIR}/telegram-broadcaster.zip"

mkdir -p "${BUILD_DIR}"

echo "Empaquetando Lambda en ${ZIP_PATH}..."
(
  cd "${ROOT_DIR}/src/lambda"
  pip install -r requirements.txt -t "${BUILD_DIR}/package" --quiet
  cp *.py "${BUILD_DIR}/package/"
  cd "${BUILD_DIR}/package"
  zip -r "${ZIP_PATH}" . -x "*.pyc" -x "__pycache__/*"
)

echo "Artefacto listo: ${ZIP_PATH}"
