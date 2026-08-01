"""Ponte estreita do robô para o gateway — o que NÃO se resolve abrindo a :3888.

O robô precisa falar com o gateway do Garra, que escuta em `127.0.0.1:3888`.
A saída óbvia seria trocar o `--host` do `garraia.service` para `0.0.0.0`.
Medi antes de fazer, e não dá:

  * `POST /api/sessions` responde **201 sem autenticação nenhuma** (o
    `/api/auth-check` diz `auth_required: true`, mas o REST não exige);
  * o agente padrão do gateway tem **`bash`** entre as 27 ferramentas de runtime.

Somando os dois, `--host 0.0.0.0` entregaria execução de comando na máquina do
Michel a qualquer aparelho da rede. Um bug de configuração vira um shell remoto.

Então esta ponte expõe na LAN só o mínimo, e com três travas:

  1. **allowlist de rotas** — as quatro que o app do robô usa, e mais nada;
  2. **token obrigatório** — o mesmo `GARRA_VOZ_TOKEN` já gravado no robô;
  3. **`agent_id` forçado** para `reachy_voice` — mesmo que o robô peça outro,
     ou não peça nenhum. É o que impede a ponte de alcançar o agente padrão,
     que é o que tem `bash`.

O gateway continua intocado: nem recompilado, nem reiniciado, nem exposto.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time

import httpx

from . import agente
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("companion.ponte")

PORTA = 8126
AGENTE = "reachy_voice"
GATEWAY = "http://127.0.0.1:3888"

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

app = FastAPI(title="Garra Reachy — ponte do robô", docs_url=None, redoc_url=None)
_cliente: httpx.AsyncClient | None = None


def _permitida(metodo: str, caminho: str) -> bool:
    return any(m == metodo and p.fullmatch(caminho) for m, p in ROTAS)


def _autorizado(request: Request, token: str) -> bool:
    cab = request.headers.get("authorization") or ""
    enviado = cab[7:] if cab.lower().startswith("bearer ") else ""
    return bool(enviado) and hmac.compare_digest(enviado, token)


def _ping_liberado(agora: float | None = None) -> bool:
    """Janela deslizante de um minuto. Barata, e não guarda quem chamou."""
    agora = time.monotonic() if agora is None else agora
    corte = agora - 60.0
    _ping_janela[:] = [t for t in _ping_janela if t > corte]
    if len(_ping_janela) >= PING_MAX_POR_MINUTO:
        return False
    _ping_janela.append(agora)
    return True


def montar(token: str, chave_gateway: str | None) -> FastAPI:
    """Devolve o app da ponte já com o token e a chave real do gateway."""

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

        corpo = await request.body()
        # Força o agente. Sem isto o robô — ou qualquer um com o token — poderia
        # pedir o agente padrão do gateway, que tem `bash`.
        if corpo and request.method == "POST":
            try:
                import json

                dados = json.loads(corpo)
                if isinstance(dados, dict):
                    dados["agent_id"] = AGENTE
                    corpo = json.dumps(dados).encode()
            except ValueError:
                return JSONResponse({"erro": "corpo inválido"}, status_code=400)

        if _cliente is None:
            _cliente = httpx.AsyncClient(base_url=GATEWAY, timeout=180.0)
        cabecalhos = {"Content-Type": "application/json"}
        if chave_gateway:
            cabecalhos["Authorization"] = f"Bearer {chave_gateway}"
        try:
            r = await _cliente.request(request.method, alvo, content=corpo or None,
                                       headers=cabecalhos,
                                       params=dict(request.query_params))
        except httpx.HTTPError as e:
            return JSONResponse({"erro": f"gateway indisponível: {type(e).__name__}"},
                                status_code=502)
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"))

    return app
