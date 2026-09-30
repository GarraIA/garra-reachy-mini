"""Música no laço de voz: pedido falado → ferramenta do PC → confirmação falada.

O PC aqui é um cliente de mentira; o laço, o STT de roteiro e o TTS são os do
`test_escuta_no_laco`. O que se prova (letras da missão de 30/09):

- F: o robô só diz "está tocando" com o resultado conferido do PC, e a falha
  vira a frase do problema real;
- o pedido de música não vai ao modelo, e nada da Spotify vai (III.14);
- de fábrica, o comando por voz está desligado e o robô diz como ligar;
- comando solto ("continua") só é música com música no assunto;
- a música é abaixada para a confirmação e devolvida depois, e também enquanto
  o modelo responde outra coisa com a música tocando.
"""

from __future__ import annotations

import threading
import time

import pytest

from garra_reachy_mini import envelope, musica
from test_escuta_no_laco import Cena

LIGADA = {"wake_phrase_enabled": True, "music_voice_enabled": True}


def tocando(nome="Hotel California - 2013 Remaster", artistas=("Eagles",), **extra):
    r = {"provider": "spotatui", "status": "playing", "reason": None, "device": "spotatui",
         "track": {"name": nome, "artists": list(artistas), "album": None, "uri": "spotify:track:x"},
         "playlist": None, "observed": {"state": "playing", "device": "spotatui", "progress_s": 2,
                                        "volume_percent": 40, "advancing": True},
         "verified": True, "evidence": "player_state", "already": False, "duplicate": False,
         "ducked": {"ducked": True, "from": 40, "to": 12}}
    r.update(extra)
    return r


class ClienteFalso:
    def __init__(self, respostas: dict | None = None, atraso_s: float = 0.0) -> None:
        self.respostas = respostas or {}
        self.atraso_s = atraso_s
        self.pedidos: list[dict] = []
        self.abafados: list[str] = []
        self.restaurados: list[str] = []
        self.feito = threading.Event()

    def comando(self, pedido: dict) -> dict | None:
        self.pedidos.append(pedido)
        if self.atraso_s:
            time.sleep(self.atraso_s)
        r = self.respostas.get(pedido["operation"], tocando())
        return None if r is None else {**r, "operation": pedido["operation"], "request_id": pedido["request_id"]}

    def abafar(self, rid: str) -> dict:
        self.abafados.append(rid)
        return {"ducked": True}

    def restaurar(self, rid: str) -> dict:
        self.restaurados.append(rid)
        return {"restored": True}


@pytest.fixture()
def montar(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.setenv("GARRA_LIMIAR", "0.05")
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "0")
    monkeypatch.setenv("GARRA_NOTIFICACOES_S", "3600")
    cenas: list[Cena] = []

    def _montar(conversa, roteiro, respostas=(), cliente=None):
        cena = Cena(backend, tmp_path, conversa, roteiro, list(respostas))
        cena.cliente = cliente or ClienteFalso()
        cena.app._cliente_da_musica = lambda: cena.cliente
        cenas.append(cena)
        return cena

    yield _montar
    for c in cenas:
        c.encerrar()


def esperar(condicao, prazo_s: float = 10.0) -> None:
    fim = time.monotonic() + prazo_s
    while time.monotonic() < fim:
        if condicao():
            return
        time.sleep(0.05)
    raise AssertionError("a condição não chegou a valer")


def falar_e_esperar_resposta(cena: Cena) -> None:
    """Uma fala e a resposta inteira: a confirmação publicada, falada e o
    microfone drenado — antes disso, a próxima fala do teste seria descartada
    como eco."""
    antes = len([e for e in cena.eventos("chat.message") if e["role"] == "assistant"])
    cena.alguem_fala()
    esperar(lambda: len([e for e in cena.eventos("chat.message") if e["role"] == "assistant"]) > antes)
    time.sleep(0.2)
    esperar(lambda: not cena.app._lock_fala.locked())
    time.sleep(0.3)


def test_toca_e_confirma_com_o_que_o_pc_conferiu(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, toca Hotel California"])
    falar_e_esperar_resposta(cena)
    [pedido] = cena.cliente.pedidos
    correlacao = cena.eventos("chat.message")[0]["correlation_id"]
    assert pedido == {"request_id": correlacao, "source": "voz", "operation": "play_track",
                      "query": "Hotel California", "duck": True}
    assert cena.voz.faladas == ["Pronto, está tocando Hotel California, de Eagles."]
    assert cena.cerebro.recebidas == [], "o pedido de música não vai ao modelo"
    [r] = cena.eventos("music.command.result")
    assert r["status"] == "playing" and r["verified"] is True and r["evidence"] == "player_state"
    esperar(lambda: cena.cliente.restaurados == [correlacao])


def test_sem_conferencia_nao_ha_tocando(montar) -> None:
    cliente = ClienteFalso({"play_track": {**tocando(), "status": "failed", "reason": "not_verified",
                                           "verified": False, "ducked": {"ducked": False}}})
    cena = montar(LIGADA, ["Fala Garra, toca Hotel California"], cliente=cliente)
    falar_e_esperar_resposta(cena)
    assert cena.voz.faladas == ["Pedi ao Spotify, mas não consegui confirmar que a música começou."]
    assert cena.cliente.restaurados == []


def test_pc_fora_do_ar(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, pausa a música"], cliente=ClienteFalso({"pause": None}))
    falar_e_esperar_resposta(cena)
    assert cena.voz.faladas == [musica.SEM_COMPUTADOR]
    assert cena.eventos("music.command.result")[0]["status"] == "bridge_unreachable"


def test_player_fechado_diz_o_problema(montar) -> None:
    cliente = ClienteFalso({"play_generic": {**tocando(), "status": "unavailable", "reason": "player_off",
                                             "track": None, "verified": False, "ducked": {"ducked": False}}})
    cena = montar(LIGADA, ["Fala Garra, toca uma música"], cliente=cliente)
    falar_e_esperar_resposta(cena)
    dito = " ".join(cena.voz.faladas)
    assert "indisponível" in dito and "está tocando" not in dito


def test_demora_avisa_sem_anunciar_o_que_nao_aconteceu(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, toca Hotel California"], cliente=ClienteFalso(atraso_s=2.2))
    falar_e_esperar_resposta(cena)
    assert cena.voz.faladas == ["Vou procurar essa música.", "Pronto, está tocando Hotel California, de Eagles."]


def test_desligado_de_fabrica_diz_como_ligar_e_nao_chama_o_pc(montar) -> None:
    cena = montar({"wake_phrase_enabled": True}, ["Fala Garra, toca uma música no Spotify"])
    falar_e_esperar_resposta(cena)
    assert " ".join(cena.voz.faladas) == musica.DESLIGADO, "falada por frase, como tudo"
    assert cena.cliente.pedidos == [] and cena.cerebro.recebidas == []
    chat = [(e["role"], e["content"]) for e in cena.eventos("chat.message")]
    assert chat == [("user", "toca uma música no Spotify"), ("assistant", musica.DESLIGADO)]


def test_pergunta_de_capacidade_responde_sem_modelo(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, você consegue tocar música?"])
    falar_e_esperar_resposta(cena)
    assert " ".join(cena.voz.faladas) == musica.capacidade(True)
    assert cena.cliente.pedidos == [] and cena.cerebro.recebidas == []


def test_comando_solto_sem_musica_no_assunto_vai_ao_modelo(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, continua"], ["Continuando a história..."])
    cena.alguem_fala()
    assert cena.cerebro.recebidas == ["continua"] and cena.cliente.pedidos == []


def test_comando_solto_com_musica_no_assunto_e_musica(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, toca Hotel California", "Fala Garra, continua"])
    falar_e_esperar_resposta(cena)
    falar_e_esperar_resposta(cena)
    assert [p["operation"] for p in cena.cliente.pedidos] == ["play_track", "resume"]
    assert cena.cerebro.recebidas == []


def test_conversa_sobre_musica_vai_ao_modelo_com_a_situacao(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, eu gosto muito dessa playlist"], ["Que bom!"])
    cena.alguem_fala()
    [mensagem] = cena.cerebro.recebidas
    assert mensagem.startswith("[MÚSICA — situação do sistema")
    assert envelope.pedido_de(mensagem) == "eu gosto muito dessa playlist"
    assert "spotify:track" not in mensagem and cena.cliente.pedidos == []


def test_musica_tocando_fica_baixa_enquanto_o_modelo_responde(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, toca Hotel California", "Fala Garra, que horas são?"], ["São três."])
    falar_e_esperar_resposta(cena)
    cena.alguem_fala()
    [_, turno] = [e["correlation_id"] for e in cena.eventos("chat.message") if e["role"] == "user"]
    esperar(lambda: turno in cena.cliente.restaurados)
    assert cena.cliente.abafados == [turno], "abaixada no começo do turno do modelo"


def test_pausada_nao_abaixa_no_turno_do_modelo(montar) -> None:
    cliente = ClienteFalso({"pause": {**tocando(), "status": "paused",
                                      "observed": {"state": "paused", "device": "spotatui", "progress_s": 9,
                                                   "volume_percent": 40, "advancing": False},
                                      "ducked": {"ducked": False}}})
    cena = montar(LIGADA, ["Fala Garra, pausa a música", "Fala Garra, que horas são?"], ["São três."],
                  cliente=cliente)
    falar_e_esperar_resposta(cena)
    assert cena.voz.faladas == ["Pausei."]
    cena.alguem_fala()
    assert cena.cliente.abafados == []


def test_pedido_comum_continua_indo_ao_modelo(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, que horas são?"], ["São três."])
    cena.alguem_fala()
    assert cena.cerebro.recebidas == ["que horas são?"] and cena.cliente.pedidos == []


# ── o que a revisão e o teste real acharam (30/09) ───────────────────────────

def test_confirmacao_falada_vira_evento(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, toca Hotel California"])
    falar_e_esperar_resposta(cena)
    [c] = cena.eventos("music.confirmation")
    assert c["spoken"] is True and c["status"] == "playing" and c["operation"] == "play_track"


def test_pedido_ambiguo_vai_ao_modelo_com_a_situacao(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, coloca Hotel California"], ["Quer que eu toque?"])
    cena.alguem_fala()
    [mensagem] = cena.cerebro.recebidas
    assert mensagem.startswith("[MÚSICA — situação do sistema") and cena.cliente.pedidos == []


def test_volume_com_musica_no_assunto_vai_a_ferramenta_e_nao_ao_modelo(montar) -> None:
    cena = montar(LIGADA, ["Fala Garra, toca Hotel California", "Fala Garra, aumento de volume"])
    falar_e_esperar_resposta(cena)
    falar_e_esperar_resposta(cena)
    assert [p["operation"] for p in cena.cliente.pedidos] == ["play_track", "change_volume"]
    assert cena.cerebro.recebidas == []


def test_com_musica_tocando_quem_tenta_chamar_abaixa_a_musica(montar, monkeypatch) -> None:
    from garra_reachy_mini import main as principal
    monkeypatch.setattr(principal, "ESCUTA_ABAFADA_S", 0.5)
    # a frase de ativação se perdeu na música, mas "pausa" foi ouvido — com a
    # sessão de ativação já fechada (janela mínima de 3 s)
    cena = montar({**LIGADA, "wake_phrase_window_s": 3, "wake_second_listen_enabled": False},
                  ["Fala Garra, toca Hotel California", "ela agarra pausa a música"])
    falar_e_esperar_resposta(cena)
    time.sleep(3.5)
    cena.alguem_fala()
    esperar(lambda: any(r.startswith("escuta_") for r in cena.cliente.restaurados), 5)
    assert any(r.startswith("escuta_") for r in cena.cliente.abafados)
    assert [p["operation"] for p in cena.cliente.pedidos] == ["play_track"], "abaixar não é executar o comando"
    assert cena.eventos("music.listen_duck")


def test_sem_musica_tocando_ninguem_abaixa_nada(montar) -> None:
    cena = montar(LIGADA, ["fala gata pausa a música"])
    cena.alguem_fala()
    time.sleep(0.5)
    assert cena.cliente.abafados == [] and not cena.eventos("music.listen_duck")
