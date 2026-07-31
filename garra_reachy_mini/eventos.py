"""Eventos assíncronos do cérebro (resultados de tarefas delegadas etc.).

O poller do gateway apenas ENFILEIRA eventos aqui; quem fala, gesticula e
mexe no áudio é exclusivamente o loop principal, quando o usuário está em
silêncio.
"""

import queue
from dataclasses import dataclass


@dataclass
class EventoCerebro:
    tipo: str                 # "notificacao" (mensagem nova do agente) | "aviso"
    texto: str
    task_id: str | None = None
    seq: int | None = None    # posição no histórico — dedup provisório
                              # (ver ESPEC_GATEWAY_TAREFAS.md)


class FilaEventos:
    def __init__(self) -> None:
        self._fila: "queue.Queue[EventoCerebro]" = queue.Queue()

    def publicar(self, evento: EventoCerebro) -> None:
        self._fila.put(evento)

    def proximo(self) -> EventoCerebro | None:
        try:
            return self._fila.get_nowait()
        except queue.Empty:
            return None
