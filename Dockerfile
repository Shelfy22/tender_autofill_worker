FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       libreoffice-core \
       libreoffice-writer \
       libreoffice-calc \
       p7zip-full \
       unar \
       fonts-dejavu-core \
       fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/tender-autofill

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY pyproject.toml ./

RUN groupadd --system tender \
    && useradd --system --gid tender --home-dir /srv/tender-autofill tender \
    && mkdir -p /tmp/tender-autofill \
    && chown -R tender:tender /srv/tender-autofill /tmp/tender-autofill

USER tender

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
