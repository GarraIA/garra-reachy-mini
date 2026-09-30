"""A visão no laço de voz de verdade: "o que você está vendo?" por voz.

O que se prova aqui é a costura: o pedido certo faz o robô olhar UMA vez, o
que foi visto vai ao modelo citado e com a hora, e uma falha vira "não estou
conseguindo ver" — nunca a descrição da vez anterior.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from garra_reachy_mini import envelope, visao
from garra_reachy_mini.web.camera import Quadro
from test_escuta_no_laco import Cena
from test_visao import DESCRICAO, JPEG, Cliente, Hub

LIGADA = {"wake_phrase_enabled": True, "vision_enabled": True}


def quadro(dados: bytes = JPEG, seq: int = 1) -> Quadro:
    return Quadro(jpeg=dados, seq=seq, ts=time.monotonic(), largura=640, altura=480)


class HubVivo(Hub):
    """Como o `FrameHub` de verdade: se o quadro guardado envelheceu, captura
    outro — aqui, o mesmo conteúdo com carimbo novo."""

    def instantaneo(self, idade_maxima_s: float = 0.5) -> Quadro | None:
        q = super().instantaneo(idade_maxima_s)
        if q is not None and q.idade_s > idade_maxima_s:
            self.quadro = q = Quadro(jpeg=q.jpeg, seq=q.seq + 1, ts=time.monotonic(),
                                     largura=q.largura, altura=q.altura)
        return q


@pytest.fixture()
def montar(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.setenv("GARRA_LIMIAR", "0.05")
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "0")
    monkeypatch.setenv("GARRA_NOTIFICACOES_S", "3600")
    cenas: list[Cena] = []

    def _montar(conversa, roteiro, respostas, hub=None, cliente=None):
        c = Cena(backend, tmp_path, conversa, roteiro, respostas)
        c.hub, c.cliente = hub or HubVivo(quadro()), cliente or Cliente([DESCRICAO])
        c.app.olho = visao.Olho(c.hub, c.cliente, c.ctrl.eventos, hora=lambda: "21:40:05")
        cenas.append(c)
        return c

    yield _montar
    for c in cenas:
        c.encerrar()


def test_por_voz_o_robo_olha_e_o_modelo_recebe_o_que_foi_visto(montar, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cena = montar(LIGADA, ["Fala Garra, o que você está vendo?"], ["Vejo uma mesa com um copo azul."])
    cena.alguem_fala()

    [mensagem] = cena.cerebro.recebidas
    linhas = mensagem.split("\n")
    assert linhas[0].startswith("[O QUE A CÂMERA VÊ") and "21:40:05" in linhas[0]
    assert "> Uma mesa com um copo azul." in linhas
    assert envelope.pedido_de(mensagem) == "o que você está vendo?"
    assert cena.voz.faladas == ["Vejo uma mesa com um copo azul."]
    assert len(cena.cliente.chamadas) == 1, "um quadro por pedido"
    # o chat mostra o que a pessoa disse, e não o envelope
    assert [(e["role"], e["content"]) for e in cena.eventos("chat.message")] == [
        ("user", "o que você está vendo?"), ("assistant", "Vejo uma mesa com um copo azul.")]
    # e a descrição não está em evento nem em log
    assert "copo azul" not in json.dumps(cena.eventos(), ensure_ascii=False).replace(
        "Vejo uma mesa com um copo azul.", "")
    assert "O QUE A CÂMERA VÊ" not in caplog.text


def test_pedido_que_nao_pede_para_olhar_nao_olha(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, que horas são?"], ["São três horas."])
    cena.alguem_fala()
    assert cena.cliente.chamadas == []
    assert "CÂMERA" not in cena.cerebro.recebidas[0]


def test_com_a_visao_desligada_o_modelo_e_mandado_a_dizer_que_nao_ve(montar) -> None:
    cena = montar({"wake_phrase_enabled": True}, ["Fala Garra, o que você está vendo?"],
                  ["Não estou conseguindo ver agora."])
    cena.alguem_fala()
    [mensagem] = cena.cerebro.recebidas
    assert mensagem.startswith("[CÂMERA —") and "desligada" in mensagem
    assert cena.cliente.chamadas == []


def test_camera_parada_nao_vira_descricao_inventada(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, o que tem na sua frente?"], ["Não consigo ver."],
                  hub=Hub(None))
    cena.alguem_fala()
    [mensagem] = cena.cerebro.recebidas
    assert mensagem.startswith("[CÂMERA —") and "O QUE A CÂMERA VÊ" not in mensagem
    [falha] = cena.eventos("vision.failed")
    assert falha["reason"] == "camera_unavailable"


def test_duas_cenas_duas_descricoes_e_a_falha_do_meio_nao_repete_a_anterior(montar) -> None:
    hub = HubVivo(quadro(b"\xff\xd8" + b"A" * 40 + b"\xff\xd9", 1))
    cliente = Cliente([{**DESCRICAO, "texto": "Um copo."}, "timeout", {**DESCRICAO, "texto": "Um livro."}])
    cena = montar(LIGADA, ["Fala Garra, o que você vê?", "Fala Garra, o que você vê?",
                           "Fala Garra, o que você vê?"], ["Um copo.", "Não vejo.", "Um livro."])
    cena.hub, cena.cliente = hub, cliente
    cena.app.olho = visao.Olho(hub, cliente, cena.ctrl.eventos, hora=lambda: "21:40:05")

    cena.alguem_fala()
    hub.quadro = quadro(b"\xff\xd8" + b"B" * 40 + b"\xff\xd9", 2)
    cena.alguem_fala()
    cena.alguem_fala()

    primeira, segunda, terceira = cena.cerebro.recebidas
    assert "> Um copo." in primeira
    assert segunda.startswith("[CÂMERA —") and "copo" not in segunda
    assert "> Um livro." in terceira and "copo" not in terceira
