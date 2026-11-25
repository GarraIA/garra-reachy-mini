"""O chamado da conversa ao vivo, no laço de voz de verdade.

`test_roteador.py` confere a decisão por texto. Aqui roda o `_laco_voz` inteiro
e se confere o que saiu por cada porta: o que o agente do Garra recebeu, o que
foi para o contexto da sala, o que o alto-falante disse e os eventos.

Nesta fase o robô reconhece o chamado e ainda não conduz a conversa: ele diz que
a Hera não está disponível. O que já tem de valer é a FRONTEIRA entre os dois
chamados — é ela que não pode vazar.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from garra_reachy_mini import ao_vivo
from test_escuta_no_laco import Cena, montar_cena  # noqa: F401  (fixture)

GARRA = {"wake_phrase_enabled": True, "wake_phrase_text": "Fala Garra",
         "wake_second_listen_enabled": False, "wake_ack_sound_enabled": False,
         "wake_ack_motion_enabled": False}
COM_HERA = {**GARRA, "live_voice_enabled": True}


def esperar(condicao, segundos: float = 10.0) -> bool:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.05)
    return False


def chamar(cena: Cena) -> None:
    """Uma fala que é chamado da Hera, e espera o laço terminar de atendê-la."""
    antes = len(cena.eventos("voice.live.unavailable"))
    cena.mic.dizer()
    assert esperar(lambda: len(cena.eventos("voice.live.unavailable")) > antes), \
        "o chamado da conversa ao vivo não foi atendido"
    time.sleep(1.6)      # o aviso falado e o dreno do microfone depois dele


# ─── desligada: nada muda ────────────────────────────────────────────────────
def test_desligada_o_nome_e_conversa_da_sala_como_sempre(montar_cena):
    cena = montar_cena(GARRA, ["Oi Hera, que horas são?"], [])
    cena.alguem_fala()
    assert [e["reason"] for e in cena.eventos("voice.wake.rejected")] == ["wake_phrase_required"]
    assert cena.eventos("voice.live.unavailable") == []
    assert cena.cerebro.recebidas == [] and cena.voz.faladas == []


def test_desligada_hera_no_meio_da_conversa_com_o_garra_vai_ao_garra(montar_cena):
    cena = montar_cena(GARRA, ["Fala Garra", "Hera"], ["Quem?"])
    cena.alguem_fala()
    cena.alguem_fala()
    assert cena.cerebro.recebidas and "Hera" in cena.cerebro.recebidas[0]


# ─── ligada: a fronteira ─────────────────────────────────────────────────────
def test_o_chamado_da_hera_nao_chega_ao_agente_do_garra(montar_cena):
    cena = montar_cena(COM_HERA, ["Oi Hera, que horas são?"], [])
    chamar(cena)
    assert cena.cerebro.recebidas == []
    (evento,) = cena.eventos("voice.live.unavailable")
    assert evento["destination"] == "hera"
    # Não é recusa do Garra, nem quase-chamado dele, nem turno.
    for alheio in ("voice.wake.rejected", "voice.wake.near_miss", "voice.wake.activated",
                   "voice.turn.started", "chat.message"):
        assert cena.eventos(alheio) == [], alheio


def test_o_que_foi_dito_a_hera_nao_vai_para_o_contexto_do_garra(montar_cena):
    """A escuta de contexto guarda a conversa da sala para o próximo pedido ao
    Garra. O que se diz à Hera não é conversa da sala."""
    cena = montar_cena({**COM_HERA, "ambient_context_enabled": True},
                       ["Oi Hera, qual é a senha do cofre?", "Fala Garra, anota isso"], ["Anotado."])
    chamar(cena)
    cena.alguem_fala()
    (recebido,) = cena.cerebro.recebidas
    assert "anota isso" in recebido
    assert "cofre" not in recebido and "Hera" not in recebido


def test_o_chamado_fecha_a_conversa_aberta_com_o_garra(montar_cena):
    cena = montar_cena(COM_HERA, ["Fala Garra", "Hera", "que horas são?"], [])
    cena.alguem_fala()                    # abre a conversa com o Garra
    chamar(cena)                          # troca de conversa
    cena.alguem_fala()                    # sem chamado: não é mais com o Garra
    assert cena.cerebro.recebidas == []
    assert cena.eventos("voice.wake.rejected")[-1]["reason"] == "wake_phrase_required"


def test_era_isso_com_o_garra_aberto_continua_sendo_do_garra(montar_cena):
    """O caso que o rigor estrito existe para proteger."""
    cena = montar_cena(COM_HERA, ["Fala Garra", "Era isso que eu queria."], ["Entendi."])
    cena.alguem_fala()
    cena.alguem_fala()
    assert cena.cerebro.recebidas and "Era isso que eu queria." in cena.cerebro.recebidas[0]
    assert cena.eventos("voice.live.unavailable") == []


def test_o_pedido_que_veio_com_o_chamado_nao_fica_em_evento_nem_em_log(montar_cena, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    cena = montar_cena(COM_HERA, ["Oi Hera, qual é a senha do cofre?"], [])
    chamar(cena)
    assert "cofre" not in caplog.text.lower()
    assert "cofre" not in str(cena.eventos()).lower()


# ─── o aviso de indisponível ─────────────────────────────────────────────────
def test_indisponivel_e_dito_uma_vez_e_nao_a_cada_chamado(montar_cena):
    cena = montar_cena({**COM_HERA, "speech_output_enabled": True}, ["Hera", "Hera"], [])
    chamar(cena)
    chamar(cena)
    assert cena.voz.faladas == [ao_vivo.AVISO_INDISPONIVEL]
    assert len(cena.eventos("voice.live.unavailable")) == 2


def test_com_a_saida_de_voz_desligada_o_aviso_e_so_o_evento(montar_cena):
    cena = montar_cena({**COM_HERA, "speech_output_enabled": False}, ["Hera"], [])
    chamar(cena)
    assert cena.voz.faladas == [] and len(cena.eventos("voice.live.unavailable")) == 1


# ─── parada ──────────────────────────────────────────────────────────────────
def test_hera_pare_e_parada_e_nao_chamado(montar_cena, monkeypatch):
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "1")
    cena = montar_cena(COM_HERA, ["Hera, pare"], [])
    cena.mic.dizer()
    assert esperar(lambda: cena.ctrl.em_estop), "\"Hera, pare\" não parou o robô"
    assert cena.eventos("voice.live.unavailable") == []
    assert cena.cerebro.recebidas == []


# ─── a ordem no fonte ────────────────────────────────────────────────────────
MAIN = (Path(__file__).resolve().parent.parent / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")


def _processar() -> str:
    linhas = MAIN.splitlines()
    i = next(n for n, l in enumerate(linhas) if "def processar(" in l)
    nivel = len(linhas[i]) - len(linhas[i].lstrip())
    for n in range(i + 1, len(linhas)):
        if linhas[n].strip() and (len(linhas[n]) - len(linhas[n].lstrip())) <= nivel:
            return "\n".join(linhas[i:n])
    return "\n".join(linhas[i:])


def test_o_chamado_e_decidido_antes_do_portao_e_atendido_fora_do_turno():
    corpo = _processar()
    assert corpo.index("roteador.chamado") < corpo.index("ativacao.avaliar")
    assert corpo.index("roteador.parada") < corpo.index("_sonda_escuta"), \
        "\"Hera, pare\" tem de ser parada antes até do teste de escuta"
    # Quem conduz a conversa é o laço, dono do microfone — nunca `processar`.
    assert "atendente" not in corpo and "ao_vivo." not in corpo
    assert MAIN.count("atender_ao_vivo()") >= 3      # a definição e as duas chamadas
