FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
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
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8003/health', timeout=2)" || exit 1

# Run service
CMD ["python", "-m", "cli", "--host", "0.0.0.0", "--port", "8003"]
