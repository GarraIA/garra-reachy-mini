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

import hmac
import logging
import re

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("companion.ponte")

PORTA = 8126
AGENTE = "reachy_voice"
GATEWAY = "http://127.0.0.1:3888"

# Só isto atravessa. `re.fullmatch`, e não `startswith`: um prefixo deixaria
# passar `/api/sessions/../algo`.
ROTAS = (
    ("GET", re.compile(r"/ping")),
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


def montar(token: str, chave_gateway: str | None) -> FastAPI:
    """Devolve o app da ponte já com o token e a chave real do gateway."""

    @app.api_route("/{caminho:path}",
                   methods=["GET", "POST", "DELETE", "OPTIONS"])
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
