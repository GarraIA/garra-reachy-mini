"""A ponte do robô: allowlist, token, e a sonda de saúde que não pode mentir.

O gateway é substituído por um duplo controlável — o que se testa aqui é a
ponte, não o Garra.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from companion import ponte, reachy

TOKEN = "token-de-teste-nao-e-segredo"
SESSAO = "123e4567-e89b-12d3-a456-426614174000"


class GatewayFalso:
    """Duplo do `httpx.AsyncClient` que a ponte usa para falar com o gateway."""

    def __init__(self, ping_status: int = 200, erro: bool = False) -> None:
        self.ping_status = ping_status
        self.erro = erro
        self.chamadas: list[tuple[str, str, dict]] = []

    async def get(self, caminho, **kw):
        self.chamadas.append(("GET", caminho, kw))
        if self.erro:
            raise httpx.ConnectError("recusada")
        return httpx.Response(self.ping_status, content=b"pong")

    async def request(self, metodo, caminho, **kw):
        self.chamadas.append((metodo, caminho, kw))
        if self.erro:
            raise httpx.ConnectError("recusada")
        return httpx.Response(200, json={"content": "ok"})


@pytest.fixture()
def ponte_app(monkeypatch):
    """App da ponte com gateway falso e a janela de rate limit zerada."""
    def montar(gateway: GatewayFalso | None = None, chave: str | None = "chave-gw"):
        gw = gateway or GatewayFalso()
        app = ponte.montar(TOKEN, chave)
        monkeypatch.setattr(ponte, "_cliente", gw)
        ponte._ping_janela.clear()
        return TestClient(app), gw
    yield montar
    ponte._ping_janela.clear()


# ── a sonda de saúde ───────────────────────────────────────────────────────
def test_ping_dispensa_token(ponte_app):
    """`cerebro.sondar()` sonda sem `Authorization`; era isso que dava 401."""
    c, _ = ponte_app()
    r = c.get("/ping")
    assert r.status_code == 200
    assert r.text == "pong"


def test_ping_prova_o_gateway_e_nao_a_ponte(ponte_app):
    """Ponte viva e gateway morto tem de ser 503, não 200.

    Responder 200 daqui faria o robô mostrar `gateway: available` e só descobrir
    a verdade na primeira conversa — um health check que mente é pior que
    nenhum.
    """
    c, gw = ponte_app(GatewayFalso(erro=True))
    r = c.get("/ping")
    assert r.status_code == 503
    assert "unavailable" in r.text
    # E foi mesmo até o gateway antes de decidir.
    assert gw.chamadas and gw.chamadas[0][:2] == ("GET", "/ping")


def test_gateway_respondendo_erro_tambem_e_503(ponte_app):
    c, _ = ponte_app(GatewayFalso(ping_status=500))
    assert c.get("/ping").status_code == 503


def test_ping_nao_repassa_credencial_nenhuma(ponte_app):
    """Nem o Authorization do cliente, nem a chave do gateway."""
    c, gw = ponte_app()
    c.get("/ping", headers={"Authorization": "Bearer o-que-o-cliente-mandou"})
    _, _, kw = gw.chamadas[0]
    cabecalhos = {k.lower(): v for k, v in (kw.get("headers") or {}).items()}
    assert "authorization" not in cabecalhos


def test_ping_usa_timeout_curto(ponte_app):
    """180 s é o timeout da conversa; numa sonda travaria o supervisor do robô."""
    c, gw = ponte_app()
    c.get("/ping")
    assert gw.chamadas[0][2].get("timeout") == ponte.PING_TIMEOUT_S
    assert ponte.PING_TIMEOUT_S <= 2.0


def test_ping_nao_e_cacheavel(ponte_app):
    c, _ = ponte_app()
    assert c.get("/ping").headers["cache-control"] == "no-store"


def test_ping_aceita_head(ponte_app):
    assert ponte_app()[0].head("/ping").status_code == 200


def test_ping_recusa_post(ponte_app):
    """Só GET e HEAD. POST cai no catch-all, que exige token."""
    c, _ = ponte_app()
    assert c.post("/ping").status_code in (401, 404, 405)


def test_ping_tem_teto_por_minuto(ponte_app):
    """Exposta na LAN sem token: não pode virar amplificador contra o gateway."""
    c, _ = ponte_app()
    for _ in range(ponte.PING_MAX_POR_MINUTO):
        assert c.get("/ping").status_code == 200
    assert c.get("/ping").status_code == 429


def test_a_janela_do_teto_desliza():
    ponte._ping_janela.clear()
    for i in range(ponte.PING_MAX_POR_MINUTO):
        assert ponte._ping_liberado(agora=1000.0 + i * 0.001)
    assert not ponte._ping_liberado(agora=1000.5)
    # Um minuto depois, tudo liberado de novo.
    assert ponte._ping_liberado(agora=1062.0)


def test_ping_nao_revela_o_motivo_interno(ponte_app):
    """A sonda é pública; o tipo da exceção é detalhe de dentro."""
    c, _ = ponte_app(GatewayFalso(erro=True))
    corpo = c.get("/ping").text
    assert "ConnectError" not in corpo and "3888" not in corpo


def test_a_ponte_nao_abre_cors(ponte_app):
    c, _ = ponte_app()
    r = c.get("/ping", headers={"Origin": "http://exemplo.invalido"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ── allowlist e token, que continuam valendo para o resto ──────────────────
def test_rota_fora_da_allowlist_da_404(ponte_app):
    c, _ = ponte_app()
    for caminho in ("/api/settings", "/admin", "/api/providers"):
        assert c.get(caminho).status_code == 404


def test_rota_da_allowlist_sem_token_da_401(ponte_app):
    c, _ = ponte_app()
    assert c.post("/api/sessions", json={}).status_code == 401
    assert c.post(f"/api/sessions/{SESSAO}/messages", json={}).status_code == 401


def test_metodo_errado_nao_atravessa(ponte_app):
    """A allowlist casa (método, caminho): GET em /api/sessions não passa."""
    c, _ = ponte_app()
    assert c.get("/api/sessions").status_code == 404


def test_com_token_atravessa_e_forca_o_agente(ponte_app):
    """A trava que impede alcançar o agente padrão do gateway, que tem `bash`."""
    import json

    c, gw = ponte_app()
    r = c.post(f"/api/sessions/{SESSAO}/messages",
               headers={"Authorization": f"Bearer {TOKEN}"},
               json={"content": "oi", "agent_id": "agente-que-tem-bash"})
    assert r.status_code == 200
    enviado = json.loads(gw.chamadas[-1][2]["content"])
    assert enviado["agent_id"] == ponte.AGENTE


def test_token_errado_nao_atravessa(ponte_app):
    c, _ = ponte_app()
    r = c.post("/api/sessions", headers={"Authorization": "Bearer errado"},
               json={})
    assert r.status_code == 401


# ── o segredo não pode voltar no corpo ─────────────────────────────────────
def test_configure_nao_devolve_credencial(monkeypatch):
    """`gateway_key` carrega o mesmo valor de `voz_token` — os dois são segredo.

    Redigir só um deixava a credencial sair em claro para o navegador.
    """
    segredo = "segredo-que-nao-pode-vazar"
    monkeypatch.setattr(reachy.voz, "token", lambda: segredo)
    monkeypatch.setattr(reachy.voz, "ip_lan", lambda: "10.0.0.24")
    monkeypatch.setattr(reachy, "_config_atual",
                        lambda p: {"efetiva": {}, "salva": {}})
    monkeypatch.setattr(reachy, "_gravar", lambda p, v: {})
    monkeypatch.setattr(reachy, "_esperar_alcancar", lambda *a, **k: (True, {}))

    r = reachy.configurar("http://10.0.0.142:8042")
    texto = repr(r)
    assert segredo not in texto, "a credencial saiu no relatório"
    assert r["applied"]["gateway_key"] == reachy.MASCARA
    assert r["applied"]["voz_token"] == reachy.MASCARA
    # E o que não é segredo continua visível, que é o ponto do relatório.
    assert r["applied"]["gateway_url"] == "http://10.0.0.24:8126"
    assert r["applied"]["agent_id"] == "reachy_voice"


def test_o_rollback_tambem_nao_vaza(monkeypatch):
    segredo = "outro-segredo"
    monkeypatch.setattr(reachy.voz, "token", lambda: segredo)
    monkeypatch.setattr(reachy.voz, "ip_lan", lambda: "10.0.0.24")
    monkeypatch.setattr(reachy, "_config_atual",
                        lambda p: {"efetiva": {}, "salva": {"gateway_key": "velho"}})
    monkeypatch.setattr(reachy, "_gravar", lambda p, v: {})
    monkeypatch.setattr(reachy, "_esperar_alcancar", lambda *a, **k: (False, {}))

    r = reachy.configurar("http://10.0.0.142:8042")
    assert r["rolled_back"] is True
    assert segredo not in repr(r) and "velho" not in repr(r)


# ── chave do gateway: exigida ou apenas avisada ─────────────────────────────
# O gateway pode fechar `/api/*` (`local_api_auth = token_required`). A ponte
# fala com ele em nome do robô; sem chave, o robô perderia o cérebro reportando
# apenas "gateway inalcançável". Falhar no arranque é mais honesto — mas só
# quando o gateway realmente exige.
def _companion_com_config(monkeypatch, cfg):
    from companion import servidor

    monkeypatch.setattr(servidor, "_config_do_gateway", lambda: cfg)
    return servidor


def test_sem_chave_em_token_required_falha_no_arranque(monkeypatch):
    import pytest

    servidor = _companion_com_config(monkeypatch, {"local_api_auth": "token_required"})
    with pytest.raises(SystemExit) as e:
        servidor._conferir_chave_do_gateway()
    assert "token_required" in str(e.value)
    assert "api_key" in str(e.value)


def test_sem_chave_em_loopback_trust_apenas_avisa(monkeypatch, caplog):
    import logging

    servidor = _companion_com_config(monkeypatch, {"local_api_auth": "loopback_trust"})
    with caplog.at_level(logging.WARNING):
        assert servidor._conferir_chave_do_gateway() is None
    assert any("api_key" in r.message for r in caplog.records)


def test_modo_ausente_e_tratado_como_loopback_trust(monkeypatch):
    servidor = _companion_com_config(monkeypatch, {})
    assert servidor._conferir_chave_do_gateway() is None


def test_com_chave_nao_reclama_em_nenhum_modo(monkeypatch):
    for modo in ("loopback_trust", "token_required"):
        servidor = _companion_com_config(
            monkeypatch, {"local_api_auth": modo, "api_key": "k"}
        )
        assert servidor._conferir_chave_do_gateway() == "k"
