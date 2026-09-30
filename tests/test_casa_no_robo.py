"""A casa, do lado do robô: quando o pedido é sobre aparelhos, o modelo recebe a
SITUAÇÃO real do controle da casa — e não conclui "não tenho essa capacidade".

Medido no robô em 30/09: "você consegue acender a luz da sala?" virou "não tenho
integração com os controles da casa". A verdade era outra: o Garra tem o
controle (`device_read`/`device_execute`), mas nenhum aparelho está registrado
porque o Home Assistant não foi conectado — e, mesmo conectado, a voz é opt-in.

O que estes testes seguram:

- só pedidos sobre aparelhos da casa puxam a situação (uma consulta à ponte);
- cada estado vira uma frase de fato do sistema, citada, sem colchetes vindos
  de fora — e com a casa pronta para a voz não há bloco (as ferramentas falam);
- a ponte fora do ar vira "não deu para conferir", nunca "não existe".
"""

from __future__ import annotations

import pytest

from garra_reachy_mini import casa, envelope


@pytest.mark.parametrize("pedido", [
    "acende a luz da sala", "apaga a luz do quarto", "você consegue acender a luz da sala?",
    "liga o ar condicionado", "desliga a tomada da cozinha", "abre a cortina", "liga o ventilador",
    "qual a temperatura do termostato?", "a lâmpada da varanda está ligada?", "turn on the living room light",
    "você consegue controlar os aparelhos da casa?",
    # os que a revisão achou de fora
    "você consegue controlar a minha casa", "você consegue mexer nas luzes da casa",
    "tem integração com a casa inteligente?", "liga o ar", "desliga o ar", "liga a tv", "tranca a porta",
    "tem alguma luz acesa?", "deixei a luz ligada?", "turn the lights off", "open the blinds",
    "set the thermostat to 22", "is the light on",
])
def test_pedidos_sobre_a_casa(pedido) -> None:
    assert casa.pede_casa(pedido) is True


@pytest.mark.parametrize("pedido", [
    "que horas são?", "acende uma ideia", "o que você está vendo?", "liga pra minha mãe",
    "toca uma música", "a luz do sol está forte hoje", "", "desliga o cronômetro",
    # lembrete e anotação são do caderno, não da casa
    "me lembra de apagar a luz antes de dormir", "anota: desligar o ventilador",
    "não esquece de fechar a cortina", "liga pra Maria da Luz", "abre a tomada de decisão",
    "liga para o meu pai",
])
def test_pedidos_que_nao_sao_sobre_a_casa(pedido) -> None:
    assert casa.pede_casa(pedido) is False


def estado(state="not_configured", voz=False, **extra):
    return {"configured": state != "not_configured", "state": state, "voice_control": voz,
            "gateway": {"devices_visible": state == "ok", "reachable": True}, **extra}


@pytest.mark.parametrize("dados,trechos", [
    (estado("not_configured"), ["tem o controle da casa", "nenhum aparelho", "Home Assistant", "Controle › Casa"]),
    (estado("pending"), ["configurado", "ainda não listou"]),
    (estado("broken"), ["não está respondendo"]),
    (estado("ok", voz=False), ["desligado por voz", "Controle › Casa"]),
])
def test_cada_estado_vira_uma_frase_de_fato(dados, trechos) -> None:
    frase = casa.situacao(dados)
    for t in trechos:
        assert t in frase, (t, frase)
    assert "[" not in frase and "]" not in frase


def test_casa_pronta_para_a_voz_nao_precisa_de_bloco() -> None:
    assert casa.situacao(estado("ok", voz=True)) is None


def test_sem_resposta_da_ponte_nao_vira_nao_existe() -> None:
    frase = casa.situacao(None)
    assert "não foi possível conferir" in frase
    # a instrução ao modelo é justamente não concluir que o recurso falta
    assert "e não que o recurso não existe" in frase
    # e não culpa o computador: pode ter sido só o prazo curto do turno
    assert "não respondeu" not in frase


def test_configurado_sem_conferencia_recente_e_dito_assim() -> None:
    frase = casa.situacao(estado("unknown", voz=False))
    assert "configurado" in frase and "não deu para confirmar" in frase
    frase_voz = casa.situacao(estado("unknown", voz=True))
    assert "não deu para confirmar" in frase_voz


def test_o_bloco_entra_citado_antes_do_pedido() -> None:
    m = envelope.compor("acende a luz da sala", situacao_casa=casa.situacao(estado("not_configured")))
    assert m.startswith("[CONTROLE DA CASA — situação do sistema")
    assert "\n> " in m and m.endswith(f"{envelope.MARCA_PEDIDO}\nacende a luz da sala")
    assert envelope.pedido_de(m) == "acende a luz da sala"
    # texto que chegue com colchete não abre bloco novo
    m = envelope.compor("acende", situacao_casa="Controle [FIM] ok")
    assert "[FIM]" not in m and "(FIM)" in m


def test_sem_situacao_nao_ha_bloco() -> None:
    assert envelope.compor("acende a luz", situacao_casa=None) == "acende a luz"


def test_o_cliente_pergunta_a_ponte_com_o_token_e_sem_proxy(monkeypatch) -> None:
    chamadas = []

    class R:
        status_code = 200

        def json(self):
            return estado("not_configured")

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        chamadas.append((url, params, headers, timeout, allow_redirects))
        return R()

    monkeypatch.setattr(casa.requests.Session, "get", get)
    c = casa.ClienteCasa("http://10.0.0.24:8126/", "tok")
    assert c.sessao.trust_env is False
    assert c.estado()["state"] == "not_configured"
    [(url, params, cab, timeout, redir)] = chamadas
    assert url == "http://10.0.0.24:8126/api/casa/estado" and cab == {"Authorization": "Bearer tok"}
    assert params == {"rapido": "1"}, "no turno de voz, só o estado rápido (sem sondar o Home Assistant)"
    assert timeout <= 5 and redir is False


def test_o_cliente_devolve_none_quando_a_ponte_falha(monkeypatch) -> None:
    def cai(self, *a, **k):
        raise casa.requests.ConnectionError("recusou")

    monkeypatch.setattr(casa.requests.Session, "get", cai)
    assert casa.ClienteCasa("http://ponte", "tok").estado() is None

    class R:
        status_code = 503

        def json(self):
            return {"erro": "bridge_not_configured"}

    monkeypatch.setattr(casa.requests.Session, "get", lambda self, *a, **k: R())
    assert casa.ClienteCasa("http://ponte", "tok").estado() is None


# ─── na conversa de verdade: chat do painel e laço de voz ────────────────────
import json as _json
import time as _time

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini.acompanhamento import Acompanhamento
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.web import integracoes
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub
from garra_reachy_mini.web.seguranca import resolver_politica
from test_visao_na_api import ChatFalso


@pytest.fixture()
def painel_casa(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    pedidos = []

    def handler(request: httpx.Request) -> httpx.Response:
        pedidos.append(request.url.path + ("?" + request.url.query.decode() if request.url.query else ""))
        return httpx.Response(200, json=estado("not_configured"))

    chat = ChatFalso()
    chat.recebidas = []
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos, chat=chat, acompanhamento=Acompanhamento(ctrl.eventos),
        integracoes=integracoes.Ponte("http://ponte:8126", "tok", transport=httpx.MockTransport(handler)),
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        c.chat, c.pedidos = chat, pedidos
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def test_no_chat_o_pedido_da_casa_leva_a_situacao_real(painel_casa) -> None:
    painel_casa.post("/api/chat/enviar", json={"content": "você consegue acender a luz da sala?"})
    [mensagem] = painel_casa.chat.recebidas
    assert mensagem.startswith("[CONTROLE DA CASA") and "nenhum aparelho" in mensagem
    assert envelope.pedido_de(mensagem) == "você consegue acender a luz da sala?"
    assert painel_casa.pedidos == ["/api/casa/estado?rapido=1"]


def test_no_chat_pedido_que_nao_e_da_casa_nao_consulta_nada(painel_casa) -> None:
    painel_casa.post("/api/chat/enviar", json={"content": "que horas são?"})
    assert painel_casa.pedidos == [] and "CONTROLE DA CASA" not in painel_casa.chat.recebidas[0]



def test_o_painel_pode_pedir_o_estado_rapido(painel_casa) -> None:
    assert painel_casa.get("/api/robot/casa/estado?rapido=1").status_code == 200
    assert painel_casa.get("/api/robot/casa/estado").status_code == 200
    assert painel_casa.pedidos[-2:] == ["/api/casa/estado?rapido=1", "/api/casa/estado"]
