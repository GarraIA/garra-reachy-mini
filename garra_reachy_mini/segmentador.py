"""Onde começa e onde termina cada fala — o detector do laço de voz.

Morava dentro de `_laco_voz`, como meia dúzia de `if`. Saiu de lá no dia em que
o robô parou de responder e não havia como testar o porquê.

O que se mediu em 29/09/2026 (duração e energia do que chegava ao servidor de
voz; o conteúdo nunca foi olhado):

* Um "trecho de fala" de 182 segundos sem palavra nenhuma. Três minutos com o
  detector aberto, e portanto três minutos sem ouvir ninguém.
* O ruído de fundo indo de 0,007 a 0,045 sem que nada mudasse na sala. A placa
  de áudio do robô tem ganho automático (de 1× a 16×): depois de alguém falar
  baixo ou de longe ela amplifica tudo, e o ruído sobe junto.
* O limiar era UM número, medido em 1,2 s na partida. Com ruído acima dele o
  detector abria e não fechava, porque a duração máxima só era conferida nos
  blocos abaixo do limiar — exatamente os que não chegavam.

Daí as três regras daqui:

1. O limiar acompanha o ruído. Vale a RAZÃO entre o som e o piso, que o ganho
   automático não altera, e não o valor absoluto, que ele altera à vontade.
2. O piso é o décimo percentil dos últimos segundos, não a média: numa fala
   sempre há os vãos entre as sílabas, então fala comprida não vira "ruído",
   e ruído contínuo vira em poucos segundos.
3. A duração máxima vale sempre.

Nada aqui conhece texto. Entra áudio, sai áudio; `medida()` só tem números.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .config import FALA_MAXIMA_S, FIM_DE_FALA_S, PRE_ROLL_S
from .voz import PreRoll

# Abaixo disto não se abre nem no quarto mais silencioso: é o ruído elétrico do
# próprio microfone, e quatro vezes quase nada ainda é quase nada.
LIMIAR_MINIMO = 0.006
# Abrir pede quatro vezes o piso (o que o laço sempre pediu). Continuar pede
# menos: quem baixa a voz no fim da frase ainda está falando.
K_ABRE = 4.0
K_FECHA = 2.5
# Som acima do limiar por menos que isto é estalo, não ataque de palavra.
ATAQUE_S = 0.06
# E um trecho com menos que isto de som, somado, não vale uma transcrição. O
# "pare" mais curto que se mediu tem 0,3 s.
ATIVO_MINIMO_S = 0.2

# O piso olha esta janela de áudio…
JANELA_PISO_S = 3.0
# …e só opina com pelo menos isto dentro dela. Meio segundo de fala não é
# amostra de ruído de coisa nenhuma.
JANELA_MINIMA_S = 1.5
PERCENTIL_PISO = 10.0
# Desce depressa (o silêncio voltou, e quem fala baixo tem de ser ouvido já) e
# sobe devagar (para não tomar por ruído a fala que acabou de começar).
TAU_DESCE_S = 0.3
TAU_SOBE_S = 2.0
# Áudio que deixou de chegar por mais que isto não é continuação do anterior:
# o laço esteve ocupado transcrevendo ou falando.
VAO_S = 1.0
PISO_PADRAO = 0.0015
# Faixas do que o dono pode ajustar. Fora delas o detector deixa de ser um
# detector: com ganho 4 o ruído do próprio motor abre trecho, e com 0,25 só
# grito abre.
SENSIBILIDADE_MIN, SENSIBILIDADE_MAX = 0.4, 2.5
FIM_DE_FALA_MIN_S, FIM_DE_FALA_MAX_S = 0.3, 2.5


@dataclass
class Trecho:
    """Um pedaço de áudio pronto para transcrever."""

    audio: np.ndarray
    # "silence" (quem falava parou), "no_audio" (o microfone parou de entregar)
    # ou "max_duration" (a trava).
    motivo: str
    ativo_s: float

    def __repr__(self) -> str:       # nunca as amostras
        return (f"Trecho({self.audio.size} amostras, motivo={self.motivo!r}, "
                f"ativo_s={self.ativo_s:.2f})")


class Segmentador:
    """Recebe o microfone em blocos e devolve os trechos de fala."""

    def __init__(self, sr: int, *, piso_inicial: float | None = None,
                 limiar_fixo: float | None = None, sensibilidade: float = 1.0,
                 fim_de_fala_s: float = FIM_DE_FALA_S) -> None:
        self.sr = int(sr)
        self._fixo = float(limiar_fixo) if limiar_fixo is not None else None
        self._ganho = 1.0
        self._fim_de_fala_s = FIM_DE_FALA_S
        self.ajustar(sensibilidade=sensibilidade, fim_de_fala_s=fim_de_fala_s)
        self._piso = float(piso_inicial) if piso_inicial else PISO_PADRAO
        # (duração, rms) dos últimos JANELA_PISO_S de áudio.
        self._janela: deque[tuple[float, float]] = deque()
        self._na_janela = 0.0
        self._pre = PreRoll(int((PRE_ROLL_S + ATAQUE_S) * self.sr))
        self._buffer: list[np.ndarray] = []
        self.em_fala = False
        self._duracao = 0.0          # do trecho aberto
        self._ativo = 0.0            # quanto dele foi som
        self._calado = 0.0           # há quanto áudio não há som
        self._ataque = 0.0           # som seguido antes de abrir
        self._ultimo_bloco_em: float | None = None
        self._blocos_ms: deque[float] = deque(maxlen=50)
        self._trechos = 0
        self._curtos = 0
        self._cortados = 0

    # ── ajuste do dono ───────────────────────────────────────────────────────
    def ajustar(self, *, sensibilidade: float | None = None,
                fim_de_fala_s: float | None = None) -> None:
        """Sensibilidade do microfone e a pausa que encerra a fala, a quente.

        `sensibilidade` divide os limiares: 2.0 abre com metade do som (ouve
        quem fala baixo ou longe, e mais ruído também); 0.5 pede o dobro. Não
        mexe no piso — o ruído da sala é medido, não escolhido. `fim_de_fala_s`
        é quanto silêncio encerra o trecho: curto corta quem pausa para pensar,
        longo atrasa toda resposta.
        """
        if sensibilidade is not None:
            self._ganho = min(SENSIBILIDADE_MAX, max(SENSIBILIDADE_MIN, float(sensibilidade)))
        if fim_de_fala_s is not None:
            self._fim_de_fala_s = min(FIM_DE_FALA_MAX_S, max(FIM_DE_FALA_MIN_S, float(fim_de_fala_s)))

    # ── limiares ─────────────────────────────────────────────────────────────
    @property
    def limiar_abre(self) -> float:
        if self._fixo is not None:
            return self._fixo
        return max(LIMIAR_MINIMO, K_ABRE * self._piso) / self._ganho

    @property
    def limiar_fecha(self) -> float:
        if self._fixo is not None:
            return self._fixo
        return self.limiar_abre * (K_FECHA / K_ABRE)

    def _acompanhar(self, dur: float, rms: float) -> None:
        self._janela.append((dur, rms))
        self._na_janela += dur
        while self._na_janela - self._janela[0][0] >= JANELA_PISO_S:
            self._na_janela -= self._janela.popleft()[0]
        if self._na_janela < JANELA_MINIMA_S:
            return
        base = float(np.percentile([r for _, r in self._janela], PERCENTIL_PISO))
        tau = TAU_DESCE_S if base < self._piso else TAU_SOBE_S
        self._piso += (base - self._piso) * min(1.0, dur / tau)

    # ── entrada ──────────────────────────────────────────────────────────────
    def bloco(self, mono: np.ndarray, agora: float) -> Trecho | None:
        """Um bloco do microfone. Devolve o trecho, se este bloco fechou um."""
        if mono.size == 0:
            return self.sem_audio(agora)
        dur = mono.size / self.sr
        rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
        if self._ultimo_bloco_em is not None and agora - self._ultimo_bloco_em > VAO_S:
            self._janela.clear()
            self._na_janela = 0.0
        self._ultimo_bloco_em = agora
        self._blocos_ms.append(dur * 1000.0)
        self._acompanhar(dur, rms)

        if not self.em_fala:
            self._pre.guardar(mono)
            if rms < self.limiar_abre:
                self._ataque = 0.0
                return None
            self._ataque += dur
            if self._ataque < ATAQUE_S:
                return None
            # Abriu. O anel já tem o ataque e o que veio antes dele.
            self.em_fala = True
            self._buffer = self._pre.drenar()
            self._duracao = sum(b.size for b in self._buffer) / self.sr
            self._ativo, self._calado, self._ataque = self._ataque, 0.0, 0.0
            return self._conferir()

        self._buffer.append(mono)
        self._duracao += dur
        if rms >= self.limiar_fecha:
            self._ativo += dur
            self._calado = 0.0
        else:
            self._calado += dur
        return self._conferir()

    def sem_audio(self, agora: float) -> Trecho | None:
        """O microfone não tinha nada para entregar."""
        if (self.em_fala and self._ultimo_bloco_em is not None
                and agora - self._ultimo_bloco_em > self._fim_de_fala_s):
            return self._fechar("no_audio")
        return None

    def descartar(self) -> None:
        """Esquece o trecho aberto e o anel. O piso fica: o ruído da sala não
        mudou porque o robô falou."""
        self.em_fala = False
        self._buffer = []
        self._pre.limpar()
        self._duracao = self._ativo = self._calado = self._ataque = 0.0

    # ── saída ────────────────────────────────────────────────────────────────
    def _conferir(self) -> Trecho | None:
        if self._duracao >= FALA_MAXIMA_S:
            self._cortados += 1
            return self._fechar("max_duration")
        if self._calado > self._fim_de_fala_s:
            return self._fechar("silence")
        return None

    def _fechar(self, motivo: str) -> Trecho | None:
        audio = (np.concatenate(self._buffer) if self._buffer
                 else np.empty(0, dtype=np.float32))
        ativo = self._ativo
        self.descartar()
        if ativo < ATIVO_MINIMO_S:
            self._curtos += 1
            return None
        self._trechos += 1
        return Trecho(audio, motivo, ativo)

    def medida(self) -> dict[str, float | int | bool]:
        blocos = sorted(self._blocos_ms)
        return {
            "floor": round(self._piso, 5),
            "open_threshold": round(self.limiar_abre, 5),
            "close_threshold": round(self.limiar_fecha, 5),
            "fixed_threshold": self._fixo is not None,
            "in_speech": self.em_fala,
            "segments": self._trechos,
            "dropped_short": self._curtos,
            "cut_by_max": self._cortados,
            "block_ms": round(blocos[len(blocos) // 2], 1) if blocos else 0.0,
        }
