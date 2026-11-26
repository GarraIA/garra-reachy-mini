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


# ─── tolerância ao erro do STT ───────────────────────────────────────────────
# Medido em 29/09/2026 com o Whisper do servidor de voz: "Fala Garra" dito 60
# vezes, reconhecido em 45. As linhas abaixo são transcrições REAIS daquela
# medida. Casamento exato recusava todas, e quem falava com o robô só via o
# robô ignorar. Os erros caem em três lugares, e só neles: as palavras se
# fundem, uma letra nasce na emenda entre elas, ou o primeiro som sai trocado.
@pytest.mark.parametrize("ouvido,resto", [
    ("Falagarra que dia é hoje", "que dia é hoje"),            # fundiu
    ("Falangarra, que horas são?", "que horas são?"),          # fundiu, letra na emenda
    ("Fala, agarra, levanta a cabeça.", "levanta a cabeça."),  # letra na emenda
    ("Fala a garra! Que horas são?", "Que horas são?"),        # a mesma, solta
    ("Palagarra! Que horas são?", "Que horas são?"),           # fundiu, 1º som trocado
    ("Balagarra, olhe para mim.", "olhe para mim."),
    ("Bala Garra, olhe para mim!", "olhe para mim!"),          # 1º som trocado
])
def test_o_erro_de_borda_do_stt_ainda_ativa(ouvido, resto) -> None:
    s = nova()
    d = avaliar(ouvido, s, 100.0)
    assert d.aceito is True and d.abriu_sessao
    assert d.motivo == "wake_phrase"
    assert d.texto == resto, "o que segue ao modelo é o original, sem a frase"


@pytest.mark.parametrize("ouvido", [
    "fala garrafa agora",             # a garantia nº 1 não pode cair com a tolerância
    # Uma letra de diferença, e frase de verdade: foi a falsa ativação que uma
    # tolerância de "qualquer letra" produziu na mesma medida.
    "Falta garra nesse time.",
    "Fala barra de ferro",
    "Fala garras afiadas",
    # "Fala gara" saiu desta lista em 29/09/2026: foi o que o Whisper escreveu
    # quando o Michel disse a frase. Ver a seção da letra dobrada, mais abaixo.
    "Paula Garra, tudo bem com você?",
    "Fala, guerra, que horas são?",
    "Vou agarrar aqui",
    "Oi Garra, tudo bem?",
    "E aí, fala Garra, tudo bem?",    # aproximado também é só no COMEÇO
])
def test_erro_fora_da_borda_nao_ativa(ouvido) -> None:
    s = nova()
    d = avaliar(ouvido, s, 100.0)
    assert d.aceito is False
    assert d.motivo == "wake_phrase_required"
    assert s.abertura is None


def test_a_tolerancia_nao_engole_a_primeira_palavra_do_pedido() -> None:
    """`"falagarraa"` está a uma letra da frase; casar por aí comeria o "a"."""
    s = nova()
    assert avaliar("Fala Garra e aí?", s, 100.0).texto == "e aí?"
    assert avaliar("Fala Garra, a luz está acesa?", s, 200.0).texto == "a luz está acesa?"
    assert avaliar("Falagarra a luz está acesa?", s, 300.0).texto == "a luz está acesa?"
    assert avaliar("Palagarra e aí?", s, 400.0).texto == "e aí?"


def test_a_emenda_e_a_da_frase_configurada() -> None:
    """A tolerância acompanha a frase do dono, não a de fábrica."""
    outra = ativacao.Ativacao(habilitada=True, frase=("oi", "reachy", "mini"),
                              janela_s=15.0, sessao_max_s=90.0)
    for ouvido in ("Oi Reachy Mini, olhe", "Oireachy mini, olhe",
                   "Oi Reachyn Mini, olhe", "Ei Reachy Mini, olhe"):
        assert avaliar(ouvido, nova(), 100.0, ativ=outra).texto == "olhe", ouvido
    assert avaliar("Oi Reachy Mimi, olhe", nova(), 100.0, ativ=outra).aceito is False


def test_a_frase_aproximada_sozinha_abre_sessao_sem_turno() -> None:
    s = nova()
    d = avaliar("Palagarra!", s, 100.0)
    assert d.aceito and d.so_ativacao and d.abriu_sessao
    assert d.texto == ""


def test_frase_curta_nao_ganha_tolerancia() -> None:
    """Uma letra em cinco não é erro de STT, é outra palavra: barra, jarra, agarra."""
    curta = ativacao.Ativacao(habilitada=True, frase=("garra",),
                              janela_s=15.0, sessao_max_s=90.0)
    for outra in ("Barra de ferro", "Agarra isso", "Gara", "Garras afiadas"):
        assert avaliar(outra, nova(), 100.0, ativ=curta).aceito is False, outra
    assert avaliar("Garra, olhe para mim", nova(), 100.0, ativ=curta).aceito is True


def test_o_eco_aproximado_tambem_nao_abre_sessao() -> None:
    """A margem anti-eco vale para o que ATIVA, exato ou não."""
    s = nova()
    s.falou_ate = 100.0
    d = avaliar("Falangarra, tudo bem", s, 100.3, capturado=100.2)
    assert d.aceito is False and d.motivo == "echo_suppressed"
    assert s.abertura is None


def test_a_decisao_diz_se_o_casamento_foi_exato_ou_aproximado() -> None:
    """Sem isto não há como saber, no robô, quanto a tolerância está salvando."""
    assert avaliar("Fala Garra, oi", nova(), 100.0).extras == {"wake_match": "exact"}
    assert avaliar("Falangarra, oi", nova(), 100.0).extras == {
        "wake_match": "approximate"}


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
    # Do 1.5.0 em diante a instalação nova nasce chamando pelo nome: robô de voz
    # que responde a tudo o que se diz na sala é o pior primeiro contato.
    assert c["wake_phrase_enabled"] is True
    assert c["wake_phrase_text"] == "Fala Garra"
    assert c["wake_phrase_alternatives"] == []
    assert c["wake_phrase_window_s"] == 15
    assert c["wake_phrase_session_max_s"] == 90


def test_config_antigo_recebe_os_padroes_sem_quebrar() -> None:
    c = conversa.normalizar({"mode": "informative", "revision": 7})
    assert c["wake_phrase_enabled"] is True      # chave ausente → padrão novo
    assert c["revision"] == 7 and c["mode"] == "informative"
    # Quem já tinha escolhido fica como estava, ligado ou desligado.
    assert conversa.normalizar({"wake_phrase_enabled": False})["wake_phrase_enabled"] is False


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
    # `ouvir(audio)` é quem chama o servidor de voz desde o 1.4.1.
    assert corpo.index("capturado_em") < corpo.index("ouvir(audio)"), (
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


def test_o_evento_de_ativacao_diz_como_a_frase_casou() -> None:
    """`exact` ou `approximate`, e nada do que foi dito."""
    corpo = _processar()
    i = corpo.index('"voice.wake.activated"')
    evento = corpo[i:i + 300]
    assert 'match=veredicto.extras.get("wake_match"' in evento
    for proibido in ("content=", "texto", "transcript", "hash"):
        assert proibido not in evento, f"o evento carrega {proibido}"


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
        "wake_phrase_enabled": False,
        "wake_phrase_text": "Ei Robô",
        "wake_phrase_alternatives": ["Olá robô", "Garra, escuta"],
        "wake_ack_sound_enabled": False,
        "wake_ack_motion_enabled": False,
        "listening_enabled": False,
        "mic_sensitivity": 8,
        "vad_silence_ms": 1200,
        "speech_language": "en",
        "speech_voice": "clara",
        "speech_rate_pct": 115,
        "speech_volume_pct": 60,
        "wake_phrase_window_s": 42,
        "wake_phrase_session_max_s": 300,
        "wake_second_listen_enabled": False,
        "wake_second_listen_always": True,
        "wake_diagnostics_enabled": True,
        "clock_enabled": True,
        "clock_timezone": "America/Sao_Paulo",
        "vision_enabled": True,
        "vision_provider": "local",
        "vision_local_endpoint": "http://10.0.0.24:11434",
        "vision_local_model": "gemma3:12b",
        "vision_openrouter_model": "openai/gpt-5-mini",
        "vision_cloud_enabled": True,
        "family_recognition_enabled": True,
        "music_voice_enabled": True,
        "ambient_context_enabled": True,
        "ambient_context_window_s": 90,
        "notebook_enabled": True,
        "progress_update_delay_ms": 5000,
        "max_progress_messages": 3,
        "acknowledgement_cut_threshold_ms": 900,
        "live_voice_enabled": True,
        "live_wake_phrase_text": "Atena",
        "live_wake_phrase_alternatives": ["Oi Atena"],
        "live_wake_spellings": ["Atenas"],
        "live_wake_strictness": "normal",
        "live_idle_close_s": 45,
        "live_motion": "speaking",
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


# ─── diagnóstico: o que o robô ouviu NO LUGAR da frase ───────────────────────
# Medido em 29/09/2026 com som de verdade (caixas de som → microfone do robô):
# "Fala Garra, que horas são?" chegou com dois caracteres a mais e foi recusada.
# O evento de rejeição não diz o que foi ouvido, de propósito — e por isso não
# havia como saber por quê. `quase` devolve só o trecho que se parece com a
# frase, nunca o resto do enunciado.
def quase(texto: str, frase=("fala", "garra")):
    return ativacao.quase(ativacao.tokens(texto), frase)


@pytest.mark.parametrize("ouvido,posicao,distancia,trecho", [
    ("E fala Garra, que horas são?", 1, 0, "fala garra"),
    ("Ah, é... fala Garra, tudo bem?", 2, 0, "fala garra"),
    ("Paula Garra, tudo bem com você?", 0, 2, "paula garra"),
    ("Fala, guerra, que horas são?", 0, 2, "fala guerra"),
    ("Fala Gara, tudo bem?", 0, 1, "fala gara"),
    ("Obrigado. Falangarra, que horas são?", 1, 1, "falangarra"),
])
def test_quase_acha_o_trecho_parecido_e_onde_ele_estava(ouvido, posicao, distancia,
                                                         trecho) -> None:
    q = quase(ouvido)
    assert q == {"position": posicao, "distance": distancia, "heard": trecho}


@pytest.mark.parametrize("ouvido", [
    "Que horas são agora?",
    "Acabou a manteiga, tem que comprar mais.",
    "Vamos almoçar daqui a pouco",
    "",
])
def test_conversa_que_nao_lembra_a_frase_nao_deixa_rastro(ouvido) -> None:
    assert quase(ouvido) is None


def test_quase_so_olha_o_comeco_do_enunciado() -> None:
    """Depois de meia dúzia de palavras já não é tentativa de chamar o robô."""
    assert quase("eu estava dizendo para ela ontem que fala garra é o nome") is None


def test_quase_nunca_devolve_o_resto_do_enunciado() -> None:
    q = quase("E fala Garra, o número do cartão é 4111 e a senha é segredo")
    assert q["heard"] == "fala garra"
    for palavra in ("cartao", "4111", "senha", "segredo", "numero"):
        assert palavra not in repr(q)


@pytest.mark.parametrize("ouvido", [
    "fala a garra que horas sao",
    # Em quatro pedaços a frase fecha certinho — e ainda assim não se guarda
    # quatro palavras: o limite é o que impede o trecho de virar o enunciado.
    "fa la ga rra que horas sao",
])
def test_quase_devolve_no_maximo_uma_palavra_a_mais_que_a_frase(ouvido) -> None:
    q = quase(ouvido)
    assert q is not None and len(q["heard"].split()) <= 3


def test_frase_curta_nao_tem_quase() -> None:
    """A três letras de "garra" está metade do dicionário."""
    assert ativacao.quase(ativacao.tokens("Barra de ferro"), ("garra",)) is None


# ─── letra dobrada: o mesmo som, escrito de outro jeito ──────────────────────
# Medido em 29/09/2026 com a voz do Michel, pelo microfone do robô: "Fala Garra"
# chegou do Whisper como "Fala Gara". Não é outra palavra, é a mesma com um `r`.
@pytest.mark.parametrize("ouvido,pedido", [
    ("Fala Gara, que horas são?", "que horas são?"),
    ("Falla Garra, que horas são?", "que horas são?"),
    ("Fala Garrra, liga a luz", "liga a luz"),
    ("fala gara", ""),
])
def test_letra_dobrada_a_mais_ou_a_menos_e_a_mesma_frase(ouvido, pedido) -> None:
    d = avaliar(ouvido, nova(), 100.0)
    assert d.aceito is True
    assert d.texto == pedido
    assert d.extras == {"wake_match": "approximate"}


@pytest.mark.parametrize("ouvido", [
    "Fala cara, tudo bem?",          # outra consoante
    "Fala gera, que horas são?",     # outra vogal
    "Fala para ela que eu já vou",
    "Fala agora o que você quer",
    "Fala galera, beleza?",
    "Falta garra nesse time",
    "Fala garrafa agora",
    "Fala gar, que horas são",       # sobrou meia palavra
])
def test_dobrar_letra_nao_abre_para_outras_palavras(ouvido) -> None:
    assert avaliar(ouvido, nova(), 100.0).aceito is False


def test_frase_curta_continua_exata_mesmo_com_letra_dobrada() -> None:
    curta = ativacao.Ativacao(habilitada=True, frase=("garra",))
    assert avaliar("Gara, que horas são?", nova(), 100.0, ativ=curta).aceito is False
    assert avaliar("Garra, que horas são?", nova(), 100.0, ativ=curta).aceito is True


# ─── segunda escuta: quando vale perguntar de novo ao STT ────────────────────
# Pela bancada (caixas de som → microfone do robô), 30 vezes "Fala Garra, …":
# o Whisper acertou a frase em 6. Nas outras saiu "fala gabi", "fala garne",
# "fala gab", "fala dara"… Dando a frase como dica ele acertou as 30 — mas dica
# em TUDO faz ruído virar "Fala Garra" (medido antes, e descartado).
#
# Primeira tentativa de meio-termo, medida no robô e REPROVADA: perguntar de
# novo sempre que o começo lembrasse a frase. "Fala galera, beleza?" e "Fala,
# cara, tudo bem?" ativaram o robô em 3 de 16 — a dica convence o Whisper a
# trocar "galera" por "Garra".
#
# O que separa os dois casos é o quanto o próprio Whisper confia no que
# escreveu. Quando erra a frase, ele duvida da palavra errada (0,13 a 0,62 em
# 12 medidas). Quando alguém diz mesmo "galera", "cara" ou "agora", ele tem
# certeza (0,83 a 0,99 em 9 de 11; nas outras duas a dica não o convenceu).
INCERTO = (0.95, 0.40, 0.98, 0.99, 0.99)
SEGURO = (0.95, 0.93, 0.98, 0.99, 0.99)


def merece(texto: str, confianca=INCERTO, frase=("fala", "garra")):
    toks = ativacao.tokens(texto)
    if confianca is not None:
        confianca = tuple(confianca[:len(toks)]) + (0.99,) * max(0, len(toks) - len(confianca))
    return ativacao.merece_segunda_escuta(toks, frase, confianca)


@pytest.mark.parametrize("ouvido", [
    "Fala Gabi, que horas são?",
    "Fala garne, que horas são?",
    "Fala gab o que tem na lista de compras",
    "Fala dara, quanto é sete vezes oito?",
    "Fala agora, qual é o seu nome?",        # o "Garra" do robô, ouvido como "agora"
])
def test_comeco_parecido_e_transcricao_em_duvida_merecem_segunda_escuta(ouvido) -> None:
    assert merece(ouvido) is not None


@pytest.mark.parametrize("ouvido", [
    "Fala galera, beleza?",
    "Fala, cara, tudo bem com você?",
    "Fala agora o que você quer",
    "Fala para ela que eu já vou",
    "Falta garra nesse time",
])
def test_quando_a_transcricao_tem_certeza_ninguem_pergunta_de_novo(ouvido) -> None:
    """A falsa ativação medida: o Whisper ouviu "galera" com 0,99 de confiança,
    e a dica o fez escrever "Garra" mesmo assim."""
    assert merece(ouvido, SEGURO) is None


def test_a_duvida_que_conta_e_a_do_trecho_parecido() -> None:
    # dúvida no pedido, certeza na frase: não é caso de conferir
    assert merece("Fala galera, beleza pura?", (0.95, 0.93, 0.20, 0.30)) is None
    # dúvida na primeira palavra também conta
    assert merece("Pau a garra", (0.32, 0.64, 0.71)) is not None
    # …mesmo quando é a ÚNICA em dúvida: quem saiu errado foi o "Fala"
    assert merece("Pau a garra", (0.32, 0.90, 0.95)) is not None
    assert merece("Pau a garra", (0.32, 0.90, 0.95))["confidence"] == 0.32
    assert merece("Palagra", (0.29,)) is not None


def test_no_limite_a_duvida_ainda_vale() -> None:
    assert merece("Fala agora, qual é o seu nome?", (0.92, 0.74)) is not None
    assert merece("Fala agora, qual é o seu nome?", (0.92, 0.75)) is None


def test_sem_saber_a_confianca_nao_ha_segunda_escuta() -> None:
    """Servidor de voz antigo não diz o quanto confia. Sem isso não há como
    separar "fala gabi" de "fala galera", e o erro que sobra é o pior dos dois."""
    assert merece("Fala Gabi, que horas são?", None) is None


@pytest.mark.parametrize("ouvido", [
    "Acabou a manteiga, hein. Tem que comprar mais.",
    "Que horas são agora?",
    "A barra de ferro caiu no chão",
    "Obrigado.",
    "",
    # parecido, mas não no começo: o robô não é chamado no meio da frase
    "E aí, fala gabi, que horas são?",
    "Eu disse fala garne para ele",
])
def test_conversa_comum_nao_e_ouvida_duas_vezes(ouvido) -> None:
    assert merece(ouvido, (0.2,) * 12) is None


def test_quem_ja_casou_nao_precisa_de_segunda_escuta() -> None:
    assert merece("Fala Garra, que horas são?", (0.2,) * 6) is None
    assert merece("Falagarra que horas são", (0.2,) * 6) is None


def test_frase_curta_nao_tem_segunda_escuta() -> None:
    assert ativacao.merece_segunda_escuta(
        ativacao.tokens("Barra de ferro"), ("garra",), (0.2, 0.2, 0.2)) is None


# ─── a confiança, palavra por palavra ────────────────────────────────────────
@pytest.mark.parametrize("texto,por_palavra,por_token", [
    ("Fala, cara, tudo bem?", (0.9, 0.8, 0.7, 0.6), (0.9, 0.8, 0.7, 0.6)),
    # uma palavra do STT que vira dois tokens: os dois herdam a confiança dela
    ("Fala guarda-chuva agora", (0.9, 0.4, 0.7), (0.9, 0.4, 0.4, 0.7)),
    # e uma que não vira token nenhum
    ("Fala — Garra", (0.9, 0.5, 0.3), (0.9, 0.3)),
    ("", (), ()),
])
def test_a_confianca_acompanha_os_tokens(texto, por_palavra, por_token) -> None:
    assert ativacao.confianca_por_token(texto, por_palavra) == por_token


@pytest.mark.parametrize("texto,por_palavra", [
    ("Fala cara tudo bem", (0.9, 0.8)),          # faltam palavras
    ("Fala cara", (0.9, 0.8, 0.7)),              # sobram
    ("Fala cara", None),
    ("Fala cara", ("x", 0.8)),
    ("Fala cara", (0.9, 1.7)),                   # probabilidade que não é probabilidade
])
def test_confianca_que_nao_bate_com_o_texto_e_desconhecida(texto, por_palavra) -> None:
    assert ativacao.confianca_por_token(texto, por_palavra) is None


# ─── a dica da segunda escuta ────────────────────────────────────────────────
@pytest.mark.parametrize("configurada,dica", [
    ("Fala Garra", "Fala Garra"),
    ("  Fala,   Garra! ", "Fala Garra"),
    ("Ei Robô", "Ei Robô"),              # com o acento: é o que o Whisper escreveria
    ("R2 D2", "R2 D2"),
])
def test_a_dica_e_a_frase_configurada_so_com_as_palavras(configurada, dica) -> None:
    ativ = ativacao.Ativacao.de({"wake_phrase_enabled": True, "wake_phrase_text": configurada})
    assert ativ.dica == dica


@pytest.mark.parametrize("texto,confirma", [
    ("Fala Garra, que horas são?", True),
    ("fala garra", True),
    ("Fala agarra, que horas são?", False),     # aproximado não confirma
    ("Falagarra que horas são", False),
    ("Fala Gara, que horas são?", False),
    ("Que horas são, fala Garra?", False),
    ("", False),
])
def test_confirmar_pede_a_frase_exata_no_comeco(texto, confirma) -> None:
    assert ativacao.confirma(texto, ("fala", "garra")) is confirma
