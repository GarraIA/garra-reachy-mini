"""A situação da casa no laço de voz de verdade: "Fala Garra, acende a luz da sala"."""

from __future__ import annotations

import pytest

from garra_reachy_mini import envelope
from test_escuta_no_laco import Cena


class CasaFalsa:
    def __init__(self, dados) -> None:
        self.dados, self.chamadas = dados, 0

    def estado(self):
        self.chamadas += 1
        return self.dados


@pytest.fixture()
def montar(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.setenv("GARRA_LIMIAR", "0.05")
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "0")
    monkeypatch.setenv("GARRA_NOTIFICACOES_S", "3600")
    cenas = []

    def _montar(roteiro, respostas, dados):
        c = Cena(backend, tmp_path, {"wake_phrase_enabled": True}, roteiro, respostas)
        c.casa = CasaFalsa(dados)
        c.app._cliente_da_casa = lambda: c.casa
        cenas.append(c)
        return c

    yield _montar
    for c in cenas:
        c.encerrar()


def test_por_voz_o_pedido_da_casa_leva_a_situacao(montar) -> None:
    cena = montar(["Fala Garra, acende a luz da sala"], ["Ainda não está configurado."],
                  {"state": "not_configured", "voice_control": False})
    cena.alguem_fala()
    [mensagem] = cena.cerebro.recebidas
    assert mensagem.startswith("[CONTROLE DA CASA") and "Home Assistant" in mensagem
    assert envelope.pedido_de(mensagem) == "acende a luz da sala"
    assert cena.casa.chamadas == 1


def test_por_voz_com_a_ponte_fora_diz_que_nao_conferiu(montar) -> None:
    cena = montar(["Fala Garra, acende a luz da sala"], ["Não consegui verificar."], None)
    cena.alguem_fala()
    [mensagem] = cena.cerebro.recebidas
    assert "não foi possível conferir" in mensagem


def test_por_voz_pedido_comum_nao_consulta_a_casa(montar) -> None:
    cena = montar(["Fala Garra, que horas são?"], ["Três horas."], {"state": "not_configured"})
    cena.alguem_fala()
    assert cena.casa.chamadas == 0 and "CONTROLE DA CASA" not in cena.cerebro.recebidas[0]
