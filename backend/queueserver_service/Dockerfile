FROM python:3.11.8-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpam0g \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY queueserver_service ./queueserver_service/
# One-module legacy import surface for bluesky-queueserver-api (see its docstring)
COPY bluesky_queueserver ./bluesky_queueserver/

# The `all` extra pulls the optional, deployment-specific runtime deps kept out
# of the base install (see pyproject [project.optional-dependencies]):
#   pandas     -- manager/conversions.py spreadsheet->plan conversion
#   matplotlib -- the shipped profile_collection_sim startup (05-run-engine.py)
#   pyepics    -- ophyd EPICS layer for config-service consume-mode devices
RUN pip install --no-cache-dir -e ".[all]"

RUN useradd -m -u 1000 qserver && chown -R qserver:qserver /app
USER qserver

# Manager ZMQ control (REQ/REP) + info (PUB/SUB) ports
EXPOSE 60615 60625
# Default HTTP server port
EXPOSE 60610

# Unified mode: one process serves 0MQ + HTTP. The compose file supplies --config
# (http_server + config_service sections) and --redis-addr; this CMD is the
# standalone default.
CMD ["start-re-manager", \
     "--startup-dir", "/app/queueserver_service/profile_collection_sim", \
     "--zmq-control-addr", "tcp://*:60615", \
     "--zmq-info-addr", "tcp://*:60625", \
     "--http-port", "60610"]
