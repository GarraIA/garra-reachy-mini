"""Recomeçar a conversa tem de ser possível de fora.

A retomada de sessão entre arranques é o comportamento certo no uso normal — o
robô não devia esquecer tudo quando o app reinicia. Mas ela não tinha escape, e
isso apareceu num teste: um turno em que o modelo tinha olhado pela câmera e
descrito o chão ficou no histórico, e todo turno seguinte herdava esse contexto.

`DELETE /api/sessions/{id}` do gateway não resolve — é logout (revoga tokens e
desconecta), e o histórico continua respondendo 200. Então o escape tem de vir
do lado do app: uma sessão nova, para onde ele passa a escrever.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini import build_info, cerebro as mod_cerebro
from garra_reachy_mini.web.seguranca import resolver_politica


class GatewayFalso:
    """Só o que `nova_sessao` toca: criar sessão e falhar quando mandado."""

    def __init__(self, *, aceita: bool = True) -> None:
        self.aceita = aceita
        self.criadas: list[str] = []
        self.headers: dict[str, str] = {}

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/api/sessions")
        self.criadas.append(json.get("agent_id", ""))
        return _Resposta(201 if self.aceita else 503,
                         {"session_id": f"nova-{len(self.criadas)}"})


class _Resposta:
    def __init__(self, status, corpo):
        self.status_code = status
        self._corpo = corpo
        self.text = str(corpo)

    def json(self):
        return self._corpo


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    gravado: dict = {}
    monkeypatch.setattr(mod_cerebro.armazenamento, "salvar_estado",
                        lambda d: gravado.update(d))
    monkeypatch.setattr(mod_cerebro.armazenamento, "carregar_estado",
                        lambda: dict(gravado))
    c = mod_cerebro.GatewayBrain.__new__(mod_cerebro.GatewayBrain)
    import logging
    import threading
    c.log = logging.getLogger("teste")
    c._trava = threading.RLock()
    c.session_id = "antiga-contaminada"
    c.cursor = c.cursor_falado = 7
    c._ultima_resposta = "piso de madeira"
    c.http = GatewayFalso()
    c.cfg = type("Cfg", (), {"agent_id": "reachy_voice", "gateway_url": ""})()
    c._url = lambda caminho: caminho
    c._estado = gravado
    return c


def test_troca_a_sessao_e_zera_os_cursores(gateway):
    nova = gateway.nova_sessao()
    assert nova == "nova-1"
    assert gateway.session_id == "nova-1"
    # Cursores herdados apontariam para o histórico da sessão antiga.
    assert gateway.cursor == gateway.cursor_falado == 0
    assert gateway._ultima_resposta is None


def test_a_sessao_nova_usa_o_agente_configurado(gateway):
    gateway.nova_sessao()
    assert gateway.http.criadas == ["reachy_voice"]


def test_persiste_para_o_proximo_arranque(gateway):
    gateway.nova_sessao()
    assert gateway._estado["session_id"] == "nova-1"
    assert gateway._estado["cursor_falado"] == 0


def test_gateway_fora_do_ar_nao_deixa_o_app_sem_sessao(gateway):
    """Falhar em criar não pode ser pior que não ter tentado: a antiga volta."""
    gateway.http.aceita = False
    assert gateway.nova_sessao() is None
    assert gateway.session_id == "antiga-contaminada"
    assert gateway._estado["session_id"] == "antiga-contaminada"


# ── a rota ───────────────────────────────────────────────────────────────────
def _montar(nova_sessao):
    from garra_reachy_mini.web.api import ContextoWeb, montar
    app = FastAPI()
    montar(app, ContextoWeb(controlador=None, hub=None, eventos=None,
                            politica=resolver_politica(ambiente={}, escolher_porta=False,
                                                       no_robo=False),
                            nova_sessao=nova_sessao))
    return app


def test_a_rota_devolve_o_id_novo():
    async def nova():
        return "abc-123"
    with TestClient(_montar(nova)) as c:
        r = c.post("/api/robot/conversation/session")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "session_id": "abc-123"}


def test_sem_cerebro_pronto_da_503_e_nao_500():
    with TestClient(_montar(None)) as c:
        assert c.post("/api/robot/conversation/session").status_code == 503


def test_gateway_recusando_da_502():
    async def nova():
        return None
    with TestClient(_montar(nova)) as c:
        assert c.post("/api/robot/conversation/session").status_code == 502


def test_a_capacidade_e_anunciada():
    """O painel decide por capacidade, não por versão — ver build_info."""
    assert build_info.CAPACIDADES["brain_session_reset"] is True
