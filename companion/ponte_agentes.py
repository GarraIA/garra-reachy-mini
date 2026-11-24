"""A parte da ponte que conhece os agentes: escolher um, e conversar com ele.

O robô continua falando o protocolo de sessão de sempre. O que muda é quem
responde do outro lado:

* agente ativo é o **Garra** → nada aqui atende a conversa; ela segue pelo
  proxy travado de `ponte.py`, como sempre;
* agente ativo é **qualquer outro** → as quatro rotas de sessão são atendidas
  aqui, por um adaptador (`companion/agentes`).

Trocar de agente não reinicia nada. A sessão é de quem a abriu: depois da
troca, a sessão antiga responde 404 e o app do robô abre outra sozinho — o
mesmo caminho de quando o gateway reinicia.

**O que pode ser mudado por aqui é o que não afrouxa nada.** Estas rotas ficam
na rede da casa, atrás do token que o robô guarda, e o painel do robô não pede
senha. Por elas se escolhe um agente, se testa, se ajusta opção segura e se
cadastra um servidor HTTP da casa. Receita de linha de comando e nível de
ferramenta sem restrição só entram NESTE computador
(`python -m companion.agentes`, ou o arquivo `agentes.json`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .agentes import Adaptador, Conversa, ErroDoAgente, Registro, Sessoes
from .agentes.persona import IDIOMAS

log = logging.getLogger("companion.ponte")

_SESSOES = re.compile(r"/api/sessions")
_NA_SESSAO = re.compile(r"/api/sessions/([0-9a-fA-F-]{36})(?:/(messages|history))?")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")
NAO_GUARDAR = {"Cache-Control": "no-store"}

# Um teste é uma conversa de verdade: custa token e segundos do agente.
TESTES_POR_MINUTO = 8
MUDANCAS_POR_MINUTO = 30
# Cada turno lança um agente (um processo, uma chamada paga). Uma conversa de
# verdade não passa de meia dúzia por minuto; o teto existe para que um
# aparelho da rede, pelo chat do painel, não transforme a ponte numa torneira.
TURNOS_POR_MINUTO = 30
PEDIDO_MAX = 24_000            # o envelope do robô, com folga

# Como cada falha do agente chega ao robô. O código vai no corpo; o status é
# para quem só olha o HTTP.
_STATUS = {"timeout": 504, "unavailable": 503, "auth": 503, "session_lost": 503,
           "bad_response": 502, "refused": 400}
# Falhas em que vale tentar o agente reserva: o escolhido não respondeu. Uma
# resposta ruim ou um pedido recusado não melhora trocando de agente.
_PEDE_RESERVA = frozenset({"timeout", "unavailable", "auth"})


class _Janela:
    """Limite por minuto, sem guardar quem chamou."""

    def __init__(self, maximo: int) -> None:
        self.maximo, self._marcas = maximo, []

    def liberado(self) -> bool:
        agora = time.monotonic()
        self._marcas[:] = [t for t in self._marcas if t > agora - 60.0]
        if len(self._marcas) >= self.maximo:
            return False
        self._marcas.append(agora)
        return True


def _erro(codigo: str, status: int, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": codigo, **extra}},
                        status_code=status, headers=NAO_GUARDAR)


def _falha(agente: Adaptador, e: ErroDoAgente) -> JSONResponse:
    # O nome vai junto para o robô poder DIZER quem não respondeu.
    return _erro(f"agent_{e.codigo}", _STATUS.get(e.codigo, 502),
                 agent=agente.id, agent_name=agente.nome, detail=e.detalhe)


class Agentes:
    """O registro, as conversas abertas e as rotas que os expõem ao robô."""

    def __init__(self, registro: Registro, sessoes: Sessoes | None = None) -> None:
        self.registro = registro
        self.sessoes = sessoes or Sessoes()
        self._testes = _Janela(TESTES_POR_MINUTO)
        self._mudancas = _Janela(MUDANCAS_POR_MINUTO)
        self._turnos = _Janela(TURNOS_POR_MINUTO)
        registro.ao_mudar = self._esquecer

    def _esquecer(self, agente: str) -> None:
        for conversa in self.sessoes.esquecer(agente):
            adaptador = self.registro.obter(agente)
            if adaptador is not None:
                try:
                    adaptador.encerrar(conversa)
                except Exception:
                    pass

    # ── quem atende ──────────────────────────────────────────────────────────
    def ativo(self) -> Adaptador | None:
        self.registro.sincronizar()
        return self.registro.efetivo()

    def vivo(self) -> bool | None:
        """`True`/`False` para um agente atendido aqui; `None` se é o gateway."""
        agente = self.ativo()
        if agente is None or agente.pelo_gateway:
            return None
        if self.registro.saude(agente.id).disponivel:
            return True
        reserva = self.registro.reserva()
        return reserva is not None and self.registro.saude(reserva.id).disponivel

    # ── conversa ─────────────────────────────────────────────────────────────
    async def atender(self, request: Request, alvo: str) -> Response | None:
        """Atende uma rota de sessão, ou devolve `None` para o proxy do gateway.

        Chamado DEPOIS de a ponte conferir a allowlist e o token.
        """
        agente = self.ativo()
        if agente is None:
            return None
        casou = _NA_SESSAO.fullmatch(alvo)
        conversa = self.sessoes.obter(casou.group(1)) if casou else None
        if agente.pelo_gateway:
            if conversa is None:
                return None                      # sessão do gateway: é com o proxy
            # Sessão aberta com outro agente, e o dono voltou ao Garra.
            self._fechar(conversa)
            return _erro("session_agent_changed", 404)

        if request.method == "POST" and _SESSOES.fullmatch(alvo):
            nova = self.sessoes.criar(agente.id)
            log.info("conversa %s… aberta com %s", nova.id[:8], agente.id)
            return JSONResponse({"session_id": nova.id, "agent": agente.id,
                                 "agent_name": agente.nome}, status_code=201,
                                headers=NAO_GUARDAR)
        if casou is None:
            return _erro("route_not_exposed", 404)
        if conversa is None:
            return _erro("session_not_found", 404)
        if conversa.agente != agente.id:
            self._fechar(conversa)
            return _erro("session_agent_changed", 404)

        acao = casou.group(2)
        if request.method == "DELETE" and acao is None:
            self._fechar(conversa)
            return JSONResponse({"ok": True}, headers=NAO_GUARDAR)
        if request.method == "GET" and acao == "history":
            return JSONResponse({"session_id": conversa.id,
                                 "messages": list(conversa.mensagens)}, headers=NAO_GUARDAR)
        if request.method == "POST" and acao == "messages":
            return await self._turno(request, agente, conversa)
        return _erro("route_not_exposed", 404)

    def _fechar(self, conversa: Conversa) -> None:
        self.sessoes.remover(conversa.id)
        adaptador = self.registro.obter(conversa.agente)
        if adaptador is not None:
            try:
                adaptador.encerrar(conversa)
            except Exception:
                pass

    async def _turno(self, request: Request, agente: Adaptador,
                     conversa: Conversa) -> Response:
        bruto = await request.body()
        if len(bruto) > PEDIDO_MAX:
            return _erro("request_too_large", 413)
        try:
            dados = json.loads(bruto or b"null")
        except ValueError:
            dados = None
        texto = dados.get("content") if isinstance(dados, dict) else None
        if not isinstance(texto, str) or not texto.strip():
            return _erro("invalid_body", 400)
        # O mesmo cuidado do caminho do gateway: mais de um agente trata o
        # começo com barra como comando dele, e a voz não manda comando.
        if texto.strip().startswith("/"):
            return _erro("slash_command_refused", 400)
        if not self._turnos.liberado():
            return _erro("too_many_requests", 429)
        idioma = dados.get("language") if dados.get("language") in IDIOMAS else "pt"

        def responder() -> tuple[str, Adaptador]:
            with conversa.trava:
                try:
                    return self._perguntar(agente, conversa, texto, idioma), agente
                except ErroDoAgente as e:
                    reserva = self.registro.reserva()
                    if (e.codigo not in _PEDE_RESERVA or reserva is None
                            or reserva.id == agente.id):
                        raise
                    log.warning("%s não respondeu (%s); usando o reserva %s",
                                agente.id, e.codigo, reserva.id)
                    # Conversa à parte, com o histórico desta: o reserva não
                    # herda o `ref` de sessão de outro agente.
                    paralela = Conversa(id=conversa.id, agente=reserva.id,
                                        mensagens=list(conversa.mensagens))
                    try:
                        return self._perguntar(reserva, paralela, texto, idioma), reserva
                    finally:
                        try:
                            reserva.encerrar(paralela)
                        except Exception:
                            pass

        t0 = time.monotonic()
        try:
            resposta, quem = await asyncio.to_thread(responder)
        except ErroDoAgente as e:
            # Sem o texto do pedido, sempre: é a conversa da casa.
            log.warning("%s falhou em %.1f s: %s", agente.id, time.monotonic() - t0, e.codigo)
            return _falha(agente, e)
        except Exception:
            log.exception("defeito no adaptador %s", agente.id)
            return _falha(agente, ErroDoAgente("bad_response", "defeito no adaptador"))
        self.sessoes.registrar(conversa, texto, resposta)
        log.info("%s respondeu em %.1f s (%d caracteres)", quem.id,
                 time.monotonic() - t0, len(resposta))
        return JSONResponse({"session_id": conversa.id, "content": resposta,
                             "provider": quem.id, "agent": quem.id,
                             "agent_name": quem.nome, "fallback": quem is not agente},
                            headers=NAO_GUARDAR)

    def _perguntar(self, agente: Adaptador, conversa: Conversa, texto: str,
                   idioma: str) -> str:
        instrucoes = self.registro.instrucoes(agente, idioma)
        try:
            return agente.responder(conversa, texto, instrucoes=instrucoes)
        except ErroDoAgente as e:
            if e.codigo != "session_lost":
                raise
            # O agente esqueceu a conversa: recomeça nele, uma vez, levando
            # junto o que a ponte ainda lembra.
            conversa.ref = None
            return agente.responder(conversa, texto, instrucoes=instrucoes)


# ─── rotas de gestão ─────────────────────────────────────────────────────────
def registrar_rotas(app: FastAPI, autorizado: Callable[[Request], bool],
                    atual: Callable[[], "Agentes | None"]) -> None:
    """Liga as rotas `/api/agentes*` ao app da ponte.

    `atual` devolve o `Agentes` em vigor AGORA: o app da ponte é um só no
    processo, e quem o monta de novo (os testes, um recarregamento) troca o
    registro sem trocar as rotas. Sem registro, as rotas não existem (404).
    """
    negado = JSONResponse({"erro": "token inválido"}, status_code=401,
                          headers=NAO_GUARDAR)
    ausente = JSONResponse({"erro": "rota não exposta"}, status_code=404)

    async def corpo_de(request: Request) -> dict | None:
        try:
            dados = json.loads(await request.body() or b"null")
        except ValueError:
            return None
        return dados if isinstance(dados, dict) else None

    def lista(ag: Agentes) -> JSONResponse:
        return JSONResponse({"ok": True, **ag.registro.listar()}, headers=NAO_GUARDAR)

    async def mudar(request: Request, acao: Callable[[Agentes, dict], Any]) -> Response:
        ag = atual()
        if ag is None:
            return ausente
        if not autorizado(request):
            return negado
        if not ag._mudancas.liberado():
            return Response(status_code=429, headers=NAO_GUARDAR)
        dados = await corpo_de(request)
        if dados is None:
            return _erro("invalid_body", 400)
        try:
            await asyncio.to_thread(acao, ag, dados)
        except ErroDoAgente as e:
            return _erro(e.codigo, 400, detail=e.detalhe)
        except OSError:
            log.exception("não consegui gravar agentes.json")
            return _erro("storage_failed", 500)
        return await asyncio.to_thread(lista, ag)

    @app.get("/api/agentes")
    async def agentes_listar(request: Request) -> Response:
        ag = atual()
        if ag is None:
            return ausente
        if not autorizado(request):
            return negado
        ag.registro.sincronizar()
        return await asyncio.to_thread(lista, ag)

    @app.post("/api/agentes/descobrir")
    async def agentes_descobrir(request: Request) -> Response:
        return await mudar(request, lambda ag, _d: ag.registro.recarregar())

    @app.put("/api/agentes/ativo")
    async def agentes_escolher(request: Request) -> Response:
        return await mudar(
            request, lambda ag, d: ag.registro.escolher(str(d.get("id") or "")))

    @app.put("/api/agentes/reserva")
    async def agentes_reserva(request: Request) -> Response:
        return await mudar(request, lambda ag, d: ag.registro.definir_reserva(
            str(d["id"]) if d.get("id") else None))

    @app.post("/api/agentes")
    async def agentes_adicionar(request: Request) -> Response:
        # `local=False`: pela rede só entra servidor HTTP da casa.
        return await mudar(request, lambda ag, d: ag.registro.adicionar(d, local=False))

    @app.put("/api/agentes/{ident}/opcoes")
    async def agentes_configurar(ident: str, request: Request) -> Response:
        if not _ID.fullmatch(ident):
            return _erro("refused", 400)
        return await mudar(
            request, lambda ag, d: ag.registro.configurar(ident, d, local=False))

    @app.delete("/api/agentes/{ident}")
    async def agentes_remover(ident: str, request: Request) -> Response:
        ag = atual()
        if ag is None:
            return ausente
        if not autorizado(request):
            return negado
        if not _ID.fullmatch(ident) or not ag._mudancas.liberado():
            return _erro("refused", 400)
        alvo = ag.registro.obter(ident)
        # Pela rede só se desfaz o que pela rede se faz.
        if alvo is None or alvo.interface != "http":
            return _erro("refused", 400, detail="só neste computador")
        try:
            await asyncio.to_thread(ag.registro.remover, ident)
        except ErroDoAgente as e:
            return _erro(e.codigo, 400, detail=e.detalhe)
        return await asyncio.to_thread(lista, ag)

    @app.post("/api/agentes/{ident}/testar")
    async def agentes_testar(ident: str, request: Request) -> Response:
        ag = atual()
        if ag is None:
            return ausente
        if not autorizado(request):
            return negado
        if not _ID.fullmatch(ident):
            return _erro("refused", 400)
        if not ag._testes.liberado():
            return Response(status_code=429, headers=NAO_GUARDAR)
        return JSONResponse(await asyncio.to_thread(ag.registro.testar, ident),
                            headers=NAO_GUARDAR)
