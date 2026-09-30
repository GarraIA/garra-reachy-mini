"""A casa nas rotas do painel do robô: só repasse, pela ponte, com o token."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini.acompanhamento import Acompanhamento
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.web import integracoes
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub
from garra_reachy_mini.web.seguranca import resolver_politica


class PonteFalsa:
    def __init__(self) -> None:
        self.pedidos: list[tuple[str, str, object]] = []
        self.falha: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append((request.method, request.url.path,
                             json.loads(request.content) if request.content else None,
                             dict(request.headers).get("authorization")))
        if self.falha == "timeout":
            raise httpx.ReadTimeout("demorou")
        if self.falha == "offline":
            raise httpx.ConnectError("recusado")
        if request.url.path == "/api/casa/estado":
            return httpx.Response(200, json={"state": "not_configured", "configured": False})
        if request.url.path == "/api/casa/configurar":
            return httpx.Response(401, json={"erro": "unauthorized", "detalhe": ""})
        if request.url.path == "/api/casa/entidades":
            return httpx.Response(409, json={"erro": "not_configured"})
        if request.url.path == "/api/casa/voz":
            return httpx.Response(200, json={"voice_control": True})
        if request.url.path == "/api/casa" and request.method == "DELETE":
            return httpx.Response(200, json={"configured": False})
        return httpx.Response(404, json={"erro": "nada"})


@pytest.fixture()
def painel(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    falsa = PonteFalsa()
    ponte = integracoes.Ponte("http://ponte:8126", "tok-robo",
                              transport=httpx.MockTransport(falsa.handler))
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos, acompanhamento=Acompanhamento(ctrl.eventos),
        integracoes=ponte,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        c.falsa = falsa
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def test_as_rotas_repassam_com_o_token_do_robo(painel) -> None:
    assert painel.get("/api/robot/casa/estado").json()["state"] == "not_configured"
    r = painel.post("/api/robot/casa/configurar", json={"url": "http://ha.local:8123", "token": "x" * 40})
    assert r.status_code == 401 and r.json()["erro"] == "unauthorized"
    assert painel.get("/api/robot/casa/entidades").status_code == 409
    assert painel.post("/api/robot/casa/voz", json={"enabled": True}).json()["voice_control"] is True
    assert painel.delete("/api/robot/casa").json()["configured"] is False
    metodos = [(m, p) for m, p, _, _ in painel.falsa.pedidos]
    assert metodos == [("GET", "/api/casa/estado"), ("POST", "/api/casa/configurar"),
                       ("GET", "/api/casa/entidades"), ("POST", "/api/casa/voz"), ("DELETE", "/api/casa")]
    assert all(a == "Bearer tok-robo" for _, _, _, a in painel.falsa.pedidos)
    assert painel.falsa.pedidos[1][2] == {"url": "http://ha.local:8123", "token": "x" * 40}


def test_ponte_fora_do_ar_e_dito_como_ponte(painel) -> None:
    painel.falsa.falha = "offline"
    r = painel.get("/api/robot/casa/estado")
    assert r.status_code == 502 and r.json()["erro"] == "bridge_unreachable"
    painel.falsa.falha = "timeout"
    assert painel.get("/api/robot/casa/estado").json()["erro"] == "bridge_timeout"


def test_configurar_mal_formado_nem_chega_a_ponte(painel) -> None:
    assert painel.post("/api/robot/casa/configurar", json={"url": "x"}).status_code == 400
    assert painel.post("/api/robot/casa/voz", json={"enabled": "sim"}).status_code == 400
    assert painel.falsa.pedidos == []


def test_sem_ponte_configurada_as_rotas_dizem_isso(backend, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    app = FastAPI()
    montar(app, ContextoWeb(controlador=ctrl, hub=hub, eventos=ctrl.eventos,
                            acompanhamento=Acompanhamento(ctrl.eventos),
                            politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        r = c.get("/api/robot/casa/estado")
        assert r.status_code == 503 and r.json()["error"]["code"] == "bridge_not_configured"
    hub.encerrar()
    ctrl.encerrar(timeout=2)
