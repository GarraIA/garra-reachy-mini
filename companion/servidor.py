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

from . import agente, agente_teste, ponte, reachy, seguranca, unidade, voz

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


# ── ritmo da conversa ────────────────────────────────────────────────────────
# Só transporte. A configuração vive no `config.json` do robô e em lugar nenhum
# mais; estas rotas repassam a chamada e devolvem o que o robô confirmou.
def _painel_de(corpo_ou_query: str | None):
    painel = (corpo_ou_query or "").strip()
    if not painel.startswith("http://"):
        return None
    return painel.rstrip("/")


def _falha(e: Exception, painel: str) -> JSONResponse:
    """Traduz a exceção num código ESTÁVEL, que o painel possa ler.

    O frontend não deve depender do número HTTP: `error.code` é o contrato.
    Cada código pede uma reação diferente — repetir sozinho, oferecer
    reconexão, ou parar de tentar — e colapsar todos em "indisponível" foi
    exatamente o que fez o painel dizer "robô indisponível" para um robô que
    estava conectado e respondendo.
    """
    if isinstance(e, reachy.RecursoNaoSuportado):
        return JSONResponse(
            {"ok": False, "reachable": True, "supported": False,
             "robot_version": e.robot_version,
             "robot": reachy.identidade(painel),
             "error": {"code": "robot_feature_unsupported",
                       "upstream_status": e.upstream_status}},
            status_code=501)
    status = getattr(getattr(e, "response", None), "status_code", None)
    if status in (401, 403):
        # Token errado não é robô offline: reconectar não resolve, e oferecer
        # reconexão mandaria o usuário para o lado errado do problema.
        return JSONResponse(
            {"ok": False, "reachable": True, "supported": True,
             "error": {"code": "robot_auth_failed", "upstream_status": status}},
            status_code=502)
    if status is not None:
        return JSONResponse(
            {"ok": False, "reachable": True, "supported": True,
             "error": {"code": "robot_error", "upstream_status": status,
                       # Tipo da exceção, nunca o corpo: ele pode trazer
                       # configuração do robô.
                       "detail": type(e).__name__}},
            status_code=502)
    return JSONResponse(
        {"ok": False, "reachable": False, "supported": None,
         "error": {"code": "robot_unreachable", "detail": type(e).__name__}},
        status_code=502)


@app.get("/api/reachy/conversation/settings")
async def conversa_ler(panel: str | None = None):
    painel = _painel_de(panel)
    if painel is None:
        return JSONResponse({"ok": False, "erro": "informe `panel`"}, status_code=400)
    try:
        return {"ok": True, **await asyncio.to_thread(reachy.conversa_ler, painel)}
    except Exception as e:
        return _falha(e, painel)


@app.put("/api/reachy/conversation/settings")
async def conversa_gravar(corpo: dict):
    painel = _painel_de(corpo.get("panel"))
    if painel is None:
        return JSONResponse({"ok": False, "erro": "informe `panel`"}, status_code=400)
    mudancas = {k: v for k, v in corpo.items() if k != "panel"}
    mudancas.setdefault("updated_by", "garra-dashboard")
    try:
        return {"ok": True,
                **await asyncio.to_thread(reachy.conversa_gravar, painel, mudancas)}
    except reachy.ConflitoConversa as e:
        # 409 intacto: quem escreveu por último no outro painel não é
        # sobrescrito em silêncio.
        return JSONResponse({"ok": False, "conflict": True, **e.atual},
                            status_code=409)
    except Exception as e:
        return _falha(e, painel)


@app.get("/api/reachy/conversation/status")
async def conversa_estado(panel: str | None = None):
    painel = _painel_de(panel)
    if painel is None:
        return JSONResponse({"ok": False, "erro": "informe `panel`"}, status_code=400)
    try:
        return {"ok": True, "reachable": True, "supported": True,
                **await asyncio.to_thread(reachy.conversa_estado, painel)}
    except Exception as e:
        return _falha(e, painel)


# ── identidade do agente ─────────────────────────────────────────────────────
# Nome e personalidade vivem no `config.yml` do gateway, e só lá. Estas rotas
# são o único escritor: o painel do robô chega por aqui pela ponte, em vez de
# guardar uma cópia que sobreviveria a um restore e sobrescreveria o gateway
# com um valor velho. O `system_prompt` (núcleo protegido) não é exposto.
@app.get("/api/reachy/agent-identity")
async def agente_ler():
    try:
        return {"ok": True, **await asyncio.to_thread(agente.ler)}
    except agente.ErroAgente as e:
        return JSONResponse({"ok": False, "error": {"code": "gateway_config_error",
                                                    "detail": str(e)}},
                            status_code=503)


@app.put("/api/reachy/agent-identity")
async def agente_gravar(corpo: dict):
    try:
        return {"ok": True, **await asyncio.to_thread(
            agente.gravar, corpo, str(corpo.get("updated_by") or "garra-dashboard"))}
    except agente.ConflitoAgente as e:
        # 409 com o estado atual: quem editou pelo outro painel não é
        # sobrescrito em silêncio.
        return JSONResponse({"ok": False, "conflict": True, **e.atual}, status_code=409)
    except agente.ErroAgente as e:
        return JSONResponse({"ok": False, "error": {"code": "invalid_identity",
                                                    "detail": str(e)}},
                            status_code=400)


@app.post("/api/reachy/agent-identity/reset")
async def agente_restaurar(corpo: dict | None = None):
    corpo = corpo or {}
    try:
        return {"ok": True, **await asyncio.to_thread(
            agente.restaurar, corpo.get("revision"),
            str(corpo.get("updated_by") or "garra-dashboard"))}
    except agente.ConflitoAgente as e:
        return JSONResponse({"ok": False, "conflict": True, **e.atual}, status_code=409)
    except agente.ErroAgente as e:
        return JSONResponse({"ok": False, "error": {"code": "invalid_identity",
                                                    "detail": str(e)}},
                            status_code=400)


@app.post("/api/reachy/agent-identity/test")
async def agente_testar(corpo: dict | None = None):
    """Sessão temporária, só texto. Não move o robô e não toca a sessão de voz."""
    corpo = corpo or {}
    perguntas = corpo.get("questions")
    try:
        return {"ok": True, **await asyncio.to_thread(
            agente_teste.executar, _chave_do_gateway(), perguntas)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": {"code": "test_failed",
                                                    "detail": type(e).__name__}},
                            status_code=502)


def _chave_do_gateway() -> str | None:
    """Chave real do gateway, lida do config.yml do Garra nesta máquina."""
    import pathlib

    try:
        import yaml

        arq = pathlib.Path("~/.config/garraia/config.yml").expanduser()
        dados = yaml.safe_load(arq.read_text(encoding="utf-8")) or {}
        chave = (dados.get("gateway") or {}).get("api_key")
        return str(chave) if chave else None
    except Exception:
        return None


def main() -> int:
    import threading

    import uvicorn

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    token = voz.token()  # cria o token 0600 na primeira execução

    # Dois servidores no mesmo processo, de propósito:
    #   127.0.0.1:8125  administração (liga/desliga serviço) — nunca sai daqui;
    #   0.0.0.0:8126    ponte do robô — quatro rotas, com token e agente fixo.
    # Ver companion/ponte.py para por que a alternativa (abrir a :3888) não é
    # aceitável.
    lan = ponte.montar(token, _chave_do_gateway())
    cfg = uvicorn.Config(lan, host="0.0.0.0", port=ponte.PORTA, log_level="warning")
    threading.Thread(target=uvicorn.Server(cfg).run, daemon=True,
                     name="ponte-robo").start()

    log.info("companion em http://%s:%d · ponte do robô em http://%s:%d · voz em %s",
             HOST, PORTA, voz.ip_lan(), ponte.PORTA, voz.url_lan())
    uvicorn.run(app, host=HOST, port=PORTA, log_level="warning")
    return 0
