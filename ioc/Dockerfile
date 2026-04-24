FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir caproto numpy

RUN useradd -m -u 1000 ioc && chown -R ioc:ioc /app
USER ioc

# Channel Access: 5064/tcp (virtual circuits), 5064/udp (beacons + search replies), 5065/udp (repeater beacons).
EXPOSE 5064/tcp 5064/udp 5065/udp

# `caproto.ioc_examples.simple` serves PVs `simple:A` (int), `simple:B` (float), `simple:C` (int array[3]).
# Easy to swap for `mini_beamline` once direct-control tests want motors/detectors.
CMD ["python", "-m", "caproto.ioc_examples.simple", "--list-pvs", "--interfaces", "0.0.0.0"]
