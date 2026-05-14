FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 spamfilter \
    && useradd  --system --uid 1000 --gid 1000 --home-dir /app spamfilter

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY filter.py bootstrap_train.py ./

RUN mkdir -p /state && chown -R spamfilter:spamfilter /app /state
USER spamfilter

ENTRYPOINT ["python", "-u", "filter.py"]
