"""O cliente estreito da ponte, do lado do robô: reconfigura sem reiniciar e
diz "não configurado" quando não há para onde ir."""

from __future__ import annotations

import json

import httpx
import pytest

from garra_reachy_mini.web import integracoes


def test_sem_url_e_bridge_not_configured_e_nao_unreachable() -> None:
    p = integracoes.Ponte("", None)
    r = pytest.importorskip("anyio").run(p.pedir, "GET", "/api/casa/estado")
    assert r.status_code == 503 and json.loads(r.body)["erro"] == "bridge_not_configured"


def test_reconfigurar_troca_o_destino_sem_reiniciar() -> None:
    vistos: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})

    p = integracoes.Ponte("http://a:8126", "tok-a", transport=httpx.MockTransport(handler))
    anyio = pytest.importorskip("anyio")
    anyio.run(p.pedir, "GET", "/x")
    assert p.reconfigurar("http://a:8126", "tok-a") is False, "nada mudou"
    assert p.reconfigurar("http://b:8126/", "tok-b", transport=httpx.MockTransport(handler)) is True
    anyio.run(p.pedir, "GET", "/x")
    assert vistos == [("http://a:8126/x", "Bearer tok-a"), ("http://b:8126/x", "Bearer tok-b")]
    assert "tok-b" not in repr(p) and "tok-b" not in str(p)
