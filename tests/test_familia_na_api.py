"""A família nas rotas do painel do robô.

Cadastro é repasse pela ponte; a amostra e o teste de reconhecimento pegam UM
quadro da câmera do robô AGORA (é o dono, no painel, pedindo) e o mandam — o
painel nunca envia imagem, e nenhuma imagem volta.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini import conversa, familia
from garra_reachy_mini.acompanhamento import Acompanhamento
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.web import integracoes
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub
from garra_reachy_mini.web.seguranca import resolver_politica

CONSENTE = {"accepted": True, "by": "Maria", "minor": False}
PERFIL = {"id": "a1b2c3d4e5f6", "name": "Maria", "active": True, "samples": 1, "samples_readable": True,
          "consent": dict(CONSENTE, guardian=None, at="2026-09-30T05:00:00+00:00"),
          "created_at": "2026-09-30T05:00:00+00:00"}


class PonteFalsa:
    def __init__(self) -> None:
        self.pedidos: list[tuple] = []
        self.falha: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        corpo = json.loads(request.content) if request.content else None
        self.pedidos.append((request.method, request.url.path, corpo, dict(request.headers).get("authorization")))
        if self.falha == "offline":
            raise httpx.ConnectError("recusado")
        p = request.url.path
        if p == "/api/rostos/estado":
            return httpx.Response(200, json={"state": "ok", "engine": "yunet+sface", "profiles": 1})
        if p == "/api/rostos/perfis" and request.method == "GET":
            return httpx.Response(200, json={"profiles": [PERFIL]})
        if p == "/api/rostos/perfis" and request.method == "POST":
            return httpx.Response(201, json={"ok": True, "profile": dict(PERFIL, name=corpo["name"], samples=0)})
        if p == "/api/rostos/perfis/a1b2c3d4e5f6" and request.method == "PATCH":
            return httpx.Response(200, json={"ok": True, "profile": dict(PERFIL, active=corpo["active"])})
        if p == "/api/rostos/perfis/a1b2c3d4e5f6" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        if p == "/api/rostos/perfis/a1b2c3d4e5f6/amostras" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True, "removed": 1})
        if p == "/api/rostos/perfis/a1b2c3d4e5f6/amostras" and request.method == "POST":
            assert base64.b64decode(corpo["imagem"])[:2] == b"\xff\xd8"
            return httpx.Response(200, json={"ok": True, "samples": 2, "quality": {"size_px": 150, "sharpness": 80.0, "brightness": 100.0}})
        if p == "/api/rostos/reconhecer":
            return httpx.Response(200, json={"faces": [{"box": [0, 0, 100, 100], "known": True, "profile_id": "a1b2c3d4e5f6",
                                                        "name": "Maria", "score": 0.8, "reason": None}], "ms": 9.0})
        return httpx.Response(404, json={"erro": "not_found"})


@pytest.fixture()
def painel(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    falsa = PonteFalsa()
    ponte = integracoes.Ponte("http://ponte:8126", "tok-robo", transport=httpx.MockTransport(falsa.handler))

    def cliente():
        # O cliente da família fala com a mesma ponte falsa, pelo httpx.
        class C:
            def reconhecer(self, jpeg):
                r = httpx.Client(transport=httpx.MockTransport(falsa.handler)).post(
                    "http://ponte:8126/api/rostos/reconhecer",
                    json={"imagem": base64.b64encode(jpeg).decode()}, headers={"Authorization": "Bearer tok-robo"})
                if r.status_code != 200:
                    raise familia.ErroDaPonte("service_error")
                return r.json()
        return C()

    fam = familia.Familia(hub, cliente, ctrl.eventos, hora=lambda: "21:40:05")
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos, acompanhamento=Acompanhamento(ctrl.eventos),
        integracoes=ponte, familia=fam,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        c.falsa, c.hub = falsa, hub
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def test_estado_junta_o_interruptor_a_ultima_captura_e_o_servico(painel) -> None:
    r = painel.get("/api/robot/familia/estado")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["enabled"] is False and corpo["last"] is None
    assert corpo["service"] == {"state": "ok", "engine": "yunet+sface", "profiles": 1}
    painel.falsa.falha = "offline"
    corpo = painel.get("/api/robot/familia/estado").json()
    assert corpo["service"]["state"] == "unavailable" and corpo["service"]["error"] == "bridge_unreachable"


def test_o_cadastro_e_repasse_com_o_token_do_robo(painel) -> None:
    assert painel.get("/api/robot/familia/perfis").json() == {"profiles": [PERFIL]}
    r = painel.post("/api/robot/familia/perfis", json={"name": "João", "consent": CONSENTE})
    assert r.status_code == 201 and r.json()["profile"]["name"] == "João"
    r = painel.patch("/api/robot/familia/perfis/a1b2c3d4e5f6", json={"active": False})
    assert r.status_code == 200 and r.json()["profile"]["active"] is False
    assert painel.delete("/api/robot/familia/perfis/a1b2c3d4e5f6/amostras").json() == {"ok": True, "removed": 1}
    assert painel.delete("/api/robot/familia/perfis/a1b2c3d4e5f6").json() == {"ok": True}
    metodos = [(m, p) for m, p, _, _ in painel.falsa.pedidos]
    assert metodos == [("GET", "/api/rostos/perfis"), ("POST", "/api/rostos/perfis"),
                       ("PATCH", "/api/rostos/perfis/a1b2c3d4e5f6"),
                       ("DELETE", "/api/rostos/perfis/a1b2c3d4e5f6/amostras"),
                       ("DELETE", "/api/rostos/perfis/a1b2c3d4e5f6")]
    assert all(a == "Bearer tok-robo" for _, _, _, a in painel.falsa.pedidos)
    assert painel.falsa.pedidos[1][2] == {"name": "João", "consent": CONSENTE}


@pytest.mark.parametrize("corpo", [{"consent": CONSENTE}, {"name": "", "consent": CONSENTE},
                                   {"name": "Maria"}, {"name": "Maria", "consent": "sim"},
                                   {"name": "x" * 41, "consent": CONSENTE}])
def test_perfil_torto_nao_chega_a_ponte(painel, corpo) -> None:
    r = painel.post("/api/robot/familia/perfis", json=corpo)
    assert r.status_code == 400 and painel.falsa.pedidos == []


def test_id_torto_nao_chega_a_ponte(painel) -> None:
    assert painel.delete("/api/robot/familia/perfis/..%2Fsaude").status_code in (400, 404, 405)
    assert painel.delete("/api/robot/familia/perfis/ZZ").status_code == 400
    assert painel.patch("/api/robot/familia/perfis/ZZ", json={"active": True}).status_code == 400
    assert painel.patch("/api/robot/familia/perfis/a1b2c3d4e5f6", json={"active": "sim"}).status_code == 400
    assert painel.falsa.pedidos == []


def test_a_amostra_e_um_quadro_da_camera_agora_e_nao_do_painel(painel) -> None:
    r = painel.post("/api/robot/familia/perfis/a1b2c3d4e5f6/amostras", json={"imagem": "AAAA"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "samples": 2, "quality": {"size_px": 150, "sharpness": 80.0, "brightness": 100.0}}
    [(metodo, caminho, corpo, _)] = painel.falsa.pedidos
    assert (metodo, caminho) == ("POST", "/api/rostos/perfis/a1b2c3d4e5f6/amostras")
    assert corpo["imagem"] != "AAAA" and base64.b64decode(corpo["imagem"])[:2] == b"\xff\xd8"


def test_sem_camera_a_amostra_e_503(painel) -> None:
    painel.hub.encerrar()
    painel.hub.instantaneo = lambda idade_maxima_s=0.5: None
    r = painel.post("/api/robot/familia/perfis/a1b2c3d4e5f6/amostras")
    assert r.status_code == 503 and r.json()["detail"]["error"]["code"] == "camera_unavailable"
    assert painel.falsa.pedidos == []


def test_o_teste_de_reconhecimento_funciona_mesmo_com_a_voz_desligada(painel) -> None:
    r = painel.post("/api/robot/familia/reconhecer")
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["ok"] is True and corpo["names"] == ["Maria"] and corpo["unknown"] == 0 and corpo["faces"] == 1
    assert corpo["captured_at"] == "21:40:05"
    assert "imagem" not in json.dumps(corpo)
    assert painel.get("/api/robot/familia/estado").json()["last"]["names"] == ["Maria"]


def test_o_teste_de_reconhecimento_sem_camera_diz_por_que(painel) -> None:
    painel.hub.encerrar()
    painel.hub.instantaneo = lambda idade_maxima_s=0.5: None
    r = painel.post("/api/robot/familia/reconhecer")
    assert r.status_code == 200 and r.json() == {"ok": False, "reason": "camera_unavailable",
                                                  "detail": familia.explicar("camera_unavailable"), "at": "21:40:05"}


def test_o_interruptor_e_salvo_pela_configuracao_normal(painel) -> None:
    r = painel.put("/api/robot/conversation", json={"family_recognition_enabled": True})
    assert r.status_code == 200, r.text
    assert painel.get("/api/robot/familia/estado").json()["enabled"] is True
