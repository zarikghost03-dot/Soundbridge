# -*- coding: utf-8 -*-
"""Кто стоит за spotify.com: настоящий Spotify или перехват провайдера?"""
import socket
import ssl
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import soundbridge.spotify_client  # noqa: F401  (inject truststore)

for host in ("accounts.spotify.com", "api.spotify.com", "open.spotify.com"):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                cert = s.getpeercert()
                issuer = dict(pair for pair in cert["issuer"][0])
                subject = dict(pair for pair in cert["subject"][0])
                print(
                    f"{host:22} issuer-O={issuer.get('organizationName', '?'):24}"
                    f" issuer-CN={issuer.get('commonName', '?')}"
                )
    except Exception as exc:
        print(f"{host:22} ОШИБКА: {type(exc).__name__}: {str(exc)[:120]}")
