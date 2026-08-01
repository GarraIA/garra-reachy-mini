"""Rotas HTTP e WebSocket do painel do Reachy.

Montado no FastAPI que o `ReachyMiniApp` já sobe (o mesmo que serve
`/api/config`). Tudo o que mexe no robô passa pelo `ControladorRobo`; esta
camada só traduz HTTP em pedido e evento em JSON.

Regra de thread: o controlador é síncrono e bloqueante de propósito (é ele que
serializa o acesso ao robô). Toda chamada a ele sai da event loop por
`asyncio.to_thread` — senão um `dance` de 20 s congelaria o servidor inteiro,
inclusive o botão de parada de emergência.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import (
    APIRouter,
    Body,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)

from .. import armazenamento, build_info, conversa
from ..robo.acoes import PRIO_AMBIENTE, ControladorRobo
from ..robo.barramento import Barramento
from ..servicos import Servicos
from .camera import FrameHub
from .chat import ErroChat, PonteChat
from .seguranca import Limitador, Politica, origem_permitida, token_valido

log = logging.getLogger("garra_reachy_mini.web.api")

# Rotas que passam sem token mesmo com a API na rede. A lista é curta de
# propósito: só a parada de emergência, porque um botão de pânico que responde
# 401 é um risco de segurança física maior do que o acesso que ele barraria.
SEM_TOKEN = frozenset({"/api/robot/stop"})

LIMITE_MJPEG = "quadro-garraia-reachy"
INTERVALO_POLL_EVENTOS_S = 0.05
PING_WS_S = 20.0


@dataclass
class ContextoWeb:
    """Tudo o que as rotas precisam. Montado uma vez pelo app."""

    controlador: ControladorRobo
    hub: FrameHub
    eventos: Barramento
    politica: Politica
    chat: PonteChat | None = None
    # Quais subsistemas estão de pé. Sem isto o painel não tem como distinguir
    # "sem voz porque você não configurou" de "sem voz porque quebrou".
    servicos: Servicos = field(default_factory=Servicos)
    limitador: Limitador = field(default_factory=Limitador)
    # Registrado pelo loop de voz: é o único que pode tocar áudio no robô.
    falar: Callable[[str], Awaitable[None]] | None = None
    # Corta o áudio em curso. Separado de `falar` porque a parada de emergência
    # precisa silenciar o robô sem esperar a frase atual terminar.
    calar: Callable[[str], None] | None = None
    # Abre uma sessão limpa no gateway. Registrado pelo loop de voz, porque é
    # ele que tem o cérebro; sem isto, a retomada de sessão não tem escape.
    nova_sessao: Callable[[], Awaitable[str | None]] | None = None
    dir_estatico: Path | None = None
    iniciado_em: float = field(default_factory=time.monotonic)


def _erro(status: int, mensagem: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"ok": False, "error": mensagem})


def preparar(app: FastAPI, politica: Politica, limitador: Limitador | None = None) -> None:
    """Instala o porteiro. **Tem de rodar antes de o servidor subir.**

    Starlette monta a pilha de middleware no arranque e recusa alterações depois
    (`Cannot add middleware after an application has started`). O `ReachyMiniApp`
    sobe o uvicorn ANTES de chamar `run()`, e é só dentro do `run()` que existe
    uma conexão com o robô para montar o controlador. Daí a separação: o
    middleware entra cedo e lê o contexto de `app.state` quando ele aparecer.
    Rota, ao contrário de middleware, pode ser acrescentada com o servidor de pé.
    """
    app.state.reachy = None
    app.state.reachy_politica = politica
    app.state.reachy_limitador = limitador or Limitador()
    METODOS_MUTANTES = {"POST", "PUT", "PATCH", "DELETE"}

    @app.middleware("http")
    async def porteiro(request: Request, call_next: Any) -> Response:
        caminho = request.url.path
        origem = request.headers.get("origin")
        pol: Politica = request.app.state.reachy_politica

        # `/api/config` guarda a chave do gateway: escrever nele sem token na
        # rede seria entregar a configuração do app a qualquer um da LAN.
        if not caminho.startswith(("/api/robot", "/api/chat", "/api/config", "/ws/")):
            return await call_next(request)

        if request.app.state.reachy is None and not caminho.startswith("/api/config"):
            return JSONResponse(
                {"ok": False, "error": "o controlador do robô ainda está subindo"},
                status_code=503,
            )

        # A parada de emergência nunca pede credencial. Um botão de pânico que
        # falha por 401 é pior do que a API aberta que ele protegeria: quem
        # alcança o robô fisicamente precisa conseguir pará-lo.
        if pol.exige_token() and caminho not in SEM_TOKEN and not token_valido(
            request.headers.get("authorization"),
            request.query_params.get("token"),
            pol,
        ):
            return JSONResponse({"ok": False, "error": "token inválido"}, status_code=401)

        anfitriao = request.headers.get("host")
        if request.method in METODOS_MUTANTES:
            if not origem_permitida(origem, pol, anfitriao):
                return JSONResponse(
                    {"ok": False, "error": f"origem não autorizada: {origem}"},
                    status_code=403,
                )
            cliente = request.client.host if request.client else "?"
            if not request.app.state.reachy_limitador.permitir(cliente):
                return JSONResponse(
                    {"ok": False, "error": "muitos pedidos; tente de novo em instantes"},
                    status_code=429,
                )

        resposta = await call_next(request)
        if origem and origem_permitida(origem, pol, anfitriao):
            resposta.headers["Access-Control-Allow-Origin"] = origem
            resposta.headers["Access-Control-Allow-Credentials"] = "true"
            resposta.headers["Vary"] = "Origin"
        return resposta

    @app.options("/api/{resto:path}", include_in_schema=False)
    async def preflight(resto: str, request: Request) -> Response:
        origem = request.headers.get("origin")
        if not origem_permitida(origem, request.app.state.reachy_politica,
                                request.headers.get("host")):
            raise _erro(403, "origem não autorizada")
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": origem or "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "600",
            },
        )


def montar(app: FastAPI, ctx: ContextoWeb) -> None:
    """Acrescenta as rotas do robô. Pode rodar com o servidor já de pé."""
    if not hasattr(app.state, "reachy_politica"):
        # Caminho de quem monta tudo de uma vez (modo simulado, testes).
        preparar(app, ctx.politica, ctx.limitador)
    app.state.reachy = ctx
    app.state.reachy_politica = ctx.politica
    app.state.reachy_limitador = ctx.limitador
    app.include_router(_rotas_robo(ctx))
    app.include_router(_rotas_chat(ctx))
    _montar_ws(app, ctx)
    _montar_painel(app, ctx)


# ─── rotas do robô ───────────────────────────────────────────────────────────
def _rotas_robo(ctx: ContextoWeb) -> APIRouter:
    r = APIRouter(prefix="/api/robot", tags=["robot"])
    ctrl = ctx.controlador

    @r.get("/status")
    async def status() -> dict[str, Any]:
        dados = await asyncio.to_thread(ctrl.status)
        dados["camera"] = ctx.hub.status()
        dados["uptime_s"] = round(time.monotonic() - ctx.iniciado_em, 1)
        dados.update(ctx.servicos.json())
        dados["voice"] = {"tts_disponivel": ctx.falar is not None}
        # Quem é este app e o que ele sabe fazer. O console em `:3888` decide
        # por `capabilities`, não por número de versão: comparar versões o
        # obrigaria a saber em qual release cada recurso entrou.
        dados.update(build_info.identidade())
        dados["chat"] = {
            "gateway": ctx.chat.base if ctx.chat else None,
            "agent_id": ctx.chat.agent_id if ctx.chat else None,
            "session_id": ctx.chat.sessao if ctx.chat else None,
        }
        dados["network"] = {
            "bind": ctx.politica.host,
            "port": ctx.politica.porta,
            "remote": ctx.politica.remoto,
            "token_required": ctx.politica.exige_token(),
        }
        return dados

    @r.get("/capabilities")
    async def capacidades() -> dict[str, Any]:
        return await asyncio.to_thread(ctrl.capacidades)

    @r.get("/queue")
    async def fila() -> dict[str, Any]:
        return await asyncio.to_thread(ctrl.fila)

    @r.post("/action")
    async def acao(corpo: dict[str, Any] = Body(...)) -> JSONResponse:
        nome = corpo.get("action")
        if not isinstance(nome, str) or not nome:
            raise _erro(400, "informe `action`")
        params = {k: v for k, v in corpo.items()
                  if k not in ("action", "wait", "source", "correlation_id", "priority")}
        esperar = bool(corpo.get("wait", True))
        prioridade = corpo.get("priority")
        resultado = await asyncio.to_thread(
            ctrl.submeter, nome, params,
            prioridade=prioridade if isinstance(prioridade, int) else None,
            source=str(corpo.get("source") or "painel"),
            correlation_id=corpo.get("correlation_id"),
            esperar=esperar,
        )
        # 200 mesmo quando a ação falhou: o corpo é que carrega ok/executed. Só
        # rejeição de entrada vira 4xx, porque aí não houve ação nenhuma.
        codigo = 200 if resultado.accepted else 400
        return JSONResponse(resultado.json(), status_code=codigo)

    @r.post("/stop")
    async def parar(corpo: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        corpo = corpo or {}
        if ctx.calar is not None:
            # Parar o corpo e continuar falando seria contraditório.
            await asyncio.to_thread(ctx.calar, "stop")
        resultado = await asyncio.to_thread(
            ctrl.parar_tudo,
            source=str(corpo.get("source") or "painel"),
            correlation_id=corpo.get("correlation_id"),
        )
        return resultado.json()

    @r.post("/clear-estop")
    async def liberar() -> dict[str, Any]:
        return (await asyncio.to_thread(ctrl.limpar_estop, source="painel")).json()

    @r.post("/neutral")
    async def neutro(corpo: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        corpo = corpo or {}
        resultado = await asyncio.to_thread(
            ctrl.submeter, "return_to_neutral",
            {"duration": corpo.get("duration")} if corpo.get("duration") else {},
            source="painel",
        )
        return resultado.json()

    @r.post("/tracking")
    async def tracking(corpo: dict[str, Any] = Body(...)) -> dict[str, Any]:
        ligado = bool(corpo.get("enabled", True))
        params: dict[str, Any] = {"enabled": ligado}
        if corpo.get("weight") is not None:
            params["weight"] = corpo["weight"]
        resultado = await asyncio.to_thread(
            ctrl.submeter, "face_tracking", params, source="painel"
        )
        return resultado.json()

    # ── câmera ──────────────────────────────────────────────────────────────
    @r.get("/camera/status")
    async def camera_status() -> dict[str, Any]:
        return ctx.hub.status()

    @r.get("/camera/snapshot")
    async def instantaneo() -> Response:
        quadro = await asyncio.to_thread(ctx.hub.instantaneo)
        if quadro is None:
            raise _erro(503, "a câmera do robô não está disponível")
        return Response(
            content=quadro.jpeg,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Seq": str(quadro.seq),
                "X-Frame-Stale": "1" if quadro.obsoleto else "0",
            },
        )

    @r.get("/camera/stream")
    async def stream(fps: float = Query(default=12.0, ge=1.0, le=30.0)) -> StreamingResponse:
        if not ctx.hub.entrar():
            raise _erro(429, "limite de espectadores da câmera atingido")

        async def quadros():
            ultimo = -1
            periodo = 1.0 / fps
            try:
                while True:
                    quadro = await asyncio.to_thread(ctx.hub.esperar_proximo, ultimo, 5.0)
                    if quadro is None:
                        # Sem quadro novo em 5 s: mantém a conexão viva em vez de
                        # derrubar o <img> do painel.
                        await asyncio.sleep(0.2)
                        continue
                    ultimo = quadro.seq
                    yield (
                        b"--" + LIMITE_MJPEG.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(quadro.jpeg)).encode() + b"\r\n\r\n"
                        + quadro.jpeg + b"\r\n"
                    )
                    await asyncio.sleep(periodo)
            finally:
                ctx.hub.sair()

        return StreamingResponse(
            quadros(),
            media_type=f"multipart/x-mixed-replace; boundary={LIMITE_MJPEG}",
            headers={"Cache-Control": "no-store", "Connection": "close"},
        )

    # ── apps do robô ────────────────────────────────────────────────────────
    @r.get("/apps")
    async def apps() -> dict[str, Any]:
        resultado = await asyncio.to_thread(ctrl.submeter, "list_apps", {}, source="painel")
        if not resultado.ok:
            raise _erro(503, resultado.message)
        return resultado.data

    @r.post("/apps/{nome}/start")
    async def app_iniciar(nome: str) -> dict[str, Any]:
        return (await asyncio.to_thread(
            ctrl.submeter, "start_app", {"name": nome}, source="painel"
        )).json()

    @r.post("/apps/stop")
    async def app_parar() -> dict[str, Any]:
        return (await asyncio.to_thread(ctrl.submeter, "stop_app", {}, source="painel")).json()

    @r.post("/apps/restart")
    async def app_reiniciar() -> dict[str, Any]:
        return (await asyncio.to_thread(ctrl.submeter, "restart_app", {}, source="painel")).json()

    # ── observabilidade ─────────────────────────────────────────────────────
    @r.get("/events")
    async def eventos(limite: int = Query(default=100, ge=1, le=400),
                      tipos: str | None = None) -> dict[str, Any]:
        # `tipos` (separados por vírgula) existe para o painel poder acompanhar
        # só as métricas de turno sem baixar 400 eventos a cada segundo.
        filtro = frozenset(t for t in (tipos or "").split(",") if t) or None
        return {"events": ctx.eventos.historico(limite, filtro)}

    @r.get("/logs")
    async def logs(limite: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        return {
            "errors": ctrl.erros(limite),
            "events": ctx.eventos.historico(limite),
        }

    # ── ritmo da conversa ───────────────────────────────────────────────────
    # Este arquivo é a ÚNICA cópia persistente da configuração: o console em
    # :3888 escreve aqui pelo companion, e o painel do robô escreve aqui direto.
    # Um segundo armazenamento no desktop criaria duas verdades e um laço de
    # sincronização — e o comportamento roda aqui, então aqui é o lugar.
    # ── diagnóstico de runtime ─────────────────────────────────────────────
    # O venv dos apps é COMPARTILHADO: outro app pode trocar uma dependência
    # sem este ficar sabendo, e não há shell no robô para um `pip freeze`.
    # Passa pelo mesmo porteiro do resto de `/api/robot` — token exigido quando
    # a API está na rede. Responde "este app tem o que precisa?" e nada além:
    # sem lista de pacotes do robô, sem caminhos, sem variáveis de ambiente.
    @r.get("/diagnostics/runtime")
    async def diagnostico_runtime() -> dict[str, Any]:
        return await asyncio.to_thread(build_info.diagnostico)

    @r.get("/conversation")
    async def conversa_ler() -> dict[str, Any]:
        return await asyncio.to_thread(_ler_conversa)

    @r.put("/conversation")
    async def conversa_gravar(corpo: dict[str, Any] = Body(...)) -> Any:
        try:
            return await asyncio.to_thread(_gravar_conversa, corpo)
        except _Conflito as e:
            # 409 com o estado atual no corpo: o cliente recarrega e avisa, em
            # vez de sobrescrever em silêncio uma alteração feita no outro painel.
            return JSONResponse(e.atual, status_code=409)
        except ValueError as e:
            raise _erro(400, str(e)) from e

    # ── identidade do assistente (interface REMOTA) ──────────────────────────
    # Ao contrário do ritmo da conversa, isto NÃO mora aqui. O agente vive na
    # configuração do Garra, e é lá que fica: uma cópia no robô sobreviveria a
    # um restore do `config.yml` e o sobrescreveria com um valor velho. Estas
    # rotas só repassam para a ponte, que é quem escreve — e sem Garra no ar
    # não há leitura, nem cache, nem alteração pendente.
    def _identidade(metodo: str, sufixo: str = "", corpo: dict | None = None):
        import requests

        cfg = armazenamento.carregar_config()
        base = str(cfg.get("gateway_url") or "").rstrip("/")
        chave = cfg.get("gateway_key")
        if not base:
            raise _erro(503, "o Garra não está configurado neste robô")
        cabecalhos = {"Authorization": f"Bearer {chave}"} if chave else {}
        try:
            resp = requests.request(
                metodo, f"{base}/api/agent-identity{sufixo}",
                json=corpo, headers=cabecalhos, timeout=120)
        except requests.RequestException as e:
            raise _erro(503, f"o Garra não respondeu ({type(e).__name__})") from e
        dados = resp.json() if resp.content else {}
        if resp.status_code == 409:
            raise _Conflito(dados)
        if not resp.ok:
            detalhe = ((dados.get("error") or {}).get("detail")
                       if isinstance(dados, dict) else None)
            raise _erro(resp.status_code if resp.status_code < 500 else 502,
                        detalhe or "o Garra recusou a alteração")
        return dados

    @r.get("/agent-identity")
    async def identidade_ler() -> dict[str, Any]:
        return await asyncio.to_thread(_identidade, "GET")

    @r.put("/agent-identity")
    async def identidade_gravar(corpo: dict[str, Any] = Body(...)) -> Any:
        try:
            return await asyncio.to_thread(_identidade, "PUT", "", corpo)
        except _Conflito as e:
            return JSONResponse(e.atual, status_code=409)

    @r.post("/agent-identity/reset")
    async def identidade_restaurar(corpo: dict[str, Any] | None = Body(None)) -> Any:
        try:
            return await asyncio.to_thread(_identidade, "POST", "/reset", corpo or {})
        except _Conflito as e:
            return JSONResponse(e.atual, status_code=409)

    @r.post("/conversation/session")
    async def conversa_sessao_nova() -> dict[str, Any]:
        """Recomeça a conversa: sessão limpa no gateway, sem histórico herdado.

        Existe porque a retomada entre arranques não tinha escape. Para o uso
        normal, retomar é o certo — ninguém quer que o robô esqueça tudo quando
        o app reinicia. Para medir, é o contrário: um turno anterior em que o
        modelo olhou pela câmera enviesa o turno seguinte, e o `DELETE` do
        gateway é logout, não remoção, então o histórico velho não sai do
        caminho sozinho.
        """
        if ctx.nova_sessao is None:
            raise _erro(503, "o cérebro ainda não está pronto")
        sessao = await ctx.nova_sessao()
        if sessao is None:
            raise _erro(502, "o gateway não criou a sessão")
        # `""` é sucesso sem id: as reservas (`garra ask`, OpenRouter) não têm
        # sessão no servidor, só o histórico local, que foi limpo.
        return {"ok": True, "session_id": sessao or None}

    return r


class _Conflito(RuntimeError):
    def __init__(self, atual: dict[str, Any]) -> None:
        super().__init__("revisão desatualizada")
        self.atual = atual


def _ler_conversa() -> dict[str, Any]:
    salvo = armazenamento.carregar_config()
    conf = conversa.normalizar(salvo.get("conversation"))
    arranque = conversa.normalizar_arranque(salvo.get("startup"))
    return {
        "conversation": conf,
        # `startup` é irmão de `conversation`, não filho: a saudação não
        # pertence a turno nenhum. Mas viaja na mesma leitura e na mesma
        # revisão, senão os dois painéis precisariam de dois ciclos de
        # concorrência otimista para uma tela só.
        "startup": arranque,
        "effective": conversa.Politica.de(conf, arranque).__dict__,
        "updated_at": salvo.get("conversation_updated_at"),
        "updated_by": salvo.get("conversation_updated_by"),
    }


def _gravar_conversa(corpo: dict[str, Any]) -> dict[str, Any]:
    salvo = armazenamento.carregar_config()
    atual = conversa.normalizar(salvo.get("conversation"))

    esperada = corpo.get("revision")
    if esperada is not None and int(esperada) != atual["revision"]:
        raise _Conflito(_ler_conversa())

    mudancas = {k: v for k, v in corpo.items()
                if k not in ("revision", "updated_by", "startup")}
    novo = conversa.perfil_atualizado(atual, mudancas)
    novo["revision"] = atual["revision"] + 1

    # O bloco `startup` chega aninhado e é gravado junto, sob a MESMA revisão:
    # desligar tudo numa tela é uma escrita só, e ou vale inteira ou dá 409.
    if isinstance(corpo.get("startup"), dict):
        salvo["startup"] = conversa.normalizar_arranque(
            {**conversa.normalizar_arranque(salvo.get("startup")),
             **corpo["startup"]})

    salvo["conversation"] = novo
    salvo["conversation_updated_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    salvo["conversation_updated_by"] = str(corpo.get("updated_by") or "unknown")[:60]
    # Gravação atômica (mkstemp + os.replace) já vem do armazenamento: o arquivo
    # anterior sobrevive se a escrita falhar no meio.
    armazenamento.salvar_config(salvo)
    return _ler_conversa()


# ─── rotas de chat ───────────────────────────────────────────────────────────
def _rotas_chat(ctx: ContextoWeb) -> APIRouter:
    r = APIRouter(prefix="/api/chat", tags=["chat"])

    def ponte() -> PonteChat:
        if ctx.chat is None:
            raise _erro(503, "o chat não está configurado")
        return ctx.chat

    @r.get("/status")
    async def status() -> dict[str, Any]:
        p = ctx.chat
        if p is None:
            return {"available": False, "reason": "não configurado"}
        return {
            "available": await p.disponivel(),
            "gateway": p.base,
            "agent_id": p.agent_id,
            "session_id": p.sessao,
            "streaming": False,
        }

    @r.post("/enviar")
    async def enviar(corpo: dict[str, Any] = Body(...)) -> dict[str, Any]:
        texto = str(corpo.get("content") or "").strip()
        if not texto:
            raise _erro(400, "mensagem vazia")
        if len(texto) > 8000:
            raise _erro(413, "mensagem grande demais")
        correlacao = corpo.get("correlation_id")
        ctx.eventos.publicar(
            "chat.message", correlation_id=correlacao, role="user", content=texto,
            source="painel",
        )
        try:
            resposta = await ponte().enviar(texto)
        except ErroChat as e:
            ctx.eventos.publicar("chat.error", correlation_id=correlacao, error=str(e))
            raise _erro(502, str(e)) from e
        ctx.eventos.publicar(
            "chat.message", correlation_id=correlacao, role="assistant",
            content=resposta["content"], source="garra",
        )
        if bool(corpo.get("speak")) and ctx.falar is not None:
            asyncio.create_task(_falar_seguro(ctx, resposta["content"]))
        return resposta

    @r.get("/historico")
    async def historico() -> dict[str, Any]:
        try:
            return {"messages": await ponte().historico()}
        except ErroChat as e:
            raise _erro(502, str(e)) from e

    @r.post("/limpar")
    async def limpar() -> dict[str, Any]:
        antiga = await ponte().limpar()
        ctx.eventos.publicar("chat.cleared", previous_session=antiga)
        return {"ok": True, "previous_session": antiga}

    @r.post("/falar")
    async def falar(corpo: dict[str, Any] = Body(...)) -> dict[str, Any]:
        texto = str(corpo.get("text") or corpo.get("texto") or "").strip()
        if not texto:
            raise _erro(400, "texto vazio")
        if ctx.falar is None:
            raise _erro(503, "a síntese de voz não está disponível neste processo")
        await _falar_seguro(ctx, texto)
        return {"ok": True}

    return r


async def _falar_seguro(ctx: ContextoWeb, texto: str) -> None:
    if ctx.falar is None:
        return
    try:
        await ctx.falar(texto)
    except Exception as e:  # pragma: no cover
        log.warning("falha ao falar: %s", e)
        ctx.eventos.publicar("voice.error", error=str(e))


# ─── WebSocket de eventos ────────────────────────────────────────────────────
def _montar_ws(app: FastAPI, ctx: ContextoWeb) -> None:
    @app.websocket("/ws/eventos")
    async def eventos_ws(websocket: WebSocket) -> None:
        if ctx.politica.exige_token() and not token_valido(
            None, websocket.query_params.get("token"), ctx.politica
        ):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        assinatura = ctx.eventos.assinar()
        ultimo_ping = time.monotonic()
        try:
            estado = await asyncio.to_thread(ctx.controlador.status)
            await websocket.send_json({"type": "robot.status", **estado})
            while True:
                enviou = False
                # Drena tudo que houver: um cliente que voltou de um congelamento
                # recupera o atraso numa tacada só.
                while True:
                    evento = assinatura.obter(timeout=0)
                    if evento is None:
                        break
                    await websocket.send_json(evento.json())
                    enviou = True
                agora = time.monotonic()
                if not enviou and agora - ultimo_ping > PING_WS_S:
                    await websocket.send_json({"type": "ping", "dropped": assinatura.descartados})
                    ultimo_ping = agora
                await asyncio.sleep(INTERVALO_POLL_EVENTOS_S)
        except WebSocketDisconnect:
            pass
        except Exception as e:  # pragma: no cover
            log.debug("websocket de eventos encerrado: %s", e)
        finally:
            assinatura.fechar()


# ─── painel estático ─────────────────────────────────────────────────────────
def _liberar_raiz(app: FastAPI) -> None:
    """Tira o `GET /` que a classe base registrou, para pôr o nosso no lugar."""
    app.router.routes = [
        rota for rota in app.router.routes
        if not (getattr(rota, "path", None) == "/" and "GET" in getattr(rota, "methods", set()))
    ]


def _montar_painel_ausente(app: FastAPI) -> None:
    """Sem os estáticos no build, `/` e `/reachy` dizem por quê.

    Antes daqui `_montar_painel` só retornava, e o FastAPI respondia
    `{"detail":"Not Found"}` — exatamente o que ele responde para uma URL
    digitada errada. Custou uma rodada inteira no hardware descobrir que a
    causa era o `package-data` do `pyproject.toml` apontando para o nome antigo
    do pacote, e que `/` e `/reachy` estavam ambas ausentes (redirecionar uma
    para a outra não teria mudado nada).

    503, e não 404: o app está de pé e a API funciona; o que falta é este build.
    """
    faltando = build_info.ativos_do_painel()["missing"] or list(build_info.ATIVOS_PAINEL)
    lista = ", ".join(faltando)
    corpo = (
        "<!doctype html><meta charset=utf-8>"
        "<title>Painel não empacotado</title>"
        "<style>body{font:16px/1.6 system-ui;margin:3rem auto;max-width:34rem;"
        "padding:0 1rem}code{background:#eee;padding:.1em .3em;border-radius:3px}</style>"
        "<h1>Este build não traz o painel</h1>"
        f"<p>O app <code>{build_info.DISTRIBUICAO}</code> está no ar e a API responde, "
        "mas os arquivos da interface não vieram no pacote instalado.</p>"
        f"<p><b>Faltando em <code>static/</code>:</b> {lista}</p>"
        "<p>É defeito de empacotamento, não de configuração: confira "
        "<code>[tool.setuptools.package-data]</code> no <code>pyproject.toml</code> "
        "e reinstale. A API continua utilizável em <code>/api/robot/status</code>.</p>"
    )

    async def ausente() -> HTMLResponse:
        return HTMLResponse(corpo, status_code=503)

    _liberar_raiz(app)
    for rota in ("/", "/reachy"):
        app.add_api_route(rota, ausente, methods=["GET"], include_in_schema=False)


def _montar_painel(app: FastAPI, ctx: ContextoWeb) -> None:
    if ctx.dir_estatico is None:
        return
    painel = ctx.dir_estatico / "reachy.html"
    if not painel.exists():
        _montar_painel_ausente(app)
        return

    # No app real quem monta `/static` é o `ReachyMiniApp`; no modo simulado (e
    # nos testes) o FastAPI é nosso e ninguém montou ainda.
    if not any(getattr(r, "path", "").startswith("/static") for r in app.router.routes):
        from fastapi.staticfiles import StaticFiles

        app.mount("/static", StaticFiles(directory=ctx.dir_estatico), name="static")

    # A classe base já registrou `GET /` servindo `index.html` (a página de
    # configurações). O painel é a cara nova do app, então `/` passa a ser ele e
    # a antiga continua acessível em `/configuracoes`.
    _liberar_raiz(app)

    @app.get("/", include_in_schema=False)
    async def raiz() -> FileResponse:
        return FileResponse(painel)

    @app.get("/reachy", include_in_schema=False)
    async def reachy() -> FileResponse:
        return FileResponse(painel)

    indice = ctx.dir_estatico / "index.html"
    if indice.exists():
        @app.get("/configuracoes", include_in_schema=False)
        async def configuracoes() -> FileResponse:
            return FileResponse(indice)
