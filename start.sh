#!/bin/sh
# Navegador visível numa tela virtual fixa (:99) + VNC local; a API serve o noVNC (/vnc/{token}/...) e faz a ponte.
set -e
export DISPLAY=:99
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99  # "up" reaproveita o container: a trava do Xvfb sobra
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
sleep 1
x11vnc -display :99 -forever -shared -nopw -localhost -quiet -rfbport 5900 &  # só localhost: ninguém de fora conecta direto
exec uvicorn app:app --host 0.0.0.0 --port 8000
