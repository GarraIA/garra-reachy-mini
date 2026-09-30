"""O diagnóstico do gateway, pela ponte: só leitura, só o que o painel mostra.

O painel do robô lista o Capability Registry com nome/origem/estado/motivo/
ação. O que passa é `checks` (id, label, status, detail, next_step) e o
cabeçalho; nada de configuração, chave ou caminho além do que o gateway já
escreve no próprio `detail`.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from companion import ponte

TOKEN = "token-de-teste-nao-e-segredo"
AUT = {"Authorization": f"Bearer {TOKEN}"}

DIAG = {"status": "warning", "version": "0.4.7", "uptime_secs": 10, "generated_at": "2026-09-30T05:52:41Z",
        "checks": [
            {"id": "gateway.responds", "label": "Gateway responds", "status": "ok", "detail": "Live", "next_step": None},
            {"id": "tools.capabilities", "label": "Capability registry", "status": "warning",
             "detail": "11 visivel(is) · indisponivel: device_execute (no_devices), device_read (no_devices) · nao configurado: bash (not_configured)",
             "next_step": "cada item traz o motivo", "segredo": "nao-deveria-passar"},
        ],
        "config": {"api_key": "SEGREDO"}}


class GatewayFalso:
    def __init__(self) -> None:
        self.pedidos: list[tuple[str, str, str | None]] = []
        self.falha: str | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append((request.method, request.url.path, request.headers.get("authorization")))
        if self.falha == "offline":
            raise httpx.ConnectError("recusado")
        if self.falha == "html":
            return httpx.Response(200, content=b"<html>")
        if request.url.path == "/api/diagnostics":
            return httpx.Response(200, json=DIAG)
        return httpx.Response(404, json={"error": "nada"})


@pytest.fixture()
def montar(monkeypatch):
    def _montar():
        g = GatewayFalso()
        app = ponte.montar(TOKEN, "chave-gw")
        monkeypatch.setattr(ponte, "_cliente", httpx.AsyncClient(base_url="http://gw", transport=httpx.MockTransport(g.handler)))
        return TestClient(app), g
    return _montar


def test_exige_token(montar) -> None:
    cliente, g = montar()
    assert cliente.get("/api/diagnostico").status_code == 401
    assert g.pedidos == []


def test_repassa_so_o_que_o_painel_mostra_com_a_chave_do_gateway(montar) -> None:
    cliente, g = montar()
    r = cliente.get("/api/diagnostico", headers=AUT)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["status"] == "warning" and corpo["version"] == "0.4.7" and corpo["generated_at"] == DIAG["generated_at"]
    assert [c["id"] for c in corpo["checks"]] == ["gateway.responds", "tools.capabilities"]
    assert set(corpo["checks"][1]) == {"id", "label", "status", "detail", "next_step"}
    assert "config" not in corpo and "SEGREDO" not in r.text and "nao-deveria-passar" not in r.text
    assert g.pedidos == [("GET", "/api/diagnostics", "Bearer chave-gw")]
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("falha,status,codigo", [("offline", 502, "gateway_unreachable"), ("html", 502, "invalid_gateway_response")])
def test_gateway_fora_ou_torto_vira_codigo(montar, falha, status, codigo) -> None:
    cliente, g = montar()
    g.falha = falha
    r = cliente.get("/api/diagnostico", headers=AUT)
    assert r.status_code == status and r.json()["erro"] == codigo
