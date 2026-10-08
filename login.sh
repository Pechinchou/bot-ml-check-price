#!/bin/sh
# Tela virtual + VNC + noVNC (página web na porta 6080); depois abre o navegador para o login.
set -e
export DISPLAY=:99
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
sleep 1
x11vnc -display :99 -forever -shared -nopw -localhost -quiet -rfbport 5900 &  # só localhost: quem expõe é o websockify
websockify --web /usr/share/novnc 6080 localhost:5900 &
echo "Abra http://localhost:6080/vnc.html?autoconnect=1 (via túnel SSH) para ver o navegador"
exec python login.py
