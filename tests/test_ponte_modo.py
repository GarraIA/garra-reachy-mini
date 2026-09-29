"""A ponte contra o gateway público (0.4.x): o modo restrito é a trava.

No build privado de agosto a trava era o `agent_id` forçado — o agente
`reachy_voice` tinha lista própria de ferramentas. O gateway público **não
aplica** a lista de ferramentas do agente no caminho REST; quem restringe é o
*modo* da sessão. Medido em 2026-09-29 contra o 0.4.7:

  * sessão sem modo: 12 ferramentas, inclusive escrita em arquivo;
  * sessão no modo `reachy-voz`: 1 (`web_fetch`);
  * `"/mode code"` como conteúdo de mensagem tira a sessão do modo restrito.

Cada teste aqui fecha um desses caminhos.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from companion import modo, ponte

TOKEN = "token-de-teste-nao-e-segredo"
SESSAO = "123e4567-e89b-12d3-a456-426614174000"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Gateway:
    """Duplo do gateway: grava o que recebeu e responde o que o teste mandar."""

    def __init__(self, modo_da_sessao=modo.NOME, modo_ao_criar=modo.NOME,
                 status_criar=201, erro_no_modo=False) -> None:
        self.modo_da_sessao = modo_da_sessao
        self.modo_ao_criar = modo_ao_criar
        self.status_criar = status_criar
        self.erro_no_modo = erro_no_modo
        self.chamadas: list[tuple[str, str, dict]] = []
        self.consertos = 0
        self.conserto_resolve = False

    def garantir(self, chave):
        """Duplo de `ponte._garantir_modo`."""
        self.consertos += 1
        if self.conserto_resolve:
            self.status_criar = 201
            return "criado"
        return "indisponivel"

    async def get(self, caminho, **kw):
        self.chamadas.append(("GET", caminho, kw))
        if caminho == "/api/mode/current":
            if self.erro_no_modo:
                raise httpx.ConnectError("recusada")
            sid = (kw.get("headers") or {}).get("X-Session-Id")
            return httpx.Response(200, json={"mode": self.modo_da_sessao,
                                             "session_id": sid})
        return httpx.Response(200, content=b"pong")

    async def request(self, metodo, caminho, **kw):
        self.chamadas.append((metodo, caminho, kw))
        if metodo == "POST" and caminho == "/api/sessions":
            if self.status_criar != 201:
                return httpx.Response(self.status_criar,
                                      json={"error": "Invalid mode"})
            return httpx.Response(201, json={
                "session_id": SESSAO, "agent_id": ponte.AGENTE,
                "mode": self.modo_ao_criar, "working_dir": None})
        if metodo == "GET":
            return httpx.Response(200, json={"messages": []})
        return httpx.Response(200, json={"content": "ok", "session_id": SESSAO})

    def enviados(self, metodo, caminho):
        return [kw for m, c, kw in self.chamadas if (m, c) == (metodo, caminho)]


@pytest.fixture()
def ponte_app(monkeypatch):
    def montar(gateway: Gateway | None = None):
        gw = gateway or Gateway()
        app = ponte.montar(TOKEN, "chave-gw")
        monkeypatch.setattr(ponte, "_cliente", gw)
        # Nunca o gateway de verdade: o conserto do modo é um duplo também.
        monkeypatch.setattr(ponte, "_garantir_modo", gw.garantir)
        ponte._ping_janela.clear()
        return TestClient(app), gw
    yield montar
    ponte._ping_janela.clear()


MENSAGENS = f"/api/sessions/{SESSAO}/messages"
HISTORICO = f"/api/sessions/{SESSAO}/history"


# ── criar sessão: o corpo é da ponte, não do cliente ───────────────────────
def test_criar_sessao_forca_agente_e_modo(ponte_app):
    c, gw = ponte_app()
    r = c.post("/api/sessions", headers=AUTH,
               json={"agent_id": "outro", "mode": "code", "working_dir": "/home"})
    assert r.status_code == 201
    corpo = json.loads(gw.enviados("POST", "/api/sessions")[0]["content"])
    assert corpo == {"agent_id": ponte.AGENTE, "mode": modo.NOME}


def test_criar_sessao_sem_corpo_tambem_forca(ponte_app):
    """O código antigo só reescrevia corpo não-vazio: corpo vazio passava cru."""
    c, gw = ponte_app()
    assert c.post("/api/sessions", headers=AUTH).status_code == 201
    corpo = json.loads(gw.enviados("POST", "/api/sessions")[0]["content"])
    assert corpo == {"agent_id": ponte.AGENTE, "mode": modo.NOME}


def test_gateway_que_ignora_o_modo_falha_fechado(ponte_app):
    """201 com `mode: null` é sessão SEM política. Não pode chegar ao robô.

    É o comportamento de gateway antigo, em que o serde descartava o campo:
    o cliente acha que restringiu e não restringiu.
    """
    c, gw = ponte_app(Gateway(modo_ao_criar=None))
    r = c.post("/api/sessions", headers=AUTH, json={})
    assert r.status_code == 502
    assert SESSAO not in r.text, "o id da sessão sem política vazou"
    assert r.json()["error"]["code"] == "restricted_mode_not_enforced"
    # E a sessão órfã foi encerrada no gateway.
    assert gw.enviados("DELETE", f"/api/sessions/{SESSAO}")


def test_modo_diferente_do_pedido_tambem_falha_fechado(ponte_app):
    c, _ = ponte_app(Gateway(modo_ao_criar="code"))
    assert c.post("/api/sessions", headers=AUTH, json={}).status_code == 502


def test_recusa_do_gateway_ao_criar_atravessa_sem_virar_sucesso(ponte_app):
    c, _ = ponte_app(Gateway(status_criar=400))
    assert c.post("/api/sessions", headers=AUTH, json={}).status_code == 400


def test_modo_ausente_no_gateway_e_recriado_e_a_sessao_nasce(ponte_app):
    """Banco do gateway novo, ou modo apagado com o companion no ar."""
    gw = Gateway(status_criar=400)
    gw.conserto_resolve = True
    c, _ = ponte_app(gw)
    r = c.post("/api/sessions", headers=AUTH, json={})
    assert r.status_code == 201
    assert gw.consertos == 1
    assert len(gw.enviados("POST", "/api/sessions")) == 2


def test_query_string_nao_atravessa_em_post(ponte_app):
    c, gw = ponte_app()
    c.post("/api/sessions?mode=code&working_dir=/", headers=AUTH, json={})
    assert not gw.enviados("POST", "/api/sessions")[0].get("params")


# ── mensagens: só `content`, e só para sessão no modo restrito ─────────────
def test_mensagem_leva_so_conteudo_e_agente(ponte_app, monkeypatch):
    """`model` no corpo escolheria modelo — e custo — pela conta do dono."""
    monkeypatch.delenv("GARRA_PONTE_MODELO", raising=False)
    c, gw = ponte_app()
    r = c.post(MENSAGENS, headers=AUTH,
               json={"content": "oi", "model": "caro/modelo", "agent_id": "x",
                     "working_dir": "/", "qualquer": 1})
    assert r.status_code == 200
    corpo = json.loads(gw.enviados("POST", MENSAGENS)[0]["content"])
    assert corpo == {"content": "oi", "agent_id": ponte.AGENTE}


def test_o_modelo_da_voz_e_o_do_dono_e_nao_o_do_cliente(ponte_app, monkeypatch):
    monkeypatch.setenv("GARRA_PONTE_MODELO", "anthropic/claude-haiku-4.5")
    c, gw = ponte_app()
    c.post(MENSAGENS, headers=AUTH, json={"content": "oi", "model": "caro/modelo"})
    corpo = json.loads(gw.enviados("POST", MENSAGENS)[0]["content"])
    assert corpo == {"content": "oi", "agent_id": ponte.AGENTE,
                     "model": "anthropic/claude-haiku-4.5"}


@pytest.mark.parametrize("valor", ["", "   ", "a b", "x" * 200, "modelo;rm -rf",
                                   "../etc", "\"aspas\""])
def test_modelo_invalido_no_ambiente_e_ignorado(valor):
    assert ponte.modelo_configurado({"GARRA_PONTE_MODELO": valor}) is None


@pytest.mark.parametrize("valor", ["anthropic/claude-haiku-4.5", "openrouter/auto",
                                   "z-ai/glm-5.3-flash@preset/z-ai-glm-5-3-flash",
                                   "qwen3.5:0.8b"])
def test_modelo_valido_no_ambiente_e_aceito(valor):
    assert ponte.modelo_configurado({"GARRA_PONTE_MODELO": valor}) == valor


@pytest.mark.parametrize("texto", ["/mode code", "  /mode code", "\n/model x/y",
                                   "/help", "/"])
def test_comando_por_barra_nao_atravessa(ponte_app, texto):
    """`/mode code` como conteúdo tira a sessão do modo restrito (medido)."""
    c, gw = ponte_app()
    r = c.post(MENSAGENS, headers=AUTH, json={"content": texto})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "slash_command_refused"
    assert not gw.enviados("POST", MENSAGENS), "o comando chegou ao gateway"


def test_barra_no_meio_da_frase_atravessa(ponte_app):
    c, gw = ponte_app()
    r = c.post(MENSAGENS, headers=AUTH, json={"content": "quanto é 10/2?"})
    assert r.status_code == 200
    assert gw.enviados("POST", MENSAGENS)


@pytest.mark.parametrize("corpo", [{}, {"content": ""}, {"content": "   "},
                                   {"content": 3}, {"content": None}, []])
def test_mensagem_sem_conteudo_e_recusada(ponte_app, corpo):
    c, gw = ponte_app()
    assert c.post(MENSAGENS, headers=AUTH, json=corpo).status_code == 400
    assert not gw.enviados("POST", MENSAGENS)


def test_corpo_que_nao_e_json_e_recusado(ponte_app):
    c, gw = ponte_app()
    r = c.post(MENSAGENS, headers={**AUTH, "Content-Type": "application/json"},
               content=b"{nao e json")
    assert r.status_code == 400
    assert not gw.enviados("POST", MENSAGENS)


@pytest.mark.parametrize("modo_atual", [None, "code", "ask", "auto"])
def test_sessao_fora_do_modo_restrito_da_404(ponte_app, modo_atual):
    """404 de propósito: é o que faz o app do robô abrir uma sessão nova.

    Cobre a sessão antiga, criada antes desta trava, que o app retomaria do
    `estado.json` — e a que alguém tirou do modo por fora da ponte.
    """
    c, gw = ponte_app(Gateway(modo_da_sessao=modo_atual))
    r = c.post(MENSAGENS, headers=AUTH, json={"content": "oi"})
    assert r.status_code == 404
    assert not gw.enviados("POST", MENSAGENS), "a mensagem chegou ao gateway"


def test_historico_de_sessao_fora_do_modo_tambem_da_404(ponte_app):
    """`_retomar` lê o histórico: 200 aqui e o app adota a sessão sem política."""
    c, gw = ponte_app(Gateway(modo_da_sessao=None))
    assert c.get(HISTORICO, headers=AUTH).status_code == 404
    assert not gw.enviados("GET", HISTORICO)


def test_historico_de_sessao_restrita_atravessa(ponte_app):
    c, gw = ponte_app()
    assert c.get(HISTORICO, headers=AUTH).status_code == 200
    assert gw.enviados("GET", HISTORICO)


def test_sem_conseguir_conferir_o_modo_nao_encaminha(ponte_app):
    """Gateway mudo na conferência: a dúvida fecha, não abre."""
    c, gw = ponte_app(Gateway(erro_no_modo=True))
    r = c.post(MENSAGENS, headers=AUTH, json={"content": "oi"})
    assert r.status_code == 502
    assert not gw.enviados("POST", MENSAGENS)


def test_a_conferencia_do_modo_usa_a_chave_do_gateway(ponte_app):
    c, gw = ponte_app()
    c.post(MENSAGENS, headers=AUTH, json={"content": "oi"})
    kw = gw.enviados("GET", "/api/mode/current")[0]
    assert kw["headers"]["X-Session-Id"] == SESSAO
    assert kw["headers"]["Authorization"] == "Bearer chave-gw"


def test_encerrar_sessao_continua_atravessando(ponte_app):
    """Encerrar não precisa de modo: fechar uma sessão ruim é sempre bom."""
    c, gw = ponte_app(Gateway(modo_da_sessao=None))
    assert c.delete(f"/api/sessions/{SESSAO}", headers=AUTH).status_code == 200


# ── a política do modo ─────────────────────────────────────────────────────
PERIGOSAS = {"bash", "run_program", "file_write", "file_read", "list_dir",
             "repo_search", "device_execute", "git_diff", "run_tests",
             "code_review", "schedule_recurring", "schedule_heartbeat"}


def test_a_politica_e_lista_branca_e_nao_tem_nada_perigoso():
    p = modo.POLITICA
    assert p["whitelist_mode"] is True
    assert p["no_tools"] is False
    # Lista branca VAZIA permite tudo no gateway — o contrário do que parece.
    assert p["allowed"], "lista branca vazia = tudo permitido"
    assert not (set(p["allowed"]) & PERIGOSAS)
    assert {"bash", "file_write", "device_execute"} <= set(p["denied"])


def test_o_prompt_proibe_o_que_a_voz_nao_pode_ler():
    t = modo.PROMPT_PADRAO.lower()
    for regra in ("emoji", "markdown", "url", "português"):
        assert regra in t


class GatewaySincrono:
    """Duplo do `httpx.Client` que `modo.garantir` usa no arranque."""

    def __init__(self, existentes=(), falha=False):
        self.existentes = list(existentes)
        self.falha = falha
        self.chamadas: list[tuple[str, str, dict | None]] = []

    def request(self, metodo, caminho, **kw):
        self.chamadas.append((metodo, caminho, kw.get("json")))
        if self.falha:
            raise httpx.ConnectError("recusada")
        if metodo == "GET":
            return httpx.Response(200, json={"modes": self.existentes,
                                             "success": True})
        if metodo == "POST":
            return httpx.Response(201, json={"success": True,
                                             "mode": {"id": "novo", **kw["json"]}})
        return httpx.Response(200, json={"success": True})


def test_garantir_cria_o_modo_quando_falta():
    gw = GatewaySincrono()
    assert modo.garantir(gw) == "criado"
    metodo, caminho, corpo = gw.chamadas[-1]
    assert (metodo, caminho) == ("POST", "/api/modes/custom")
    assert corpo["name"] == modo.NOME
    assert corpo["base_mode"] == modo.BASE
    assert corpo["tool_policy_overrides"] == modo.POLITICA
    assert corpo["prompt_override"] == modo.PROMPT_PADRAO


def test_garantir_nao_mexe_em_modo_que_ja_esta_certo():
    existente = {"id": "abc", "name": modo.NOME, "base_mode": modo.BASE,
                 "tool_policy_overrides": dict(modo.POLITICA),
                 "prompt_override": "prompt que o dono escreveu"}
    gw = GatewaySincrono([existente])
    assert modo.garantir(gw) == "ok"
    assert [m for m, _, _ in gw.chamadas] == ["GET"]


def test_garantir_conserta_a_politica_e_preserva_o_prompt_do_dono():
    """A política é da ponte (segurança); o prompt é de quem usa o robô."""
    existente = {"id": "abc", "name": modo.NOME, "base_mode": modo.BASE,
                 "tool_policy_overrides": {"whitelist_mode": False, "allowed": [],
                                           "denied": [], "no_tools": False},
                 "prompt_override": "prompt que o dono escreveu"}
    gw = GatewaySincrono([existente])
    assert modo.garantir(gw) == "politica_corrigida"
    metodo, caminho, corpo = gw.chamadas[-1]
    assert (metodo, caminho) == ("PATCH", "/api/modes/custom/abc")
    assert corpo == {"tool_policy_overrides": modo.POLITICA}


def test_garantir_preserva_lista_branca_que_o_dono_ampliou():
    """Acrescentar as ferramentas do próprio robô não é defeito."""
    ampliada = {"whitelist_mode": True, "no_tools": False,
                "allowed": ["web_fetch", "reachy/dance", "reachy/turn_head"],
                "denied": ["bash", "file_write", "device_execute"]}
    existente = {"id": "abc", "name": modo.NOME, "base_mode": modo.BASE,
                 "tool_policy_overrides": ampliada, "prompt_override": "x"}
    gw = GatewaySincrono([existente])
    assert modo.garantir(gw) == "ok"
    assert [m for m, _, _ in gw.chamadas] == ["GET"]


@pytest.mark.parametrize("politica", [
    {"whitelist_mode": True, "allowed": [], "denied": [], "no_tools": False},
    {"whitelist_mode": True, "allowed": ["web_fetch", "bash"], "denied": [],
     "no_tools": False},
    {"whitelist_mode": True, "allowed": ["file_write"], "denied": [],
     "no_tools": False},
    {}, None, "texto",
])
def test_politica_insegura_e_reconhecida(politica):
    assert not modo.politica_segura(politica)


def test_sem_ferramenta_nenhuma_e_seguro():
    assert modo.politica_segura({"no_tools": True})


def test_garantir_acha_o_modo_sem_diferenciar_maiusculas():
    """O gateway compara o nome com `eq_ignore_ascii_case`."""
    existente = {"id": "abc", "name": modo.NOME.upper(), "base_mode": modo.BASE,
                 "tool_policy_overrides": dict(modo.POLITICA), "prompt_override": "x"}
    assert modo.garantir(GatewaySincrono([existente])) == "ok"


def test_garantir_com_gateway_fora_do_ar_nao_derruba_o_companion():
    assert modo.garantir(GatewaySincrono(falha=True)) == "indisponivel"
