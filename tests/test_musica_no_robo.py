"""Música no robô: por voz e pelo chat, a recusa fixa; pelo painel, o repasse."""

from __future__ import annotations

import json

import pytest

from garra_reachy_mini import musica
from test_escuta_no_laco import Cena


@pytest.fixture()
def montar(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.setenv("GARRA_LIMIAR", "0.05")
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "0")
    monkeypatch.setenv("GARRA_NOTIFICACOES_S", "3600")
    cenas: list[Cena] = []

    def _montar(conversa, roteiro, respostas):
        cenas.append(Cena(backend, tmp_path, conversa, roteiro, respostas))
        return cenas[-1]

    yield _montar
    for c in cenas:
        c.encerrar()


def test_por_voz_o_pedido_de_musica_e_recusado_sem_passar_pelo_modelo(montar) -> None:
    cena = montar({"wake_phrase_enabled": True}, ["Fala Garra, toca uma música no Spotify"], [])
    cena.alguem_fala()
    assert cena.cerebro.recebidas == [], "nada da Spotify vai ao modelo, nem o pedido"
    assert " ".join(cena.voz.faladas) == musica.RECUSA_POR_VOZ, "falada por frase, como tudo"
    chat = [(e["role"], e["content"]) for e in cena.eventos("chat.message")]
    assert chat == [("user", "toca uma música no Spotify"), ("assistant", musica.RECUSA_POR_VOZ)]


def test_pedido_comum_continua_indo_ao_modelo(montar) -> None:
    cena = montar({"wake_phrase_enabled": True}, ["Fala Garra, que horas são?"], ["São três."])
    cena.alguem_fala()
    assert cena.cerebro.recebidas == ["que horas são?"]


def test_a_regra_do_robo_e_a_mesma_do_companion() -> None:
    from companion import musica as do_pc
    for frase in ("toca uma música", "que horas são?", "pausa", "qual o volume do tanque?",
                  "play some music", "coloca o spotify"):
        assert musica.pede_controle(frase) == do_pc.pede_controle(frase), frase
    assert musica.RECUSA_POR_VOZ == do_pc.RECUSA_POR_VOZ
