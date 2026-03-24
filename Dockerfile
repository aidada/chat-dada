# ---- build stage: install dependencies with uv (much faster than pip) ----
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

# ---- runtime stage: copy only what we need ----
FROM python:3.13-slim

WORKDIR /app
ENV FRONTEND_DIST_DIR=/app/frontend-dist

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code (filtered by .dockerignore)
COPY . .
RUN mkdir -p /app/frontend-dist/assets

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
