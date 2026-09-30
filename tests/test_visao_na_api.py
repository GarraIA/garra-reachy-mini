"""A visão pelas rotas do painel: analisar um quadro agora, e o estado."""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini import visao
from garra_reachy_mini.acompanhamento import Acompanhamento
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub, Quadro
from garra_reachy_mini.web.seguranca import resolver_politica
from test_visao import DESCRICAO, JPEG, Cliente, Hub


class ChatFalso:
    base, agent_id, sessao = "http://ponte:8126", "reachy_voice", "s1"
    recebidas: list[str] = []

    async def enviar(self, conteudo: str) -> dict:
        self.recebidas.append(conteudo)
        return {"content": "Vejo uma mesa."}

    async def historico(self) -> list: return []
    async def disponivel(self) -> bool: return True
    async def limpar(self): return "s2"
    async def fechar(self) -> None: pass
    def reconfigurar(self, *a, **k) -> None: pass


@pytest.fixture()
def painel(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub_real = FrameHub(backend, fps_ativo=4.0)
    hub = Hub(Quadro(jpeg=JPEG, seq=1, ts=time.monotonic(), largura=640, altura=480))
    cliente = Cliente([DESCRICAO, DESCRICAO, "provider_offline"])
    olho = visao.Olho(hub, cliente, ctrl.eventos, hora=lambda: "21:40:05")
    chat = ChatFalso()
    chat.recebidas = []
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub_real, eventos=ctrl.eventos, chat=chat,
        acompanhamento=Acompanhamento(ctrl.eventos), olho=olho,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        c.olho, c.cliente, c.hub, c.chat = olho, cliente, hub, chat
        yield c
    hub_real.encerrar()
    ctrl.encerrar(timeout=2)


def ligar(painel, **chaves) -> None:
    assert painel.put("/api/robot/conversation", json=chaves).status_code == 200


def test_analisar_pega_um_quadro_e_devolve_a_descricao(painel) -> None:
    ligar(painel, vision_enabled=True)
    r = painel.post("/api/robot/vision/analyze", json={"question": "o que tem na mesa?"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["text"] == "Uma mesa com um copo azul."
    assert (d["provider"], d["model"], d["captured_at"]) == ("local", "qwen2.5vl:7b", "21:40:05")
    assert painel.cliente.chamadas[0][1] == "o que tem na mesa?"


def test_analisar_com_a_visao_desligada_diz_por_que(painel) -> None:
    r = painel.post("/api/robot/vision/analyze")
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["reason"] == "disabled"
    assert painel.cliente.chamadas == []


def test_analisar_com_o_provedor_fora_do_ar_diz_por_que(painel) -> None:
    ligar(painel, vision_enabled=True)
    painel.post("/api/robot/vision/analyze")
    painel.post("/api/robot/vision/analyze")
    r = painel.post("/api/robot/vision/analyze")
    assert r.json() == {"ok": False, "reason": "provider_offline",
                        "detail": visao.explicar("provider_offline"), "at": "21:40:05"}


def test_o_estado_traz_a_configuracao_e_a_ultima_analise_sem_texto(painel) -> None:
    ligar(painel, vision_enabled=True, vision_provider="local")
    painel.post("/api/robot/vision/analyze")
    d = painel.get("/api/robot/vision/status").json()
    assert d["settings"]["vision_provider"] == "local" and d["settings"]["vision_enabled"] is True
    assert d["last"]["ok"] is True and d["last"]["provider"] == "local"
    assert d["providers"] == {"effective": "local"}
    assert "copo" not in r_text(d)


def r_text(d) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)


def test_o_estado_com_a_visao_desligada_nao_consulta_a_ponte(painel) -> None:
    d = painel.get("/api/robot/vision/status").json()
    assert d["providers"] is None and d["last"] is None


def test_o_chat_digitado_tambem_olha_quando_o_texto_pede(painel) -> None:
    ligar(painel, vision_enabled=True)
    painel.post("/api/chat/enviar", json={"content": "o que você está vendo?"})
    [mensagem] = painel.chat.recebidas
    assert mensagem.startswith("[O QUE A CÂMERA VÊ") and "> Uma mesa com um copo azul." in mensagem
    painel.post("/api/chat/enviar", json={"content": "que horas são?"})
    assert "CÂMERA" not in painel.chat.recebidas[1]


def test_a_pergunta_do_painel_e_limitada(painel) -> None:
    ligar(painel, vision_enabled=True)
    painel.post("/api/robot/vision/analyze", json={"question": "x" * 900})
    assert len(painel.cliente.chamadas[0][1]) == 500
