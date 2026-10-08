FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# libs exigidas pelo Firefox/Camoufox no Linux + Xvfb (display virtual, HEADLESS=virtual)
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb libgtk-3-0 libx11-xcb1 libasound2 libdbus-glib-1-2 libxt6 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# g++/libzstd-dev só para compilar o indexed-zstd (sem wheel em algumas plataformas); removidos no mesmo layer
RUN apt-get update && apt-get install -y --no-install-recommends g++ gcc libc6-dev libzstd-dev \
    && pip install -r requirements.txt \
    && python -m camoufox fetch \
    && find /root/.cache/camoufox/browsers -name version.json | grep -q . \
    && apt-get purge -y g++ gcc libc6-dev libzstd-dev && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY app.py .

# um único worker: o perfil do Firefox (profile/) só aceita um processo
# ponytail: roda como root; não-root exigiria acertar permissões do volume profile/ vindo do host
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
