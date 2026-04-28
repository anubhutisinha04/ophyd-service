FROM python:3.11.8-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src/
COPY scripts ./scripts/

RUN pip install --no-cache-dir -e .

RUN useradd -m -u 1000 config && chown -R config:config /app
USER config

EXPOSE 8004

HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8004/health', timeout=2).status == 200 else 1)" || exit 1

CMD ["bluesky-configuration-service", "--host", "0.0.0.0", "--port", "8004"]
