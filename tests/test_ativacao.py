"""A frase de ativação: o que o robô escuta, e o que ele ignora.

O caso que mais importa aqui é `"fala garrafa"`. Um `startswith` sobre o texto
normalizado aceitaria essa frase como ativação de `"fala garra"`, e o defeito
seria invisível em qualquer teste que só usasse frases bem-comportadas.

O segundo é a preservação do texto: a normalização existe para DETECTAR. Se o
texto normalizado chegasse ao modelo, toda pergunta em português perderia
acento e pontuação — `"qual é o preço do café em São Paulo?"` viraria
`"qual e o preco do cafe em sao paulo"`.
"""

from __future__ import annotations

import threading

import pytest

from garra_reachy_mini import ativacao, conversa

LIGADA = ativacao.Ativacao(habilitada=True, frase=("fala", "garra"),
                           janela_s=15.0, sessao_max_s=90.0)
DESLIGADA = ativacao.Ativacao(habilitada=False)


def nova() -> ativacao.Sessao:
    return ativacao.Sessao()


def avaliar(texto, sessao, agora, ativ=LIGADA, capturado=None, e_stop=False):
    """Captura bem antes do último áudio falado, salvo quando o teste quiser eco."""
    return ativacao.avaliar(texto, capturado if capturado is not None else agora,
                            sessao, ativ, agora, e_stop=e_stop)


# ─── fronteira de token ──────────────────────────────────────────────────────
def test_fala_garrafa_nao_ativa_fala_garra() -> None:
    """O caso que um `startswith` erraria."""
    s = nova()
    d = avaliar("fala garrafa agora", s, 100.0)
    assert d.aceito is False
    assert d.motivo == "wake_phrase_required"


@pytest.mark.parametrize("frase", [
    "Fala Garra",
    "fala garra",
    "FALA GARRA",
    "Fala, Garra!",
    "  fala   garra  ",
])
def test_a_frase_e_reconhecida_apesar_de_caixa_acento_e_pontuacao(frase) -> None:
    s = nova()
    assert avaliar(frase, s, 100.0).aceito is True


def test_frase_no_meio_do_enunciado_nao_ativa() -> None:
    """São tokens INICIAIS: a frase abre o enunciado ou não vale."""
    s = nova()
    assert avaliar("por favor fala garra olhe para mim", s, 100.0).aceito is False


def test_prefixo_parcial_nao_ativa() -> None:
    s = nova()
    assert avaliar("fala", s, 100.0).aceito is False
    assert avaliar("garra", s, 100.0).aceito is False


# ─── preservação do texto original ───────────────────────────────────────────
def test_o_texto_util_preserva_acento_caixa_e_pontuacao() -> None:
    s = nova()
    d = avaliar("Fala Garra, qual é o preço do café em São Paulo?", s, 100.0)
    assert d.aceito is True
    assert d.texto == "qual é o preço do café em São Paulo?", (
        "o normalizado serve para detectar; ao modelo vai o original")


def test_so_a_frase_e_removida() -> None:
    s = nova()
    assert avaliar("Fala Garra olhe para mim", s, 100.0).texto == "olhe para mim"
    assert avaliar("Fala Garra: dance!", s, 200.0).texto == "dance!"


def test_frase_sozinha_abre_sessao_sem_turno() -> None:
    s = nova()
    d = avaliar("Fala Garra", s, 100.0)
    assert d.aceito and d.so_ativacao and d.abriu_sessao
    assert d.texto == ""


# ─── janela deslizante e teto ────────────────────────────────────────────────
def test_janela_aceita_dentro_da_inatividade() -> None:
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    assert avaliar("olhe para mim", s, 110.0).aceito is True


def test_janela_fecha_por_inatividade() -> None:
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    d = avaliar("olhe para mim", s, 116.0)
    assert d.aceito is False and d.motivo == "wake_phrase_required"


def test_a_renovacao_e_ao_FIM_do_turno_e_desliza() -> None:
    """Aceitar não renova; quem renova é o fim do turno."""
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    assert avaliar("primeira", s, 110.0).aceito is True
    s.renovar(114.0)                       # o turno terminou (modelo + TTS)
    assert avaliar("segunda", s, 125.0).aceito is True, "deslizou"
    s.renovar(128.0)
    assert avaliar("terceira", s, 140.0).aceito is True


def test_o_teto_fecha_mesmo_com_renovacoes() -> None:
    """O teto conta da ativação e nunca renova."""
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    t = 100.0
    while t < 185.0:                       # renova de 10 em 10 s
        t += 10.0
        s.renovar(t)
    assert avaliar("ainda estou aqui", s, 191.0).aceito is False, (
        "90 s depois da ativação a sessão fecha, por mais que se renove")


def test_repetir_a_frase_comeca_sessao_nova() -> None:
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    avaliar("Fala Garra", s, 150.0)        # nova sessão, teto reiniciado
    s.renovar(160.0)
    assert avaliar("dentro da nova", s, 170.0).aceito is True


# ─── ciclo de vida da sessão ─────────────────────────────────────────────────
def test_desabilitar_aceita_tudo_e_nao_usa_sessao() -> None:
    s = nova()
    d = ativacao.avaliar("qualquer coisa", 0.0, s, DESLIGADA, 100.0)
    assert d.aceito and d.texto == "qualquer coisa"
    assert s.abertura is None


def test_mudar_a_frase_fecha_a_sessao_aberta() -> None:
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    outra = ativacao.Ativacao(habilitada=True, frase=("ei", "robo"),
                              janela_s=15.0, sessao_max_s=90.0)
    d = ativacao.avaliar("olhe para mim", 105.0, s, outra, 105.0)
    assert d.aceito is False, "a sessão da frase antiga não sobrevive à troca"
    assert s.abertura is None


def test_mudar_os_limites_fecha_a_sessao_aberta() -> None:
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    outra = ativacao.Ativacao(habilitada=True, frase=("fala", "garra"),
                              janela_s=30.0, sessao_max_s=90.0)
    assert ativacao.avaliar("olhe", 105.0, s, outra, 105.0).aceito is False


def test_sessao_nova_nasce_fechada() -> None:
    """"Resetada no restart" é isto: o objeto morre com o laço de voz."""
    assert nova().aberta(100.0, LIGADA) is False


# ─── e-stop ──────────────────────────────────────────────────────────────────
def test_stop_passa_com_a_sessao_fechada() -> None:
    s = nova()
    d = avaliar("pare", s, 100.0, e_stop=True)
    assert d.aceito is True and d.motivo == "emergency_stop"


def test_stop_nao_abre_nem_renova_a_sessao() -> None:
    """Obedecer "pare" não é ser chamado — não pode virar porta de conversa."""
    s = nova()
    avaliar("pare", s, 100.0, e_stop=True)
    assert s.abertura is None, "o stop não pode abrir sessão"
    assert avaliar("e agora dance", s, 101.0).aceito is False

    avaliar("Fala Garra", s, 200.0)
    antes = s.atividade
    avaliar("pare", s, 210.0, e_stop=True)
    assert s.atividade == antes, "o stop não renova a janela"


def test_stop_independe_da_saida_de_voz() -> None:
    """A ativação e a saída de voz são eixos separados."""
    p = conversa.Politica.de({"speech_output_enabled": False,
                              "wake_phrase_enabled": True})
    assert p.saida_habilitada is False
    s = nova()
    assert avaliar("pare", s, 100.0, e_stop=True).aceito is True


# ─── anti-eco ────────────────────────────────────────────────────────────────
def test_o_proprio_tts_nao_abre_sessao() -> None:
    """O robô dizendo a frase numa resposta não pode se autoativar."""
    s = nova()
    s.falou_ate = 100.0
    d = avaliar("Fala Garra", s, 100.3, capturado=100.2)
    assert d.aceito is False and d.motivo == "echo_suppressed"
    assert s.abertura is None


def test_o_eco_nao_fecha_sessao_ja_aberta() -> None:
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    s.falou_ate = 110.0
    d = avaliar("Fala Garra olhe", s, 110.3, capturado=110.2)
    assert d.aceito is True, "a margem é contra autoativação, não contra a pessoa"


def test_a_margem_usa_a_captura_e_nao_a_avaliacao() -> None:
    """O STT leva segundos; medir depois dele julgaria o áudio errado."""
    s = nova()
    s.falou_ate = 100.0
    # Áudio capturado 5 s DEPOIS do robô calar, mas avaliado só aos 108 s
    # porque o STT demorou. Tem de ser aceito.
    d = ativacao.avaliar("Fala Garra", 105.0, s, LIGADA, 108.0)
    assert d.aceito is True
    # E o inverso: capturado durante o eco, avaliado tarde. Tem de ser recusado.
    s2 = nova()
    s2.falou_ate = 100.0
    assert ativacao.avaliar("Fala Garra", 100.2, s2, LIGADA, 108.0).aceito is False


# ─── evento de rejeição ──────────────────────────────────────────────────────
@pytest.mark.parametrize("segundos,esperado", [
    (0.4, "0-1s"), (1.0, "1-3s"), (2.9, "1-3s"), (3.0, "3-10s"),
    (9.9, "3-10s"), (10.0, "10s+"), (60.0, "10s+"), (None, "unknown"),
])
def test_faixa_de_duracao_nunca_devolve_o_valor_exato(segundos, esperado) -> None:
    assert ativacao.faixa_de_duracao(segundos) == esperado


def test_a_decisao_rejeitada_nao_carrega_o_texto() -> None:
    s = nova()
    d = avaliar("meu número do cartão é 4111 1111 1111 1111", s, 100.0)
    assert d.aceito is False
    assert d.texto == ""
    assert "cartão" not in repr(d) and "4111" not in repr(d)


# ─── nada de recurso novo ────────────────────────────────────────────────────
def test_avaliar_nao_cria_thread_nem_timer() -> None:
    antes = threading.active_count()
    s = nova()
    for i in range(500):
        avaliar("Fala Garra teste", s, 100.0 + i)
    assert threading.active_count() == antes


def test_a_sessao_nao_guarda_recurso_nenhum() -> None:
    s = nova()
    avaliar("Fala Garra", s, 100.0)
    for valor in vars(s).values():
        assert not isinstance(valor, (threading.Thread, threading.Timer))
    assert set(vars(s)) == {"abertura", "atividade", "assinatura", "falou_ate"}


# ─── configuração ────────────────────────────────────────────────────────────
def test_padroes_da_wake_phrase() -> None:
    c = conversa.normalizar({})
    assert c["wake_phrase_enabled"] is False, "ligar muda como se fala com o robô"
    assert c["wake_phrase_text"] == "Fala Garra"
    assert c["wake_phrase_window_s"] == 15
    assert c["wake_phrase_session_max_s"] == 90


def test_config_antigo_recebe_os_padroes_sem_quebrar() -> None:
    c = conversa.normalizar({"mode": "informative", "revision": 7})
    assert c["wake_phrase_enabled"] is False
    assert c["revision"] == 7 and c["mode"] == "informative"


@pytest.mark.parametrize("ruim", ["", "   ", "!!!", "...", "  ,  "])
def test_frase_vazia_ou_so_pontuacao_e_recusada(ruim) -> None:
    """Gravar vazio deixaria a ativação ligada e inalcançável — robô surdo."""
    c = conversa.normalizar({"wake_phrase_text": ruim})
    assert c["wake_phrase_text"] == "Fala Garra", "a anterior tem de ficar"


def test_frase_longa_e_truncada_e_nao_recusada() -> None:
    c = conversa.normalizar({"wake_phrase_text": "a" * 200})
    assert len(c["wake_phrase_text"]) == conversa.FRASE_MAX


def test_limites_das_janelas() -> None:
    assert conversa.normalizar({"wake_phrase_window_s": 1})["wake_phrase_window_s"] == 3
    assert conversa.normalizar({"wake_phrase_window_s": 999})["wake_phrase_window_s"] == 120
    assert conversa.normalizar(
        {"wake_phrase_session_max_s": 5})["wake_phrase_session_max_s"] == 15
    assert conversa.normalizar(
        {"wake_phrase_session_max_s": 9999})["wake_phrase_session_max_s"] == 600


def test_teto_menor_que_a_janela_e_corrigido() -> None:
    """Um teto menor que a inatividade expiraria antes do primeiro silêncio."""
    c = conversa.normalizar({"wake_phrase_window_s": 60,
                             "wake_phrase_session_max_s": 20})
    assert c["wake_phrase_session_max_s"] >= c["wake_phrase_window_s"]


def test_a_ativacao_independe_dos_mestres_de_fala() -> None:
    """Mestre da saída OFF + wake ON continua governando a escuta."""
    conf = {"speech_output_enabled": False, "automatic_speech_enabled": False,
            "wake_phrase_enabled": True}
    a = ativacao.Ativacao.de(conf)
    assert a.habilitada is True
    p = conversa.Politica.de(conf)
    assert p.saida_habilitada is False
    s = nova()
    assert avaliar("Fala Garra olhe para mim", s, 100.0, ativ=a).aceito is True


# ─── a ORDEM no laço de voz: o que nenhum teste de unidade alcança ───────────
from pathlib import Path   # noqa: E402

MAIN = (Path(__file__).resolve().parent.parent
        / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")


def _processar() -> str:
    linhas = MAIN.splitlines()
    i = next(n for n, l in enumerate(linhas) if "def processar(" in l)
    nivel = len(linhas[i]) - len(linhas[i].lstrip())
    for n in range(i + 1, len(linhas)):
        if linhas[n].strip() and (len(linhas[n]) - len(linhas[n].lstrip())) <= nivel:
            return "\n".join(linhas[i:n])
    return "\n".join(linhas[i:])


def test_o_gate_vem_antes_do_log_do_chat_e_de_tudo_a_jusante() -> None:
    """A ordem É o requisito: o `publicar` leva o transcript ao histórico."""
    corpo = _processar()
    gate = corpo.index("ativacao.avaliar")
    for depois, oque in (
            ('log.info("🎤', "o log do app"),
            ('"chat.message"', "o evento de chat/histórico"),
            ("controlador.submeter", "a execução de tool"),
            ("esperar_cerebro", "a consulta ao modelo")):
        assert gate < corpo.index(depois), (
            f"o gate roda DEPOIS de {oque} — um transcript rejeitado já teria "
            "sido persistido")


def test_o_carimbo_de_captura_e_calculado_antes_do_stt() -> None:
    corpo = _processar()
    assert corpo.index("capturado_em") < corpo.index("voz.transcrever"), (
        "medir o eco depois do STT julgaria o áudio errado")
    assert "time.monotonic() - dur" in corpo


def test_o_evento_de_rejeicao_e_redigido_e_limitado() -> None:
    corpo = _processar()
    i = corpo.index('"voice.wake.rejected"')
    evento = corpo[i:i + 500]
    assert "audio_duration_bucket" in evento
    assert "reason=" in evento and "wake_session_state" in evento
    for proibido in ("content=", "texto", "transcript", "hash"):
        assert proibido not in evento, f"o evento carrega {proibido}"
    assert "REJEICAO_INTERVALO_S" in corpo, "sem rate limit"


def test_o_estop_e_reconhecido_antes_da_decisao() -> None:
    corpo = _processar()
    assert corpo.index("e_stop = ") < corpo.index("ativacao.avaliar")


def test_a_sessao_e_criada_uma_vez_por_laco_e_nao_por_turno() -> None:
    """Uma sessão por transcrição seria um recurso novo por turno."""
    assert MAIN.count("ativacao.Sessao()") == 1
    corpo = _processar()
    assert "ativacao.Sessao()" not in corpo, (
        "a sessão nasce no laço de voz, não dentro de `processar`")


def test_a_renovacao_acontece_no_fim_do_turno() -> None:
    i = MAIN.index("sessao_ativacao.renovar")
    assert MAIN.index("confirmar_falado", i - 800, i + 800) > i - 800
    assert "sessao_ativacao.renovar" in MAIN


def test_nenhuma_thread_timer_ou_task_nova_no_caminho_da_ativacao() -> None:
    corpo = _processar()
    for proibido in ("threading.Thread", "threading.Timer", "asyncio.create_task",
                     "ThreadPoolExecutor"):
        assert proibido not in corpo, (
            f"{proibido} no caminho do turno — é a forma do defeito do 1.2.1")


# ─── o defeito que só o hardware pegou ───────────────────────────────────────
# `normalizar()` (leitura do disco) e `perfil_atualizado()` (escrita pelo
# painel) tinham listas de campos SEPARADAS. As chaves novas entraram só na
# primeira, então a configuração valia pelo arquivo e era silenciosamente
# ignorada pelo painel. Nenhum teste de unidade sobre `normalizar` alcançava
# isso; a validação no robô alcançou.

def test_toda_chave_do_padrao_sobrevive_a_escrita_pelo_painel() -> None:
    """A invariante que impede as duas listas de divergirem de novo."""
    base = conversa.normalizar({})
    alteracoes = {
        "speech_output_enabled": False,
        "automatic_speech_enabled": True,
        "spoken_acknowledgements_enabled": True,
        "spoken_progress_updates": True,
        "announce_tool_usage": True,
        "wake_phrase_enabled": True,
        "wake_phrase_text": "Ei Robô",
        "wake_phrase_window_s": 42,
        "wake_phrase_session_max_s": 300,
        "progress_update_delay_ms": 5000,
        "max_progress_messages": 3,
        "acknowledgement_cut_threshold_ms": 900,
        "mode": "informative",
    }
    escrito = conversa.perfil_atualizado(base, alteracoes)
    for chave, valor in alteracoes.items():
        assert escrito[chave] == valor, (
            f"{chave} não sobreviveu à escrita — `perfil_atualizado` não "
            "conhece a chave, e o painel a ignoraria em silêncio")
    # E nenhuma chave do padrão pode ficar de fora dos dois caminhos.
    assert set(conversa.PADRAO) - {"profiles", "revision"} <= set(alteracoes) | {
        "mode"}, "há chave no PADRÃO que este teste não exercita"


def test_a_escrita_valida_igual_a_leitura() -> None:
    base = conversa.normalizar({})
    for mudanca in ({"wake_phrase_text": "   "}, {"wake_phrase_text": "!!!"},
                    {"wake_phrase_window_s": 1}, {"wake_phrase_window_s": 999},
                    {"wake_phrase_session_max_s": 5}):
        pela_escrita = conversa.perfil_atualizado(base, mudanca)
        pela_leitura = conversa.normalizar({**base, **mudanca})
        for chave in ("wake_phrase_text", "wake_phrase_window_s",
                      "wake_phrase_session_max_s"):
            assert pela_escrita[chave] == pela_leitura[chave], (
                f"{chave} valida diferente conforme o caminho: {mudanca}")


def test_escrita_parcial_preserva_as_demais_chaves() -> None:
    base = conversa.perfil_atualizado(conversa.normalizar({}), {
        "wake_phrase_enabled": True, "wake_phrase_text": "Ei Robô",
        "speech_output_enabled": False})
    depois = conversa.perfil_atualizado(base, {"mode": "informative"})
    assert depois["wake_phrase_enabled"] is True
    assert depois["wake_phrase_text"] == "Ei Robô"
    assert depois["speech_output_enabled"] is False
