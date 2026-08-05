"""Registry de agentes (Fase 1): leitura factual, e nada além de leitura.

Duas superfícies, testadas com duplos:

  * a rota `GET /api/agents` da PONTE (companion, :8126) — token obrigatório,
    upstream fixo no gateway local, resposta filtrada por allowlist de campos,
    erros que diferenciam gateway fora / rota inexistente / resposta inválida;
  * a rota `GET /api/robot/agents` do APP — repassa pela ponte autenticada,
    nunca inventa estado, nunca guarda cache, `Cache-Control: no-store`.

Nenhum teste toca gateway ou companion reais.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from companion import ponte

TOKEN = "token-de-teste-nao-e-segredo"

DESCRITOR_GARRA = {
    "id": "reachy_voice",
    "kind": "native_gateway_agent",
    "display_name": "Garra",
    "enabled_for_routing": True,
    "has_config": True,
    "backend": "gateway_runtime",
    "adapter_integrated": True,
    "model": {
        "global_default_model": "openrouter/auto",
        "model_mode": "override",
        "configured_model": "anthropic/claude-sonnet-5",
        "effective_requested_model": "anthropic/claude-sonnet-5",
        "transport_provider": "anthropic",
        "resolved_model": None,
        "resolved_model_status": "unavailable",
        # Campo NÃO listado na allowlist — não pode atravessar.
        "internal_debug": "não-deveria-sair",
    },
    "allowed_tools_count": 2,
    "api_tagged_sessions": 0,
    # Campos NÃO listados — não podem atravessar.
    "system_prompt": "SECRET-PROMPT",
    "operator_name": "SECRET-OPERATOR",
}


class GatewayFalso:
    """Duplo do `httpx.AsyncClient` da ponte, com resposta configurável."""

    def __init__(self, status: int = 200, corpo: object = None,
                 erro: bool = False) -> None:
        self.status = status
        self.corpo = corpo if corpo is not None else {
            "agents": [DESCRITOR_GARRA]}
        self.erro = erro
        self.chamadas: list[tuple[str, str, dict]] = []

    async def get(self, caminho, **kw):
        self.chamadas.append(("GET", caminho, kw))
        if self.erro:
            raise httpx.ConnectError("recusada")
        if isinstance(self.corpo, (bytes, str)):
            return httpx.Response(self.status, content=self.corpo)
        return httpx.Response(self.status, json=self.corpo)

    async def request(self, metodo, caminho, **kw):
        self.chamadas.append((metodo, caminho, kw))
        return httpx.Response(200, json={})


@pytest.fixture()
def ponte_app(monkeypatch):
    def montar(gateway: GatewayFalso | None = None):
        gw = gateway or GatewayFalso()
        app = ponte.montar(TOKEN, "chave-gw")
        monkeypatch.setattr(ponte, "_cliente", gw)
        ponte._ping_janela.clear()
        ponte._agents_janela.clear()
        return TestClient(app), gw
    yield montar
    ponte._ping_janela.clear()
    ponte._agents_janela.clear()


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ── ponte: autenticação e método ───────────────────────────────────────────
def test_ponte_agents_exige_token(ponte_app):
    c, _ = ponte_app()
    assert c.get("/api/agents").status_code == 401
    assert c.get("/api/agents",
                 headers={"Authorization": "Bearer errado"}).status_code == 401


def test_ponte_agents_rejeita_escrita(ponte_app):
    """POST cai no catch-all, que não tem /api/agents na allowlist."""
    c, _ = ponte_app()
    r = c.post("/api/agents", headers=_auth(), json={})
    assert r.status_code == 404


def test_ponte_agents_upstream_fixo(ponte_app):
    """O caminho consultado é sempre o /api/agents do gateway local — nada do
    request do cliente (query, header) muda o upstream."""
    c, gw = ponte_app()
    r = c.get("/api/agents?url=http://evil/x", headers=_auth())
    assert r.status_code == 200
    assert gw.chamadas[-1][1] == "/api/agents"


# ── ponte: redação por allowlist ───────────────────────────────────────────
def test_ponte_agents_filtra_campos_desconhecidos(ponte_app):
    c, _ = ponte_app()
    r = c.get("/api/agents", headers=_auth())
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    corpo = r.json()
    assert corpo["ok"] is True
    (agente,) = corpo["agents"]
    assert agente["id"] == "reachy_voice"
    assert agente["model"]["configured_model"] == "anthropic/claude-sonnet-5"
    texto = json.dumps(corpo)
    assert "SECRET-PROMPT" not in texto
    assert "SECRET-OPERATOR" not in texto
    assert "internal_debug" not in texto


def test_ponte_agents_nao_repassa_authorization_do_cliente(ponte_app):
    """O gateway recebe o token LOCAL da ponte, nunca o Bearer do robô."""
    c, gw = ponte_app()
    c.get("/api/agents", headers=_auth())
    upstream = gw.chamadas[-1][2]["headers"]
    assert upstream.get("Authorization") == "Bearer chave-gw"


# ── ponte: erros diferenciados ─────────────────────────────────────────────
def test_ponte_agents_gateway_fora(ponte_app):
    c, _ = ponte_app(GatewayFalso(erro=True))
    r = c.get("/api/agents", headers=_auth())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "gateway_unreachable"


def test_ponte_agents_gateway_sem_a_rota(ponte_app):
    """Gateway de produção (sem /api/agents) → unsupported, não unreachable."""
    c, _ = ponte_app(GatewayFalso(status=404, corpo={}))
    r = c.get("/api/agents", headers=_auth())
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "agent_registry_unsupported"


def test_ponte_agents_resposta_invalida(ponte_app):
    c, _ = ponte_app(GatewayFalso(corpo=b"isto nao e json"))
    r = c.get("/api/agents", headers=_auth())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "invalid_gateway_response"


def test_ponte_agents_formato_inesperado(ponte_app):
    c, _ = ponte_app(GatewayFalso(corpo={"agents": "não é lista"}))
    r = c.get("/api/agents", headers=_auth())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "invalid_gateway_response"


def test_ponte_agents_rate_limit(ponte_app):
    c, _ = ponte_app()
    for _ in range(ponte.AGENTS_MAX_POR_MINUTO):
        assert c.get("/api/agents", headers=_auth()).status_code == 200
    assert c.get("/api/agents", headers=_auth()).status_code == 429


# ── app do robô: GET /api/robot/agents ─────────────────────────────────────
class _RespostaFalsa:
    def __init__(self, status: int, corpo: object, *, json_invalido=False):
        self.status_code = status
        self._corpo = corpo
        self._json_invalido = json_invalido
        self.ok = 200 <= status < 300
        self.content = b"x"

    def json(self):
        if self._json_invalido:
            raise ValueError("não é json")
        return self._corpo


@pytest.fixture()
def app_cliente(backend, tmp_path, monkeypatch):
    """Painel com config isolado apontando para uma ponte falsa."""
    from garra_reachy_mini import armazenamento
    from garra_reachy_mini.robo.acoes import ControladorRobo
    from garra_reachy_mini.web.api import ContextoWeb, montar
    from garra_reachy_mini.web.camera import FrameHub
    from garra_reachy_mini.web.seguranca import resolver_politica

    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.setattr(armazenamento, "carregar_config",
                        lambda: {"gateway_url": "http://192.0.2.24:8126",
                                 "gateway_key": "chave-nao-e-segredo"})
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos,
        politica=resolver_politica(ambiente={}, escolher_porta=False,
                                   no_robo=False)))
    with TestClient(app) as c:
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def _instalar_resposta(monkeypatch, resposta=None, excecao=None):
    import requests

    def get(url, **kw):
        if excecao is not None:
            raise excecao
        assert url.endswith("/api/agents")
        assert "params" not in kw or not kw["params"]
        return resposta

    monkeypatch.setattr(requests, "get", get)


def test_app_agents_repassa_registry(app_cliente, monkeypatch):
    _instalar_resposta(monkeypatch, _RespostaFalsa(
        200, {"ok": True, "agents": [{"id": "reachy_voice"}]}))
    r = app_cliente.get("/api/robot/agents")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["agents"][0]["id"] == "reachy_voice"


def test_app_agents_companion_fora(app_cliente, monkeypatch):
    import requests

    _instalar_resposta(monkeypatch, excecao=requests.ConnectionError("fora"))
    r = app_cliente.get("/api/robot/agents")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "companion_unreachable"


def test_app_agents_gateway_fora(app_cliente, monkeypatch):
    """A ponte respondeu 502 (o gateway atrás dela caiu): repassado como tal."""
    _instalar_resposta(monkeypatch, _RespostaFalsa(
        502, {"ok": False, "error": {"code": "gateway_unreachable"}}))
    r = app_cliente.get("/api/robot/agents")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "gateway_unreachable"


def test_app_agents_sem_suporte(app_cliente, monkeypatch):
    _instalar_resposta(monkeypatch, _RespostaFalsa(
        501, {"ok": False, "error": {"code": "agent_registry_unsupported"}}))
    r = app_cliente.get("/api/robot/agents")
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "agent_registry_unsupported"


def test_app_agents_credencial_recusada(app_cliente, monkeypatch):
    _instalar_resposta(monkeypatch, _RespostaFalsa(401, {"erro": "token"}))
    r = app_cliente.get("/api/robot/agents")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "companion_unauthorized"


def test_app_agents_json_invalido(app_cliente, monkeypatch):
    _instalar_resposta(monkeypatch, _RespostaFalsa(200, None, json_invalido=True))
    r = app_cliente.get("/api/robot/agents")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "invalid_gateway_response"


def test_app_agents_nao_vaza_credencial(app_cliente, monkeypatch):
    """Nenhuma variação de erro devolve a chave usada com a ponte."""
    import requests

    for resposta, excecao in [
        (_RespostaFalsa(200, {"ok": True, "agents": []}), None),
        (None, requests.ConnectionError("fora")),
        (_RespostaFalsa(401, {"erro": "x"}), None),
    ]:
        _instalar_resposta(monkeypatch, resposta, excecao)
        r = app_cliente.get("/api/robot/agents")
        assert "chave-nao-e-segredo" not in r.text


def test_app_agents_so_get(app_cliente):
    for metodo in ("post", "put", "delete"):
        r = getattr(app_cliente, metodo)("/api/robot/agents")
        assert r.status_code in (403, 405), metodo


def test_capacidade_anunciada():
    from garra_reachy_mini import build_info

    assert build_info.CAPACIDADES["agent_registry_read_only"] is True
    for proibida in ("agent_admin", "agent_messages",
                     "agent_enable_disable", "agent_prompt_editing"):
        assert proibida not in build_info.CAPACIDADES


# ── o painel (DOM estático) ────────────────────────────────────────────────
def test_html_tem_secao_e_testids():
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini"
    html = (raiz / "static" / "reachy.html").read_text(encoding="utf-8")
    assert 'data-testid="reachy-agents-section"' in html
    assert 'data-testid="reachy-agents-unavailable"' in html
    # Sem botões administrativos na seção de agentes.
    secao = html.split('data-testid="reachy-agents-section"')[1]
    secao = secao.split("</section>")[0]
    for proibido in ("salvar", "save", "enable", "toggle", "checkbox",
                     "textarea", "cancel"):
        assert proibido not in secao.lower(), proibido


def test_js_renderiza_da_api_e_sem_storage():
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini"
    js = (raiz / "static" / "reachy.js").read_text(encoding="utf-8")
    # Cards vêm da API; test IDs por agente são interpolados, não hardcoded.
    assert "api('/api/robot/agents')" in js
    assert "reachy-agent-card-${esc(a.id)}" in js
    # A seção de agentes não guarda nada. Procura USO (acesso com ponto),
    # não a palavra — o comentário do bloco menciona localStorage de
    # propósito, para dizer que ele não entra.
    bloco = js.split("agentes (registry factual")[1].split("ritmo da conversa")[0]
    assert "localStorage." not in bloco
    assert "sessionStorage." not in bloco
    assert "indexedDB." not in bloco.lower()
    # Offline limpa os cards.
    assert "innerHTML = ''" in bloco


def test_o_portao_le_a_capability_do_status_e_nao_do_catalogo_de_acoes():
    """A causa raiz de "os agentes não aparecem", travada.

    São duas coisas com o mesmo nome: `/api/robot/capabilities` é o catálogo de
    AÇÕES do robô e é o que `estado.capacidades` guarda; as flags do build vêm
    no bloco `capabilities` do `/api/robot/status`, em `estado.status`.

    O portão perguntava ao objeto errado, a condição era verdadeira sempre, e a
    seção se escondia sem nunca chamar `/api/robot/agents` — rota no ar, dado
    disponível, tela vazia e nenhum erro para explicar.
    """
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini"
    js = (raiz / "static" / "reachy.js").read_text(encoding="utf-8")
    bloco = js.split("function temRegistroDeAgentes")[1].split("function carregarAgentes")[0]

    assert "estado.status" in bloco, "a capability de build vem do status"
    assert "estado.capacidades" not in bloco, (
        "estado.capacidades é o catálogo de ações; não carrega flags de build"
    )
    # Não sei ainda ≠ não tem. Esconder no primeiro caso e nunca voltar é a
    # forma do defeito anterior.
    assert "return null" in bloco


def test_carregando_e_um_estado_visivel():
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini"
    js = (raiz / "static" / "reachy.js").read_text(encoding="utf-8")
    assert "function agentesCarregando" in js
    assert "agentes.carregando" in js
    # A falha precisa dizer o que houve, e não só apagar a tela.
    indisp = js.split("function agentesIndisponiveis")[1].split("\n}")[0]
    assert "textContent = mensagem" in indisp


def test_html_aponta_onde_administrar_sem_virar_dono():
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini"
    html = (raiz / "static" / "reachy.html").read_text(encoding="utf-8")
    secao = html.split('data-testid="reachy-agents-section"')[1].split("</section>")[0]
    assert 'data-testid="reachy-agents-admin-link"' in secao
    assert "3888" in secao, "o link leva ao painel do gateway"
    assert "agentes.administrar_em" in secao
    # Continua somente leitura: o link é uma indicação, não um controle.
    assert "<form" not in secao.lower()
    assert "<input" not in secao.lower()


def test_i18n_tem_as_chaves_nos_dois_idiomas():
    import pathlib
    import re

    raiz = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini"
    js = (raiz / "static" / "i18n.js").read_text(encoding="utf-8")
    chaves = re.findall(r"'(agentes\.[a-z_]+)'", js)
    assert chaves, "chaves agentes.* presentes"
    # Cada chave aparece duas vezes: uma no bloco en, outra no pt.
    for chave in set(chaves):
        assert chaves.count(chave) == 2, chave
