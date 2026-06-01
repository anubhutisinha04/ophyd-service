FROM python:3.11-slim

WORKDIR /app

# procps provides pgrep for the healthcheck in docker-compose.yml.
RUN apt-get update && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir caproto numpy

# Custom caproto IOCs that aren't in caproto.ioc_examples (e.g. the ADSim
# camera). Services that use them override `command:` to run the script.
COPY ioc_adsim.py /app/ioc_adsim.py

RUN useradd -m -u 1000 ioc && chown -R ioc:ioc /app
USER ioc

# Channel Access: 5064/tcp (virtual circuits), 5064/udp (beacons + search replies), 5065/udp (repeater beacons).
EXPOSE 5064/tcp 5064/udp 5065/udp

# mini_beamline prefix `mini:`. Exposes motor-like PVs (`mini:dot:mtrx`, `mini:dot:mtry`,
# `mini:ph1:mtr`, etc.), detectors, and a shutter.
CMD ["python", "-m", "caproto.ioc_examples.mini_beamline", "--list-pvs", "--interfaces", "0.0.0.0"]
