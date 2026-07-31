#!/bin/bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${PROJECT_DIR}/src"
BUILD_DIR="${PROJECT_DIR}/build"
FNPACK="${PROJECT_DIR}/fnpack"
CLI_SRC_URL="https://github.com/miskcoo/ugreen_leds_controller.git"
CLI_OUTPUT="${APP_DIR}/app/server/ugreen_leds_cli"

echo "========================================="
echo "  UGreenLedPilot Build Script"
echo "========================================="

# ------------------------------
# Step 1: Build LED driver
# ------------------------------
echo ""
echo "[1/3] Building LED driver (ugreen_leds_cli)..."

if [ -f "${CLI_OUTPUT}" ]; then
    echo "  Binary already exists, skipping."
else
    echo "  Cross-compiling static x86_64 binary via Docker..."
    docker run --platform linux/amd64 --rm \
        -v "${APP_DIR}/app/server":/output \
        -w /build alpine:latest sh -c '
            set -e
            apk add --no-cache git g++ make linux-headers > /dev/null 2>&1
            git clone --depth 1 "$1" > /dev/null 2>&1
            cd ugreen_leds_controller
            git fetch --depth 1 origin af2b7ae84f65a8730768d4b626570bc824b196e0 > /dev/null 2>&1
            git checkout af2b7ae84f65a8730768d4b626570bc824b196e0 > /dev/null 2>&1
            [ "$(git rev-parse HEAD)" = "af2b7ae84f65a8730768d4b626570bc824b196e0" ]
            cd cli
            make > /dev/null 2>&1
            cp ugreen_leds_cli /output/
        ' -- "$CLI_SRC_URL"

    if [ -f "${CLI_OUTPUT}" ]; then
        chmod +x "${CLI_OUTPUT}"
        echo "  Built: ${CLI_OUTPUT}"
    else
        echo "  ERROR: Failed to build LED driver"
        exit 1
    fi
fi

# ------------------------------
# Step 2: Build fpk
# ------------------------------
echo ""
echo "[2/3] Building fpk package..."

cd "${APP_DIR}"
if "${FNPACK}" build 2>&1 | tail -5; then
    echo "  Package built successfully"
else
    echo "  ERROR: fnpack build failed"
    exit 1
fi

# ------------------------------
# Step 3: Collect artifacts
# ------------------------------
echo ""
echo "[3/3] Collecting build artifacts..."

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"

VERSION=$(awk -F '=' '/^version[[:space:]]*=/{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}' "${APP_DIR}/manifest")
FPK_FILE="${APP_DIR}/UGreenLedPilot.fpk"

if [ -f "${FPK_FILE}" ]; then
    cp "${FPK_FILE}" "${BUILD_DIR}/UGreenLedPilot-${VERSION}.x86_64.fpk"
    echo "  ${BUILD_DIR}/UGreenLedPilot-${VERSION}.x86_64.fpk"
else
    FPK_FILE="${PROJECT_DIR}/UGreenLedPilot.fpk"
    if [ -f "${FPK_FILE}" ]; then
        cp "${FPK_FILE}" "${BUILD_DIR}/UGreenLedPilot-${VERSION}.x86_64.fpk"
        echo "  ${BUILD_DIR}/UGreenLedPilot-${VERSION}.x86_64.fpk"
    else
        echo "  ERROR: fpk file not found"
        exit 1
    fi
fi

# Also copy the binary for standalone use
cp "${CLI_OUTPUT}" "${BUILD_DIR}/ugreen_leds_cli" 2>/dev/null && \
    echo "  ${BUILD_DIR}/ugreen_leds_cli"

echo ""
echo "========================================="
echo "  Build complete"
echo "========================================="
