"""As rotas que o painel 1.5.0 usa: agente de IA, robô, áudio, jeito e o teste
de escuta. O robô é falso, a ponte é um duplo, e o daemon também.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from garra_reachy_mini import armazenamento, build_info, cerebro, sistema
from garra_reachy_mini.config import Config
from garra_reachy_mini.main import GarraReachyMini
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.servicos import Servicos
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub
from garra_reachy_mini.web.seguranca import resolver_politica


class Ponte:
    """O que `web.integracoes.Ponte` faz, guardando o que foi pedido."""

    def __init__(self) -> None:
        self.pedidos: list[tuple] = []

    async def pedir(self, metodo, caminho, corpo=None, params=None, timeout_s=None):
        self.pedidos.append((metodo, caminho, corpo))
        return JSONResponse({"ok": True, "agents": [], "active": "hermes"})


class SistemaFalso:
    def __init__(self) -> None:
        self.volumes: list[dict] = []
        self.sons = 0
        self.falha = False

    def resumo(self):
        return {"daemon": {"version": "1.15.1", "ip": "10.0.0.142"},
                "audio": {"speaker_volume": 80, "microphone_volume": 100,
                          "output_device": "Reachy Mini Audio", "input_device": "Reachy Mini Audio"},
                "update": None}

    def conferir_atualizacao(self):
        if self.falha:
            raise RuntimeError("sem internet")
        return {"available": True, "current_version": "1.11.0", "available_version": "1.15.1"}

    def definir_volume(self, **kw):
        self.volumes.append(kw)
        return {"speaker_volume": kw.get("alto_falante", 80)}

    def som_de_teste(self):
        self.sons += 1


@pytest.fixture()
def painel(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    ctrl = ControladorRobo(backend, semente=7, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=5)
    ponte, sis = Ponte(), SistemaFalso()
    jeito = {"ambient_motion": True, "face_tracking": False, "speech_wobble": True}
    acordou: list[int] = []
    sondas: list[float] = []

    def sondar(prazo: float):
        sondas.append(prazo)
        return {"text": "fala garra", "duration_s": 1.2, "stt_ms": 150,
                "wake_phrase": True, "wake_match": "exact", "wake_enabled": True}

    app = FastAPI()
    ctx = ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False),
        integracoes=ponte, sistema=sis, jeito=lambda: dict(jeito),
        ajustar_jeito=lambda m: jeito.update(m) or dict(jeito),
        vozes=lambda: {"engines": [{"id": "chatterbox"}], "voices": ["clara"], "speed": True},
        sondar_escuta=sondar, acordar=lambda: acordou.append(1),
        fase=lambda: {"phase": "wake_listening", "reason": None, "last_error": None})
    montar(app, ctx)
    with TestClient(app) as c:
        c.ponte, c.sis, c.jeito, c.acordou, c.sondas, c.ctx = ponte, sis, jeito, acordou, sondas, ctx
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


# ─── agente de IA: o robô só repassa o que tem forma conhecida ───────────────
def test_listar_escolher_testar_e_reserva_vao_a_ponte(painel) -> None:
    assert painel.get("/api/robot/ai-agents").json()["active"] == "hermes"
    painel.put("/api/robot/ai-agents/active", json={"id": "pi"})
    painel.put("/api/robot/ai-agents/fallback", json={"id": None})
    painel.post("/api/robot/ai-agents/hermes/test")
    painel.post("/api/robot/ai-agents/discover")
    painel.put("/api/robot/ai-agents/hermes/options", json={"tools": "web"})
    painel.delete("/api/robot/ai-agents/lm")
    assert painel.ponte.pedidos == [
        ("GET", "/api/agentes", None),
        ("PUT", "/api/agentes/ativo", {"id": "pi"}),
        ("PUT", "/api/agentes/reserva", {"id": None}),
        ("POST", "/api/agentes/hermes/testar", {}),
        ("POST", "/api/agentes/descobrir", {}),
        ("PUT", "/api/agentes/hermes/opcoes", {"tools": "web"}),
        ("DELETE", "/api/agentes/lm", None),
    ]
    assert painel.acordou == [1], "trocar de agente reconfere o cérebro na hora"


@pytest.mark.parametrize("metodo, caminho, corpo", [
    ("put", "/api/robot/ai-agents/active", {"id": "../../etc"}),
    ("put", "/api/robot/ai-agents/active", {"id": "Hermes"}),
    ("put", "/api/robot/ai-agents/active", {}),
    ("post", "/api/robot/ai-agents/a b/test", None),
    ("put", "/api/robot/ai-agents/hermes/options", {}),
    ("put", "/api/robot/ai-agents/hermes/options", {"tools": ["web"]}),
    ("put", "/api/robot/ai-agents/hermes/options", {"tools": "x" * 500}),
])
def test_pedido_malformado_nem_chega_a_ponte(painel, metodo, caminho, corpo) -> None:
    r = getattr(painel, metodo)(caminho, json=corpo) if corpo is not None else getattr(painel, metodo)(caminho)
    assert r.status_code in (400, 404, 422)
    assert painel.ponte.pedidos == []


def test_o_painel_so_cadastra_servidor_http_e_so_os_campos_dele(painel) -> None:
    """O painel não descreve comando: `kind` é do robô, e o que sobrar é descartado."""
    r = painel.post("/api/robot/ai-agents", json={
        "id": "lm", "name": "LM Studio", "base_url": "http://10.0.0.9:1234", "model": "",
        "kind": "command", "command": ["/bin/sh", "-c", "{prompt}"], "api_key_env": "X"})
    assert r.status_code == 200
    [(_, _, corpo)] = painel.ponte.pedidos
    assert corpo == {"kind": "http", "id": "lm", "name": "LM Studio",
                     "base_url": "http://10.0.0.9:1234", "model": ""}


def test_o_build_anuncia_a_escolha_de_agente_sem_virar_administrador() -> None:
    assert build_info.CAPACIDADES["ai_agent_selection"] is True
    # O registro interno do gateway segue somente leitura (ver test_agentes.py).
    assert build_info.CAPACIDADES["agent_registry_read_only"] is True
    assert "agent_admin" not in build_info.CAPACIDADES


# ─── o robô: versões, atualização, áudio, jeito ──────────────────────────────
def test_o_sistema_junta_daemon_app_e_jeito(painel) -> None:
    d = painel.get("/api/robot/system").json()
    assert d["daemon"]["version"] == "1.15.1" and d["audio"]["speaker_volume"] == 80
    assert d["app"]["version"] and d["behavior"] == painel.jeito


def test_conferir_atualizacao_so_consulta(painel) -> None:
    d = painel.post("/api/robot/system/check-update").json()
    assert d == {"available": True, "current_version": "1.11.0", "available_version": "1.15.1"}
    painel.sis.falha = True
    r = painel.post("/api/robot/system/check-update")
    assert r.status_code == 502 and r.json()["detail"]["error"]["code"] == "update_check_failed"


@pytest.mark.parametrize("corpo", [{}, {"speaker_volume": 101}, {"speaker_volume": -1},
                                   {"speaker_volume": "alto"}, {"microphone_volume": True},
                                   {"speaker_volume": 50.5}])
def test_volume_fora_de_zero_a_cem_nao_chega_ao_robo(painel, corpo) -> None:
    assert painel.put("/api/robot/audio", json=corpo).status_code == 400
    assert painel.sis.volumes == []


def test_volume_e_ganho_vao_ao_daemon(painel) -> None:
    assert painel.put("/api/robot/audio", json={"speaker_volume": 60}).status_code == 200
    painel.put("/api/robot/audio", json={"microphone_volume": 90})
    assert painel.sis.volumes == [{"alto_falante": 60}, {"microfone": 90}]
    assert painel.post("/api/robot/audio/test-sound").json() == {"ok": True}
    assert painel.sis.sons == 1


def test_jeito_so_aceita_os_tres_interruptores(painel) -> None:
    assert painel.put("/api/robot/behavior", json={"face_tracking": True}).json()["face_tracking"] is True
    assert painel.put("/api/robot/behavior", json={"face_tracking": "sim"}).status_code == 400
    assert painel.put("/api/robot/behavior", json={"velocidade": True}).status_code == 400


def test_sem_daemon_as_rotas_dizem_que_nao_ha_sistema(painel) -> None:
    painel.ctx.sistema = None
    for metodo, caminho in (("get", "/api/robot/system"), ("post", "/api/robot/system/check-update"),
                            ("post", "/api/robot/audio/test-sound")):
        assert getattr(painel, metodo)(caminho).status_code == 503


def test_o_resumo_do_sistema_sobrevive_ao_daemon_calado(monkeypatch) -> None:
    def cai(*a, **kw):
        raise sistema.requests.ConnectionError("fora")
    monkeypatch.setattr(sistema.requests, "get", cai)
    assert sistema.Sistema("http://127.0.0.1:8000").resumo() == {
        "daemon": None, "audio": None, "update": None}


# ─── voz: opções, fase e o teste de escuta ───────────────────────────────────
def test_opcoes_de_voz_juntam_o_robo_e_o_servidor(painel) -> None:
    d = painel.get("/api/robot/voice/options").json()
    assert "pt" in d["languages"] and d["voices"] == ["clara"] and d["server"] is True
    painel.ctx.vozes = lambda: (_ for _ in ()).throw(RuntimeError("servidor antigo"))
    d = painel.get("/api/robot/voice/options").json()
    assert d["server"] is False and d["voices"] == [] and "pt" in d["languages"]


def test_o_status_traz_a_fase_da_voz(painel) -> None:
    v = painel.get("/api/robot/status").json()["voice"]
    assert v["phase"] == "wake_listening" and "last_error" in v and "tts_disponivel" in v


def test_o_teste_de_escuta_devolve_o_que_foi_ouvido(painel) -> None:
    d = painel.post("/api/robot/voice/test/listen").json()
    assert d == {"heard": True, "text": "fala garra", "duration_s": 1.2, "stt_ms": 150,
                 "wake_phrase": True, "wake_match": "exact", "wake_enabled": True}
    painel.ctx.sondar_escuta = lambda prazo: None
    assert painel.post("/api/robot/voice/test/listen").json() == {"heard": False}


def test_com_a_escuta_desligada_o_teste_nao_finge_esperar(painel) -> None:
    armazenamento.salvar_config({"conversation": {"listening_enabled": False}})
    r = painel.post("/api/robot/voice/test/listen")
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "listening_disabled"
    assert painel.sondas == [], "o microfone está liberado: não há o que esperar"


def test_segundo_teste_de_escuta_ao_mesmo_tempo_e_recusado_na_hora(painel) -> None:
    """Sem fila: cada espera ocuparia uma thread, e o robô ficaria só testando."""
    painel.ctx.sondar_escuta = lambda prazo: {"busy": True}
    r = painel.post("/api/robot/voice/test/listen")
    assert r.status_code == 409 and r.json()["detail"]["error"]["code"] == "listen_test_busy"


def test_o_teste_de_escuta_tem_teto(painel) -> None:
    """Enquanto o teste espera, a fala da sala não vira conversa: repeti-lo sem
    parar deixaria o robô surdo."""
    from garra_reachy_mini.web import api
    codigos = [painel.post("/api/robot/voice/test/listen").status_code
               for _ in range(api.TESTES_DE_ESCUTA_POR_5_MIN + 2)]
    assert codigos == [200] * api.TESTES_DE_ESCUTA_POR_5_MIN + [429, 429]
    assert len(painel.sondas) == api.TESTES_DE_ESCUTA_POR_5_MIN


def test_a_sonda_de_escuta_e_uma_por_vez_e_nao_faz_fila() -> None:
    import threading
    import time as _time
    from garra_reachy_mini.main import GarraReachyMini
    app = GarraReachyMini.__new__(GarraReachyMini)
    app._falar_texto = lambda *a, **k: None
    app._sonda_escuta, app._trava_sonda = None, threading.Lock()
    primeiro: list = []
    fio = threading.Thread(target=lambda: primeiro.append(app._sondar_escuta(5.0)), daemon=True)
    fio.start()
    fim = _time.monotonic() + 3
    while app._sonda_escuta is None and _time.monotonic() < fim:
        _time.sleep(0.01)
    t0 = _time.monotonic()
    assert app._sondar_escuta(5.0) == {"busy": True}
    assert _time.monotonic() - t0 < 0.5, "a segunda sonda não pode esperar a primeira"
    app._sonda_escuta["resultado"] = {"text": "oi"}
    app._sonda_escuta["evento"].set()
    fio.join(3)
    assert primeiro == [{"text": "oi"}] and app._sonda_escuta is None
    assert app._sondar_escuta(0.05) is None       # liberada: a próxima arma de novo


def test_sem_laco_de_voz_o_teste_de_escuta_diz_isso(painel) -> None:
    painel.ctx.sondar_escuta = None
    assert painel.post("/api/robot/voice/test/listen").status_code == 503


# ─── computador que responde, agente que não ─────────────────────────────────
def _app(monkeypatch, tmp_path, agente):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    app = GarraReachyMini.__new__(GarraReachyMini)
    app.logger = logging.getLogger("teste")
    app.servicos = Servicos()
    monkeypatch.setattr("garra_reachy_mini.main.cerebro_sondar",
                        lambda cfg: (False, "none", "no brain configured"))
    monkeypatch.setattr(cerebro, "agente_ativo", lambda cfg, timeout=2.0: agente)
    app._sondar_cerebro(Config.carregar())
    return {s["name"]: s for s in app.servicos.json()["services"]}


def test_agente_caido_nao_aparece_como_computador_inacessivel(monkeypatch, tmp_path) -> None:
    """Consertos opostos: um manda olhar rede e firewall; o outro, o agente."""
    s = _app(monkeypatch, tmp_path, {"id": "hermes", "name": "Hermes", "available": False})
    assert s["gateway"]["available"] is True
    assert s["brain"]["available"] is False
    assert s["brain"]["reason_code"] == "agent_unavailable" and "Hermes" in s["brain"]["detail"]


def test_computador_inacessivel_continua_sendo_dito_assim(monkeypatch, tmp_path) -> None:
    s = _app(monkeypatch, tmp_path, None)
    assert s["gateway"]["available"] is False and s["gateway"]["reason_code"] == "unreachable"
    assert s["brain"]["reason_code"] == "not_configured"
