FROM python:3.14.7-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && (getent group 100 || groupadd --system --gid 100 users) \
    && (getent passwd 99 || useradd  --system --uid 99 --gid 100 --home-dir /app nobody2)

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY filter.py bootstrap_train.py dashboard.py favicon.png ./

# Read-only web dashboard; disabled by default. Operator opts in by
# setting DASHBOARD_USER + DASHBOARD_PASSWORD.
EXPOSE 8080

RUN mkdir -p /state && chown -R 99:100 /app /state
# Run as Unraid's nobody:users so bind-mounted appdata files
# created with their default ownership are readable.
USER 99:100

# Bake the healthcheck into the image so orchestrators (docker-compose,
# Unraid templates) don't need to ship a shell-quoted `--health-cmd`
# that gets expanded on the host instead of in the container.
HEALTHCHECK --interval=2m --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys,time,pathlib; hb=pathlib.Path('/state/heartbeat'); sys.exit(0 if hb.exists() and time.time()-int(hb.read_text().strip() or 0)<600 else 1)"]

ENTRYPOINT ["python", "-u", "filter.py"]
