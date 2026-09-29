"""O que acompanha o pedido: a escuta de contexto e o caderno, ligados ao app.

`ambiente`, `caderno` e `envelope` não sabem de configuração nem de evento —
são contas e texto. Este módulo é quem lê os interruptores e publica o que
aconteceu, e por isso é aqui que mora a promessa de privacidade:

- recurso desligado, nada do que foi dito sem a frase de ativação fica em lugar
  nenhum; e o que JÁ tinha sido ouvido é esquecido na hora em que se desliga,
  não na próxima vez que alguém chamar o robô;
- os eventos dizem quanto, nunca o quê.

A configuração chega por parâmetro, lida por quem chama a cada uso: é o que faz
o interruptor do painel valer na fala seguinte, sem reiniciar o app.
"""

from __future__ import annotations

from typing import Any

from . import caderno as _caderno
from . import envelope
from .ambiente import MemoriaAmbiente

_OPERACAO = {"+": "add", "-": "remove"}


def _escuta_ligada(conversa: dict) -> bool:
    # Sem a frase de ativação toda fala já é um turno: não existe "ambiente", e
    # guardar seria mandar tudo ao modelo em dobro.
    return bool(conversa.get("ambient_context_enabled")
                and conversa.get("wake_phrase_enabled"))


class Acompanhamento:
    def __init__(self, eventos: Any, caderno: _caderno.Caderno | None = None,
                 memoria: MemoriaAmbiente | None = None) -> None:
        self.eventos = eventos
        self.caderno = caderno or _caderno.Caderno()
        self.memoria = memoria or MemoriaAmbiente()

    def _ajustar(self, conversa: dict) -> bool:
        """Aplica os interruptores à memória. Devolve se a escuta está ligada."""
        ligada = _escuta_ligada(conversa)
        if not ligada:
            self.memoria.limpar()
            return False
        self.memoria.janela_s = float(conversa.get("ambient_context_window_s") or 120)
        return True

    # ── o que foi dito sem chamar o robô ─────────────────────────────────────
    def ouviu(self, texto: str, capturado_em: float, conversa: dict) -> bool:
        """Uma fala recusada pela frase de ativação. Devolve se foi guardada."""
        if not self._ajustar(conversa):
            return False
        antes = len(self.memoria)
        self.memoria.anotar(texto, capturado_em)
        return len(self.memoria) > antes

    def esquecer(self) -> int:
        """Esquece agora o que foi ouvido. Devolve quantas falas havia."""
        quantas = len(self.memoria)
        self.memoria.limpar()
        return quantas

    # ── o pedido ─────────────────────────────────────────────────────────────
    def montar(self, pedido: str, agora: float, conversa: dict,
               correlacao: str | None = None, com_ambiente: bool = True) -> str:
        """A mensagem que vai ao modelo.

        `com_ambiente=False` é o chat do painel: quem digita não estava sendo
        ouvido, e o contexto continua esperando o pedido por voz.
        """
        falas = []
        if self._ajustar(conversa) and com_ambiente:
            falas = self.memoria.colher(agora)
        listas = self.caderno.ler() if conversa.get("notebook_enabled") else None
        if falas:
            # `span_s` é até onde o contexto alcança para trás, contado do
            # pedido: a idade da fala mais antiga que foi junto.
            self.eventos.publicar(
                "voice.context.attached", correlation_id=correlacao,
                utterances=len(falas),
                span_s=max(0, int(agora - min(f.quando for f in falas))),
                chars=sum(len(f.texto) for f in falas))
        return envelope.compor(pedido, falas=falas, agora=agora, listas=listas)

    # ── a resposta ───────────────────────────────────────────────────────────
    def receber(self, resposta: str, conversa: dict,
                correlacao: str | None = None) -> str:
        """A resposta sem as linhas de caderno, com as anotações já feitas."""
        if not conversa.get("notebook_enabled"):
            return _caderno.sem_diretivas(resposta)
        texto, mudancas = self.caderno.aplicar(resposta)
        for m in mudancas:
            self.eventos.publicar(
                "notebook.changed", correlation_id=correlacao, list=m.lista,
                op=_OPERACAO[m.op], applied=m.aplicada, items=m.itens)
        return texto


__all__ = ["Acompanhamento"]
