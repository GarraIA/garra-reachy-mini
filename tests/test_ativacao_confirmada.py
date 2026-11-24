"""O que o 1.5.0 acrescenta à escuta: mais de uma frase, o "ouvi", as fases, e
o interruptor que devolve o microfone.

Os testes de módulo conferem a regra; os de laço rodam `_laco_voz` inteiro (a
mesma cena de `test_escuta_no_laco`) e conferem o que saiu por cada porta.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from garra_reachy_mini import armazenamento, ativacao, conversa
from garra_reachy_mini.robo import comportamento as fases
from garra_reachy_mini.segmentador import Segmentador
from test_escuta_no_laco import montar_cena  # noqa: F401  (fixture)

SR = 16000
COM_FRASE = {"wake_phrase_enabled": True}


# ─── mais de uma frase ───────────────────────────────────────────────────────
def _ativ(**extra) -> ativacao.Ativacao:
    return ativacao.Ativacao.de({"wake_phrase_enabled": True, **extra})


def test_qualquer_das_frases_chama_o_robo() -> None:
    ativ = _ativ(wake_phrase_alternatives=["Ei Garra", "Olá robô"])
    for dito, resto in (("Fala Garra, que horas são?", "que horas são?"),
                        ("Ei, Garra! acende a luz", "acende a luz"),
                        ("Olá, robô. tudo bem?", "tudo bem?")):
        d = ativacao.avaliar(dito, 100.0, ativacao.Sessao(), ativ, 100.0)
        assert d.aceito and d.abriu_sessao and d.texto == resto, dito


def test_frase_que_nao_esta_na_lista_continua_recusada() -> None:
    ativ = _ativ(wake_phrase_alternatives=["Ei Garra"])
    d = ativacao.avaliar("Oi Garra, tudo bem?", 100.0, ativacao.Sessao(), ativ, 100.0)
    assert not d.aceito and d.motivo == "wake_phrase_required"


def test_vale_a_frase_que_consome_mais_palavras() -> None:
    """Com "Garra Garra" e "Fala Garra" não importa; com prefixo comum, importa:
    o pedido não pode ficar com um pedaço da frase na frente."""
    ativ = _ativ(wake_phrase_text="Ei robô", wake_phrase_alternatives=["Ei robô Garra"])
    d = ativacao.avaliar("Ei robô Garra, dance", 1.0, ativacao.Sessao(), ativ, 1.0)
    assert d.texto == "dance"


def test_trocar_a_lista_fecha_a_sessao_aberta() -> None:
    antes, depois = _ativ(), _ativ(wake_phrase_alternatives=["Ei Garra"])
    sessao = ativacao.Sessao()
    sessao.abrir(10.0, antes)
    assert sessao.aberta(11.0, antes)
    assert not sessao.aberta(11.0, depois)


def test_a_lista_e_validada_como_a_frase_principal() -> None:
    c = conversa.normalizar({"wake_phrase_alternatives":
                             ["Ei Garra", "", "!!!", "ei, garra", "x" * 200,
                              "Olá robô", "Acorda", "Escuta aqui", "Mais uma"]})
    lista = c["wake_phrase_alternatives"]
    assert lista[0] == "Ei Garra" and "!!!" not in lista and "" not in lista
    assert len(lista) == conversa.FRASES_ALTERNATIVAS_MAX
    assert sum(ativacao.tokens(f) == ["ei", "garra"] for f in lista) == 1, "repetida entrou"
    assert all(len(f) <= conversa.FRASE_MAX for f in lista)
    # Não-lista é recusada, e o que havia fica.
    assert conversa.perfil_atualizado(c, {"wake_phrase_alternatives": "Ei Garra"})[
        "wake_phrase_alternatives"] == lista
    # Lista vazia apaga.
    assert conversa.perfil_atualizado(c, {"wake_phrase_alternatives": []})[
        "wake_phrase_alternatives"] == []


def test_normalizar_nao_compartilha_a_lista_do_padrao() -> None:
    conversa.normalizar({})["wake_phrase_alternatives"].append("vazou")
    assert conversa.PADRAO["wake_phrase_alternatives"] == []


def test_a_segunda_escuta_confere_contra_a_frase_que_pareceu() -> None:
    ativ = _ativ(wake_phrase_alternatives=["Olá robozinho"])
    ouvido = ativacao.tokens("olá robozino acende a luz")
    perto, motivo, frase, dica = ativacao.exame_entre(ouvido, ativ, (0.4, 0.3, 0.9, 0.9, 0.9))
    assert motivo == "unsure" and perto["heard"] == "ola robozino"
    assert frase == ("ola", "robozinho") and dica == "Olá robozinho"
    # E, sem nada parecido com frase nenhuma, não há o que conferir.
    assert ativacao.exame_entre(ativacao.tokens("que calor hoje"), ativ, (0.9, 0.9, 0.9))[1] == "none"


# ─── a frase que o STT só entendeu pela metade ───────────────────────────────
FRASE = ("fala", "garra")


@pytest.mark.parametrize("ouvido", ["fala h qual é a capital", "fala a qual é", "fala rá que horas são"])
def test_primeira_palavra_certa_e_nome_incerto_vai_a_segunda_escuta(ouvido) -> None:
    """Medido no robô: "Fala Garra" voltava como "Fala H" e nem era conferido."""
    tokens = ativacao.tokens(ouvido)
    perto, motivo = ativacao.exame(tokens, FRASE, (0.9, 0.2) + (0.9,) * (len(tokens) - 2))
    assert motivo == "unsure" and perto["heard"] == " ".join(tokens[:2]) and perto["position"] == 0


def test_nome_dito_com_clareza_nao_e_conferido_so_por_comecar_com_fala() -> None:
    """ "Fala H" com o STT seguro do que ouviu é só alguém dizendo "fala H"."""
    tokens = ativacao.tokens("fala h qual é a capital")
    assert ativacao.exame(tokens, FRASE, (0.95, 0.92, 0.9, 0.9, 0.9, 0.9))[1] == "confident"


@pytest.mark.parametrize("ouvido", ["falam muito aqui", "fala", "hoje fala h comigo",
                                    "fala extraordinariamente bem"])
def test_o_comeco_certo_nao_basta_fora_das_condicoes(ouvido) -> None:
    tokens = ativacao.tokens(ouvido)
    assert ativacao.exame(tokens, FRASE, (0.2,) * len(tokens))[1] in ("none", "elsewhere")


def test_frase_de_uma_palavra_nao_ganha_a_regra_do_comeco() -> None:
    assert ativacao.quase(["computar"], ("computador",)) is not None       # pela distância
    assert ativacao.quase(["x"], ("computador",)) is None


def test_so_o_comeco_certo_pede_mais_duvida_do_stt() -> None:
    """Dúvida moderada basta para o parecido de perto, não para "fala" + qualquer coisa."""
    tokens = ativacao.tokens("fala h qual é")
    assert ativacao.exame(tokens, FRASE, (0.9, 0.6, 0.9, 0.9))[1] == "confident"
    assert ativacao.exame(tokens, FRASE, (0.9, 0.4, 0.9, 0.9))[1] == "unsure"
    perto = ativacao.tokens("fala gabi qual é")
    assert ativacao.exame(perto, FRASE, (0.9, 0.6, 0.9, 0.9))[1] == "unsure"


def test_o_pedido_fica_com_a_escuta_que_o_ouviu_melhor() -> None:
    primeira, conf1 = "Palagado, qual é a capital da França?", (0.2, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9)
    segunda, conf2 = "Fala Garra, porém é o telefone?", (0.9, 0.9, 0.3, 0.4, 0.3, 0.3)
    assert ativacao.resto_mais_confiavel(primeira, conf1, 1, segunda, conf2, 2) == \
        "qual é a capital da França?"
    # Empate, ou a segunda melhor: fica a segunda (devolve None).
    assert ativacao.resto_mais_confiavel(primeira, conf1, 1, segunda,
                                         (0.9, 0.9, 0.9, 0.9, 0.9, 0.9), 2) is None
    # Sem confiança, não há o que comparar.
    assert ativacao.resto_mais_confiavel(primeira, None, 1, segunda, conf2, 2) is None
    assert ativacao.resto_mais_confiavel("Palagado", (0.2,), 1, segunda, conf2, 2) is None


# ─── o "ouvi" ────────────────────────────────────────────────────────────────
def test_o_bipe_e_curto_baixo_e_nao_estala() -> None:
    onda = ativacao.bipe(SR)
    assert onda.dtype == np.float32 and 0.1 < onda.size / SR < 0.3
    assert float(np.abs(onda).max()) <= 0.25
    assert abs(float(onda[0])) < 1e-3 and abs(float(onda[-1])) < 1e-3


def test_o_bipe_sozinho_nunca_vira_um_trecho_de_fala() -> None:
    """Se o microfone do robô o captar inteiro, o detector o descarta."""
    detector = Segmentador(SR, limiar_fixo=0.02)
    onda = ativacao.bipe(SR, volume=1.0)
    agora, saiu = 0.0, []
    for i in range(0, onda.size, SR // 50):
        agora += 0.02
        saiu.append(detector.bloco(onda[i:i + SR // 50], agora))
    for _ in range(80):
        agora += 0.02
        saiu.append(detector.bloco(np.zeros(SR // 50, dtype=np.float32), agora))
    assert not any(saiu), "o bipe virou trecho e seria mandado à transcrição"


def test_frase_sozinha_ganha_bipe_e_gesto(montar_cena) -> None:  # noqa: F811
    cena = montar_cena(COM_FRASE, ["Fala Garra."], [])
    cena.alguem_fala()
    assert cena.app.comportamento.ativacoes == 1
    assert len(cena.mic.tocadas) == 1, "o bipe é o único som"
    assert np.array_equal(cena.mic.tocadas[0], ativacao.bipe(SR))
    [evento] = cena.eventos("voice.wake.acknowledged")
    assert evento["sound"] is True and evento["motion"] is True
    assert cena.cerebro.recebidas == []


def test_frase_com_pedido_ganha_o_bipe_antes_da_resposta(montar_cena) -> None:  # noqa: F811
    cena = montar_cena(COM_FRASE, ["Fala Garra, que horas são?"], ["São dez horas."])
    cena.alguem_fala()
    assert cena.app.comportamento.ativacoes == 1
    assert np.array_equal(cena.mic.tocadas[0], ativacao.bipe(SR)), "o bipe vem primeiro"
    assert len(cena.mic.tocadas) == 2 and cena.voz.faladas == ["São dez horas."]


def test_dentro_da_sessao_aberta_ninguem_bipa_de_novo(montar_cena) -> None:  # noqa: F811
    cena = montar_cena(COM_FRASE, ["Fala Garra, oi", "e amanhã?"], ["Oi.", "Chove."])
    cena.alguem_fala()
    cena.alguem_fala()
    assert cena.app.comportamento.ativacoes == 1
    assert len(cena.eventos("voice.wake.acknowledged")) == 1


@pytest.mark.parametrize("conf, som, gesto", [
    ({"wake_ack_sound_enabled": False}, False, 1),
    ({"wake_ack_motion_enabled": False}, True, 0),
    # O mestre da saída cala também o bipe; o gesto não é som.
    ({"speech_output_enabled": False}, False, 1),
])
def test_cada_metade_do_ouvi_desliga_sozinha(montar_cena, conf, som, gesto) -> None:  # noqa: F811
    cena = montar_cena({**COM_FRASE, **conf}, ["Fala Garra."], [])
    cena.alguem_fala()
    assert len(cena.mic.tocadas) == (1 if som else 0)
    assert cena.app.comportamento.ativacoes == gesto
    assert cena.eventos("voice.wake.acknowledged")[0]["sound"] is som


# ─── fases ───────────────────────────────────────────────────────────────────
def test_as_fases_de_um_turno_na_ordem(montar_cena) -> None:  # noqa: F811
    cena = montar_cena(COM_FRASE, ["Fala Garra, que horas são?"], ["São dez horas."])
    gestos = cena.app.comportamento
    assert gestos.fases[-1] == (fases.FASE_AGUARDANDO_FRASE, None)
    cena.alguem_fala()
    nomes = [f for f, _ in gestos.fases]
    meio = nomes[nomes.index(fases.FASE_TRANSCREVENDO):]
    assert meio == [fases.FASE_TRANSCREVENDO, fases.FASE_PROCESSANDO,
                    fases.FASE_FALANDO, fases.FASE_OUVINDO], nomes


def test_sem_frase_de_ativacao_o_robo_esta_sempre_ouvindo(montar_cena) -> None:  # noqa: F811
    cena = montar_cena({"wake_phrase_enabled": False}, [], [])
    assert cena.app.comportamento.fases[-1] == (fases.FASE_OUVINDO, None)


def test_conversa_da_sala_volta_a_aguardar_a_frase(montar_cena) -> None:  # noqa: F811
    cena = montar_cena(COM_FRASE, ["Que calor hoje."], [])
    cena.alguem_fala()
    assert cena.app.comportamento.fases[-1] == (fases.FASE_AGUARDANDO_FRASE, None)


def test_o_laco_que_para_deixa_a_fase_em_espera(montar_cena) -> None:  # noqa: F811
    cena = montar_cena(COM_FRASE, [], [])
    gestos = cena.app.comportamento
    cena.encerrar()
    assert gestos.fases[-1] == (fases.FASE_ESPERA, "voice_loop_stopped")
    assert gestos.fase_ao_ouvir is None


def test_o_comportamento_publica_a_fase_so_quando_muda(backend, tmp_path) -> None:
    from garra_reachy_mini.robo.acoes import ControladorRobo
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    c = fases.Comportamento(ctrl, ativo=False)
    c.fase(fases.FASE_ESPERA, "voice_server_unreachable")
    c.fase(fases.FASE_ESPERA, "voice_server_unreachable")
    c.fase("inventada")
    c.erro("stt_failed", "x" * 500)
    eventos = [e for e in ctrl.eventos.historico(50) if e["type"] == "voice.phase"]
    assert [(e["phase"], e["reason"]) for e in eventos] == [
        (fases.FASE_ESPERA, "voice_server_unreachable")]
    estado = c.estado_da_voz()
    assert estado["phase"] == fases.FASE_ESPERA
    assert estado["last_error"]["code"] == "stt_failed"
    assert len(estado["last_error"]["detail"]) <= 160


# ─── o corpo só se mexe por pedido aceito ────────────────────────────────────
def test_barulho_da_sala_nao_mexe_o_robo(montar_cena) -> None:  # noqa: F811
    """O servo da antena fica ao lado dos microfones. Mexê-la a cada som ouvido
    fazia o robô ouvir a si mesmo: um trecho falso a cada 2 s, em laço (medido
    no robô em 24/11/2026)."""
    cena = montar_cena(COM_FRASE, ["Que calor hoje.", "Liga a televisão aí."], [])
    cena.alguem_fala()
    cena.alguem_fala()
    assert cena.app.comportamento.pensou == 0
    assert cena.app.comportamento.ativacoes == 0


def test_pedido_aceito_leva_o_corpo_a_pensando_uma_vez(montar_cena) -> None:  # noqa: F811
    cena = montar_cena(COM_FRASE, ["Fala Garra, que horas são?"], ["São dez horas."])
    cena.alguem_fala()
    assert cena.app.comportamento.pensou == 1


def test_o_comportamento_nao_repete_gesto_sem_mudanca_de_estado(backend, tmp_path) -> None:
    from garra_reachy_mini.robo.acoes import ControladorRobo
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    pedidos: list[str] = []
    ctrl.submeter = lambda nome, params, **kw: pedidos.append(nome)   # type: ignore[assignment]
    c = fases.Comportamento(ctrl, ativo=True, wobbling=False)
    c.ouvindo()
    c.ouvindo()
    c.ouvindo()
    assert pedidos == ["move_antennas"], "três 'ouvindo' seguidos, um gesto só"
    c.pensando()
    c.pensando()
    c.ouvindo()
    assert pedidos == ["move_antennas"] * 3


# ─── o interruptor da escuta ─────────────────────────────────────────────────
def _espera(condicao, limite_s: float = 6.0) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.05)
    return False


def test_desligar_a_escuta_libera_o_microfone_e_religar_o_reabre(montar_cena) -> None:  # noqa: F811
    cena = montar_cena(COM_FRASE, ["Fala Garra, oi"], ["Oi."])
    paradas, abertas = [], []
    cena.mic.stop_recording = lambda: paradas.append(1)
    cena.mic.start_recording = lambda: abertas.append(1)
    gestos = cena.app.comportamento

    armazenamento.salvar_config({"conversation": {**COM_FRASE, "listening_enabled": False}})
    assert _espera(lambda: paradas), "o microfone não foi liberado"
    assert _espera(lambda: gestos.fases[-1] == (fases.FASE_ESPERA, "listening_disabled"))
    [desligou] = cena.eventos("voice.listening")[-1:]
    assert desligou["enabled"] is False

    # Desligada, ninguém é ouvido: o áudio nem chega a ser lido.
    cena.mic.dizer()
    time.sleep(1.5)
    assert cena.voz.transcritas == 0

    armazenamento.salvar_config({"conversation": {**COM_FRASE, "listening_enabled": True}})
    assert _espera(lambda: abertas), "o microfone não foi reaberto"
    assert _espera(lambda: gestos.fases[-1] == (fases.FASE_AGUARDANDO_FRASE, None))
    cena.alguem_fala()
    assert cena.voz.faladas == ["Oi."]


# ─── microfone e fala ────────────────────────────────────────────────────────
def test_sensibilidade_muda_o_limiar_e_nao_o_piso() -> None:
    d = Segmentador(SR, piso_inicial=0.004)
    normal = d.limiar_abre
    d.ajustar(sensibilidade=conversa.ganho_do_microfone(10))
    assert d.limiar_abre < normal / 2
    d.ajustar(sensibilidade=conversa.ganho_do_microfone(1))
    assert d.limiar_abre == pytest.approx(normal * 2)
    d.ajustar(sensibilidade=99.0)          # fora da faixa: fica no teto
    assert d.limiar_abre > normal / 3
    assert d.medida()["floor"] == pytest.approx(0.004)


def test_a_pausa_que_encerra_a_fala_e_ajustavel() -> None:
    def segundos_ate_fechar(pausa_s: float) -> float:
        d = Segmentador(SR, limiar_fixo=0.05, fim_de_fala_s=pausa_s)
        agora = 0.0
        for _ in range(30):
            agora += 0.02
            d.bloco(np.full(SR // 50, 0.2, dtype=np.float32), agora)
        calado = 0.0
        while calado < 5.0:
            agora += 0.02
            calado += 0.02
            if d.bloco(np.zeros(SR // 50, dtype=np.float32), agora) is not None:
                return calado
        return calado
    assert segundos_ate_fechar(0.4) < segundos_ate_fechar(1.5)
    assert 0.35 < segundos_ate_fechar(0.4) < 0.6


def test_o_cliente_de_voz_so_manda_o_que_difere_do_padrao(monkeypatch) -> None:
    from garra_reachy_mini import voz as voz_mod
    enviados: list[dict] = []

    class Resposta:
        content = np.full(100, 0.5, dtype=np.float32).tobytes()
        def raise_for_status(self) -> None: pass
        def json(self) -> dict: return {"texto": "oi", "confianca": [0.9]}

    def post(url, **kw):
        enviados.append({"url": url, **kw})
        return Resposta()

    monkeypatch.setattr(voz_mod.requests, "post", post)
    cliente = voz_mod.VozClient("http://voz")
    cliente.falar("oi", SR)
    cliente.ouvir(np.zeros(SR, dtype=np.float32), SR)
    assert enviados[0]["json"] == {"texto": "oi", "sr": SR}
    assert enviados[1]["params"] == {"sr": SR}

    cliente.preferir(conversa.normalizar({"speech_language": "en", "speech_voice": "clara",
                                           "speech_rate_pct": 120, "speech_volume_pct": 50}))
    onda = cliente.falar("hi", SR)
    cliente.ouvir(np.zeros(SR, dtype=np.float32), SR)
    assert enviados[2]["json"] == {"texto": "hi", "sr": SR, "idioma": "en",
                                   "voz": "clara", "velocidade": 1.2}
    assert enviados[3]["params"] == {"sr": SR, "idioma": "en"}
    assert float(onda.max()) == pytest.approx(0.25), "o volume é aplicado no robô"


@pytest.mark.parametrize("chave, ruim", [
    ("speech_language", "klingon"), ("speech_voice", "../../etc/passwd"),
    ("mic_sensitivity", "alta"),
])
def test_preferencia_invalida_nao_e_gravada(chave, ruim) -> None:
    base = conversa.normalizar({})
    assert conversa.perfil_atualizado(base, {chave: ruim})[chave] == base[chave]
