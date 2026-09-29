"""A segunda escuta e o diagnóstico da frase, no laço de voz de verdade.

O que se mediu em 29/09/2026, pelo microfone do robô: a frase de ativação sai
errada do Whisper na maior parte das vezes ("fala gabi", "fala garne", "fala
gab"…) e sai certa quando ele sabe qual é a frase. Então o robô pergunta de
novo — mas só quando o COMEÇO do que ouviu já lembrava a frase.

E ninguém conseguia ver por que o robô não respondia: o evento de recusa não
diz o que foi ouvido, de propósito. O diagnóstico, que nasce desligado, mostra o
trecho parecido com a frase e mais nada.
"""

from __future__ import annotations

import json
import logging

import pytest

from test_escuta_no_laco import Cena, Voz

FRASE = "Fala Garra"          # como está na configuração: foi assim que se mediu
ERRADA = "Fala Gabi, que horas são?"
CERTA = "Fala Garra, que horas são?"
LIGADA = {"wake_phrase_enabled": True}


class VozComDica(Voz):
    """Cada fala do roteiro é um par: o que o STT ouve sozinho, e o que ouve
    sabendo a frase. `None` no segundo: o servidor de voz falha."""

    def __init__(self, pares: list[tuple[str, str | None]]) -> None:
        super().__init__([])
        self.pares = list(pares)
        self.dicas: list[str | None] = []
        self._atual: tuple[str, str | None] = ("", "")

    def transcrever(self, audio, sr, dica=None) -> str:
        self.dicas.append(dica)
        if dica is None:
            self._atual = self.pares[self.transcritas]
            self.transcritas += 1
            return self._atual[0]
        if self._atual[1] is None:
            raise RuntimeError("servidor de voz fora do ar")
        return self._atual[1]


@pytest.fixture()
def montar(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.setenv("GARRA_LIMIAR", "0.05")
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "0")
    monkeypatch.setenv("GARRA_NOTIFICACOES_S", "3600")
    cenas: list[Cena] = []

    def _montar(conversa: dict, pares, respostas: list[str]) -> Cena:
        cenas.append(Cena(backend, tmp_path, conversa, [], respostas, voz=VozComDica(pares)))
        return cenas[-1]

    yield _montar
    for c in cenas:
        c.encerrar()


def tudo(cena: Cena) -> str:
    return json.dumps(cena.eventos(), ensure_ascii=False).lower()


# ─── a segunda escuta ────────────────────────────────────────────────────────
def test_frase_que_saiu_parecida_e_ouvida_de_novo_e_o_robo_responde(montar) -> None:
    cena = montar(LIGADA, [(ERRADA, CERTA)], ["São três horas."])
    cena.alguem_fala()

    assert cena.voz.dicas == [None, FRASE]
    assert cena.cerebro.recebidas == ["que horas são?"]
    assert cena.voz.faladas == ["São três horas."]
    [ativou] = cena.eventos("voice.wake.activated")
    assert ativou["match"] == "second_listen"
    assert [(e["role"], e["content"]) for e in cena.eventos("chat.message")] == [
        ("user", "que horas são?"), ("assistant", "São três horas.")]


def test_segunda_escuta_que_nao_confirma_nao_ativa(montar) -> None:
    cena = montar(LIGADA, [("Fala, cara, tudo bem?", "Fala, cara, tudo bem?")], [])
    cena.alguem_fala()

    assert cena.voz.dicas == [None, FRASE]
    assert cena.cerebro.recebidas == [] and cena.voz.faladas == []
    assert cena.eventos("voice.wake.activated") == []


def test_so_vale_a_frase_exata_na_segunda_escuta(montar) -> None:
    """A dica empurra o Whisper para a frase. Se nem assim ela saiu inteira,
    não foi dita."""
    cena = montar(LIGADA, [(ERRADA, "Fala agarra, que horas são?")], [])
    cena.alguem_fala()
    assert cena.cerebro.recebidas == []


def test_conversa_comum_nao_e_ouvida_duas_vezes(montar) -> None:
    cena = montar(LIGADA, [("Acabou a manteiga, hein.", CERTA),
                           ("Que horas são agora?", CERTA)], [])
    cena.alguem_fala()
    cena.alguem_fala()

    assert cena.voz.dicas == [None, None]
    assert cena.cerebro.recebidas == []


def test_a_segunda_escuta_pode_ser_desligada(montar) -> None:
    cena = montar({**LIGADA, "wake_second_listen_enabled": False}, [(ERRADA, CERTA)], [])
    cena.alguem_fala()

    assert cena.voz.dicas == [None]
    assert cena.cerebro.recebidas == []


def test_sem_frase_de_ativacao_nao_ha_o_que_conferir(montar) -> None:
    cena = montar({"wake_phrase_enabled": False}, [(ERRADA, CERTA)], ["Oi."])
    cena.alguem_fala()

    assert cena.voz.dicas == [None]
    assert cena.cerebro.recebidas == [ERRADA]


def test_com_a_sessao_aberta_ninguem_pergunta_de_novo(montar) -> None:
    cena = montar(LIGADA, [(CERTA, CERTA), ("Fala Gabi, e amanhã?", CERTA)],
                  ["São três horas.", "Amanhã também."])
    cena.alguem_fala()
    cena.alguem_fala()

    assert cena.voz.dicas == [None, None]
    assert cena.cerebro.recebidas == ["que horas são?", "Fala Gabi, e amanhã?"]


def test_servidor_de_voz_que_falha_na_segunda_escuta_nao_derruba_o_laco(montar) -> None:
    cena = montar(LIGADA, [(ERRADA, None), (CERTA, CERTA)], ["São três horas."])
    cena.alguem_fala()
    assert cena.cerebro.recebidas == []
    # Conferido AGORA, antes da próxima fala: uma transcrição que dá certo zera
    # o contador e esconderia o erro.
    assert cena.app._falhas_voz == 0, "falha na conferência não é a voz caindo"
    cena.alguem_fala()
    assert cena.cerebro.recebidas == ["que horas são?"]


def test_a_falha_da_segunda_escuta_nao_leva_o_enunciado_para_o_log(montar, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cena = montar(LIGADA, [(ERRADA, None)], [])
    cena.alguem_fala()
    assert "segunda escuta falhou" in caplog.text
    assert "gabi" not in caplog.text.lower() and "horas" not in caplog.text.lower()


def test_a_frase_recusada_nao_vai_para_o_log_nem_a_conferida(montar, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cena = montar(LIGADA, [("Fala, cara, tudo bem com você?", "Fala, cara, tudo bem com você?")], [])
    cena.alguem_fala()
    assert "cara" not in caplog.text.lower()
    assert "tudo bem" not in caplog.text.lower()


def test_o_que_foi_confirmado_nao_vira_conversa_da_sala(montar) -> None:
    cena = montar({**LIGADA, "ambient_context_enabled": True},
                  [(ERRADA, CERTA)], ["São três horas."])
    cena.alguem_fala()
    assert len(cena.app.acompanhamento.memoria) == 0
    assert cena.cerebro.recebidas == ["que horas são?"]


def test_o_que_nao_foi_confirmado_continua_sendo_conversa_da_sala(montar) -> None:
    cena = montar({**LIGADA, "ambient_context_enabled": True},
                  [("Fala, cara, tudo bem?", "Fala, cara, tudo bem?")], [])
    cena.alguem_fala()
    assert cena.app.acompanhamento.memoria.medida()["utterances"] == 1


# ─── o diagnóstico ───────────────────────────────────────────────────────────
DIAGNOSTICO = {**LIGADA, "wake_diagnostics_enabled": True}


def test_desligado_nenhum_evento_diz_o_que_foi_ouvido(montar) -> None:
    cena = montar(LIGADA, [(ERRADA, ERRADA)], [])
    cena.alguem_fala()

    assert cena.eventos("voice.wake.near_miss") == []
    assert "gabi" not in tudo(cena) and "horas" not in tudo(cena)


def test_ligado_o_evento_traz_so_o_trecho_parecido(montar) -> None:
    cena = montar(DIAGNOSTICO, [(ERRADA, ERRADA)], [])
    cena.alguem_fala()

    [perto] = cena.eventos("voice.wake.near_miss")
    assert (perto["heard"], perto["position"], perto["distance"]) == ("fala gabi", 0, 3)
    assert perto["second_listen"] == "rejected"
    assert "horas" not in tudo(cena), "o resto do enunciado não entra"


def test_o_diagnostico_conta_quando_a_segunda_escuta_salvou(montar) -> None:
    cena = montar(DIAGNOSTICO, [(ERRADA, CERTA)], ["São três horas."])
    cena.alguem_fala()

    [perto] = cena.eventos("voice.wake.near_miss")
    assert (perto["heard"], perto["second_listen"]) == ("fala gabi", "confirmed")


def test_o_diagnostico_mostra_a_frase_que_veio_fora_do_comeco(montar) -> None:
    cena = montar(DIAGNOSTICO, [("E aí, fala Garra, que horas são?", CERTA)], [])
    cena.alguem_fala()

    [perto] = cena.eventos("voice.wake.near_miss")
    assert (perto["heard"], perto["position"], perto["distance"]) == ("fala garra", 2, 0)
    assert perto["second_listen"] == "not_tried"
    assert cena.voz.dicas == [None]


def test_o_diagnostico_diz_quando_a_conferencia_falhou(montar) -> None:
    cena = montar(DIAGNOSTICO, [(ERRADA, None)], [])
    cena.alguem_fala()
    [perto] = cena.eventos("voice.wake.near_miss")
    assert perto["second_listen"] == "failed"


def test_conversa_que_nao_lembra_a_frase_nao_gera_diagnostico(montar) -> None:
    cena = montar(DIAGNOSTICO, [("Acabou a manteiga, hein.", CERTA)], [])
    cena.alguem_fala()

    assert cena.eventos("voice.wake.near_miss") == []
    assert "manteiga" not in tudo(cena)


def test_o_diagnostico_nao_vai_para_o_log(montar, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cena = montar(DIAGNOSTICO, [(ERRADA, ERRADA)], [])
    cena.alguem_fala()
    assert "gabi" not in caplog.text.lower()
