"""API do companion — o que o console do Garra em :3888 chama.

Escuta em `127.0.0.1:8125`, porta **fixa**: a página precisa de um endereço
determinístico para o `fetch`, e uma porta que muda seria um problema de
descoberta dentro de um serviço cuja razão de existir é resolver descoberta.

Ele não abre `ReachyMini`, não toca no lock e não fala com o hardware. Administra
processos do desktop e escreve configuração no robô por HTTP — o dono do robô
continua sendo o app instalado nele.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import reachy, seguranca, unidade, voz

HOST = "127.0.0.1"
PORTA = 8125

log = logging.getLogger("companion")
app = FastAPI(title="Garra Reachy companion", docs_url=None, redoc_url=None)


@app.middleware("http")
async def porteiro(request: Request, call_next):
    origem = request.headers.get("origin")
    if not seguranca.origem_confiavel(request.headers.get("host"), origem):
        return JSONResponse({"erro": "origem não autorizada"}, status_code=403)
    resposta = await call_next(request)
    if origem:
        resposta.headers["Access-Control-Allow-Origin"] = origem
        resposta.headers["Vary"] = "Origin"
    return resposta


@app.options("/api/{resto:path}", include_in_schema=False)
async def preflight(resto: str, request: Request):
    origem = request.headers.get("origin") or "*"
    return JSONResponse(None, status_code=204, headers={
        "Access-Control-Allow-Origin": origem,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
    })


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "garra-reachy-companion", "port": PORTA}


# ── voz ──────────────────────────────────────────────────────────────────────
@app.get("/api/voice/status")
async def voice_status():
    return await asyncio.to_thread(voz.estado)


async def _acionar(verbo: str):
    # Idempotente: pedir start com a unidade já ativa é sucesso, não erro.
    if verbo == "start" and await asyncio.to_thread(unidade.ativa):
        return {"ok": True, "verb": verbo, "message": "já estava no ar", "noop": True}
    if verbo == "stop" and not await asyncio.to_thread(unidade.ativa):
        return {"ok": True, "verb": verbo, "message": "já estava parado", "noop": True}
    try:
        return await asyncio.to_thread(unidade.agir, verbo)
    except unidade.ErroUnidade as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)


# Rotas explícitas, e não `/api/voice/{verbo}`: um path param aqui capturaria
# `/api/voice/test` e `/api/voice/logs` antes de elas serem tentadas, porque o
# FastAPI casa na ordem de registro. Já aconteceu.
@app.post("/api/voice/start")
async def voice_start():
    return await _acionar("start")


@app.post("/api/voice/stop")
async def voice_stop():
    return await _acionar("stop")


@app.post("/api/voice/restart")
async def voice_restart():
    return await _acionar("restart")


@app.post("/api/voice/autostart")
async def voice_autostart(corpo: dict):
    ligado = bool(corpo.get("enabled"))
    try:
        r = await asyncio.to_thread(unidade.agir, "enable" if ligado else "disable")
    except unidade.ErroUnidade as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)
    # Desligar o auto-start NÃO para a voz que já está rodando: são coisas
    # diferentes, e parar por tabela surpreenderia quem só queria mudar o
    # comportamento do próximo arranque.
    r["auto_start"] = await asyncio.to_thread(unidade.auto_start)
    r["still_running"] = await asyncio.to_thread(unidade.ativa)
    return r


@app.get("/api/voice/logs")
async def voice_logs(lines: int = 60):
    return {"lines": await asyncio.to_thread(unidade.registro, lines),
            "note": "transcrições e segredos são filtrados"}


@app.post("/api/voice/test")
async def voice_test():
    return await asyncio.to_thread(voz.testar)


# ── robô ─────────────────────────────────────────────────────────────────────
@app.get("/api/reachy/discover")
async def reachy_discover(timeout: float = 4.0):
    robos = await asyncio.to_thread(reachy.procurar, timeout)
    return {"robots": robos, "desktop_ip": voz.ip_lan()}


@app.post("/api/reachy/configure")
async def reachy_configure(corpo: dict):
    painel = (corpo.get("panel") or "").strip()
    if not painel.startswith("http://"):
        return JSONResponse(
            {"ok": False, "erro": "informe o painel do robô (campo `panel`). "
                                  "Com mais de um robô na rede, a escolha é sua."},
            status_code=400)
    try:
        return await asyncio.to_thread(reachy.configurar, painel)
    except (reachy.ErroReachy, Exception) as e:  # noqa: B014 - erro vira resposta
        return JSONResponse({"ok": False, "erro": f"{type(e).__name__}: {e}"[:300]},
                            status_code=502)


def main() -> int:
    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    voz.token()  # cria o token 0600 na primeira execução
    log.info("companion em http://%s:%d — voz em %s", HOST, PORTA, voz.url_lan())
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
    return 0
