"""A sentinela: o computador ouve, ele mesmo, o "pare" e o "Fala Garra".

A conversa ao vivo tem dois ouvidos para os comandos. Um é a transcrição da
sessão, que vem da nuvem. O outro é este, local: o mesmo som do robô que sobe
para a OpenAI passa pelo detector de fala do Garra (`segmentador.py`), e cada
trecho curto é transcrito pelo Whisper do servidor de voz, nesta máquina. O que
um dos dois ouvir vale: na dúvida, para.

Só "pare" e "Fala Garra". A despedida fica com a nuvem: medido em 25/11/2026 no
robô, o Whisper escreveu "Tchau." para trechos quase mudos (resto de eco, ruído
de servo). Um "pare" inventado encerra a conversa, que é o lado seguro; um
"tchau" inventado também encerraria, sem ninguém ter pedido.

O texto não fica: vira o nome de um comando, ou nada. Não vai para registro nem
para o robô. O trecho vai junto do "Fala Garra", em milissegundos do relógio do
próprio robô, para que ele mesmo ouça de novo o que foi dito ao Garra.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import Any, Callable

import numpy as np

from garra_reachy_mini.segmentador import Segmentador

from . import comandos

log = logging.getLogger(__name__)

TAXA = 16000
COMANDOS = ("stop", "handoff_garra")
# "Fala Garra, que horas são e vai chover amanhã?" cabe; um parágrafo não é comando.
DURACAO_MAX_S = 8.0
# Abaixo disto o Whisper não tinha certeza do que escreveu (média por palavra).
CONFIANCA_MIN = 0.5
PRAZO_WHISPER_S = 5.0
_FILA_MAX = 2                     # trechos à espera: o mais novo é o que importa


def decidir(texto: str, confianca: list[float] | None) -> str | None:
    """O comando que o trecho transcrito pede, se for um dos daqui."""
    nome = comandos.comando(texto or "")
    if nome not in COMANDOS:
        return None
    if confianca and float(np.mean(confianca)) < CONFIANCA_MIN:
        return None
    return nome


def transcrever_local(audio: np.ndarray) -> tuple[str, list[float] | None]:
    """O Whisper do servidor de voz, por loopback. Sem dica: a dica inventa o que espera."""
    import requests

    from .. import voz

    r = requests.post(f"{voz.url_local()}/transcrever?sr={TAXA}",
                      data=np.asarray(audio, dtype=np.float32).tobytes(),
                      headers=voz._cabecalhos(), timeout=PRAZO_WHISPER_S)
    r.raise_for_status()
    dados = r.json()
    confianca = dados.get("confianca")
    return str(dados.get("texto") or ""), confianca if isinstance(confianca, list) else None


class Sentinela:
    """Recebe o som do robô em blocos; avisa `ao_comando(nome, trecho_ms)`."""

    def __init__(self, transcrever: Callable[[np.ndarray], tuple[str, list[float] | None]],
                 ao_comando: Callable[[str, tuple[int, int] | None], Any], *,
                 eco: Callable[[str], bool] | None = None) -> None:
        self._transcrever = transcrever
        self._ao_comando = ao_comando
        self._eco = eco
        self._detector = Segmentador(TAXA)
        self._amostras = 0
        # (primeira amostra do bloco, carimbo do robô em ms) dos últimos blocos
        self._carimbos: collections.deque[tuple[int, int]] = collections.deque(maxlen=400)
        self._fila: collections.deque[tuple[np.ndarray, tuple[int, int] | None]] = \
            collections.deque(maxlen=_FILA_MAX)
        self._chegou = asyncio.Event()

    # ── do canal, no laço da ponte ───────────────────────────────────────────
    def ouvir(self, pcm: bytes, t_ms: int | None = None) -> None:
        onda = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32) / 32768.0
        if onda.size == 0:
            return
        if t_ms is not None:
            self._carimbos.append((self._amostras, int(t_ms)))
        self._amostras += onda.size
        trecho = self._detector.bloco(onda, self._amostras / TAXA)
        if trecho is None or trecho.audio.size > DURACAO_MAX_S * TAXA:
            return
        fim = self._amostras
        self._fila.append((trecho.audio, self._no_relogio_do_robo(fim - trecho.audio.size, fim)))
        self._chegou.set()

    def _quando(self, amostra: int) -> int | None:
        base = None
        for inicio, t_ms in self._carimbos:
            if inicio > amostra:
                break
            base = (inicio, t_ms)
        if base is None:
            return None
        return base[1] + (amostra - base[0]) * 1000 // TAXA

    def _no_relogio_do_robo(self, ini: int, fim: int) -> tuple[int, int] | None:
        a, b = self._quando(ini), self._quando(fim)
        return (a, b) if a is not None and b is not None else None

    # ── o trabalho, uma transcrição por vez ──────────────────────────────────
    async def rodar(self) -> None:
        while True:
            await self._chegou.wait()
            self._chegou.clear()
            while self._fila:
                audio, trecho = self._fila.popleft()
                try:
                    texto, confianca = await asyncio.to_thread(self._transcrever, audio)
                except asyncio.CancelledError:
                    raise
                except Exception as erro:
                    log.info("sentinela: o Whisper não atendeu (%s)", type(erro).__name__)
                    continue
                nome = decidir(texto, confianca)
                if nome is None:
                    continue
                if self._eco is not None and self._eco(nome):
                    log.info("sentinela: %s ouvido, mas era a voz dela", nome)
                    continue
                log.info("sentinela: %s ouvido no computador", nome)
                self._ao_comando(nome, trecho)
