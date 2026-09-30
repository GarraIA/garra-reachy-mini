"""A casa, do lado do PC: ligar o Home Assistant ao gateway do Garra.

O gateway é quem conhece dispositivos (`hardware.home_assistant` no
`config.yml`, `DeviceRegistry`, gates R0–R5). O companion não faz um segundo
sistema: ele só põe a configuração no lugar certo — URL no `config.yml`, token
no arquivo de ambiente do gateway — reinicia o gateway, e depois diz o que o
gateway diz. E cuida da única coisa que é do robô: se a voz pode ou não mexer na
casa (a lista branca do modo restrito).

O que estes testes seguram:

- URL de fora da casa não é aceita: o gateway iria bater nela com o token;
- token que o Home Assistant recusa não é gravado;
- o token nunca volta numa resposta, nem vai a log;
- "não configurado" e "configurado e quebrado" são estados diferentes;
- por voz, a casa só se mexe com o interruptor ligado — e trancas nunca.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx
import pytest
import yaml

from companion import casa, modo

TOKEN = "eyJ-token-longo-do-home-assistant-000000000000000000"
URL = "http://homeassistant.local:8123"

ESTADOS = [
    {"entity_id": "light.sala", "state": "on",
     "attributes": {"friendly_name": "Luz da sala", "brightness": 200}},
    {"entity_id": "switch.cafeteira", "state": "off", "attributes": {"friendly_name": "Cafeteira"}},
    {"entity_id": "sensor.temperatura_quarto", "state": "22.5",
     "attributes": {"friendly_name": "Temperatura do quarto", "unit_of_measurement": "°C"}},
    {"entity_id": "lock.porta_frente", "state": "locked", "attributes": {"friendly_name": "Porta"}},
    {"entity_id": "media_player.tv", "state": "off", "attributes": {"friendly_name": "TV"}},
    {"entity_id": "scene.noite", "state": "unknown", "attributes": {"friendly_name": "Noite"}},
]


class HomeAssistant:
    def __init__(self, token_certo: str = TOKEN, fora_do_ar: bool = False) -> None:
        self.token_certo, self.fora_do_ar = token_certo, fora_do_ar
        self.pedidos: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        if self.fora_do_ar:
            raise httpx.ConnectError("recusado")
        if request.headers.get("authorization") != f"Bearer {self.token_certo}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if request.url.path == "/api/states":
            return httpx.Response(200, json=ESTADOS)
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"version": "2026.9.1", "location_name": "Casa"})
        return httpx.Response(404)


class Gateway:
    """O que o companion pergunta ao gateway: o diagnóstico e o modo."""

    def __init__(self, detalhe: str | None = None, modo_atual: dict | None = None) -> None:
        self.detalhe = detalhe
        self.modo = modo_atual or {"id": "m1", "name": modo.NOME, "tool_policy_overrides": dict(modo.POLITICA)}
        self.patches: list[dict] = []
        self.fora_do_ar = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fora_do_ar:
            raise httpx.ConnectError("recusado")
        if request.url.path == "/api/diagnostics":
            checks = []
            if self.detalhe is not None:
                checks.append({"id": "tools.capabilities", "status": "warning", "detail": self.detalhe})
            return httpx.Response(200, json={"status": "ok", "checks": checks})
        if request.url.path == "/api/modes/custom" and request.method == "GET":
            return httpx.Response(200, json={"modes": [self.modo]})
        if request.url.path.startswith("/api/modes/custom/") and request.method == "PATCH":
            corpo = json.loads(request.content)
            self.patches.append(corpo)
            self.modo["tool_policy_overrides"] = corpo["tool_policy_overrides"]
            return httpx.Response(200, json=self.modo)
        return httpx.Response(404)


@pytest.fixture()
def ambiente(tmp_path, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(yaml.safe_dump({"gateway": {"port": 3888}, "hardware": {"mqtt": None,
                                     "home_assistant": None, "automations": None}}), encoding="utf-8")
    env = tmp_path / "garraia.env"
    env.write_text("OPENROUTER_API_KEY=sk-or-xxx\nTELEGRAM_BOT_TOKEN=123\n", encoding="utf-8")
    os.chmod(env, 0o600)
    estado = tmp_path / "casa.json"
    monkeypatch.setattr(casa, "ARQUIVO_CONFIG", config)
    monkeypatch.setattr(casa, "ARQUIVO_ENV", env)
    monkeypatch.setattr(casa, "ARQUIVO_ESTADO", estado)
    reinicios: list[str] = []
    monkeypatch.setattr(casa, "_reiniciar_gateway", lambda: reinicios.append("restart"))
    monkeypatch.setattr(casa, "_gateway_saudavel", lambda: True)
    return {"config": config, "env": env, "estado": estado, "reinicios": reinicios}


def montar(ha: HomeAssistant | None = None, gw: Gateway | None = None):
    ha = ha or HomeAssistant()
    gw = gw or Gateway()
    c = casa.Casa(http_casa=httpx.Client(transport=httpx.MockTransport(ha.handler)),
                  http_gateway=httpx.Client(transport=httpx.MockTransport(gw.handler),
                                            base_url="http://gw"))
    return c, ha, gw


# ─── estados ─────────────────────────────────────────────────────────────────
def test_sem_nada_configurado_o_estado_diz_nao_configurado(ambiente) -> None:
    c, _, _ = montar(gw=Gateway("11 visivel(is) · indisponivel: device_execute (no_devices), "
                                "device_read (no_devices) · nao configurado: bash (not_configured)"))
    e = c.estado()
    assert e["configured"] is False and e["url"] is None and e["token_present"] is False
    assert e["home_assistant"] is None
    assert e["gateway"]["devices_visible"] is False
    assert e["voice_control"] is False
    assert e["state"] == "not_configured"


def test_configurar_grava_url_no_config_token_no_ambiente_e_reinicia(ambiente) -> None:
    c, ha, _ = montar()
    e = c.configurar(URL, TOKEN)
    dados = yaml.safe_load(ambiente["config"].read_text(encoding="utf-8"))
    assert dados["hardware"]["home_assistant"] == {"url": URL, "token_env": casa.ENV_TOKEN}
    linhas = ambiente["env"].read_text(encoding="utf-8").splitlines()
    assert f"{casa.ENV_TOKEN}={TOKEN}" in linhas
    assert "OPENROUTER_API_KEY=sk-or-xxx" in linhas, "o que já estava no ambiente fica"
    assert oct(ambiente["env"].stat().st_mode & 0o777) == "0o600"
    assert ambiente["reinicios"] == ["restart"]
    assert e["configured"] is True and e["url"] == URL and e["token_present"] is True
    assert e["home_assistant"]["authorized"] is True
    # o token foi conferido no Home Assistant antes de ser gravado
    assert any(r.url.path == "/api/" for r in ha.pedidos)


def test_configurar_de_novo_troca_o_token_sem_duplicar_a_linha(ambiente) -> None:
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    c2, _, _ = montar(HomeAssistant(token_certo="outro-token-igualmente-longo-000000"))
    c2.configurar(URL, "outro-token-igualmente-longo-000000")
    linhas = [l for l in ambiente["env"].read_text(encoding="utf-8").splitlines()
              if l.startswith(casa.ENV_TOKEN + "=")]
    assert linhas == [f"{casa.ENV_TOKEN}=outro-token-igualmente-longo-000000"]


@pytest.mark.parametrize("url", [
    "http://example.com:8123", "https://8.8.8.8", "ftp://homeassistant.local", "homeassistant.local:8123",
    "http://homeassistant.local:8123/api/", "http://" + "x" * 300,
])
def test_url_fora_da_casa_ou_torta_e_recusada(ambiente, url) -> None:
    c, ha, _ = montar()
    with pytest.raises(casa.ErroCasa) as e:
        c.configurar(url, TOKEN)
    assert e.value.codigo == "invalid_url"
    assert ha.pedidos == [] and ambiente["reinicios"] == []


@pytest.mark.parametrize("url", ["http://homeassistant.local:8123", "http://192.168.1.10:8123",
                                 "https://10.0.0.5", "http://127.0.0.1:8123", "http://ha.lan:8123"])
def test_url_da_casa_e_aceita(ambiente, url) -> None:
    assert casa.url_da_casa(url) is True


def test_token_recusado_pelo_home_assistant_nao_e_gravado(ambiente) -> None:
    c, _, _ = montar(HomeAssistant(token_certo="certo-000000000000000000000000"))
    with pytest.raises(casa.ErroCasa) as e:
        c.configurar(URL, TOKEN)
    assert e.value.codigo == "unauthorized"
    assert casa.ENV_TOKEN not in ambiente["env"].read_text(encoding="utf-8")
    assert ambiente["reinicios"] == []


def test_home_assistant_fora_do_ar_nao_grava(ambiente) -> None:
    c, _, _ = montar(HomeAssistant(fora_do_ar=True))
    with pytest.raises(casa.ErroCasa) as e:
        c.configurar(URL, TOKEN)
    assert e.value.codigo == "unreachable"


@pytest.mark.parametrize("token", ["", "curto", "com espaço no meio " + "x" * 30, "x" * 3000, "tok\nen" + "x" * 30])
def test_token_torto_e_recusado_antes_de_qualquer_pedido(ambiente, token) -> None:
    c, ha, _ = montar()
    with pytest.raises(casa.ErroCasa) as e:
        c.configurar(URL, token)
    assert e.value.codigo == "invalid_token" and ha.pedidos == []


def test_configurado_e_quebrado_e_diferente_de_nao_configurado(ambiente) -> None:
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    c2, _, _ = montar(HomeAssistant(fora_do_ar=True),
                      Gateway("11 visivel(is) · indisponivel: device_execute (no_devices)"))
    e = c2.estado()
    assert e["configured"] is True
    assert e["home_assistant"] == {"reachable": False, "authorized": None, "version": None}
    assert e["state"] == "broken"


def test_token_revogado_depois_de_configurado_aparece_como_quebrado(ambiente) -> None:
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    c2, _, _ = montar(HomeAssistant(token_certo="revogado-e-trocado-000000000000"))
    e = c2.estado()
    assert e["home_assistant"]["reachable"] is True and e["home_assistant"]["authorized"] is False
    assert e["state"] == "broken"


def test_configurado_e_com_dispositivos_visiveis_e_ok(ambiente) -> None:
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    c2, _, _ = montar(gw=Gateway("13 visivel(is) · nao configurado: bash (not_configured)"))
    e = c2.estado()
    assert e["gateway"]["devices_visible"] is True and e["state"] == "ok"


def test_configurado_mas_gateway_ainda_sem_dispositivos_e_pendente(ambiente) -> None:
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    c2, _, _ = montar(gw=Gateway("11 visivel(is) · indisponivel: device_read (no_devices)"))
    assert c2.estado()["state"] == "pending"


def test_gateway_fora_do_ar_nao_e_confundido_com_casa_quebrada(ambiente) -> None:
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    gw = Gateway()
    gw.fora_do_ar = True
    c2, _, _ = montar(gw=gw)
    e = c2.estado()
    assert e["gateway"] == {"devices_visible": None, "reachable": False}
    assert e["state"] == "pending"


def test_remover_apaga_config_e_token_e_reinicia(ambiente) -> None:
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    e = c.remover()
    dados = yaml.safe_load(ambiente["config"].read_text(encoding="utf-8"))
    assert dados["hardware"]["home_assistant"] is None
    assert casa.ENV_TOKEN not in ambiente["env"].read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-xxx" in ambiente["env"].read_text(encoding="utf-8")
    assert ambiente["reinicios"] == ["restart", "restart"]
    assert e["configured"] is False


# ─── as entidades, só para mostrar ───────────────────────────────────────────
def test_entidades_lista_so_o_que_o_gateway_registra(ambiente) -> None:
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    ents = c.entidades()
    ids = [e["id"] for e in ents]
    assert ids == ["ha:light.sala", "ha:switch.cafeteira", "ha:sensor.temperatura_quarto",
                   "ha:lock.porta_frente"]
    luz = ents[0]
    assert luz == {"id": "ha:light.sala", "entity_id": "light.sala", "domain": "light",
                   "name": "Luz da sala", "state": "on", "risk": "R1",
                   "capabilities": ["power", "brightness"]}
    assert ents[3]["risk"] == "R3"
    assert ents[2]["capabilities"] == ["state"] and ents[2]["risk"] == "R0"


def test_entidades_sem_configuracao_e_erro(ambiente) -> None:
    c, _, _ = montar()
    with pytest.raises(casa.ErroCasa) as e:
        c.entidades()
    assert e.value.codigo == "not_configured"


# ─── a voz e a casa ──────────────────────────────────────────────────────────
def test_por_padrao_a_voz_nao_mexe_na_casa(ambiente) -> None:
    assert casa.voz_liberada() is False
    assert modo.politica_segura(modo.POLITICA) is True
    assert "device_execute" not in modo.POLITICA["allowed"]


def test_liberar_a_voz_poe_as_ferramentas_da_casa_na_lista_branca(ambiente) -> None:
    c, _, gw = montar()
    c.configurar(URL, TOKEN)
    e = c.voz(True)
    assert e["voice_control"] is True and casa.voz_liberada() is True
    [patch] = gw.patches
    permitidas = patch["tool_policy_overrides"]["allowed"]
    assert {"device_list", "device_read", "device_execute"} <= set(permitidas)
    assert "web_fetch" in permitidas, "o que já valia continua valendo"
    assert "device_execute" not in patch["tool_policy_overrides"]["denied"]
    assert "bash" in patch["tool_policy_overrides"]["denied"]


def test_desliberar_tira_as_ferramentas_da_casa(ambiente) -> None:
    c, _, gw = montar()
    c.configurar(URL, TOKEN)
    c.voz(True)
    c.voz(False)
    permitidas = gw.patches[-1]["tool_policy_overrides"]["allowed"]
    assert not ({"device_list", "device_read", "device_execute"} & set(permitidas))
    assert "device_execute" in gw.patches[-1]["tool_policy_overrides"]["denied"]
    assert casa.voz_liberada() is False


def test_a_politica_com_a_casa_liberada_continua_segura_para_o_garantir(ambiente) -> None:
    """`modo.garantir` conserta política insegura ao subir. Com a casa liberada
    de propósito, as ferramentas da casa não podem contar como insegurança —
    senão o companion desfaria a escolha do dono a cada reinício."""
    c, _, _ = montar()
    c.configurar(URL, TOKEN)
    c.voz(True)
    politica = casa.politica_com_a_casa(modo.POLITICA, True)
    assert modo.politica_segura(politica, casa_liberada=True) is True
    assert modo.politica_segura(politica, casa_liberada=False) is False
    # e o que mexe na máquina continua proibido, com ou sem casa
    ruim = dict(politica, allowed=politica["allowed"] + ["bash"])
    assert modo.politica_segura(ruim, casa_liberada=True) is False


def test_liberar_a_voz_sem_casa_configurada_e_recusado(ambiente) -> None:
    c, _, gw = montar()
    with pytest.raises(casa.ErroCasa) as e:
        c.voz(True)
    assert e.value.codigo == "not_configured" and gw.patches == []


# ─── nada de segredo para fora ───────────────────────────────────────────────
def test_o_token_nao_volta_nem_vai_ao_log(ambiente, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    c, _, _ = montar()
    e = c.configurar(URL, TOKEN)
    e2 = c.estado()
    for saida in (json.dumps(e), json.dumps(e2), caplog.text):
        assert TOKEN not in saida and TOKEN[:12] not in saida


# ─── estado rápido e cache (achado da revisão, 30/09) ───────────────────────
def montar_contando(ha=None, gw=None, relogio=None):
    ha = ha or HomeAssistant()
    gw = gw or Gateway("11 visivel(is) · indisponivel: device_execute (no_devices), device_read (no_devices)")
    chamadas_gw: list[str] = []
    original = gw.handler
    gw.handler = lambda r: chamadas_gw.append(r.url.path) or original(r)
    c = casa.Casa(http_casa=httpx.Client(transport=httpx.MockTransport(ha.handler)),
                  http_gateway=httpx.Client(transport=httpx.MockTransport(gw.handler), base_url="http://gw"),
                  agora=relogio or (lambda: 1000.0))
    return c, ha, chamadas_gw


def test_estado_rapido_sem_casa_nao_consulta_ninguem(ambiente) -> None:
    """O robô pergunta no meio de um turno de voz, com 4 s: sem Home
    Assistant configurado, a resposta sai do arquivo, sem rede."""
    c, ha, chamadas_gw = montar_contando()
    e = c.estado(rapido=True)
    assert e["state"] == "not_configured" and e["configured"] is False
    assert ha.pedidos == [] and chamadas_gw == []


def test_estado_rapido_usa_o_ultimo_estado_completo_enquanto_fresco(ambiente) -> None:
    t = [1000.0]
    c, ha, _ = montar_contando(relogio=lambda: t[0])
    c.configurar(URL, TOKEN)
    sondas = len(ha.pedidos)
    e = c.estado(rapido=True)
    assert e["configured"] is True and e["state"] in ("ok", "pending") and len(ha.pedidos) == sondas
    t[0] += casa.FRESCO_S + 1
    e = c.estado(rapido=True)
    assert e["state"] == "unknown" and len(ha.pedidos) == sondas, "rápido nunca sonda"


def test_estado_completo_fica_em_cache_e_a_mudanca_o_invalida(ambiente) -> None:
    t = [1000.0]
    c, ha, _ = montar_contando(relogio=lambda: t[0])
    c.configurar(URL, TOKEN)
    antes = len(ha.pedidos)
    c.estado(); c.estado()
    assert len(ha.pedidos) == antes, "dentro do cache não sonda de novo"
    t[0] += casa.CACHE_S + 1
    c.estado()
    assert len(ha.pedidos) > antes
    depois = len(ha.pedidos)
    c.voz(True)
    c.estado()
    assert len(ha.pedidos) > depois, "mudar a voz invalida o cache"
