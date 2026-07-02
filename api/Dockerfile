FROM python:3.12-slim

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Runs as root deliberately: the kv_caches volume is shared with llama-server
# (which writes slot files as root), and maintenance/delete must be able to
# remove those files. The stack is loopback-bound and single-user.

EXPOSE 8000

# Liveness only (does the process serve HTTP); dependency health is GET /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
