FROM python:3.14-slim

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

COPY filter.py bootstrap_train.py ./

RUN mkdir -p /state && chown -R 99:100 /app /state
# Run as Unraid's nobody:users so bind-mounted appdata files
# created with their default ownership are readable.
USER 99:100

ENTRYPOINT ["python", "-u", "filter.py"]
