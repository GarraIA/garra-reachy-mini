"""Ponte estreita do robô para o gateway — o que NÃO se resolve abrindo a :3888.

O robô precisa falar com o gateway do Garra, que escuta em `127.0.0.1:3888`.
A saída óbvia seria trocar o `--host` do `garraia.service` para `0.0.0.0`.
Medi antes de fazer, e não dá:

  * `POST /api/sessions` responde **201 sem autenticação nenhuma** (o
    `/api/auth-check` diz `auth_required: true`, mas o REST não exige);
  * o agente padrão do gateway tem **`bash`** entre as 27 ferramentas de runtime.

Somando os dois, `--host 0.0.0.0` entregaria execução de comando na máquina do
dono a qualquer aparelho da rede. Um bug de configuração vira um shell remoto.

Então esta ponte expõe na LAN só o mínimo, e com cinco travas:

  1. **allowlist de rotas** — as quatro que o app do robô usa, e mais nada;
  2. **token obrigatório** — o mesmo `GARRA_VOZ_TOKEN` já gravado no robô;
  3. **`agent_id` forçado** para `reachy_voice` — mesmo que o robô peça outro,
     ou não peça nenhum;
  4. **modo restrito forçado** (`modo.NOME`) em toda sessão, e conferido a cada
     mensagem. É esta que vale no gateway público: lá a lista de ferramentas do
     agente não é aplicada no REST, e sessão sem modo não tem política nenhuma.
     Sessão fora do modo responde 404 — o app do robô abre outra sozinho;
  5. **corpo reescrito** — de uma mensagem só atravessa o `content`, e nunca um
     que comece com `/`: o gateway executa `/mode code` vindo no texto, e isso
     tira a sessão do modo restrito (medido no 0.4.7).

O gateway continua intocado: nem recompilado, nem reiniciado, nem exposto.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time

import base64
import ipaddress
import urllib.parse

import httpx

from . import agente, casa as casa_mod, modo, visao
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("companion.ponte")

PORTA = 8126
AGENTE = "reachy_voice"
GATEWAY = "http://127.0.0.1:3888"

# Modelo da voz, decidido no desktop. O gateway aceita `model` por mensagem, e
# é o dono — não o robô, nem quem tiver o token — que escolhe o que roda na
# conta dele. Vazio = o modelo padrão do gateway.
_MODELO_VALIDO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{2,119}")


def modelo_configurado(ambiente: dict | None = None) -> str | None:
    bruto = ((os.environ if ambiente is None else ambiente)
             .get("GARRA_PONTE_MODELO") or "").strip()
    if not bruto:
        return None
    if not _MODELO_VALIDO.fullmatch(bruto):
        log.warning("GARRA_PONTE_MODELO inválido — ignorado")
        return None
    return bruto

# `GET /ping` é a sonda de saúde do app do robô, e ela chega SEM `Authorization`
# (`cerebro.sondar()` usa um `requests.get` pelado). Contra a allowlist isso dava
# 401, o app concluía "gateway inalcançável" e nunca construía o cérebro.
#
# A saída não é responder 200 daqui: isso provaria só que a ponte está viva, e um
# gateway parado apareceria como `available` até a primeira conversa falhar. Esta
# rota vai até o gateway de verdade, em loopback, e só devolve 200 se ele
# responder. Sem token porque não há o que proteger — a resposta é uma palavra —
# e sem repassar o `Authorization` do cliente, que aqui não significa nada.
PING_TIMEOUT_S = 1.0
# Sonda barata e frequente (o supervisor do robô repete a cada 20 s), mas exposta
# na LAN: o teto evita que ela vire um amplificador contra o gateway.
PING_MAX_POR_MINUTO = 120
_ping_janela: list[float] = []

# Registry de agentes (Fase 1): leitura barata, mas atravessa até o gateway.
# Janela própria — não compartilha com o ping para uma não esfomear a outra.
AGENTS_TIMEOUT_S = 5.0
AGENTS_MAX_POR_MINUTO = 60
_agents_janela: list[float] = []

# O que da resposta do gateway pode atravessar a ponte. Allowlist, não
# blocklist: o gateway já redige prompts/operador, mas a ponte não confia —
# um campo novo no gateway só chega ao robô quando alguém o listar aqui.
_AGENT_CAMPOS = frozenset({
    "id", "kind", "display_name", "enabled_for_routing", "has_config",
    "backend", "adapter_integrated", "model", "allowed_tools_count",
    "api_tagged_sessions",
})
_AGENT_MODEL_CAMPOS = frozenset({
    "global_default_model", "model_mode", "configured_model",
    "effective_requested_model", "transport_provider", "resolved_model",
    "resolved_model_status",
})


def _redigir_agente(bruto: object) -> dict | None:
    """Filtra um descriptor à allowlist. Formato inesperado → descartado."""
    if not isinstance(bruto, dict):
        return None
    limpo = {k: v for k, v in bruto.items() if k in _AGENT_CAMPOS}
    modelo = limpo.get("model")
    if isinstance(modelo, dict):
        limpo["model"] = {k: v for k, v in modelo.items()
                          if k in _AGENT_MODEL_CAMPOS}
    elif "model" in limpo:
        del limpo["model"]
    return limpo if limpo.get("id") else None


def _agents_liberado(agora: float | None = None) -> bool:
    agora = time.monotonic() if agora is None else agora
    corte = agora - 60.0
    _agents_janela[:] = [t for t in _agents_janela if t > corte]
    if len(_agents_janela) >= AGENTS_MAX_POR_MINUTO:
        return False
    _agents_janela.append(agora)
    return True

# Só isto atravessa. `re.fullmatch`, e não `startswith`: um prefixo deixaria
# passar `/api/sessions/../algo`.
#
# `/ping` NÃO está aqui: ele tem rota própria, registrada antes do catch-all,
# porque é a única que dispensa token. Tudo o que cai neste catch-all exige.
ROTAS = (
    ("POST", re.compile(r"/api/sessions")),
    ("POST", re.compile(r"/api/sessions/[0-9a-fA-F-]{36}/messages")),
    ("GET", re.compile(r"/api/sessions/[0-9a-fA-F-]{36}/history")),
    ("DELETE", re.compile(r"/api/sessions/[0-9a-fA-F-]{36}")),
)

# As duas rotas que leem ou escrevem numa sessão, e por isso exigem que ela
# esteja no modo restrito. Encerrar (`DELETE`) não exige: fechar é sempre bom.
_NA_SESSAO = re.compile(r"/api/sessions/([0-9a-fA-F-]{36})/(?:messages|history)")
_UUID = re.compile(r"[0-9a-fA-F-]{36}")

# Conferir o modo é uma leitura em loopback; se demora, o gateway está mal e a
# resposta certa é não encaminhar.
MODO_TIMEOUT_S = 3.0

app = FastAPI(title="Garra Reachy — ponte do robô", docs_url=None, redoc_url=None)
_cliente: httpx.AsyncClient | None = None


def _permitida(metodo: str, caminho: str) -> bool:
    return any(m == metodo and p.fullmatch(caminho) for m, p in ROTAS)


def _autorizado(request: Request, token: str) -> bool:
    cab = request.headers.get("authorization") or ""
    enviado = cab[7:] if cab.lower().startswith("bearer ") else ""
    return bool(enviado) and hmac.compare_digest(enviado, token)


def _erro(codigo: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": codigo}}, status_code=status)


def _garantir_modo(chave_gateway: str | None) -> str:
    """Cria ou conserta o modo restrito no gateway. Síncrono: roda em thread."""
    cabecalhos = {"Authorization": f"Bearer {chave_gateway}"} if chave_gateway else {}
    with httpx.Client(base_url=GATEWAY, headers=cabecalhos, timeout=10.0) as c:
        return modo.garantir(c)


def _corpo_da_mensagem(bruto: bytes) -> bytes | JSONResponse:
    """O que de uma mensagem atravessa: o `content`, e o agente que a ponte põe.

    O `model` do cliente é descartado: escolheria modelo — e custo — na conta
    do dono. O que vai é o que o dono configurou no desktop, se configurou.
    `working_dir` e o resto não têm o que fazer aqui.
    """
    try:
        dados = json.loads(bruto or b"null")
    except ValueError:
        return JSONResponse({"erro": "corpo inválido"}, status_code=400)
    texto = dados.get("content") if isinstance(dados, dict) else None
    if not isinstance(texto, str) or not texto.strip():
        return JSONResponse({"erro": "corpo inválido"}, status_code=400)
    # O gateway apara o texto antes de procurar a barra; a ponte apara igual.
    if texto.strip().startswith("/"):
        return _erro("slash_command_refused", 400)
    corpo = {"content": texto, "agent_id": AGENTE}
    modelo = modelo_configurado()
    if modelo:
        corpo["model"] = modelo
    return json.dumps(corpo).encode()


def _ping_liberado(agora: float | None = None) -> bool:
    """Janela deslizante de um minuto. Barata, e não guarda quem chamou."""
    agora = time.monotonic() if agora is None else agora
    corte = agora - 60.0
    _ping_janela[:] = [t for t in _ping_janela if t > corte]
    if len(_ping_janela) >= PING_MAX_POR_MINUTO:
        return False
    _ping_janela.append(agora)
    return True


# ── visão: o quadro do robô vira texto AQUI ──────────────────────────────────
# A pergunta cabe numa frase; a imagem, num quadro 720p. O que passa disso não é
# um pedido do robô.
PERGUNTA_MAX = 500
IMAGEM_B64_MAX = 6 * 1024 * 1024
_MODELO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,119}")
_STATUS_DO_ERRO = {
    "bad_image": 400, "no_provider": 400, "cloud_disabled": 403,
    "provider_offline": 503, "model_missing": 503, "model_without_vision": 503,
    "timeout": 504, "provider_error": 502, "auth_failed": 502,
}


def _endpoint_da_casa(url: str) -> bool:
    """Para onde o companion MANDA o quadro. Só loopback, rede privada ou `.local`:
    quem tiver o token da ponte não aponta a câmera da casa para a internet."""
    try:
        partes = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if partes.scheme not in ("http", "https") or not partes.hostname or len(url) > 200:
        return False
    nome = partes.hostname
    if nome in ("localhost",) or nome.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(nome)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _preferencias(bruto: object) -> visao.Preferencias | None:
    """As preferências que o robô mandou, validadas. `None` = recusadas."""
    if bruto is None:
        return visao.Preferencias()
    if not isinstance(bruto, dict):
        return None
    padrao = visao.Preferencias()
    provedor = str(bruto.get("provedor", padrao.provedor))
    endpoint = str(bruto.get("local_endpoint", padrao.local_endpoint))
    modelo_local = str(bruto.get("local_modelo", padrao.local_modelo))
    modelo_nuvem = str(bruto.get("nuvem_modelo", padrao.nuvem_modelo))
    if provedor not in visao.PROVEDORES or not _endpoint_da_casa(endpoint):
        return None
    if not (_MODELO.fullmatch(modelo_local) and _MODELO.fullmatch(modelo_nuvem)):
        return None
    return visao.Preferencias(
        provedor=provedor, local_endpoint=endpoint.rstrip("/"), local_modelo=modelo_local,
        nuvem_modelo=modelo_nuvem, nuvem_permitida=bruto.get("nuvem_permitida") is True)


_olho_local = None
_olho_nuvem = None


def _preferencias_da_query(q) -> visao.Preferencias | None:
    bruto = {k: q[k] for k in ("provedor", "local_endpoint", "local_modelo", "nuvem_modelo") if k in q}
    if "nuvem_permitida" in q:
        bruto["nuvem_permitida"] = str(q["nuvem_permitida"]).lower() in ("1", "true", "sim")
    return _preferencias(bruto)


_casa = None
_STATUS_DA_CASA = {"invalid_url": 400, "invalid_token": 400, "unreachable": 502, "unauthorized": 401,
                   "not_configured": 409, "config_error": 500, "gateway_restart_failed": 502}


def montar(token: str, chave_gateway: str | None, visao_local=None, visao_nuvem=None,
           casa=None) -> FastAPI:
    """Devolve o app da ponte já com o token e a chave real do gateway.

    `visao_local`/`visao_nuvem` são os provedores de visão; sem eles, os de
    verdade — o Ollama deste PC e a OpenRouter com a chave do ambiente. `casa`
    é quem liga o Home Assistant ao gateway (`casa.Casa`).
    """
    # Módulo, não closure: o `app` é um só e as rotas ficam registradas na
    # primeira chamada; o que `montar` decide depois precisa valer também.
    global _olho_local, _olho_nuvem, _casa
    _olho_local = visao_local or visao.Local()
    _olho_nuvem = visao_nuvem or visao.Nuvem(chave=os.environ.get("OPENROUTER_API_KEY"))
    _casa = casa or casa_mod.Casa(chave_gateway=chave_gateway)

    @app.api_route("/ping", methods=["GET", "HEAD"])
    async def saude(request: Request) -> Response:
        """Compatibilidade com a sonda do robô — e ela precisa dizer a verdade.

        200 só quando o gateway responde. 503 quando não responde: `available`
        tem de significar que o cérebro está lá, não que a ponte está de pé.
        """
        cabecalhos = {"Cache-Control": "no-store"}
        if not _ping_liberado():
            return Response(status_code=429, headers=cabecalhos)
        global _cliente
        if _cliente is None:
            _cliente = httpx.AsyncClient(base_url=GATEWAY, timeout=180.0)
        try:
            # Sem `Authorization`: nem o do cliente (não vale nada aqui) nem o do
            # gateway (uma sonda não precisa de credencial para provar vida).
            r = await _cliente.get("/ping", timeout=PING_TIMEOUT_S)
        except httpx.HTTPError:
            # Sem o tipo da exceção: a sonda é pública e o motivo é interno.
            return Response(content=b"gateway unavailable", status_code=503,
                            media_type="text/plain", headers=cabecalhos)
        if not r.is_success:
            return Response(content=b"gateway unavailable", status_code=503,
                            media_type="text/plain", headers=cabecalhos)
        return Response(content=b"pong", status_code=200,
                        media_type="text/plain", headers=cabecalhos)

    # ── identidade do agente, tratada AQUI dentro ────────────────────────────
    # Não é proxy: o catch-all abaixo repassa ao gateway, e o dono do
    # `config.yml` é este processo. O painel do robô não alcança a API
    # administrativa em `127.0.0.1:8125` (loopback por desenho, e a origem dele
    # não é loopback), então a ponte é o único caminho — com o mesmo token.
    #
    # Registradas antes do catch-all porque o FastAPI casa na ordem.
    @app.get("/api/agent-identity")
    async def identidade_ler(request: Request):
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        try:
            return {"ok": True, **await asyncio.to_thread(agente.ler)}
        except agente.ErroAgente as e:
            return JSONResponse({"ok": False, "error": {"code": "gateway_config_error",
                                                        "detail": str(e)}},
                                status_code=503)

    @app.put("/api/agent-identity")
    async def identidade_gravar(request: Request):
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        try:
            corpo = await request.json()
        except ValueError:
            return JSONResponse({"erro": "corpo inválido"}, status_code=400)
        if not isinstance(corpo, dict):
            return JSONResponse({"erro": "corpo inválido"}, status_code=400)
        try:
            return {"ok": True, **await asyncio.to_thread(
                agente.gravar, corpo, str(corpo.get("updated_by") or "painel-robo"))}
        except agente.ConflitoAgente as e:
            return JSONResponse({"ok": False, "conflict": True, **e.atual},
                                status_code=409)
        except agente.ErroAgente as e:
            return JSONResponse({"ok": False, "error": {"code": "invalid_identity",
                                                        "detail": str(e)}},
                                status_code=400)

    @app.post("/api/agent-identity/reset")
    async def identidade_restaurar(request: Request):
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        try:
            corpo = await request.json()
        except ValueError:
            corpo = {}
        corpo = corpo if isinstance(corpo, dict) else {}
        try:
            return {"ok": True, **await asyncio.to_thread(
                agente.restaurar, corpo.get("revision"), "painel-robo")}
        except agente.ConflitoAgente as e:
            return JSONResponse({"ok": False, "conflict": True, **e.atual},
                                status_code=409)
        except agente.ErroAgente as e:
            return JSONResponse({"ok": False, "error": {"code": "invalid_identity",
                                                        "detail": str(e)}},
                                status_code=400)

    # ── registry de agentes (Fase 1, SOMENTE leitura) ────────────────────────
    # Rota estreita, não catch-all: GET fixo, upstream fixo (o gateway local),
    # resposta filtrada por allowlist de campos. O token é o mesmo da ponte;
    # o Authorization do cliente NUNCA é repassado — o token do gateway é o
    # local, injetado aqui dentro. Erros diferenciam gateway fora do ar,
    # rota inexistente (gateway antigo) e resposta inválida.
    @app.get("/api/agents")
    async def agentes_ler(request: Request) -> Response:
        cabecalhos = {"Cache-Control": "no-store"}
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401,
                                headers=cabecalhos)
        if not _agents_liberado():
            return Response(status_code=429, headers=cabecalhos)
        global _cliente
        if _cliente is None:
            _cliente = httpx.AsyncClient(base_url=GATEWAY, timeout=180.0)
        upstream = {"Content-Type": "application/json"}
        if chave_gateway:
            upstream["Authorization"] = f"Bearer {chave_gateway}"
        try:
            r = await _cliente.get("/api/agents", headers=upstream,
                                   timeout=AGENTS_TIMEOUT_S)
        except httpx.HTTPError:
            # Sem o tipo da exceção: o motivo é do desktop, não da LAN.
            return JSONResponse(
                {"ok": False, "error": {"code": "gateway_unreachable"}},
                status_code=502, headers=cabecalhos)
        if r.status_code == 404:
            # Gateway de produção sem a rota nova: recurso não suportado,
            # que não é a mesma coisa que gateway fora do ar.
            return JSONResponse(
                {"ok": False, "error": {"code": "agent_registry_unsupported"}},
                status_code=501, headers=cabecalhos)
        if not r.is_success:
            return JSONResponse(
                {"ok": False, "error": {"code": "invalid_gateway_response"}},
                status_code=502, headers=cabecalhos)
        try:
            dados = r.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": {"code": "invalid_gateway_response"}},
                status_code=502, headers=cabecalhos)
        brutos = dados.get("agents") if isinstance(dados, dict) else None
        if not isinstance(brutos, list):
            return JSONResponse(
                {"ok": False, "error": {"code": "invalid_gateway_response"}},
                status_code=502, headers=cabecalhos)
        agentes = [a for a in (_redigir_agente(b) for b in brutos) if a]
        return JSONResponse({"ok": True, "agents": agentes},
                            headers=cabecalhos)

    # ── visão ────────────────────────────────────────────────────────────────
    # Não é proxy do gateway: o quadro entra aqui e sai como texto. A imagem
    # vive só nesta requisição — não vai a log (nem em erro), nem a disco.
    @app.post("/api/olhar")
    async def olhar(request: Request) -> Response:
        cabecalhos = {"Cache-Control": "no-store"}
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401, headers=cabecalhos)
        bruto = await request.body()
        if len(bruto) > IMAGEM_B64_MAX + 4096:
            return JSONResponse({"erro": "bad_image", "detalhe": "grande demais"},
                                status_code=413, headers=cabecalhos)
        try:
            corpo = json.loads(bruto)
        except ValueError:
            return JSONResponse({"erro": "bad_request"}, status_code=400, headers=cabecalhos)
        if not isinstance(corpo, dict):
            return JSONResponse({"erro": "bad_request"}, status_code=400, headers=cabecalhos)
        pergunta = corpo.get("pergunta")
        if not isinstance(pergunta, str) or not pergunta.strip() or len(pergunta) > PERGUNTA_MAX:
            return JSONResponse({"erro": "bad_request", "detalhe": "pergunta"},
                                status_code=400, headers=cabecalhos)
        prefs = _preferencias(corpo.get("preferencias"))
        if prefs is None:
            return JSONResponse({"erro": "bad_request", "detalhe": "preferencias"},
                                status_code=400, headers=cabecalhos)
        try:
            imagem = base64.b64decode(corpo.get("imagem") or "", validate=True)
        except (ValueError, TypeError):
            return JSONResponse({"erro": "bad_image", "detalhe": "base64"},
                                status_code=400, headers=cabecalhos)
        try:
            obs = await asyncio.to_thread(visao.descrever, imagem, pergunta.strip(), prefs,
                                          _olho_local, _olho_nuvem)
        except visao.ErroDeVisao as e:
            log.info("visão recusada: %s (%s, %d bytes)", e.codigo, prefs.provedor, len(imagem))
            return JSONResponse({"erro": e.codigo, "detalhe": e.detalhe[:120]},
                                status_code=_STATUS_DO_ERRO.get(e.codigo, 502), headers=cabecalhos)
        log.info("visão: %s/%s em %d ms (%d bytes de imagem, %d de texto)",
                 obs.provedor, obs.modelo, obs.ms, len(imagem), len(obs.texto))
        return JSONResponse({"texto": obs.texto, "provedor": obs.provedor,
                             "modelo": obs.modelo, "ms": obs.ms}, headers=cabecalhos)

    @app.get("/api/olhar/estado")
    async def olhar_estado(request: Request) -> Response:
        cabecalhos = {"Cache-Control": "no-store"}
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401, headers=cabecalhos)
        prefs = _preferencias_da_query(request.query_params)
        if prefs is None:
            return JSONResponse({"erro": "bad_request", "detalhe": "preferencias"},
                                status_code=400, headers=cabecalhos)
        estado = await asyncio.to_thread(visao.estado, prefs, _olho_local, _olho_nuvem)
        return JSONResponse(estado, headers=cabecalhos)

    # ── casa ─────────────────────────────────────────────────────────────────
    # O painel do robô configura o Home Assistant por aqui. O token do HA
    # atravessa UMA vez, no `configurar`, e vai para o ambiente do gateway;
    # nenhuma resposta o devolve, nenhum log o guarda.
    def _erro_da_casa(e: casa_mod.ErroCasa) -> JSONResponse:
        log.info("casa: %s", e.codigo)
        return JSONResponse({"erro": e.codigo, "detalhe": e.detalhe[:120]},
                            status_code=_STATUS_DA_CASA.get(e.codigo, 502),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/casa/estado")
    async def casa_estado(request: Request) -> Response:
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        try:
            return JSONResponse(await asyncio.to_thread(_casa.estado),
                                headers={"Cache-Control": "no-store"})
        except casa_mod.ErroCasa as e:
            return _erro_da_casa(e)

    @app.post("/api/casa/configurar")
    async def casa_configurar(request: Request) -> Response:
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        try:
            corpo = json.loads(await request.body())
        except ValueError:
            corpo = None
        if (not isinstance(corpo, dict) or not isinstance(corpo.get("url"), str)
                or not isinstance(corpo.get("token"), str)):
            return JSONResponse({"erro": "bad_request"}, status_code=400)
        try:
            return JSONResponse(await asyncio.to_thread(_casa.configurar, corpo["url"], corpo["token"]),
                                headers={"Cache-Control": "no-store"})
        except casa_mod.ErroCasa as e:
            return _erro_da_casa(e)

    @app.delete("/api/casa")
    async def casa_remover(request: Request) -> Response:
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        try:
            return JSONResponse(await asyncio.to_thread(_casa.remover),
                                headers={"Cache-Control": "no-store"})
        except casa_mod.ErroCasa as e:
            return _erro_da_casa(e)

    @app.get("/api/casa/entidades")
    async def casa_entidades(request: Request) -> Response:
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        try:
            return JSONResponse({"entities": await asyncio.to_thread(_casa.entidades)},
                                headers={"Cache-Control": "no-store"})
        except casa_mod.ErroCasa as e:
            return _erro_da_casa(e)

    @app.post("/api/casa/voz")
    async def casa_voz(request: Request) -> Response:
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        try:
            corpo = json.loads(await request.body())
        except ValueError:
            corpo = None
        if not isinstance(corpo, dict) or not isinstance(corpo.get("enabled"), bool):
            return JSONResponse({"erro": "bad_request"}, status_code=400)
        try:
            return JSONResponse(await asyncio.to_thread(_casa.voz, corpo["enabled"]),
                                headers={"Cache-Control": "no-store"})
        except casa_mod.ErroCasa as e:
            return _erro_da_casa(e)

    @app.api_route("/{caminho:path}",
                   methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    async def encaminhar(caminho: str, request: Request) -> Response:
        global _cliente
        alvo = "/" + caminho
        if request.method == "OPTIONS":
            return Response(status_code=204)
        if not _permitida(request.method, alvo):
            log.warning("ponte recusou %s %s de %s", request.method, alvo,
                        request.client.host if request.client else "?")
            return JSONResponse({"erro": "rota não exposta"}, status_code=404)
        if not _autorizado(request, token):
            return JSONResponse({"erro": "token inválido"}, status_code=401)

        if _cliente is None:
            _cliente = httpx.AsyncClient(base_url=GATEWAY, timeout=180.0)
        cabecalhos = {"Content-Type": "application/json"}
        if chave_gateway:
            cabecalhos["Authorization"] = f"Bearer {chave_gateway}"

        criando = request.method == "POST" and alvo == "/api/sessions"
        corpo: bytes | None = None
        if criando:
            # O corpo é da ponte. O que o cliente mandou — `mode`, `working_dir`,
            # outro agente, ou nada — não entra.
            corpo = json.dumps({"agent_id": AGENTE, "mode": modo.NOME}).encode()
        elif request.method == "POST":
            reescrito = _corpo_da_mensagem(await request.body())
            if isinstance(reescrito, JSONResponse):
                return reescrito
            corpo = reescrito

        na_sessao = _NA_SESSAO.fullmatch(alvo)
        if na_sessao:
            # Toda vez, e não só na criação: o modo de uma sessão muda por fora
            # da ponte, e a sessão que o app retoma do `estado.json` pode ser de
            # antes desta trava existir.
            try:
                m = await _cliente.get(
                    "/api/mode/current", timeout=MODO_TIMEOUT_S,
                    headers={**cabecalhos, "X-Session-Id": na_sessao.group(1)})
                atual = m.json().get("mode") if m.status_code == 200 else None
                conferido = m.status_code == 200
            except (httpx.HTTPError, ValueError, AttributeError):
                conferido, atual = False, None
            if not conferido:
                return _erro("gateway_unreachable", 502)
            if atual != modo.NOME:
                # Só o começo do id: basta para achar quem insiste, e o log não
                # vira um lugar de onde se copia uma sessão inteira.
                log.warning("ponte recusou sessão %s… fora do modo restrito "
                            "(modo=%r)", na_sessao.group(1)[:8], atual)
                return _erro("session_not_restricted", 404)

        # Query string só em leitura: num POST ela seria mais um corpo por onde
        # o cliente fala com o gateway sem passar pela reescrita acima.
        params = dict(request.query_params) if request.method == "GET" else None
        try:
            r = await _cliente.request(request.method, alvo, content=corpo,
                                       headers=cabecalhos, params=params)
            if criando and r.status_code == 400:
                # O gateway não conhece o modo: banco novo, ou modo apagado com
                # o companion no ar. Recria e tenta uma vez — se continuar 400,
                # o 400 atravessa e o robô fica sem cérebro, que é o lado certo
                # para errar.
                if await asyncio.to_thread(_garantir_modo, chave_gateway) != "indisponivel":
                    r = await _cliente.request(request.method, alvo, content=corpo,
                                               headers=cabecalhos)
        except httpx.HTTPError as e:
            return JSONResponse({"erro": f"gateway indisponível: {type(e).__name__}"},
                                status_code=502)

        if criando and r.is_success:
            # Gateway que ignora `mode` responde 201 e cria sessão sem política
            # nenhuma. Só vale a sessão que o gateway CONFIRMA estar no modo.
            try:
                criada = r.json()
            except ValueError:
                criada = None
            if not isinstance(criada, dict) or criada.get("mode") != modo.NOME:
                sid = criada.get("session_id") if isinstance(criada, dict) else None
                if isinstance(sid, str) and _UUID.fullmatch(sid):
                    try:
                        await _cliente.request("DELETE", f"/api/sessions/{sid}",
                                               headers=cabecalhos)
                    except httpx.HTTPError:
                        pass
                log.error("gateway criou sessão fora do modo `%s`; encerrada",
                          modo.NOME)
                return _erro("restricted_mode_not_enforced", 502)

        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))

    return app
