#!/bin/bash

# ClipWright Studio - Quick Start Script
# Starts the local stack. No AI API keys are required: transcription uses a
# local Whisper model and analysis uses a local Ollama model.

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "============================================"
echo "  ClipWright Studio - AI Video Clipping Tool"
echo "============================================"
echo ""

# Check if .env file exists
if [ ! -f .env ]; then
    echo -e "${RED}Error: .env file not found!${NC}"
    echo ""
    echo "Create one from the template:"
    echo "  1. cp .env.example .env"
    echo "  2. python scripts/setup_local_ai.py --install-whisper --write-env"
    echo ""
    echo "No AI API key is needed: Whisper and Ollama run locally."
    echo ""
    exit 1
fi

# Check if required API keys are set
source .env

# Match Compose's host port, including its legacy deployment fallback.
FRONTEND_LOCAL_URL="http://localhost:${FRONTEND_PORT:-3001}"

if [ -n "${LLM:-}" ]; then
    case "$LLM" in
        google:*|google-gla:*|openai:*|openrouter:*|anthropic:*|ollama:*)
            ;;
        *)
            echo -e "${YELLOW}Warning: Unsupported LLM value '$LLM'${NC}"
            echo "Use google-gla:*, openai:*, openrouter:*, anthropic:*, or ollama:*"
            echo ""
            ;;
    esac
fi

if [ "${TRANSCRIPTION_PROVIDER:-whisper}" != "whisper" ]; then
    echo -e "${YELLOW}Warning: TRANSCRIPTION_PROVIDER=${TRANSCRIPTION_PROVIDER}${NC}"
    echo "Offline mode expects TRANSCRIPTION_PROVIDER=whisper."
    echo ""
fi

if [ -n "${WHISPER_MODEL_DIR:-}" ] && [ -d "${WHISPER_MODEL_DIR}" ]; then
    echo "Whisper models: ${WHISPER_MODEL_DIR}"
else
    echo -e "${YELLOW}Warning: the local Whisper model is not installed yet.${NC}"
    echo "Run: python scripts/setup_local_ai.py --install-whisper"
    echo ""
fi

if command -v ollama > /dev/null 2>&1; then
    if ollama list > /dev/null 2>&1; then
        echo "Ollama: running"
    else
        echo -e "${YELLOW}Warning: Ollama is installed but not running.${NC}"
        echo "Start it (or run 'ollama serve'), then 'ollama pull llama3.2:3b'."
        echo ""
    fi
else
    echo -e "${YELLOW}Warning: Ollama is not installed.${NC}"
    echo "Install it from https://ollama.com/download, then 'ollama pull llama3.2:3b'."
    echo ""
fi

if [ "${LLM:-}" = "ollama:" ]; then
    echo -e "${YELLOW}Warning: LLM=ollama: is missing a model name${NC}"
    echo "Use a value like LLM=ollama:gpt-oss:20b"
    echo ""
elif [[ "${LLM:-}" == ollama:* ]] && [ -z "${OLLAMA_BASE_URL:-}" ]; then
    echo "Ollama base URL is not set; ClipWright Studio will use its local/Docker default."
    echo ""
fi

# Hosted providers are optional and off by default; only warn if the operator
# asked for one without a key.
if [[ "${LLM:-}" != ollama:* ]]; then
    echo -e "${YELLOW}Warning: LLM='${LLM:-}' is a hosted provider.${NC}"
    echo "ClipWright Studio runs offline with LLM=ollama:llama3.2:3b."
    if [ "${OFFLINE_MODE:-true}" = "true" ]; then
        echo "OFFLINE_MODE=true blocks this provider; the app will report a setup error instead of calling it."
    fi
    echo ""
fi

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}Error: Docker is not running!${NC}"
    echo "Please start Docker Desktop and try again."
    echo ""
    exit 1
fi

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo -e "${RED}Error: docker-compose is not installed!${NC}"
    echo "Please install Docker Compose and try again."
    echo ""
    exit 1
fi

# Determine which docker compose command to use
if docker compose version &> /dev/null; then
    DOCKER_COMPOSE="docker compose"
else
    DOCKER_COMPOSE="docker-compose"
fi

echo -e "${GREEN}Starting ClipWright Studio...${NC}"
echo ""

# Build and start containers
echo "Building and starting Docker containers..."
echo "(This may take a few minutes on the first run)"
echo ""

$DOCKER_COMPOSE up -d --build

echo ""
echo -e "${GREEN}ClipWright Studio is starting up!${NC}"
echo ""
echo "Services will be available at:"
echo "  - Frontend:  $FRONTEND_LOCAL_URL"
echo "  - Backend:   http://localhost:8000"
echo "  - API Docs:  http://localhost:8000/docs"
echo ""
echo "To view logs, run:"
echo "  $DOCKER_COMPOSE logs -f"
echo ""
echo "To stop all services, run:"
echo "  $DOCKER_COMPOSE down"
echo ""
echo "Waiting for services to be healthy..."

# Wait for services to be healthy
sleep 5

# Check if services are running
if $DOCKER_COMPOSE ps | grep -q "Up"; then
    echo -e "${GREEN}Services are starting successfully!${NC}"
    echo ""
    echo "You can now:"
    echo "  1. Open $FRONTEND_LOCAL_URL in your browser"
    echo "  2. View logs: $DOCKER_COMPOSE logs -f"
    echo "  3. Stop services: $DOCKER_COMPOSE down"
else
    echo -e "${YELLOW}Services are starting... Check logs if you encounter issues:${NC}"
    echo "  $DOCKER_COMPOSE logs -f"
fi

echo ""
echo "============================================"
