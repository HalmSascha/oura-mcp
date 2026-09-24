# syntax=docker/dockerfile:1

FROM python:3.13-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir hatchling

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps -w /wheels .


FROM python:3.13-slim

# Nicht als root laufen. UID 1000 passt zu den Volume-Rechten auf NAS01.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin oura

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Token-Store liegt im Volume, damit erneuerte Refresh-Tokens einen
# Container-Neustart überleben. Ohne das wäre nach jedem Neustart eine
# manuelle Neuautorisierung nötig.
RUN mkdir -p /data && chown oura:oura /data
VOLUME ["/data"]

USER oura

ENV OURA_MCP_TRANSPORT=http \
    OURA_MCP_HOST=0.0.0.0 \
    OURA_MCP_PORT=8000 \
    OURA_TOKEN_STORE=/data/tokens.json \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

ENTRYPOINT ["oura-mcp"]
CMD ["serve"]
