#!/usr/bin/with-contenv sh
set -eu
umask 077
exec python3 /app/app.py
