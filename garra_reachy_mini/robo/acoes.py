"""`ControladorRobo`: fila, preempção, máquina de estados e parada de emergência.

Uma thread — e só uma — toca o robô. Todo o resto (loop de voz, rotas HTTP,
WebSocket, MCP) submete pedidos e espera. Isso é o que impede dois comandos
simultâneos de brigarem pela cabeça.

## Por que não `queue.PriorityQueue`

Porque preempção precisa de três coisas que ela não dá: remover um item
arbitrário já enfileirado, cancelar o que está rodando, e invalidar em bloco
tudo que foi enfileirado antes de um e-stop. Aqui é um heap sob `Condition`,
com `(prioridade, seq)` para desempate estável, um `Token` de cancelamento por
pedido e um contador de **geração** que aposenta de uma vez tudo que já estava
na fila.

## Matriz de preempção

| nova       | em execução | resultado                                   |
|------------|-------------|---------------------------------------------|
| emergência | qualquer    | cancela na hora, entra em ESTOPPED          |
| explícita  | ambiente    | cancela a ambiente                          |
| explícita  | explícita   | `politica` da ação: preempt / queue / reject|
| ambiente   | explícita   | descartada                                  |
| ambiente   | ambiente    | substitui (só a mais recente sobrevive)     |

## Máquina de estados

    IDLE ⇄ RUNNING → STOPPING → ESTOPPED → (clear_estop) → RECOVERING → IDLE

**A parada de emergência não volta ao neutro.** Voltar ao neutro é começar um
movimento novo, e movimento novo é exatamente o que uma parada de emergência
não pode fazer. Ela cancela, para os moves no daemon, desliga rastreamento e
wobbling, e deixa o robô parado onde está. Sair de ESTOPPED é um passo separado
e explícito (`clear_estop`), e voltar ao neutro é outro (`return_to_neutral`).

## Honestidade

`executed` só vira `true` quando a ação terminou de verdade no hardware. Em modo
simulado é sempre `false`, com `mode: "simulated"`. É isso que impede o Garra de
dizer "virei a cabeça" sem ter virado — a promessa central desta camada.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import limites
from .backends import RoboBackend
from .barramento import Barramento
from .catalogo import (
    CATALOGO,
    PERMITIDAS_EM_ESTOP,
    AcaoCancelada,
    AcaoExpirou,
    AcaoFalhou,
    Contexto,
    descrever_catalogo,
    resolver_expressoes,
    validar,
)
from .daemon_api import DATASET_DANCAS, DATASET_EMOCOES
from .imagem import dimensoes_jpeg

log = logging.getLogger("garra_reachy_mini.robo.acoes")

PRIO_EMERGENCIA = 0
PRIO_EXPLICITA = 1
PRIO_AMBIENTE = 2

MAX_ERROS = 50
ESPERA_FILA_PADRAO_S = 90.0


class EstadoControlador(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    ESTOPPED = "estopped"
    RECOVERING = "recovering"


class Token:
    """Cancelamento cooperativo de um pedido."""

    __slots__ = ("_cancelado",)

    def __init__(self) -> None:
        self._cancelado = False

    @property
    def cancelado(self) -> bool:
        return self._cancelado

    def cancelar(self) -> None:
        self._cancelado = True


@dataclass
class ResultadoAcao:
    """Resposta de uma ação. O formato é o contrato com o modelo e com a UI."""

    action: str
    action_id: str
    ok: bool
    accepted: bool
    executed: bool
    mode: str
    state: str  # queued | completed | cancelled | failed | rejected
    message: str
    error: str | None = None
    adjustments: list[str] = field(default_factory=list)
    duration_ms: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        saida: dict[str, Any] = {
            "ok": self.ok,
            "accepted": self.accepted,
            "executed": self.executed,
            "mode": self.mode,
            "action": self.action,
            "action_id": self.action_id,
            "state": self.state,
            "message": self.message,
        }
        if self.error:
            saida["error"] = self.error
        if self.adjustments:
            saida["adjustments"] = self.adjustments
        if self.duration_ms is not None:
            saida["duration_ms"] = self.duration_ms
        if self.data:
            saida["data"] = self.data
        return saida


@dataclass(order=False)
class Pedido:
    id: str
    nome: str
    params: dict[str, Any]
    prioridade: int
    seq: int
    geracao: int
    source: str
    correlation_id: str | None
    politica: str
    timeout_s: float
    token: Token = field(default_factory=Token)
    estado: str = "queued"
    ident_daemon: str | None = None
    resultado: ResultadoAcao | None = None
    pronto: threading.Event = field(default_factory=threading.Event)

    def __lt__(self, outro: "Pedido") -> bool:
        return (self.prioridade, self.seq) < (outro.prioridade, outro.seq)


class ControladorRobo:
    """Dono único do robô. Tudo passa por `submeter`."""

    def __init__(
        self,
        backend: RoboBackend,
        barramento: Barramento | None = None,
        *,
        dir_capturas: Path | None = None,
        semente: int | None = None,
    ) -> None:
        self.backend = backend
        self.eventos = barramento or Barramento()
        self.dir_capturas = dir_capturas or Path("/tmp/garra_reachy_mini_capturas")
        # Fonte de quadro. O padrão vai direto ao backend; o app troca pelo
        # `FrameHub.instantaneo`, que já mantém o último quadro em memória.
        # Diferença medida: `read_jpeg()` do SDK devolve None quando o frame
        # ainda não chegou pelo WebRTC, e uma captura única cai justamente aí.
        self.fonte_quadro: Callable[[], bytes | None] = backend.frame_jpeg

        self._cond = threading.Condition(threading.RLock())
        self._heap: list[Pedido] = []
        self._atual: Pedido | None = None
        self._seq = itertools.count(1)
        self._geracao = 0
        self._estado = EstadoControlador.IDLE
        self._parar = threading.Event()
        self._erros: deque[dict[str, Any]] = deque(maxlen=MAX_ERROS)
        self._aleatorio = random.Random(semente)

        # Tracking e wobbling: três flags independentes, como a revisão pediu.
        # O desejado é `(usuario or ambiente) and not suspenso`.
        self._trk_usuario = False
        self._trk_ambiente = False
        self._trk_suspenso = False
        self._trk_peso_usuario = limites.PESO_TRACKING_MAX
        self._trk_peso_ambiente = limites.PESO_TRACKING_AMBIENTE
        self._trk_ligado_no_robo = False
        self._wob_pedido = False
        self._wob_suspenso = False
        self._wob_ligado_no_robo = False

        self._expressoes: dict[str, str | None] = {}
        self._moves: dict[str, list[str]] = {}
        self._catalogo_pronto = False
        self._ultima_latencia_ms: float | None = None

        self._worker = threading.Thread(
            target=self._laco, name="robo-executor", daemon=True
        )
        self._thr_catalogo: threading.Thread | None = None

    # ── ciclo de vida ───────────────────────────────────────────────────────
    def iniciar(self) -> None:
        self.backend.preparar()
        self._worker.start()
        # O catálogo de moves mora no HuggingFace e chega pelo daemon: são duas
        # chamadas de até 25 s cada. Em primeiro plano isso vira meia dança de
        # espera no botão Start quando a URL do daemon está errada — e no robô
        # ela nunca está errada, então o custo cai todo em cima de quem já tem
        # problema. Carregar em thread deixa painel, câmera e movimento
        # primitivo disponíveis na hora; expressões e danças entram quando
        # chegarem, e `status()["catalog_ready"]` conta a verdade nesse meio-tempo.
        self._thr_catalogo = threading.Thread(
            target=self._laco_catalogo, name="robo-catalogo", daemon=True
        )
        self._thr_catalogo.start()
        self._publicar_status()

    def _laco_catalogo(self) -> None:
        espera = 2.0
        while not self._parar.is_set():
            try:
                self._recarregar_catalogo()
            except Exception:  # pragma: no cover - rede
                log.debug("falha ao carregar o catálogo de moves", exc_info=True)
            if any(self._moves.values()):
                self._catalogo_pronto = True
                self._publicar_status()
                return
            if self._parar.wait(espera):
                return
            espera = min(espera * 2.0, 60.0)

    def encerrar(self, timeout: float = 3.0) -> None:
        self._parar.set()
        with self._cond:
            self._geracao += 1
            for p in self._heap:
                self._finalizar_cancelado(p, "o controlador está encerrando")
            self._heap.clear()
            if self._atual is not None:
                self._atual.token.cancelar()
            self._cond.notify_all()
        if self._worker.is_alive():
            self._worker.join(timeout=timeout)
        try:
            self._aplicar_tracking(forcar_desligar=True)
            if self._wob_ligado_no_robo:
                self.backend.wobbling(False)
        except Exception:  # pragma: no cover - encerramento é best-effort
            pass

    def _recarregar_catalogo(self) -> None:
        """Descobre no daemon quais moves existem e resolve os aliases.

        Feito no arranque (e sob demanda) porque a biblioteca vive no
        HuggingFace e pode mudar sem que o nosso código mude.
        """
        for dataset in (DATASET_EMOCOES, DATASET_DANCAS):
            self._moves[dataset] = self.backend.moves_disponiveis(dataset)
        self._expressoes = resolver_expressoes(self._moves.get(DATASET_EMOCOES, []))
        faltando = [a for a, real in self._expressoes.items() if real is None and a != "neutral"]
        if faltando:
            log.warning(
                "expressões sem move correspondente na biblioteca do robô: %s",
                ", ".join(sorted(faltando)),
            )

    # ── estado público ──────────────────────────────────────────────────────
    @property
    def estado(self) -> EstadoControlador:
        with self._cond:
            return self._estado

    @property
    def em_estop(self) -> bool:
        return self.estado is EstadoControlador.ESTOPPED

    def status(self) -> dict[str, Any]:
        t0 = time.monotonic()
        info = self.backend.estado()
        latencia = (time.monotonic() - t0) * 1000.0
        self._ultima_latencia_ms = latencia
        with self._cond:
            atual = self._atual
            fila = len(self._heap)
            estado = self._estado.value
            trk = {
                "user": self._trk_usuario,
                "ambient": self._trk_ambiente,
                "suspended": self._trk_suspenso,
                "active_on_robot": self._trk_ligado_no_robo,
            }
            wob = {
                "requested": self._wob_pedido,
                "suspended": self._wob_suspenso,
                "active_on_robot": self._wob_ligado_no_robo,
            }
            erros = list(self._erros)[-5:]
        return {
            "mode": self.backend.modo,
            "controller_state": estado,
            "estopped": estado == EstadoControlador.ESTOPPED.value,
            "connected": bool(info.get("conectado")),
            "motors": info.get("motores"),
            "moving": bool(info.get("movendo")) or atual is not None,
            "current_action": (
                {
                    "action": atual.nome,
                    "action_id": atual.id,
                    "source": atual.source,
                    "state": atual.estado,
                }
                if atual
                else None
            ),
            "queued": fila,
            "tracking": trk,
            "wobbling": wob,
            "face_detected": bool(info.get("rosto_detectado")),
            "catalog_ready": self._catalogo_pronto,
            "latency_ms": round(latencia, 1),
            "robot": info,
            "recent_errors": erros,
        }

    def capacidades(self) -> dict[str, Any]:
        with self._cond:
            expressoes = dict(self._expressoes)
            moves = {k: list(v) for k, v in self._moves.items()}
        from .catalogo import EXPRESSOES_PRINCIPAIS

        return {
            "mode": self.backend.modo,
            "actions": descrever_catalogo(),
            "expressions": {
                alias: {"available": real is not None or alias == "neutral", "resolved_move": real}
                for alias, real in expressoes.items()
            },
            "primary_expressions": list(EXPRESSOES_PRINCIPAIS),
            "emotions": moves.get(DATASET_EMOCOES, []),
            "dances": moves.get(DATASET_DANCAS, []),
            "directions": sorted(limites.DIRECOES),
            "limits": {
                "max_yaw_deg": limites.MAX_YAW_DEG,
                "max_pitch_deg": limites.MAX_PITCH_DEG,
                "max_roll_deg": limites.MAX_ROLL_DEG,
                "max_antenna_rad": limites.MAX_ANTENA_RAD,
                "duration_s": [limites.DURACAO_MIN_S, limites.DURACAO_MAX_S],
            },
        }

    def fila(self) -> dict[str, Any]:
        with self._cond:
            return {
                "current": (
                    {"action": self._atual.nome, "action_id": self._atual.id,
                     "state": self._atual.estado, "source": self._atual.source}
                    if self._atual else None
                ),
                "queued": [
                    {"action": p.nome, "action_id": p.id, "priority": p.prioridade,
                     "source": p.source}
                    for p in sorted(self._heap)
                ],
                "controller_state": self._estado.value,
            }

    def erros(self, n: int = MAX_ERROS) -> list[dict[str, Any]]:
        with self._cond:
            return list(self._erros)[-n:]

    # ── submissão ───────────────────────────────────────────────────────────
    def submeter(
        self,
        nome: str,
        params: dict[str, Any] | None = None,
        *,
        prioridade: int | None = None,
        source: str = "api",
        correlation_id: str | None = None,
        esperar: bool = True,
        espera_max_s: float = ESPERA_FILA_PADRAO_S,
    ) -> ResultadoAcao:
        """Ponto de entrada único. Valida, aplica preempção e enfileira."""
        params = dict(params or {})
        acao = CATALOGO.get(nome)
        action_id = f"act_{uuid.uuid4().hex[:12]}"
        if acao is None:
            return self._rejeitar(nome, action_id, f"ação desconhecida: {nome!r}")

        try:
            params = validar(nome, params)
        except AcaoFalhou as e:
            return self._rejeitar(nome, action_id, str(e))

        prio = acao.prioridade_padrao if prioridade is None else prioridade

        # Emergência não entra na fila: seria a única coisa capaz de esperar
        # atrás justamente do movimento que ela precisa interromper.
        if nome == "stop" or prio == PRIO_EMERGENCIA:
            return self.parar_tudo(source=source, correlation_id=correlation_id, action_id=action_id)

        if self.em_estop and nome not in PERMITIDAS_EM_ESTOP:
            return self._rejeitar(
                nome, action_id,
                "o robô está em parada de emergência; chame clear_estop antes",
                estado="rejected",
            )

        if acao.interna:
            return self._executar_interna(acao.nome, params, action_id, source, correlation_id)

        pedido = Pedido(
            id=action_id, nome=nome, params=params, prioridade=prio,
            seq=next(self._seq), geracao=0, source=source,
            correlation_id=correlation_id, politica=acao.politica,
            timeout_s=acao.timeout_s,
        )

        with self._cond:
            pedido.geracao = self._geracao
            decisao = self._aplicar_preempcao(pedido)
            if decisao is not None:
                return decisao
            heapq.heappush(self._heap, pedido)
            self._cond.notify_all()

        self._publicar("robot.action.queued", pedido, action=nome, parameters=params)

        if not esperar:
            return ResultadoAcao(
                action=nome, action_id=action_id, ok=True, accepted=True,
                executed=False, mode=self.backend.modo, state="queued",
                message="Ação aceita e enfileirada.",
            )

        if not pedido.pronto.wait(timeout=espera_max_s):
            pedido.token.cancelar()
            return ResultadoAcao(
                action=nome, action_id=action_id, ok=False, accepted=True,
                executed=False, mode=self.backend.modo, state="failed",
                message="A ação não terminou dentro do tempo de espera.",
                error="espera_excedida",
            )
        assert pedido.resultado is not None
        return pedido.resultado

    def _aplicar_preempcao(self, novo: Pedido) -> ResultadoAcao | None:
        """Chamado com o lock. Devolve um resultado se o pedido morre aqui."""
        atual = self._atual

        if novo.prioridade >= PRIO_AMBIENTE:
            # Ambiente nunca atrapalha comando explícito, nem se acumula.
            explicito_na_fila = any(p.prioridade < PRIO_AMBIENTE for p in self._heap)
            if (atual is not None and atual.prioridade < PRIO_AMBIENTE) or explicito_na_fila:
                return ResultadoAcao(
                    action=novo.nome, action_id=novo.id, ok=True, accepted=False,
                    executed=False, mode=self.backend.modo, state="rejected",
                    message="Movimento ambiente descartado: há um comando explícito em curso.",
                )
            # Só a ambiente mais recente sobrevive.
            for p in [p for p in self._heap if p.prioridade >= PRIO_AMBIENTE]:
                self._heap.remove(p)
                self._finalizar_cancelado(p, "substituída por um movimento ambiente mais recente")
            heapq.heapify(self._heap)
            return None

        # Explícita: limpa a fila de ambiente e decide o que fazer com a atual.
        for p in [p for p in self._heap if p.prioridade >= PRIO_AMBIENTE]:
            self._heap.remove(p)
            self._finalizar_cancelado(p, "descartada por um comando explícito")
        heapq.heapify(self._heap)

        if atual is None:
            return None
        if atual.prioridade >= PRIO_AMBIENTE:
            atual.token.cancelar()
            atual.estado = "cancelling"
            return None
        # explícita contra explícita
        if novo.politica == "reject":
            return ResultadoAcao(
                action=novo.nome, action_id=novo.id, ok=False, accepted=False,
                executed=False, mode=self.backend.modo, state="rejected",
                message=f"Já existe um comando em execução ({atual.nome}).",
            )
        if novo.politica == "queue":
            return None
        atual.token.cancelar()
        atual.estado = "cancelling"
        return None

    def _rejeitar(
        self, nome: str, action_id: str, motivo: str, estado: str = "rejected"
    ) -> ResultadoAcao:
        self._registrar_erro(nome, motivo)
        return ResultadoAcao(
            action=nome, action_id=action_id, ok=False, accepted=False,
            executed=False, mode=self.backend.modo, state=estado,
            message=motivo, error=motivo,
        )

    def _finalizar_cancelado(self, pedido: Pedido, motivo: str) -> None:
        pedido.estado = "cancelled"
        pedido.resultado = ResultadoAcao(
            action=pedido.nome, action_id=pedido.id, ok=True, accepted=True,
            executed=False, mode=self.backend.modo, state="cancelled",
            message=f"Ação cancelada: {motivo}.",
        )
        pedido.pronto.set()

    # ── parada de emergência ────────────────────────────────────────────────
    def parar_tudo(
        self,
        *,
        source: str = "api",
        correlation_id: str | None = None,
        action_id: str | None = None,
    ) -> ResultadoAcao:
        """Cancela tudo e trava o robô. NÃO volta ao neutro — de propósito."""
        action_id = action_id or f"act_{uuid.uuid4().hex[:12]}"
        t0 = time.monotonic()

        # 1. Flags sob lock: a partir daqui nada novo começa, mesmo que as
        #    chamadas ao daemon abaixo demorem.
        with self._cond:
            self._geracao += 1
            pendentes = list(self._heap)
            self._heap.clear()
            for p in pendentes:
                self._finalizar_cancelado(p, "parada de emergência")
            ident_em_voo = None
            if self._atual is not None:
                self._atual.token.cancelar()
                self._atual.estado = "cancelling"
                ident_em_voo = self._atual.ident_daemon
            self._estado = EstadoControlador.STOPPING
            self._trk_usuario = False
            self._trk_ambiente = False
            self._wob_pedido = False
            self._cond.notify_all()

        # 2. O corte físico, em UMA ida ao robô. O daemon faz `task.cancel()` na
        #    tarefa do move, e é isso que interrompe uma dança no meio.
        #    Cancelar pelo id conhecido evita o `GET /api/move/running` antes —
        #    numa rede sem fio, essa ida a menos é a diferença entre ~240 ms e
        #    ~60 ms para o robô de fato parar.
        parados = 0
        if ident_em_voo:
            try:
                parados = 1 if self.backend.parar_ident(ident_em_voo) else 0
            except Exception as e:  # pragma: no cover
                self._registrar_erro("stop", f"falha ao parar {ident_em_voo}: {e}")
        ms = int((time.monotonic() - t0) * 1000)

        # 3. Rede de segurança e limpeza, já fora do caminho crítico: varre
        #    qualquer move que não tenha saído de nós e desliga os efeitos —
        #    tudo condicional, para não gastar requisição à toa.
        try:
            parados += self.backend.parar_moves()
        except Exception as e:  # pragma: no cover
            self._registrar_erro("stop", f"falha ao varrer moves: {e}")
        try:
            self._aplicar_tracking(forcar_desligar=True)
        except Exception:
            pass
        try:
            if self._wob_ligado_no_robo:
                self.backend.wobbling(False)
                self._wob_ligado_no_robo = False
        except Exception:
            pass

        with self._cond:
            self._estado = EstadoControlador.ESTOPPED

        self.eventos.publicar(
            "robot.estop", action_id=action_id, source=source,
            correlation_id=correlation_id, stopped_moves=parados,
            cancelled=len(pendentes), latency_ms=ms,
        )
        self._publicar_status()
        return ResultadoAcao(
            action="stop", action_id=action_id, ok=True, accepted=True,
            executed=True, mode=self.backend.modo, state="completed",
            message=(
                "Parada de emergência: movimentos interrompidos e robô travado na "
                "posição atual. Chame clear_estop para liberar."
            ),
            duration_ms=ms,
            data={"stopped_moves": parados, "cancelled_queued": len(pendentes)},
        )

    def limpar_estop(self, *, source: str = "api") -> ResultadoAcao:
        action_id = f"act_{uuid.uuid4().hex[:12]}"
        with self._cond:
            if self._estado is not EstadoControlador.ESTOPPED:
                return ResultadoAcao(
                    action="clear_estop", action_id=action_id, ok=True, accepted=True,
                    executed=False, mode=self.backend.modo, state="completed",
                    message="O robô não estava em parada de emergência.",
                )
            self._estado = EstadoControlador.RECOVERING
        try:
            self.backend.preparar()
        except Exception as e:
            self._registrar_erro("clear_estop", str(e))
        with self._cond:
            self._estado = EstadoControlador.IDLE
        self.eventos.publicar("robot.estop_cleared", action_id=action_id, source=source)
        self._publicar_status()
        return ResultadoAcao(
            action="clear_estop", action_id=action_id, ok=True, accepted=True,
            executed=True, mode=self.backend.modo, state="completed",
            message="Parada de emergência liberada. O robô continua onde parou; "
                    "use return_to_neutral para recentrar.",
        )

    # ── tracking / wobbling ─────────────────────────────────────────────────
    def tracking_pedir(self, ligado: bool, peso: float, origem: str) -> dict[str, Any]:
        with self._cond:
            if origem == "ambiente":
                self._trk_ambiente = ligado
                self._trk_peso_ambiente = peso
            else:
                self._trk_usuario = ligado
                self._trk_peso_usuario = peso
        return self._aplicar_tracking()

    def _aplicar_tracking(self, forcar_desligar: bool = False) -> dict[str, Any]:
        """Reduz as três flags a um comando só para o robô."""
        with self._cond:
            desejado = (
                False
                if forcar_desligar
                else (self._trk_usuario or self._trk_ambiente) and not self._trk_suspenso
            )
            peso = self._trk_peso_usuario if self._trk_usuario else self._trk_peso_ambiente
            ja = self._trk_ligado_no_robo
        # Nada a fazer quando o robô já está no estado desejado — inclusive no
        # `forcar_desligar`, onde mandar "desliga" para algo já desligado seria
        # uma requisição inútil dentro da parada de emergência.
        if desejado == ja:
            return {"status": "ok", "enabled": ja, "weight": peso}
        estado = self.backend.tracking(desejado, peso)
        ativo = bool(estado.get("enabled"))
        with self._cond:
            self._trk_ligado_no_robo = ativo
        estado.setdefault("weight", peso)
        return estado

    def wobbling_pedir(self, ligado: bool) -> bool:
        with self._cond:
            self._wob_pedido = ligado
        return self._aplicar_wobbling()

    def _aplicar_wobbling(self) -> bool:
        with self._cond:
            desejado = self._wob_pedido and not self._wob_suspenso
            ja = self._wob_ligado_no_robo
        if desejado == ja:
            return ja
        ok = self.backend.wobbling(desejado)
        with self._cond:
            self._wob_ligado_no_robo = desejado if ok else ja
        return self._wob_ligado_no_robo

    def _suspender_efeitos(self) -> None:
        """Antes de um movimento: tira tracking e wobbling da disputa pela cabeça."""
        with self._cond:
            self._trk_suspenso = True
            self._wob_suspenso = True
        try:
            self._aplicar_tracking()
            self._aplicar_wobbling()
        except Exception:  # pragma: no cover
            pass

    def _restaurar_efeitos(self) -> None:
        """Depois: religa apenas o que estava ligado antes."""
        with self._cond:
            self._trk_suspenso = False
            self._wob_suspenso = False
        try:
            self._aplicar_tracking()
            self._aplicar_wobbling()
        except Exception:  # pragma: no cover
            pass

    # ── laço executor ───────────────────────────────────────────────────────
    def _laco(self) -> None:
        while not self._parar.is_set():
            with self._cond:
                while not self._heap and not self._parar.is_set():
                    if self._estado is EstadoControlador.RUNNING:
                        self._estado = EstadoControlador.IDLE
                    self._cond.wait(timeout=0.5)
                if self._parar.is_set():
                    return
                pedido = heapq.heappop(self._heap)
                if pedido.geracao != self._geracao:
                    self._finalizar_cancelado(pedido, "invalidada por uma parada")
                    continue
                if pedido.token.cancelado:
                    self._finalizar_cancelado(pedido, "cancelada antes de começar")
                    continue
                if self._estado is EstadoControlador.ESTOPPED:
                    self._finalizar_cancelado(pedido, "o robô está em parada de emergência")
                    continue
                self._atual = pedido
                pedido.estado = "starting"
                self._estado = EstadoControlador.RUNNING
            try:
                self._executar(pedido)
            except Exception:  # pragma: no cover - rede de segurança
                log.exception("falha inesperada ao executar %s", pedido.nome)
                self._concluir(
                    pedido, ok=False, executed=False, estado="failed",
                    mensagem="Falha interna ao executar a ação.", erro="excecao",
                )
            finally:
                with self._cond:
                    self._atual = None
                    self._cond.notify_all()

    def _executar(self, pedido: Pedido) -> None:
        acao = CATALOGO[pedido.nome]
        assert acao.handler is not None
        lim = limites.Limitador()
        t0 = time.monotonic()

        self._publicar(
            "robot.action.started", pedido, action=pedido.nome, parameters=pedido.params
        )
        pedido.estado = "running"

        suspendeu = False
        try:
            if acao.movimento:
                self._suspender_efeitos()
                suspendeu = True
                # O daemon DESCARTA em silêncio um movimento novo enquanto outro
                # roda (`play_move` → `_try_start_move`). Limpar antes é o que
                # transforma "preempção" em preempção de verdade.
                self.backend.parar_e_aguardar()
                self.backend.preparar()

            ctx = Contexto(
                backend=self.backend,
                lim=lim,
                timeout=pedido.timeout_s,
                cancelado=lambda: pedido.token.cancelado or self._parar.is_set(),
                expressoes=dict(self._expressoes),
                catalogo_moves=lambda ds: list(self._moves.get(ds, [])),
                tracking_pedir=self.tracking_pedir,
                aleatorio=self._aleatorio,
                registrar_ident=lambda i, _p=pedido: setattr(_p, "ident_daemon", i),
            )
            mensagem = acao.handler(ctx, pedido.params)
        except AcaoCancelada:
            self._concluir(
                pedido, ok=True, executed=False, estado="cancelled",
                mensagem="Ação interrompida antes de terminar.",
                ajustes=lim.ajustes, ms=int((time.monotonic() - t0) * 1000),
            )
            return
        except AcaoExpirou as e:
            self._concluir(
                pedido, ok=False, executed=False, estado="failed",
                mensagem=f"A ação não terminou a tempo: {e}", erro="timeout",
                ajustes=lim.ajustes, ms=int((time.monotonic() - t0) * 1000),
            )
            return
        except AcaoFalhou as e:
            self._concluir(
                pedido, ok=False, executed=False, estado="failed",
                mensagem=str(e), erro=str(e), ajustes=lim.ajustes,
                ms=int((time.monotonic() - t0) * 1000),
            )
            return
        except Exception as e:
            log.exception("erro executando %s", pedido.nome)
            self._concluir(
                pedido, ok=False, executed=False, estado="failed",
                mensagem=f"Falha ao executar {pedido.nome}: {e}", erro=type(e).__name__,
                ajustes=lim.ajustes, ms=int((time.monotonic() - t0) * 1000),
            )
            return
        finally:
            if suspendeu:
                self._restaurar_efeitos()

        simulado = self.backend.modo != "real"
        self._concluir(
            pedido,
            ok=True,
            executed=not simulado,
            estado="completed",
            mensagem=(
                mensagem
                if not simulado
                else f"Ação simulada; nenhum robô físico conectado. ({mensagem})"
            ),
            ajustes=lim.ajustes,
            ms=int((time.monotonic() - t0) * 1000),
        )

    def _concluir(
        self,
        pedido: Pedido,
        *,
        ok: bool,
        executed: bool,
        estado: str,
        mensagem: str,
        erro: str | None = None,
        ajustes: list[limites.Ajuste] | None = None,
        ms: int | None = None,
    ) -> None:
        pedido.estado = estado
        pedido.resultado = ResultadoAcao(
            action=pedido.nome, action_id=pedido.id, ok=ok, accepted=True,
            executed=executed, mode=self.backend.modo, state=estado,
            message=mensagem, error=erro,
            adjustments=[str(a) for a in (ajustes or [])],
            duration_ms=ms,
        )
        # Sair de `_atual` ANTES de liberar quem espera. O `_laco` também limpa
        # isto no `finally`, mas só depois de publicar os eventos — e nesse
        # intervalo `submeter(esperar=True)` já retornou, então um chamador que
        # consultasse a fila em seguida veria a ação terminada como "corrente".
        with self._cond:
            if self._atual is pedido:
                self._atual = None
            self._cond.notify_all()
        pedido.pronto.set()
        if estado == "failed":
            self._registrar_erro(pedido.nome, erro or mensagem)
            self._publicar("robot.action.failed", pedido, action=pedido.nome,
                           error=erro or mensagem, duration_ms=ms)
        elif estado == "cancelled":
            self._publicar("robot.action.cancelled", pedido, action=pedido.nome,
                           duration_ms=ms)
        else:
            self._publicar("robot.action.completed", pedido, action=pedido.nome,
                           executed=executed, message=mensagem, duration_ms=ms)

    # ── ações internas ──────────────────────────────────────────────────────
    def _executar_interna(
        self,
        nome: str,
        params: dict[str, Any],
        action_id: str,
        source: str,
        correlation_id: str | None,
    ) -> ResultadoAcao:
        def resposta(msg: str, dados: dict[str, Any] | None = None, ok: bool = True,
                     executado: bool = True) -> ResultadoAcao:
            return ResultadoAcao(
                action=nome, action_id=action_id, ok=ok, accepted=True,
                executed=executado and self.backend.modo == "real",
                mode=self.backend.modo,
                state="completed" if ok else "failed",
                message=msg, data=dados or {}, error=None if ok else msg,
            )

        try:
            if nome == "status":
                return resposta("Estado atual do robô.", self.status(), executado=False)
            if nome == "clear_estop":
                return self.limpar_estop(source=source)
            if nome == "disable_motors":
                self.backend.definir_motores("disabled")
                self._publicar_status()
                return resposta("Torque desligado. A cabeça do robô está solta.")
            if nome == "enable_motors":
                self.backend.definir_motores("enabled")
                self._publicar_status()
                return resposta("Torque religado.")
            if nome == "capture_image":
                return self._capturar(action_id, nome)
            if nome == "list_apps":
                apps = self.backend.apps_listar()
                atual = self.backend.app_status()
                return resposta(
                    f"{len(apps)} aplicativo(s) instalado(s) no robô.",
                    {"apps": apps, "current": atual}, executado=False,
                )
            if nome == "start_app":
                alvo = str(params.get("name", ""))
                if not limites.nome_app_valido(alvo):
                    return self._rejeitar(nome, action_id, f"nome de app inválido: {alvo!r}")
                st = self.backend.app_iniciar(alvo)
                return resposta(f"Aplicativo {alvo} iniciado no robô.", {"status": st})
            if nome == "stop_app":
                self.backend.app_parar()
                return resposta("Aplicativo do robô parado.")
            if nome == "restart_app":
                st = self.backend.app_reiniciar()
                return resposta("Aplicativo do robô reiniciado.", {"status": st})
        except Exception as e:
            self._registrar_erro(nome, str(e))
            return ResultadoAcao(
                action=nome, action_id=action_id, ok=False, accepted=True,
                executed=False, mode=self.backend.modo, state="failed",
                message=f"Falha em {nome}: {e}", error=type(e).__name__,
            )
        return self._rejeitar(nome, action_id, f"ação interna sem tratamento: {nome}")

    def _capturar(self, action_id: str, nome: str, tentativas: int = 3) -> ResultadoAcao:
        # Retentativa curta: o quadro pelo WebRTC chega em rajadas e a primeira
        # leitura depois de um período ocioso costuma vir vazia.
        jpeg = None
        for tentativa in range(tentativas):
            jpeg = self.fonte_quadro()
            if jpeg:
                break
            if tentativa < tentativas - 1:
                time.sleep(0.25)
        if not jpeg:
            return ResultadoAcao(
                action=nome, action_id=action_id, ok=False, accepted=True,
                executed=False, mode=self.backend.modo, state="failed",
                message=(
                    f"A câmera do robô não devolveu imagem em {tentativas} tentativas. "
                    "Verifique se a mídia está adquirida e se nenhum outro app tomou a câmera."
                ),
                error="camera_indisponivel",
            )
        self.dir_capturas.mkdir(parents=True, exist_ok=True)
        caminho = self.dir_capturas / f"captura_{int(time.time())}_{action_id}.jpg"
        caminho.write_bytes(jpeg)
        dims = dimensoes_jpeg(jpeg)
        tamanho = f"{dims[0]}x{dims[1]} px, " if dims else ""
        return ResultadoAcao(
            action=nome, action_id=action_id, ok=True, accepted=True,
            executed=True, mode=self.backend.modo, state="completed",
            message=(
                f"Imagem capturada e salva em {caminho} ({tamanho}{len(jpeg)} bytes). "
                "Para saber o que aparece nela, use a ferramenta de visão."
            ),
            data={
                "path": str(caminho),
                "bytes": len(jpeg),
                "width": dims[0] if dims else None,
                "height": dims[1] if dims else None,
            },
        )

    # ── utilidades ──────────────────────────────────────────────────────────
    def _publicar(self, tipo: str, pedido: Pedido, **dados: Any) -> None:
        self.eventos.publicar(
            tipo, action_id=pedido.id, source=pedido.source,
            correlation_id=pedido.correlation_id, **dados,
        )

    def _publicar_status(self) -> None:
        try:
            self.eventos.publicar("robot.status", **self.status())
        except Exception:  # pragma: no cover
            log.debug("falha ao publicar status", exc_info=True)

    def _registrar_erro(self, acao: str, motivo: str) -> None:
        item = {"action": acao, "error": motivo, "ts": time.time()}
        with self._cond:
            self._erros.append(item)
        self.eventos.publicar("robot.error", action=acao, error=motivo)
