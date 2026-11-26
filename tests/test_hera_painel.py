"""O cartão "Hera — conversa ao vivo" no painel do robô (fase 6).

Três camadas, como as outras integrações:

  PC     `GET /api/hera/estado` na ponte (token do robô): liberação, nível,
         tetos e consumo do livro. Números e motivos, nunca o que foi dito.
  robô   `GET /api/robot/hera/status`: os ajustes daqui, o pareamento, a
         última conversa vista aqui e o estado do PC, num formato só.
  painel o cartão, a pílula e os textos nos dois idiomas.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from companion.hera import credencial, estado, livro, registro

T0 = time.mktime((2026, 11, 25, 15, 0, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def pasta(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_CONF", str(tmp_path / "conf"))
    monkeypatch.setenv("GARRA_REACHY_DADOS", str(tmp_path / "dados"))
    monkeypatch.delenv("GARRA_HERA", raising=False)
    return tmp_path


def _conversa(lv, inicio, segundos, motivo, delegacoes=0):
    sid = lv.abrir(dispositivo="garra-sala", nivel="completo", como="alone", agora=inicio)
    lv.fechar(sid, segundos=segundos, motivo=motivo, delegacoes=delegacoes, agora=inicio + segundos)


# ─── PC: o resumo ────────────────────────────────────────────────────────────
def test_desligada_e_sem_par_o_resumo_diz_isso_e_nao_abre():
    r = estado.resumo(agora=T0)
    assert r["enabled"] is False and r["paired"] is False
    assert r["can_open"] is False and r["blocked_by"] == "disabled"
    assert r["usage"] == {"today_s": 0, "month_s": 0, "sessions_today": 0}
    assert r["last"] is None and r["live_now"] is False


def test_liberada_o_resumo_tem_nivel_tetos_consumo_e_a_ultima():
    registro.Registro().mudar({"enabled": True}, local=True)
    credencial.segredo()
    lv = livro.Livro()
    _conversa(lv, T0 - 86400, 300, "idle")           # ontem: conta no mês, não no dia
    _conversa(lv, T0 - 3600, 125.5, "farewell", 2)
    _conversa(lv, T0 - 600, 60, "stop")
    r = estado.resumo(agora=T0)
    assert r["enabled"] is True and r["paired"] is True
    assert (r["level"], r["voice"], r["model"], r["device"]) == ("completo", "bossa", "gpt-live-1", "garra-sala")
    assert r["limits"] == {"session_max_s": 600, "daily_s": 1800, "monthly_s": 18000, "opens_per_hour": 12}
    assert r["usage"] == {"today_s": 186, "month_s": 486, "sessions_today": 2}
    assert r["price_per_min_usd"] == estado.PRECO_MINUTO_USD == 0.05
    assert r["last"] == {"reason": "stop", "seconds": 60.0, "ended_at": T0 - 540}
    assert r["can_open"] is True and r["blocked_by"] is None and r["live_now"] is False


def test_teto_do_dia_aparece_como_motivo():
    registro.Registro().mudar({"enabled": True}, local=True)
    _conversa(livro.Livro(), T0 - 2000, 1800, "max")
    r = estado.resumo(agora=T0)
    assert r["can_open"] is False and r["blocked_by"] == "daily_limit"


def test_conversa_no_ar_aparece_como_ao_vivo():
    registro.Registro().mudar({"enabled": True}, local=True)
    livro.Livro().abrir(dispositivo="garra-sala", nivel="completo", como="alone", agora=T0 - 30)
    r = estado.resumo(agora=T0)
    assert r["live_now"] is True and r["blocked_by"] == "busy"


def test_o_interruptor_mestre_do_ambiente_vale_no_resumo(monkeypatch):
    registro.Registro().mudar({"enabled": True}, local=True)
    monkeypatch.setenv("GARRA_HERA", "0")
    assert estado.resumo(agora=T0)["enabled"] is False


def test_o_resumo_nao_leva_texto_nem_segredo():
    registro.Registro().mudar({"enabled": True, "persona": "Você é a Hera da família Silva."}, local=True)
    segredo = credencial.segredo()
    r = json.dumps(estado.resumo(agora=T0))
    assert segredo not in r and "Silva" not in r and "persona" not in r


# ─── PC: a rota da ponte ─────────────────────────────────────────────────────
def test_a_ponte_so_responde_com_o_token_do_robo():
    from companion import ponte
    from test_ponte_visao import TOKEN

    cliente = TestClient(ponte.montar(TOKEN, "chave-gw"))
    assert cliente.get("/api/hera/estado").status_code == 401
    assert cliente.get("/api/hera/estado", headers={"Authorization": "Bearer outro"}).status_code == 401
    r = cliente.get("/api/hera/estado", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["enabled"] is False and "usage" in r.json()


# ─── robô: a rota do painel ──────────────────────────────────────────────────
import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from garra_reachy_mini import armazenamento  # noqa: E402
from garra_reachy_mini.acompanhamento import Acompanhamento  # noqa: E402
from garra_reachy_mini.robo.acoes import ControladorRobo  # noqa: E402
from garra_reachy_mini.web import integracoes  # noqa: E402
from garra_reachy_mini.web.api import ContextoWeb, montar as montar_api  # noqa: E402
from garra_reachy_mini.web.camera import FrameHub  # noqa: E402
from garra_reachy_mini.web.seguranca import resolver_politica  # noqa: E402

DO_PC = {"enabled": True, "paired": True, "device": "garra-sala", "level": "completo", "voice": "bossa",
         "model": "gpt-live-1", "limits": {"session_max_s": 600, "daily_s": 1800, "monthly_s": 18000,
                                           "opens_per_hour": 12},
         "usage": {"today_s": 252, "month_s": 900, "sessions_today": 3}, "price_per_min_usd": 0.05,
         "can_open": True, "blocked_by": None, "live_now": False,
         "last": {"reason": "farewell", "seconds": 21, "ended_at": 1764120000.0}}


class PcDoPainel:
    def __init__(self):
        self.pedidos: list[tuple] = []
        self.fora = False
        self.antigo = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append((request.method, request.url.path, request.headers.get("authorization")))
        if self.fora:
            raise httpx.ConnectError("recusado")
        if request.url.path == "/api/hera/estado" and not self.antigo:
            return httpx.Response(200, json=DO_PC)
        return httpx.Response(404, json={"erro": "not_found"})


@pytest.fixture()
def painel(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    pc = PcDoPainel()
    ponte = integracoes.Ponte("http://10.0.0.24:8126", "tok-robo", transport=httpx.MockTransport(pc.handler))
    app = FastAPI()
    montar_api(app, ContextoWeb(controlador=ctrl, hub=hub, eventos=ctrl.eventos,
                                acompanhamento=Acompanhamento(ctrl.eventos), integracoes=ponte,
                                politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        c.pc, c.ctrl = pc, ctrl
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def _parear():
    armazenamento.salvar_config({**armazenamento.carregar_config(), "gateway_url": "http://10.0.0.24:8126",
                                 "hera_token": "segredo-do-par-0123456789", "hera_device": "garra-sala"})


def test_status_junta_os_ajustes_daqui_o_pareamento_e_o_pc(painel):
    painel.put("/api/robot/conversation", json={"live_voice_enabled": True, "live_motion": "speaking",
                                                 "live_wake_strictness": "greeting"})
    r = painel.get("/api/robot/hera/status")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["settings"]["live_voice_enabled"] is True
    assert corpo["settings"]["live_motion"] == "speaking"
    assert corpo["settings"]["live_wake_strictness"] == "greeting"
    assert corpo["settings"]["live_wake_phrase_text"] == "Hera"
    assert corpo["paired"] is False
    assert corpo["pc"] == DO_PC and corpo["pc_error"] is None
    assert painel.pc.pedidos[-1] == ("GET", "/api/hera/estado", "Bearer tok-robo")
    _parear()
    corpo = painel.get("/api/robot/hera/status").json()
    assert corpo["paired"] is True
    assert "segredo-do-par" not in json.dumps(corpo)          # o segredo nunca sai


def test_pc_fora_do_ar_vira_um_codigo_e_o_resto_segue(painel):
    painel.pc.fora = True
    corpo = painel.get("/api/robot/hera/status").json()
    assert corpo["pc"] is None and corpo["pc_error"] == "bridge_unreachable"
    assert corpo["settings"]["live_voice_enabled"] is False


def test_a_ultima_conversa_vista_aqui_vem_dos_eventos(painel):
    assert painel.get("/api/robot/hera/status").json()["last"] is None
    painel.ctrl.eventos.publicar("voice.live.unavailable", destination="hera", reason="daily_limit", match="alone")
    ultima = painel.get("/api/robot/hera/status").json()["last"]
    assert (ultima["kind"], ultima["reason"]) == ("unavailable", "daily_limit")
    painel.ctrl.eventos.publicar("voice.live.ended", destination="hera", reason="farewell", seconds=21)
    ultima = painel.get("/api/robot/hera/status").json()["last"]
    assert (ultima["kind"], ultima["reason"], ultima["seconds"]) == ("ended", "farewell", 21)
    assert ultima["at"]


def _quase(painel, match="leading", rigor="strict"):
    painel.ctrl.eventos.publicar("voice.live.near_miss", destination="hera", match=match,
                                 strictness=rigor, missing="greeting")


def test_o_quase_chamado_vem_dos_eventos_e_conta_desde_o_ultimo_chamado(painel):
    assert painel.get("/api/robot/hera/status").json()["near_miss"] is None
    _quase(painel)
    _quase(painel)
    quase = painel.get("/api/robot/hera/status").json()["near_miss"]
    assert (quase["match"], quase["strictness"], quase["since_last_call"]) == ("leading", "strict", 2)
    assert quase["at"]
    # Chamou de verdade: o último quase continua à vista, mas não conta mais.
    painel.ctrl.eventos.publicar("voice.live.connecting", destination="hera", match="alone")
    quase = painel.get("/api/robot/hera/status").json()["near_miss"]
    assert (quase["match"], quase["since_last_call"]) == ("leading", 0)
    _quase(painel, match="alone", rigor="greeting")
    quase = painel.get("/api/robot/hera/status").json()["near_miss"]
    assert (quase["match"], quase["strictness"], quase["since_last_call"]) == ("alone", "greeting", 1)
    # Recusado pelo computador também foi chamado: a frase estava certa.
    painel.ctrl.eventos.publicar("voice.live.unavailable", destination="hera", reason="daily_limit", match="alone")
    assert painel.get("/api/robot/hera/status").json()["near_miss"]["since_last_call"] == 0
    # …e a última conversa segue sendo só encerrada ou recusada.
    assert painel.get("/api/robot/hera/status").json()["last"]["kind"] == "unavailable"


def test_computador_antigo_sem_a_rota_pede_atualizacao(painel):
    painel.pc.antigo = True
    corpo = painel.get("/api/robot/hera/status").json()
    assert corpo["pc"] is None and corpo["pc_error"] == "pc_outdated"


# ─── painel: o cartão ────────────────────────────────────────────────────────
import re  # noqa: E402
from pathlib import Path  # noqa: E402

_ESTATICO = Path(__file__).resolve().parent.parent / "garra_reachy_mini" / "static"
HTML = (_ESTATICO / "reachy.html").read_text(encoding="utf-8")
JS = (_ESTATICO / "reachy.js").read_text(encoding="utf-8")
I18N = (_ESTATICO / "i18n.js").read_text(encoding="utf-8")


def _bloco() -> str:
    return HTML[HTML.index('id="hera-bloco"'):HTML.index('id="visao-bloco"')]


def test_o_cartao_fica_em_ajustes_depois_da_conversa_com_todos_os_controles():
    ajustes = HTML[HTML.index('id="tela-ajustes"'):]
    assert ajustes.index('id="conversa-bloco"') < ajustes.index('id="hera-bloco"') < ajustes.index('id="visao-bloco"')
    bloco = _bloco()
    for i in ("p-hera", "hera-ligada", "hera-aviso", "hera-frase", "hera-outras", "hera-rigor",
              "hera-movimento", "btn-hera-salvar", "btn-hera-atualizar", "hera-estado", "hera-consumo"):
        assert f'id="{i}"' in bloco, i
    for valor in ("strict", "greeting", "normal", "still", "speaking", "expressive"):
        assert f'<option value="{valor}"' in bloco, valor
    assert 'data-ir="hera-bloco"' in HTML                      # atalho no topo de Ajustes


def test_o_aviso_de_privacidade_fica_sempre_a_vista():
    bloco = _bloco()
    aviso = re.search(r'<div[^>]*id="hera-aviso"[^>]*>', bloco).group(0)
    assert "hidden" not in aviso and "privacidade" in aviso


def test_o_cartao_grava_so_as_chaves_da_hera_pelo_caminho_da_conversa():
    corpo = JS[JS.index("function mudancasDaHera"):JS.index("function mudancasDaHera") + 900]
    for chave in ("live_voice_enabled", "live_wake_phrase_text", "live_wake_phrase_alternatives",
                  "live_wake_strictness", "live_motion"):
        assert chave in corpo, chave
    assert "live_idle_close_s" not in JS                         # não está ligado a nada
    assert "salvarConversa(mudancasDaHera())" in JS
    assert "'/api/robot/hera/status'" in JS


def test_a_pilula_acompanha_a_fase_e_os_eventos_da_conversa_ao_vivo():
    fase = JS[JS.index("function pintarFase"):JS.index("function linhasDe")]
    assert "pintarPilulaDaHera()" in fase
    for evento in ("voice.live.started", "voice.live.ended", "voice.live.unavailable", "voice.live.near_miss"):
        assert f"case '{evento}':" in JS, evento


def test_o_cartao_mostra_o_quase_chamado_com_a_dica_da_forma():
    pintar = JS[JS.index("function pintarHeraEstado"):JS.index("async function carregarHera")]
    assert "d.near_miss" in pintar
    assert "hera.quase_dica_" in pintar                    # a dica muda com a forma ouvida
    # O evento também vai para o registro do painel, na hora.
    caso = JS[JS.index("case 'voice.live.near_miss':"):]
    caso = caso[:caso.index("break;")]
    assert "registrarLog(" in caso and "carregarHera()" in caso


def test_o_consumo_e_estimativa_e_o_teto_e_do_computador():
    for idioma in ("en", "pt"):
        trecho = I18N[I18N.index(f"  {idioma}: {{"):]
        assert re.search(r"'hera\.consumo_hoje': '[^']*\{custo\}", trecho)
        assert re.search(r"'hera\.estimado': '[^']+'", trecho)


@pytest.mark.parametrize("prefixo,codigos", [
    ("hera.bloqueio_", ["disabled", "daily_limit", "monthly_limit", "too_many_opens", "suspended", "busy"]),
    ("hera.erro_", ["bridge_unreachable", "bridge_timeout", "bridge_not_configured", "bridge_token_rejected",
                    "bridge_invalid_response", "bridge_error", "pc_outdated"]),
    ("hera.motivo_", ["idle", "no_speech", "max", "stop", "handoff_garra", "farewell", "robot_stop", "pc_lost",
                      "connection_lost", "robot_lost", "listening_stopped", "not_paired", "daily_limit",
                      "disabled", "busy", "unreachable", "speech_output_disabled", "unavailable"]),
    ("hera.movimento_", ["still", "speaking", "expressive"]),
    ("hera.rigor_", ["strict", "greeting", "normal"]),
    ("hera.quase", ["", "_nenhum", "_recente", "_antigo", "_dica_leading", "_dica_alone", "_log"]),
])
def test_os_codigos_tem_texto_nos_dois_idiomas(prefixo, codigos):
    for idioma in ("en", "pt"):
        trecho = I18N[I18N.index(f"  {idioma}: {{"):]
        trecho = trecho[:trecho.index("\n  },")] if "\n  }," in trecho else trecho
        faltam = [c for c in codigos if f"'{prefixo}{c}':" not in trecho]
        assert faltam == [], (idioma, faltam)
