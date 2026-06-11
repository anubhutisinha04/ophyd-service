FROM python:3.11.8-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpam0g \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml setup.cfg setup.py versioneer.py README.rst MANIFEST.in requirements.txt ./
COPY bluesky_queueserver ./bluesky_queueserver/
COPY subprojects ./subprojects/

# Extra deps the editable install doesn't pull but the service needs at runtime:
#   pandas     -- imported unconditionally by manager/conversions.py (spreadsheet->plan)
#   matplotlib -- imported by the shipped profile_collection_sim startup (05-run-engine.py)
#   pyepics    -- ophyd EPICS control layer; required to instantiate the EpicsSignal-backed
#                 devices that config_service consume-mode injects into the worker
RUN pip install --no-cache-dir -e . pandas matplotlib pyepics

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
     "--startup-dir", "/app/bluesky_queueserver/profile_collection_sim", \
     "--zmq-control-addr", "tcp://*:60615", \
     "--zmq-info-addr", "tcp://*:60625", \
     "--http-port", "60610"]
