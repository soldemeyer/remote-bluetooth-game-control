# Rendezvous broker.
#
# The broker imports nothing outside the standard library and nothing from the
# rest of this repo, so the image is a Python base plus one package -- no build
# stage, no wheels, no compiler. Keep it that way: this is the one component
# that must sit on the public internet, and its attack surface is worth
# guarding.
#
# Build from the repository root, not from this directory:
#     docker build -f packaging/docker/Dockerfile -t rbgc-broker .

FROM python:3.13-slim

# Unbuffered so `docker logs` shows registrations as they happen rather than in
# 8 kB bursts; no .pyc because the filesystem is read-only at runtime.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RBGC_BROKER_PORT=47900

WORKDIR /app

COPY rendezvous/ /app/rendezvous/
COPY packaging/docker/healthcheck.py /app/healthcheck.py

# Unprivileged: the broker binds 47900, which needs no capability, and it never
# writes to disk.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin rbgc \
    && chown -R root:root /app && chmod -R a-w /app
USER rbgc

EXPOSE 47900/udp

# A real round trip on the service port -- see healthcheck.py for why a process
# check will not do.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

# -v so registrations, introductions and relay fallbacks are visible in
# `docker logs`. Without it the broker logs only warnings, and a room that
# never pairs up looks identical to one nobody joined.
ENTRYPOINT ["python", "-m", "rendezvous.broker"]
CMD ["--host", "0.0.0.0", "-v"]
