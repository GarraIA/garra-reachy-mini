"""O mestre da saída de voz (`speech_output_enabled`).

Por que um campo NOVO, e não o reaproveitamento de `automatic_speech_enabled`:
em produção os quatro interruptores históricos estão `false` **e o robô fala a
resposta final**. Isso prova que `automatic_speech_enabled` nunca governou a
saída — governa a fala automática. Reaproveitá-lo emudeceria, no upgrade, todo
robô que hoje responde, e sem ninguém ter pedido.

O teste que mais importa aqui é o da compatibilidade: config antiga não tem a
chave, e a ausência tem de normalizar para `True`.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from garra_reachy_mini import conversa
from garra_reachy_mini.web import api as web_api

TUDO_AUTOMATICO_DESLIGADO = {
    "automatic_speech_enabled": False,
    "spoken_acknowledgements_enabled": False,
    "spoken_progress_updates": False,
    "announce_tool_usage": False,
}


def politica(**kw) -> conversa.Politica:
    """Política a partir do bloco `conversation`, como o app faz."""
    conf = {**kw}
    return conversa.Politica.de(conf, {"spoken_greeting_enabled":
                                       kw.get("spoken_greeting_enabled", True)})


# ─── compatibilidade: o que não pode mudar para quem já usa ──────────────────
def test_config_antigo_sem_o_campo_normaliza_para_ligado() -> None:
    """A garantia central. Ausência ≠ desligado."""
    c = conversa.normalizar({"mode": "fast", "revision": 3})
    assert c["speech_output_enabled"] is True


def test_config_de_producao_hoje_continua_falando_a_resposta() -> None:
    """Os quatro históricos em `false`, como está no robô agora."""
    p = politica(**TUDO_AUTOMATICO_DESLIGADO)
    assert p.pode_responder_falando is True, (
        "desligar as falas automáticas nunca calou a resposta final; "
        "mudar isso emudeceria robôs no upgrade")
    assert p.pode_avisar is False and p.pode_progredir is False


def test_escrita_parcial_nao_zera_o_mestre() -> None:
    c = conversa.normalizar({**TUDO_AUTOMATICO_DESLIGADO, "mode": "informative"})
    assert c["speech_output_enabled"] is True


def test_o_mestre_persiste_quando_gravado_desligado() -> None:
    gravado = conversa.normalizar({"speech_output_enabled": False})
    assert gravado["speech_output_enabled"] is False
    assert conversa.normalizar(gravado) == gravado, "reler devolve o mesmo"


def test_nenhum_outro_campo_e_materializado_por_carregar_config_antigo() -> None:
    """Normalizar preenche padrões em memória, não reescreve escolhas."""
    antigo = {"mode": "informative", "revision": 3}
    depois = conversa.normalizar(antigo)
    assert antigo == {"mode": "informative", "revision": 3}, "entrada intacta"
    assert depois["mode"] == "informative" and depois["revision"] == 3


# ─── semântica: a matriz ─────────────────────────────────────────────────────
@pytest.mark.parametrize("saida,automatica,resposta,avisos", [
    (True, False, True, False),    # o caso de produção hoje
    (True, True, True, True),
    (False, True, False, False),
    (False, False, False, False),
])
def test_matriz_dos_dois_mestres(saida, automatica, resposta, avisos) -> None:
    p = politica(speech_output_enabled=saida,
                 automatic_speech_enabled=automatica,
                 spoken_acknowledgements_enabled=True,
                 spoken_progress_updates=True,
                 spoken_greeting_enabled=True)
    assert p.pode_responder_falando is resposta
    assert p.pode_avisar is avisos
    assert p.pode_progredir is avisos
    assert p.pode_saudar is avisos


def test_mestre_desligado_cala_ate_os_subtoggles_todos_ligados() -> None:
    p = politica(speech_output_enabled=False, automatic_speech_enabled=True,
                 spoken_acknowledgements_enabled=True,
                 spoken_progress_updates=True, announce_tool_usage=True,
                 spoken_greeting_enabled=True)
    assert not any((p.pode_avisar, p.pode_progredir, p.pode_saudar,
                    p.pode_anunciar_ferramenta, p.pode_responder_falando))
    assert p.alguma_fala_automatica is False, (
        "nem vale pré-sintetizar: são 6 chamadas de TTS que nunca tocariam")


def test_cada_subtoggle_governa_so_o_seu_caminho() -> None:
    base = dict(speech_output_enabled=True, automatic_speech_enabled=True,
                spoken_acknowledgements_enabled=True,
                spoken_progress_updates=True, spoken_greeting_enabled=True)
    assert politica(**{**base, "spoken_acknowledgements_enabled": False}).pode_progredir
    assert not politica(**{**base, "spoken_acknowledgements_enabled": False}).pode_avisar
    assert politica(**{**base, "spoken_progress_updates": False}).pode_avisar
    assert not politica(**{**base, "spoken_progress_updates": False}).pode_progredir


def test_motivo_do_silencio_aponta_o_interruptor_certo() -> None:
    """Dizer "acknowledgements desabilitados" mandaria mexer no lugar errado."""
    assert politica(speech_output_enabled=False,
                    automatic_speech_enabled=True,
                    spoken_acknowledgements_enabled=True
                    ).motivo_silencio() == "speech_output_disabled"
    assert politica(speech_output_enabled=True,
                    automatic_speech_enabled=False
                    ).motivo_silencio() == "automatic_speech_disabled"


# ─── o coordenador recusa, mesmo se quem chamou esquecer de perguntar ────────
class _AudioFalso:
    def __init__(self) -> None:
        self.empurrados = 0

    def push_audio_sample(self, onda) -> None:
        self.empurrados += 1

    def clear_player(self) -> None:
        pass


def _turno(p: conversa.Politica) -> conversa.Turno:
    import time
    return conversa.Turno(id="trn_1", correlacao="c1", inicio=time.monotonic(),
                          politica=p)


def test_o_dono_do_alto_falante_recusa_com_a_saida_desligada() -> None:
    """Cinto e suspensórios: uma rota nova que esqueça o mestre morre aqui."""
    import logging
    audio = _AudioFalso()
    coord = conversa.CoordenadorAudio(audio, 16000, logging.getLogger("t"))
    t = _turno(politica(speech_output_enabled=False, automatic_speech_enabled=True,
                        spoken_acknowledgements_enabled=True))
    coord.abrir(t)
    onda = [0.0] * 100
    assert coord.tocar_ack(t, onda) is False
    assert coord.tocar_final(t, [onda]) is False
    assert audio.empurrados == 0, "nada pode ter chegado ao alto-falante"


def test_o_dono_do_alto_falante_toca_com_a_saida_ligada() -> None:
    import logging
    audio = _AudioFalso()
    coord = conversa.CoordenadorAudio(audio, 16000, logging.getLogger("t"))
    t = _turno(politica(speech_output_enabled=True, automatic_speech_enabled=True,
                        spoken_acknowledgements_enabled=True))
    coord.abrir(t)
    assert coord.tocar_final(t, [[0.0] * 100]) is True
    assert audio.empurrados == 1


# ─── o choke point em falar() ────────────────────────────────────────────────
FONTE = (Path(__file__).resolve().parent.parent
         / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")


def _corpo_de(fonte: str, assinatura: str) -> str:
    """A função INTEIRA, não uma janela de tamanho chutado.

    Uma janela fixa passa enquanto a função for curta e falha por corte quando
    ela crescer — mediria o comprimento do código, não a ordem que pretende
    proteger. Aqui o corpo termina na primeira linha não vazia cuja indentação
    volta ao nível do `def`.
    """
    linhas = fonte.splitlines()
    inicio = next(n for n, l in enumerate(linhas) if assinatura in l)
    nivel = len(linhas[inicio]) - len(linhas[inicio].lstrip())
    for n in range(inicio + 1, len(linhas)):
        l = linhas[n]
        if l.strip() and (len(l) - len(l.lstrip())) <= nivel:
            return "\n".join(linhas[inicio:n])
    return "\n".join(linhas[inicio:])


def test_falar_consulta_o_mestre_antes_de_qualquer_efeito() -> None:
    """A ordem é o requisito: nada de TTS, trava, estado ou dreno antes disto.

    Guarda textual porque exercitar `falar()` de verdade exige robô, SDK de
    áudio e servidor de voz. O que ela protege é a ORDEM, que nenhum teste de
    unidade sobre a política alcançaria.
    """
    corpo = _corpo_de(FONTE, "def falar(texto: str")
    gate = corpo.index("saida_habilitada")
    for depois in ("_lock_fala.acquire", "gestos.falando()", "voz.falar(",
                   "push_audio_sample", "drenar_mic", "tocar_final"):
        assert gate < corpo.index(depois), (
            f"o mestre é consultado DEPOIS de {depois} — a checagem tem de vir "
            "antes de qualquer efeito colateral")


def test_falar_le_a_configuracao_a_cada_chamada() -> None:
    """Sem isso, o interruptor do painel só valeria depois de reiniciar."""
    corpo = _corpo_de(FONTE, "def falar(texto: str")
    assert "Config.carregar()" in corpo[:corpo.index("_lock_fala.acquire")]


def test_falar_devolve_resultado_tipado() -> None:
    from garra_reachy_mini.main import ResultadoFala
    assert ResultadoFala.DESABILITADA.value == "speech_output_disabled"
    assert ResultadoFala.TTS_INDISPONIVEL.value == "tts_unavailable"
    assert ResultadoFala.FALHA.value == "playback_failed"
    assert ResultadoFala.FALADA.value == "spoken"
    assert "return ResultadoFala.DESABILITADA" in FONTE


def test_uma_fala_ja_iniciada_termina_e_a_proxima_respeita() -> None:
    """Regra deliberada do B0: não cortar áudio em andamento.

    Interromper playback acrescentaria concorrência logo depois do hotfix de
    threads e sockets. O `stop` existente continua sendo o caminho explícito de
    interrupção; a mudança vale da próxima chamada em diante — que é o que a
    releitura por chamada garante.
    """
    corpo = _corpo_de(FONTE, "def falar(texto: str")
    assert "clear_player" not in corpo, (
        "o B0 não corta áudio em andamento; isso é escopo do stop")


def test_nenhuma_rota_alternativa_ignora_o_mestre() -> None:
    """Os três caminhos até o alto-falante, todos cobertos.

    Mapeados no código: `falar()` empurra direto quando não há turno, e o
    coordenador tem `tocar_ack` e `tocar_final`. Um quarto caminho novo tem de
    aparecer aqui como falha, não no robô de alguém.
    """
    empurra_direto = FONTE.count("push_audio_sample")
    assert empurra_direto == 1, (
        f"{empurra_direto} lugares empurram áudio direto em main.py; só o de "
        "dentro de `falar()` é conhecido — um novo precisa passar pelo mestre")
    fonte_conversa = (Path(__file__).resolve().parent.parent
                      / "garra_reachy_mini" / "conversa.py").read_text("utf-8")
    # As duas portas do coordenador conferem a política do turno.
    for metodo in ("def tocar_ack", "def tocar_final"):
        i = fonte_conversa.index(metodo)
        corpo = fonte_conversa[i:i + 700]
        assert "saida_habilitada" in corpo, f"{metodo} não consulta o mestre"


# ─── a rota explícita do painel ──────────────────────────────────────────────
def test_a_rota_distingue_os_quatro_desfechos() -> None:
    fonte_api = (Path(__file__).resolve().parent.parent
                 / "garra_reachy_mini" / "web" / "api.py").read_text("utf-8")
    i = fonte_api.index('@r.post("/falar")')
    corpo = fonte_api[i:i + 1800]
    assert 'codigo="invalid_text"' in corpo
    assert 'codigo="tts_unavailable"' in corpo
    assert 'codigo="speech_output_disabled"' in corpo
    assert 'codigo="playback_failed"' in corpo
    assert "409" in corpo, "saída desligada é 409, não 200 silencioso"
    assert '"ok": True' in corpo


def test_o_erro_nunca_carrega_o_texto_pedido() -> None:
    """O que se registra é a falha, não o que mandaram o robô dizer."""
    fonte_api = (Path(__file__).resolve().parent.parent
                 / "garra_reachy_mini" / "web" / "api.py").read_text("utf-8")
    corpo = _corpo_de(fonte_api, "async def _falar_seguro")
    # Passar o texto ao TTS é o trabalho da função; o que não pode é ele
    # aparecer no tratamento do erro. É o `except` que se inspeciona.
    tratamento = corpo[corpo.index("except Exception"):]
    assert "texto" not in tratamento, (
        "o texto pedido não pode entrar no log nem no evento de erro")
    assert tratamento.count("type(e).__name__") == 2, (
        "log e evento registram o TIPO da exceção, não a mensagem — que pode "
        "carregar trecho do texto")


def test_erro_com_codigo_nao_quebra_a_forma_historica() -> None:
    """20 rotas dependem de `error` ser a mensagem; só quem passa código muda."""
    from garra_reachy_mini.web.api import _erro
    antigo = _erro(400, "texto vazio")
    assert antigo.detail == {"ok": False, "error": "texto vazio"}
    novo = _erro(409, "desligada", codigo="speech_output_disabled")
    assert novo.detail["error"]["code"] == "speech_output_disabled"


def test_capacidade_anunciada_para_o_painel() -> None:
    from garra_reachy_mini import build_info
    assert build_info.CAPACIDADES["speech_output_control"] is True
    assert build_info.CAPACIDADES["automatic_speech_toggles"] is True, (
        "a capacidade histórica continua: os controles antigos não sumiram")
    assert build_info.CAPACIDADES["wake_phrase"] is True, (
        "a wake phrase existe neste build; um painel antigo precisa saber")


# ─── recursos: o B0 não pode reintroduzir o que o 1.2.1 fechou ───────────────
def _contar() -> tuple[int, int]:
    import os
    base = f"/proc/{os.getpid()}/fd"
    return len(os.listdir(base)), threading.active_count()


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="precisa de /proc")
def test_alternar_o_mestre_nao_cria_recurso_nenhum() -> None:
    """200 alternâncias: nem fd, nem thread, nem task."""
    conversa.Politica.de({"speech_output_enabled": True})   # aquecimento
    fds0, thr0 = _contar()
    for i in range(200):
        p = conversa.Politica.de({"speech_output_enabled": bool(i % 2),
                                  "automatic_speech_enabled": True,
                                  "spoken_acknowledgements_enabled": True})
        assert p.pode_avisar is bool(i % 2)
    fds1, thr1 = _contar()
    assert fds1 - fds0 <= 1, f"fds {fds0}→{fds1}"
    assert thr1 - thr0 == 0, f"threads {thr0}→{thr1}"


def test_a_politica_e_congelada_e_sem_estado() -> None:
    """Nada de executor, timer ou task escondidos numa dataclass de decisão."""
    p = politica(speech_output_enabled=True)
    with pytest.raises(Exception):
        p.saida_habilitada = False   # frozen
    for valor in vars(p).values():
        assert not isinstance(valor, (threading.Thread, threading.Timer))
