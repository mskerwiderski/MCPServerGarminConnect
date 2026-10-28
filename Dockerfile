FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Dependencies exactly as locked in uv.lock (= what the tests ran against): the
# image is rebuilt on every deploy, and an unlocked install would pick up
# whatever is newest that day.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY garmin_mcp ./garmin_mcp
# Not editable: only /opt/venv goes into the final image.
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Everything the server keeps lives under /data: the SQLite database (users,
# OAuth clients and tokens) and the per-user FIT cache. Garmin access comes
# from MyAuth.
ENV MCP_DB=/data/app.db \
    GARMIN_MCP_CACHE=/data/cache
COPY --from=build /opt/venv /opt/venv
RUN useradd -u 1000 -m app && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"
CMD ["garmin-mcp", "serve", "--host", "0.0.0.0", "--port", "8000"]
