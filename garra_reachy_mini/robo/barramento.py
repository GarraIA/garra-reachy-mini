"""Barramento de eventos do robô: quem executou o quê, quando.

É a fonte de verdade da interface. O painel não infere "o Garra virou a cabeça"
a partir do texto da resposta — ele mostra o que este barramento disse que
começou e terminou de fato no hardware.

Regra dura de acoplamento: publicar **nunca** pode bloquear quem está mexendo no
robô. Um navegador lento com um WebSocket entupido não pode segurar a thread
executora. Por isso cada assinante tem fila própria e limitada, e o descarte
acontece do lado do assinante, não do publicador.
"""

from __future__ import annotations

import itertools
import queue
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

# Eventos que nunca podem ser descartados por fila cheia: se o assinante perder
# um destes, ele passa a mostrar um robô parado que na verdade está em falha.
CRITICOS = frozenset(
    {
        "robot.error",
        "robot.estop",
        "robot.action.failed",
        "robot.state",
    }
)

TAMANHO_FILA_ASSINANTE = 100
TAMANHO_HISTORICO = 400


@dataclass(frozen=True)
class Evento:
    """Um acontecimento do robô, pronto para virar JSON.

    `action_id` amarra queued → started → completed da mesma ação.
    `correlation_id` amarra a ação à mensagem de chat/voz que a originou, para
    o painel conseguir montar a linha do tempo completa:
    mensagem → ferramenta → ação → movimento → resposta.
    """

    event_id: str
    type: str
    timestamp: str
    dados: dict[str, Any] = field(default_factory=dict)
    action_id: str | None = None
    source: str | None = None
    correlation_id: str | None = None

    def json(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "event_id": self.event_id,
            "type": self.type,
            "timestamp": self.timestamp,
        }
        for chave, valor in (
            ("action_id", self.action_id),
            ("source", self.source),
            ("correlation_id", self.correlation_id),
        ):
            if valor is not None:
                base[chave] = valor
        base.update(self.dados)
        return base


class Assinatura:
    """Fila de um assinante. Use como context manager para não vazar."""

    def __init__(self, barramento: "Barramento", maxsize: int) -> None:
        self._barramento = barramento
        self.fila: queue.Queue[Evento] = queue.Queue(maxsize=maxsize)
        self.descartados = 0
        self.fechada = False

    def _entregar(self, evento: Evento) -> None:
        """Chamado com o lock do barramento. Não pode bloquear."""
        try:
            self.fila.put_nowait(evento)
            return
        except queue.Full:
            pass

        if evento.type in CRITICOS:
            # Abre espaço à força: descartar um status é aceitável, descartar um
            # erro não é.
            try:
                self.fila.get_nowait()
                self.descartados += 1
                self.fila.put_nowait(evento)
                return
            except (queue.Empty, queue.Full):  # pragma: no cover - corrida rara
                return
        self.descartados += 1

    def obter(self, timeout: float | None = None) -> Evento | None:
        try:
            return self.fila.get(timeout=timeout)
        except queue.Empty:
            return None

    def fechar(self) -> None:
        self.fechada = True
        self._barramento.cancelar(self)

    def __enter__(self) -> "Assinatura":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.fechar()


class Barramento:
    """Pub/sub em memória, seguro entre threads."""

    def __init__(self, tamanho_historico: int = TAMANHO_HISTORICO) -> None:
        self._lock = threading.Lock()
        self._assinantes: list[Assinatura] = []
        self._historico: deque[Evento] = deque(maxlen=tamanho_historico)
        self._contador = itertools.count(1)

    # ── publicação ──────────────────────────────────────────────────────────
    def publicar(
        self,
        tipo: str,
        *,
        action_id: str | None = None,
        source: str | None = None,
        correlation_id: str | None = None,
        **dados: Any,
    ) -> Evento:
        evento = Evento(
            event_id=f"evt_{next(self._contador)}_{uuid.uuid4().hex[:8]}",
            type=tipo,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            dados=dados,
            action_id=action_id,
            source=source,
            correlation_id=correlation_id,
        )
        with self._lock:
            self._historico.append(evento)
            alvos = list(self._assinantes)
        # Entrega fora do lock: `_entregar` não bloqueia, mas manter o lock curto
        # evita que N assinantes serializem o publicador.
        for a in alvos:
            a._entregar(evento)
        return evento

    # ── assinatura ──────────────────────────────────────────────────────────
    def assinar(self, maxsize: int = TAMANHO_FILA_ASSINANTE) -> Assinatura:
        a = Assinatura(self, maxsize)
        with self._lock:
            self._assinantes.append(a)
        return a

    def cancelar(self, assinatura: Assinatura) -> None:
        with self._lock:
            if assinatura in self._assinantes:
                self._assinantes.remove(assinatura)

    @property
    def n_assinantes(self) -> int:
        with self._lock:
            return len(self._assinantes)

    # ── histórico ───────────────────────────────────────────────────────────
    def historico(self, n: int = 100, tipos: frozenset[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            itens: Iterator[Evento] = reversed(self._historico)
            saida = []
            for e in itens:
                if tipos is not None and e.type not in tipos:
                    continue
                saida.append(e.json())
                if len(saida) >= n:
                    break
        saida.reverse()
        return saida
