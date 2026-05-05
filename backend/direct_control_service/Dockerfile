FROM python:3.11.8-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy package files
COPY pyproject.toml README.md ./
COPY src ./src/

# Install package
RUN pip install --no-cache-dir -e .

# Create non-root user
RUN useradd -m -u 1000 control && chown -R control:control /app
USER control

# Expose port
EXPOSE 8003

# Health check
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8003/health', timeout=2).status == 200 else 1)" || exit 1

# Run service
CMD ["bluesky-direct-control", "--host", "0.0.0.0", "--port", "8003"]
