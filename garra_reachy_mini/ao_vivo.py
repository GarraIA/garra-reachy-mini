"""A conversa ao vivo, do lado do robô.

Quem decide que um enunciado é chamado é `roteador.py`, dentro do turno. Quem
ATENDE o chamado é o laço de voz, fora do turno, por aqui — é ele o dono do
microfone, e uma conversa em tempo real precisa do microfone o tempo todo.

Este build reconhece o chamado e ainda não conduz a conversa: responde que a
Hera não está disponível. O `Atendente` já é a costura por onde a conversa
entra — o laço não muda quando ela chegar.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .roteador import Chamado

log = logging.getLogger(__name__)

# Dito pela voz local do robô quando a conversa ao vivo não pode começar.
AVISO_INDISPONIVEL = "A Hera não está disponível agora."
# O aviso falado não se repete a cada chamado: quem insiste já ouviu.
AVISO_INTERVALO_S = 30.0


class Atendente:
    """Atende os chamados da conversa ao vivo. Um por laço de voz; roda na thread dele."""

    def __init__(self, publicar: Callable[..., Any], falar: Callable[[str], Any],
                 relogio: Callable[[], float] = time.monotonic) -> None:
        self._publicar = publicar
        self._falar = falar
        self._relogio = relogio
        self._avisou_em: float | None = None

    def atender(self, chamado: Chamado) -> None:
        """Bloqueia até a conversa acabar. Neste build ela nem começa."""
        self.indisponivel(chamado, "unsupported")

    def indisponivel(self, chamado: Chamado, motivo: str) -> None:
        """Diz, com evento e (de vez em quando) com a voz local, que não deu.

        O evento sai sempre e não leva nada do que foi dito — nem o pedido que
        veio junto do chamado. O robô nunca finge que atendeu, e o Garra não
        assume a conversa em silêncio.
        """
        self._publicar("voice.live.unavailable", destination=chamado.destino,
                       reason=motivo, match=chamado.como)
        agora = self._relogio()
        if self._avisou_em is not None and agora - self._avisou_em < AVISO_INTERVALO_S:
            return
        self._avisou_em = agora
        try:
            self._falar(AVISO_INDISPONIVEL)
        except Exception:
            # O alto-falante ocupado ou a síntese fora do ar não podem derrubar
            # o laço: o evento já contou o que houve.
            log.debug("aviso de conversa ao vivo indisponível não foi falado", exc_info=True)
