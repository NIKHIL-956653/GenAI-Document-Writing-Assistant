# docgen HTTP API (`docgen serve`). See docker-compose.yml for the full local stack with Ollama.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[server]"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Everything stateful lives on the /data volume
    DOCGEN_STORE=/data/docgen.db \
    DOCGEN_OUTPUT_DIR=/data/docs \
    GOOGLE_TOKEN_FILE=/data/token.json \
    GOOGLE_CREDENTIALS_FILE=/data/credentials.json
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels docgen[server] && rm -rf /wheels \
    && useradd --create-home --uid 10001 docgen \
    && mkdir -p /data/docs && chown -R docgen:docgen /data
USER docgen
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
ENTRYPOINT ["docgen"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000", "--log-format", "json"]
