"""Desligar a fala automática tem de calar tudo, em qualquer duração.

O modo (`fast`/`informative`) sempre decidiu **quando** o aviso podia começar.
Nunca decidiu **se** ele existe — em qualquer modo havia um atraso, e passado o
atraso a frase saía. Quem não quer filler nenhum não tinha o que desligar.

O controle mestre é `automatic_speech_enabled`. Ele entra por `and` em cada uma
das quatro perguntas de `Politica`, e é por isso que nenhum atraso, modo ou
refinamento consegue contorná-lo: não existe caminho que produza fala automática
sem passar por lá.

Origem medida das frases, para o registro:

* `startup_greeting`   — `config.py:50` `SAUDACAO`, tocada em `main.py` logo
  após a calibração do limiar, **sem pergunta nenhuma**. No robô: `voice.state`
  → `speaking` 11,4 s depois de o loop de voz subir, sem `chat.message` de
  usuário antes.
* `local_acknowledgement` — `config.py:36-41` `FRASES_ESPERA`, e "Deixa eu ver
  isso." é a quarta delas. Escolhida por `random.choice` e tocada quando o
  prazo absoluto vence.
* `progress_update` — `config.py:45-48` `FRASES_PROGRESSO`.
* `tool_preamble` — **não existe**: nenhum caminho de código fala antes de uma
  ferramenta. O interruptor existe para que, se um preâmbulo aparecer, nasça
  desligado.
* `model_generated` — o prompt carregado do `reachy_voice` não pede anúncio
  nenhum; conferido no agente em execução, não no arquivo.
"""

from __future__ import annotations

import pathlib

import pytest

from garra_reachy_mini import build_info, conversa
from garra_reachy_mini.config import FRASES_ESPERA, FRASES_PROGRESSO, SAUDACAO

TUDO_DESLIGADO = {
    "mode": "fast",
    "automatic_speech_enabled": False,
    "spoken_acknowledgements_enabled": False,
    "spoken_progress_updates": False,
    "announce_tool_usage": False,
}
SEM_SAUDACAO = {"spoken_greeting_enabled": False}


def pol(conf=None, arranque=None) -> conversa.Politica:
    return conversa.Politica.de(conf or {}, arranque)


# ── 1. a frase que o Michel ouviu está mesmo onde eu digo que está ───────────
def test_deixa_eu_ver_isso_e_uma_frase_de_espera_local():
    """Caso 2 (`local_acknowledgement`), não preâmbulo de ferramenta."""
    assert "Deixa eu ver isso." in FRASES_ESPERA
    assert "Deixa eu ver isso." not in FRASES_PROGRESSO
    assert "Deixa eu ver isso." not in SAUDACAO


def test_nenhum_codigo_fala_antes_de_uma_ferramenta():
    """Caso 4 não existe. Se um dia existir, este teste cai e obriga a pensar."""
    fonte = (pathlib.Path(__file__).resolve().parents[1]
             / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")
    corte = fonte[fonte.index("def processar("):fonte.index("cerebro_ms =")]
    # A única fala dentro do turno antes da resposta é o ack, e ele passa pelo
    # coordenador. Nada chama `falar(` direto antes de uma tool.
    assert "controlador.submeter" in corte          # a ferramenta local roda...
    trecho = corte[:corte.index("controlador.submeter")]
    assert "falar(" not in trecho                    # ...e nada é falado antes


# ── 9/10. o controle mestre ──────────────────────────────────────────────────
def test_mestre_desligado_cala_as_quatro_saidas():
    p = pol(TUDO_DESLIGADO, SEM_SAUDACAO)
    assert not p.pode_avisar
    assert not p.pode_progredir
    assert not p.pode_anunciar_ferramenta
    assert not p.pode_saudar
    assert not p.alguma_fala_automatica


def test_mestre_desligado_vence_os_subordinados_ligados():
    """A prioridade é a razão de o mestre existir: um subordinado ligado por
    engano (ou por uma versão anterior do config) não pode falar."""
    p = pol({**TUDO_DESLIGADO,
             "spoken_acknowledgements_enabled": True,
             "spoken_progress_updates": True,
             "announce_tool_usage": True},
            {"spoken_greeting_enabled": True})
    assert not (p.pode_avisar or p.pode_progredir
                or p.pode_anunciar_ferramenta or p.pode_saudar)


def test_mestre_ligado_devolve_o_padrao_de_hoje():
    p = pol({"mode": "fast"})
    assert p.pode_avisar and p.pode_progredir and p.pode_saudar
    # Anúncio de ferramenta nasce desligado: não há código que o faça.
    assert not p.pode_anunciar_ferramenta


# ── 11/12/13. os refinamentos, um a um ───────────────────────────────────────
def test_so_o_acknowledgement_desligado():
    p = pol({"spoken_acknowledgements_enabled": False})
    assert not p.pode_avisar
    assert p.pode_progredir and p.pode_saudar


def test_so_o_progresso_desligado():
    p = pol({"spoken_progress_updates": False})
    assert p.pode_avisar and not p.pode_progredir


def test_so_o_anuncio_de_ferramenta_ligado():
    p = pol({"announce_tool_usage": True})
    assert p.pode_anunciar_ferramenta


# ── 1. saudação ──────────────────────────────────────────────────────────────
def test_saudacao_desligada_sozinha_nao_cala_o_resto():
    p = pol({}, SEM_SAUDACAO)
    assert not p.pode_saudar
    assert p.pode_avisar


def test_saudacao_e_um_bloco_separado_de_conversation():
    """Não pertence a turno nenhum, então não mora dentro de `conversation`."""
    assert "spoken_greeting_enabled" not in conversa.PADRAO
    assert conversa.PADRAO_ARRANQUE == {"spoken_greeting_enabled": True}


# ── 2/3/4. duração nenhuma contorna o mestre ─────────────────────────────────
@pytest.mark.parametrize("ms", [1_000, 3_999, 4_001, 10_000, 30_000, 300_000])
def test_nenhuma_duracao_libera_fala_com_o_mestre_desligado(ms):
    """"Mesmo que a resposta demore 4, 10 ou 30 segundos" — literalmente."""
    p = pol({**TUDO_DESLIGADO, "profiles": {"fast": {"acknowledgement_delay_ms": 0}},
             "progress_update_delay_ms": 1000})
    assert not p.pode_avisar and not p.pode_progredir
    # O prazo continua sendo calculado (as métricas o usam); o que não há é
    # permissão para falar quando ele vence.
    assert p.prazo_ack(0.0) == 0.0
    assert p.prazo_progresso(0.0) == 1.0


# ── 7/8. os dois modos ───────────────────────────────────────────────────────
@pytest.mark.parametrize("modo,esperado", [("fast", 4000), ("informative", 1500)])
def test_o_modo_segue_mandando_no_quando(modo, esperado):
    p = pol({"mode": modo})
    assert p.modo == modo and p.ack_atraso_ms == esperado


@pytest.mark.parametrize("modo", ["fast", "informative"])
def test_o_mestre_cala_os_dois_modos(modo):
    assert not pol({**TUDO_DESLIGADO, "mode": modo}).pode_avisar


# ── 8. o evento ──────────────────────────────────────────────────────────────
def test_a_decisao_desabilitado_e_distinta_de_cancelado_e_de_none():
    """Quem lê métricas precisa separar "não deu tempo" de "está desligado"."""
    assert conversa.DESABILITADO == "disabled"
    assert conversa.DESABILITADO not in (conversa.CANCELADO, conversa.CONCLUIDO,
                                         conversa.CORTADO, conversa.AGENDADO,
                                         conversa.TOCANDO, "none")


def test_resolver_ack_devolve_disabled_sem_tentar_cortar():
    coord = conversa.CoordenadorAudio(media=None, sr_saida=16000,
                                      log=__import__("logging").getLogger("t"))
    turno = conversa.Turno(id="t1", correlacao="c1", inicio=0.0,
                           politica=pol(TUDO_DESLIGADO),
                           ack_estado=conversa.DESABILITADO)
    assert coord.resolver_ack(turno) == conversa.DESABILITADO
    assert turno.ack_decisao == conversa.DESABILITADO
    # `media=None` provaria qualquer tentativa de tocar ou cortar: estouraria.


def test_o_motivo_distingue_mestre_de_refinamento():
    assert pol(TUDO_DESLIGADO).motivo_silencio() == "automatic_speech_disabled"
    assert (pol({"spoken_acknowledgements_enabled": False}).motivo_silencio()
            == "spoken_acknowledgements_disabled")


# ── 14/15. nada enfileirado, resposta final intacta ──────────────────────────
FONTE = (pathlib.Path(__file__).resolve().parents[1]
         / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")


def test_nada_e_sintetizado_quando_nao_pode_falar():
    """"nenhuma frase é sintetizada; nenhuma frase entra na fila"."""
    trecho = FONTE[FONTE.index("Pré-sintetiza frases de espera"):
                   FONTE.index("Calibra o ruído ambiente")]
    assert "if politica_inicial.pode_avisar:" in trecho
    assert "if politica_inicial.pode_progredir:" in trecho


def test_o_processamento_nao_espera_prazo_que_nao_vai_usar():
    """"o processamento do modelo continua imediatamente"."""
    trecho = FONTE[FONTE.index("resposta = None"):FONTE.index("cerebro_ms =")]
    corte = trecho.index("not politica.pode_avisar and not politica.pode_progredir")
    assert "esperar_cerebro(None)" in trecho[corte:corte + 900]


def test_a_resposta_final_continua_sendo_falada():
    """O interruptor desliga fala automática, não a resposta."""
    assert "falar(fala, turno=turno)" in FONTE


def test_a_saudacao_passa_pelo_interruptor():
    trecho = FONTE[FONTE.index("if politica_inicial.pode_saudar:"):]
    assert "falar(SAUDACAO)" in trecho[:200]
    assert "voice.startup.greeting" in trecho[:800]


# ── 5. estado visual e ferramentas sobrevivem ────────────────────────────────
def test_o_estado_visual_thinking_nao_depende_da_fala():
    """"preserve antenas e expressão de thinking" — `gestos.pensando()` é
    chamado antes de qualquer decisão sobre falar."""
    processar = FONTE[FONTE.index("def processar("):FONTE.index("cerebro_ms =")]
    assert processar.index("gestos.pensando()") < processar.index("pode_avisar")


def test_o_e_stop_nao_passa_por_politica_nenhuma():
    """Calar é imediato e não consulta interruptor de fala automática."""
    trecho = FONTE[FONTE.index("def calar("):FONTE.index("def ler_mono(")]
    assert "pode_avisar" not in trecho and "fala_automatica" not in trecho


# ── 16. persistência ─────────────────────────────────────────────────────────
def test_os_interruptores_sobrevivem_a_normalizacao():
    """Reler o que foi gravado tem de devolver o mesmo — é o que "persiste
    após restart" significa do lado do arquivo."""
    gravado = conversa.normalizar(TUDO_DESLIGADO)
    assert conversa.normalizar(gravado) == gravado
    for chave in conversa.BOOLEANOS:
        assert gravado[chave] is False
    arranque = conversa.normalizar_arranque(SEM_SAUDACAO)
    assert conversa.normalizar_arranque(arranque) == arranque


def test_config_antigo_sem_as_chaves_nao_quebra():
    """Um `config.json` de antes desta versão continua abrindo."""
    c = conversa.normalizar({"mode": "informative", "revision": 3})
    assert c["automatic_speech_enabled"] is True   # comportamento de hoje
    assert c["announce_tool_usage"] is False
    assert conversa.normalizar_arranque(None)["spoken_greeting_enabled"] is True


def test_perfil_atualizado_grava_os_interruptores():
    novo = conversa.perfil_atualizado(conversa.PADRAO,
                                      {"automatic_speech_enabled": False})
    assert novo["automatic_speech_enabled"] is False
    # E não estraga o resto.
    assert novo["profiles"]["fast"]["acknowledgement_delay_ms"] == 4000


def test_a_capacidade_e_anunciada():
    assert build_info.CAPACIDADES["automatic_speech_toggles"] is True


# ── 17/18/19. os dois painéis escrevem o mesmo config, com a mesma revisão ───
@pytest.fixture
def api(backend, tmp_path, monkeypatch):
    """O app real, com armazenamento isolado — é o mesmo alvo dos dois painéis.

    O `:3888` não fala com o robô por outra porta: o companion repassa para
    estas rotas. Testar aqui cobre os dois caminhos de escrita.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from garra_reachy_mini import armazenamento
    from garra_reachy_mini.robo.acoes import ControladorRobo
    from garra_reachy_mini.web.api import ContextoWeb, montar
    from garra_reachy_mini.web.camera import FrameHub
    from garra_reachy_mini.web.seguranca import resolver_politica

    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    if hasattr(armazenamento.diretorio, "cache_clear"):
        armazenamento.diretorio.cache_clear()
    ctrl = ControladorRobo(backend, semente=5, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def test_desligar_tudo_numa_escrita_so(api):
    r = api.put("/api/robot/conversation", json={
        **TUDO_DESLIGADO, "startup": SEM_SAUDACAO,
        "revision": 0, "updated_by": "garra-dashboard"})
    assert r.status_code == 200, r.text
    d = r.json()
    for chave in conversa.BOOLEANOS:
        assert d["conversation"][chave] is False, chave
    assert d["startup"]["spoken_greeting_enabled"] is False
    # `effective` é o que o robô vai obedecer, não o que foi pedido.
    assert d["effective"]["fala_automatica"] is False
    assert d["effective"]["saudacao_habilitada"] is False


def test_o_robo_confirma_relendo(api):
    api.put("/api/robot/conversation", json={
        **TUDO_DESLIGADO, "startup": SEM_SAUDACAO, "revision": 0})
    d = api.get("/api/robot/conversation").json()
    assert d["conversation"]["automatic_speech_enabled"] is False
    assert d["startup"]["spoken_greeting_enabled"] is False
    assert d["conversation"]["revision"] == 1


def test_revisao_antiga_da_409_e_devolve_o_estado_atual(api):
    api.put("/api/robot/conversation", json={"automatic_speech_enabled": False,
                                             "revision": 0})
    r = api.put("/api/robot/conversation", json={"automatic_speech_enabled": True,
                                                 "revision": 0})
    assert r.status_code == 409
    assert r.json()["conversation"]["automatic_speech_enabled"] is False


def test_a_saudacao_tambem_entra_no_409(api):
    """Um painel que só mexe na saudação não pode escapar da concorrência."""
    api.put("/api/robot/conversation", json={"startup": SEM_SAUDACAO, "revision": 0})
    r = api.put("/api/robot/conversation",
                json={"startup": {"spoken_greeting_enabled": True}, "revision": 0})
    assert r.status_code == 409


def test_mexer_so_no_mestre_preserva_os_subordinados(api):
    """"seus valores podem ser preservados" — desligar o mestre não apaga o
    ajuste de quem está embaixo."""
    api.put("/api/robot/conversation",
            json={"spoken_progress_updates": False, "revision": 0})
    api.put("/api/robot/conversation",
            json={"automatic_speech_enabled": False, "revision": 1})
    api.put("/api/robot/conversation",
            json={"automatic_speech_enabled": True, "revision": 2})
    d = api.get("/api/robot/conversation").json()
    assert d["conversation"]["spoken_progress_updates"] is False   # preservado
    assert d["conversation"]["automatic_speech_enabled"] is True


def test_o_modo_continua_funcionando_com_a_fala_desligada(api):
    d = api.put("/api/robot/conversation", json={
        **TUDO_DESLIGADO, "mode": "informative", "revision": 0}).json()
    assert d["conversation"]["mode"] == "informative"
    assert d["effective"]["ack_atraso_ms"] == 1500   # o "quando" sobrevive
    assert d["effective"]["fala_automatica"] is False  # o "se" manda


# ── os dois painéis desenham e gravam os mesmos interruptores ────────────────
PAINEL_LOCAL = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini" / "static"
WEBCHAT = pathlib.Path(
    "/home/michel/Documents/Projetos/GarraIA/crates/garraia-gateway/src/webchat.html")

CAMPOS = ("automatic_speech_enabled", "spoken_acknowledgements_enabled",
          "spoken_progress_updates", "announce_tool_usage")


def test_o_painel_local_grava_os_quatro_e_a_saudacao():
    js = (PAINEL_LOCAL / "reachy.js").read_text(encoding="utf-8")
    corpo = js[js.index("$('btn-conversa-salvar').addEventListener"):]
    corpo = corpo[:corpo.index("}));")]
    for campo in CAMPOS:
        assert campo in corpo, campo
    assert "startup: { spoken_greeting_enabled" in corpo


def test_o_painel_local_desenha_o_mestre_antes_do_modo():
    html = (PAINEL_LOCAL / "reachy.html").read_text(encoding="utf-8")
    assert html.index('id="conversa-mestre"') < html.index('id="btn-modo-fast"')
    assert html.count('data-conversa-sub="1"') >= 3


def test_o_painel_local_tem_traducao_nos_dois_idiomas():
    i18n = (PAINEL_LOCAL / "i18n.js").read_text(encoding="utf-8")
    en = i18n[i18n.index("  en: {"):i18n.index("  pt: {")]
    pt = i18n[i18n.index("  pt: {"):]
    for chave in ("conversa.mestre", "conversa.ack_on", "conversa.tool_on",
                  "conversa.saudacao"):
        assert f"'{chave}'" in en and f"'{chave}'" in pt, chave
    assert "Falas automáticas durante o processamento" in pt
    assert "Automatic speech while processing" in en


def test_o_console_3888_grava_os_quatro_e_a_saudacao():
    if not WEBCHAT.exists():
        return   # o repositório do gateway não acompanha este pacote
    html = WEBCHAT.read_text(encoding="utf-8")
    corpo = html[html.index("document.getElementById('btn-conversa-salvar')"):]
    corpo = corpo[:corpo.index("e.currentTarget))")]
    for campo in CAMPOS:
        assert campo in corpo, campo
    assert "spoken_greeting_enabled" in corpo


def test_o_console_3888_desabilita_os_subordinados_pelo_mestre():
    if not WEBCHAT.exists():
        return
    html = WEBCHAT.read_text(encoding="utf-8")
    fn = html[html.index("function conversaSincronizarMestre"):]
    fn = fn[:fn.index("\n}")]
    assert "[data-conversa-sub] input" in fn
    assert "disabled = !ligado" in fn
    # E o mestre é lido do robô, não de estado local do navegador.
    assert "c.automatic_speech_enabled !== false" in html


def test_os_dois_paineis_usam_os_mesmos_nomes_de_campo():
    """Nomes divergentes fariam um painel gravar e o outro nunca mostrar."""
    if not WEBCHAT.exists():
        return
    js = (PAINEL_LOCAL / "reachy.js").read_text(encoding="utf-8")
    html = WEBCHAT.read_text(encoding="utf-8")
    for campo in (*CAMPOS, "spoken_greeting_enabled"):
        assert campo in js and campo in html, campo
