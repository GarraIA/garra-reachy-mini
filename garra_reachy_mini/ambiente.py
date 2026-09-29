"""O que o robô ouviu sem ser chamado — por pouco tempo, e só em memória.

Com a frase de ativação ligada, o que é dito sem ela é recusado e some. Este
módulo é o que muda isso, quando o dono liga a escuta de contexto: a fala
recusada fica guardada aqui por uma janela curta, e acompanha o próximo pedido
feito ao robô. É o que deixa "insira isso na lista" fazer sentido.

Quatro coisas que este módulo garante:

1. **Só memória.** Nada aqui escreve em disco, em log ou em evento. Quando o app
   para, o que foi ouvido acabou.
2. **Some sozinho.** O que saiu da janela é descartado a cada fala nova, não só
   na colheita: conversa de uma hora atrás não fica em RAM esperando alguém
   chamar o robô.
3. **Uma vez só.** `colher` esvazia. A mesma fala não vai duas vezes ao modelo.
4. **Texto não confiável.** O que entra veio de um microfone numa sala — pode
   ser a TV. Fala com colchete é DESCARTADA, não limpa: colchete é o que
   delimita os blocos da mensagem montada em `envelope`, e limpar deixaria
   passar justamente o resto da frase que tentou se passar por bloco.

Sem thread, sem timer, sem task: a janela é uma comparação feita na hora.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

JANELA_PADRAO_S = 120.0
# Dois minutos de conversa de sala dão umas 300 palavras. O teto existe para o
# caso que não é conversa: uma TV ligada despejando texto sem parar.
MAX_CARACTERES_PADRAO = 4000


@dataclass(frozen=True)
class Fala:
    """Uma fala ouvida. `quando` é o relógio monotônico da captura."""

    quando: float
    texto: str

    def __repr__(self) -> str:
        # O texto fica de fora de propósito: um `log.info(fala)` por engano não
        # pode pôr a conversa da casa num arquivo.
        return f"Fala(quando={self.quando:.1f}, {len(self.texto)} caracteres)"


class MemoriaAmbiente:
    def __init__(self, janela_s: float = JANELA_PADRAO_S,
                 max_caracteres: int = MAX_CARACTERES_PADRAO) -> None:
        self.janela_s = float(janela_s)
        self.max_caracteres = int(max_caracteres)
        self._falas: list[Fala] = []
        # O laço de voz anota; o painel pode mandar esquecer, de outra thread.
        self._trava = threading.Lock()

    def __len__(self) -> int:
        with self._trava:
            return len(self._falas)

    def __repr__(self) -> str:
        with self._trava:
            total = sum(len(f.texto) for f in self._falas)
            return (f"MemoriaAmbiente({len(self._falas)} falas, {total} caracteres, "
                    f"janela {self.janela_s:.0f} s)")

    def medida(self) -> dict[str, int]:
        """Quanto há guardado — para o painel, que não pode ver o quê."""
        with self._trava:
            return {"utterances": len(self._falas),
                    "chars": sum(len(f.texto) for f in self._falas)}

    def anotar(self, texto: str, quando: float) -> None:
        limpo = re.sub(r"\s+", " ", texto or "").strip()
        if not limpo or "[" in limpo or "]" in limpo:
            return
        limpo = limpo[:self.max_caracteres]
        with self._trava:
            self._falas.append(Fala(float(quando), limpo))
            self._podar(float(quando))

    def colher(self, agora: float) -> list[Fala]:
        """As falas ainda dentro da janela, da mais antiga à mais nova. Esvazia."""
        with self._trava:
            self._podar(float(agora))
            falas, self._falas = self._falas, []
            return falas

    def limpar(self) -> None:
        with self._trava:
            self._falas = []

    def _podar(self, agora: float) -> None:
        self._falas = [f for f in self._falas if agora - f.quando < self.janela_s]
        total = sum(len(f.texto) for f in self._falas)
        while len(self._falas) > 1 and total > self.max_caracteres:
            total -= len(self._falas.pop(0).texto)


__all__ = ["Fala", "JANELA_PADRAO_S", "MAX_CARACTERES_PADRAO", "MemoriaAmbiente"]
