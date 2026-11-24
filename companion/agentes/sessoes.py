"""As conversas que a ponte guarda para os agentes atendidos aqui.

O robô fala o protocolo de sessão do gateway (`POST /api/sessions`,
`…/messages`, `…/history`). Para um agente que não é o gateway, quem representa
a sessão é este registro em memória: um id que o robô conhece, o histórico que
ele pode reler, e o `ref` com que o agente chama a mesma conversa.

Em memória de propósito. Reiniciado o companion, o robô recebe 404 na sessão
antiga e abre outra sozinho — é o mesmo caminho de quando o gateway reinicia —,
e nenhuma conversa da casa fica gravada num arquivo deste serviço.
"""

from __future__ import annotations

import threading
import time
import uuid

from .base import Conversa

MAXIMO = 64                    # conversas vivas; a mais parada sai
VALIDADE_S = 6 * 3600.0        # sem uso por isto, a conversa é esquecida
MENSAGENS_MAX = 80             # histórico guardado por conversa


class Sessoes:
    def __init__(self, maximo: int = MAXIMO, validade_s: float = VALIDADE_S) -> None:
        self._maximo, self._validade = maximo, validade_s
        self._conversas: dict[str, Conversa] = {}
        self._trava = threading.Lock()

    def criar(self, agente: str) -> Conversa:
        conversa = Conversa(id=str(uuid.uuid4()), agente=agente)
        with self._trava:
            self._podar()
            while len(self._conversas) >= self._maximo:
                mais_parada = min(self._conversas.values(), key=lambda c: c.usada_em)
                del self._conversas[mais_parada.id]
            self._conversas[conversa.id] = conversa
        return conversa

    def obter(self, ident: str) -> Conversa | None:
        with self._trava:
            self._podar()
            return self._conversas.get(ident)

    def remover(self, ident: str) -> Conversa | None:
        with self._trava:
            return self._conversas.pop(ident, None)

    def esquecer(self, agente: str) -> list[Conversa]:
        """Tira todas as conversas de um agente (ele mudou, ou saiu)."""
        with self._trava:
            saem = [c for c in self._conversas.values() if c.agente == agente]
            for c in saem:
                del self._conversas[c.id]
            return saem

    def registrar(self, conversa: Conversa, pedido: str, resposta: str) -> None:
        with self._trava:
            conversa.mensagens += [{"role": "user", "content": pedido},
                                   {"role": "assistant", "content": resposta}]
            del conversa.mensagens[:-MENSAGENS_MAX]
            conversa.usada_em = time.time()

    def __len__(self) -> int:
        with self._trava:
            return len(self._conversas)

    def _podar(self) -> None:
        corte = time.time() - self._validade
        for ident in [i for i, c in self._conversas.items() if c.usada_em < corte]:
            del self._conversas[ident]
