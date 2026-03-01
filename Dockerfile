# ── Stage 1: Build frontend ───────────────────────────────────────────────────
FROM node:20-alpine AS frontend-build

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --silent

COPY frontend/ ./
RUN npm run build
# Output: /app/frontend/dist/


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

# FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Frontend build artifacts → serve via FastAPI static files
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Data directories
RUN mkdir -p /app/data /app/projects /app/config/channels /app/prompts

# Non-root user
RUN useradd -m -u 1000 vf && chown -R vf:vf /app
USER vf

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
