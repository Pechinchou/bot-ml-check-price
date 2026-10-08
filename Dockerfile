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
# modo login: VNC + noVNC para logar/resolver captcha pelo navegador (camada após o fetch para não refazer o download)
RUN apt-get update && apt-get install -y --no-install-recommends x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*
# websockets (ponte noVNC da API) fora do requirements.txt: mexer nele invalidaria a camada do navegador (1,3 GB)
RUN pip install websockets
COPY app.py login.py login.sh start.sh ./

# um único worker: o perfil do Firefox (profile/) só aceita um processo
# ponytail: roda como root; não-root exigiria acertar permissões do volume profile/ vindo do host
CMD ["./start.sh"]
