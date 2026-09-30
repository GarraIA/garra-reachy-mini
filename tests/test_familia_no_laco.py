"""A família no laço de voz de verdade: "quem está aí?" por voz.

A costura: o pedido certo faz o robô comparar UM quadro com os perfis, os
nomes vão ao modelo citados e com o aviso, e uma falha vira "não consegui
reconhecer" — nunca o nome da vez anterior. Com o interruptor desligado, nada
disso acontece e o pedido segue como sempre.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from garra_reachy_mini import envelope, familia
from garra_reachy_mini.web.camera import Quadro
from test_escuta_no_laco import Cena
from test_familia import JPEG, MARIA_E_UM_ESTRANHO, Cliente, Hub
from test_visao_no_laco import HubVivo

LIGADO = {"wake_phrase_enabled": True, "family_recognition_enabled": True}


def quadro(dados: bytes = JPEG, seq: int = 1) -> Quadro:
    return Quadro(jpeg=dados, seq=seq, ts=time.monotonic(), largura=640, altura=480)


@pytest.fixture()
def montar(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.setenv("GARRA_LIMIAR", "0.05")
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "0")
    monkeypatch.setenv("GARRA_NOTIFICACOES_S", "3600")
    cenas: list[Cena] = []

    def _montar(conversa, roteiro, respostas, hub=None, cliente=None):
        c = Cena(backend, tmp_path, conversa, roteiro, respostas)
        c.hub, c.cliente = hub or HubVivo(quadro()), cliente or Cliente([MARIA_E_UM_ESTRANHO])
        c.app.familia = familia.Familia(c.hub, c.cliente, c.ctrl.eventos, hora=lambda: "21:40:05")
        cenas.append(c)
        return c

    yield _montar
    for c in cenas:
        c.encerrar()


def test_por_voz_o_robo_reconhece_e_o_modelo_recebe_os_nomes(montar, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cena = montar(LIGADO, ["Fala Garra, quem está aí?"], ["Estou vendo a Maria e mais uma pessoa."])
    cena.alguem_fala()

    [mensagem] = cena.cerebro.recebidas
    linhas = mensagem.split("\n")
    assert linhas[0].startswith("[PESSOAS RECONHECIDAS") and "21:40:05" in linhas[0]
    assert "> Maria" in linhas and "> 1 pessoa não reconhecida" in linhas
    assert envelope.pedido_de(mensagem) == "quem está aí?"
    assert cena.voz.faladas == ["Estou vendo a Maria e mais uma pessoa."]
    assert len(cena.cliente.chamadas) == 1, "um quadro por pedido"
    assert [(e["role"], e["content"]) for e in cena.eventos("chat.message")] == [
        ("user", "quem está aí?"), ("assistant", "Estou vendo a Maria e mais uma pessoa.")]
    # nome só no envelope e na resposta — não em evento de família nem em log
    for e in cena.eventos("family.completed") + cena.eventos("family.requested"):
        assert "Maria" not in json.dumps(e, ensure_ascii=False)
    assert "PESSOAS RECONHECIDAS" not in caplog.text


def test_pedido_que_nao_pergunta_quem_nao_reconhece(montar) -> None:
    cena = montar(LIGADO, ["Fala Garra, que horas são?"], ["São três horas."])
    cena.alguem_fala()
    assert cena.cliente.chamadas == []
    assert "PESSOAS" not in cena.cerebro.recebidas[0]


def test_desligado_o_pedido_segue_sem_bloco_e_sem_chamada(montar) -> None:
    cena = montar({"wake_phrase_enabled": True}, ["Fala Garra, quem está aí?"], ["Não sei dizer."])
    cena.alguem_fala()
    [mensagem] = cena.cerebro.recebidas
    assert "RECONHECIMENTO" not in mensagem and "PESSOAS" not in mensagem
    assert cena.cliente.chamadas == []
    assert cena.eventos("family.requested") == []


def test_camera_parada_nao_vira_nome_inventado(montar) -> None:
    cena = montar(LIGADO, ["Fala Garra, você me conhece?"], ["Não consigo ver agora."], hub=Hub(None))
    cena.alguem_fala()
    [mensagem] = cena.cerebro.recebidas
    assert mensagem.startswith("[RECONHECIMENTO —") and "PESSOAS RECONHECIDAS" not in mensagem
    [falha] = cena.eventos("family.failed")
    assert falha["reason"] == "camera_unavailable"


def test_a_falha_do_meio_nao_repete_o_nome_anterior(montar) -> None:
    hub = HubVivo(quadro(b"\xff\xd8" + b"A" * 40 + b"\xff\xd9", 1))
    cliente = Cliente([MARIA_E_UM_ESTRANHO, "timeout", {"faces": [], "ms": 5}])
    cena = montar(LIGADO, ["Fala Garra, quem está aí?", "Fala Garra, quem está aí?", "Fala Garra, quem está aí?"],
                  ["A Maria.", "Não consegui.", "Ninguém."], hub=hub, cliente=cliente)
    cena.alguem_fala()
    hub.quadro = quadro(b"\xff\xd8" + b"B" * 40 + b"\xff\xd9", 2)
    cena.alguem_fala()
    cena.alguem_fala()
    primeira, segunda, terceira = cena.cerebro.recebidas
    assert "> Maria" in primeira
    assert segunda.startswith("[RECONHECIMENTO —") and "Maria" not in segunda
    assert "> nenhum rosto na imagem" in terceira and "Maria" not in terceira
