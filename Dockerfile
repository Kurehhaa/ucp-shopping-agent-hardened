# Stage 1: Build
FROM python:3.11-slim AS builder

WORKDIR /build

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# The package metadata references README.md and the source tree, so both are
# needed before installing (a failing install must fail the build).
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir .

# Stage 2: Runtime
FROM python:3.11-slim AS runtime

# Security: run as non-root
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
# The agent also serves the demo merchants on this port (KNOWN_MERCHANT_URLS
# defaults to localhost:8020), so the container must listen on 8020.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8020

WORKDIR /app

# Copy application code
COPY --from=builder /build/src ./src

# Switch to non-root user
USER appuser

EXPOSE 8020

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://localhost:8020/health'); r.raise_for_status()"

CMD ["python", "-m", "ucp_shopping.main"]
