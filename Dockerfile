# Dockerfile for E3onCANserver
#
# Build:
#   docker build -t e3oncanserver .
#
# The image contains only the simulator code. Configuration files (devices.json,
# virtdata_*.txt) are supplied at runtime via a volume mount so they can be
# changed without rebuilding the image.

FROM python:3.12-slim

# python-can is required by bus.py even in DoIP mode (imported at module level).
RUN pip install --no-cache-dir "python-can>=4.3.0"

WORKDIR /app

# Copy simulator source
COPY simulator/ ./simulator/
COPY main.py    ./

# Runtime configuration is supplied via volume mounts at /app/config and /app/data.
RUN mkdir -p /app/config /app/data

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
