"""Comportamento ambiente: o robô parecer vivo sem atrapalhar ninguém.

Substitui o antigo `gestos.py`, que mandava `goto_target` direto no SDK a partir
de uma thread própria — o que, com a camada de ações no lugar, seria uma segunda
mão mexendo no robô sem passar pela fila.

Aqui tudo entra como **prioridade ambiente**, e a matriz de preempção garante o
resto: comando explícito sempre ganha, ambiente nunca interrompe nada e é
descartado quando chega na hora errada.

Três estados, dirigidos pelo loop de voz:

  OUVINDO     antenas em repouso, micro-movimentos raros da cabeça
  PENSANDO    antenas para trás, uma leve inclinação
  FALANDO     wobbling do daemon (balanço da cabeça reativo ao próprio áudio,
              já sincronizado no robô) + antenas alternando

O wobbling é a resposta certa para "sincronizar movimento com a fala": ele roda
dentro do daemon, lendo o áudio que está saindo do alto-falante. Qualquer
tentativa nossa de sincronizar por aqui competiria com ele e chegaria atrasada
pela rede.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from .acoes import PRIO_AMBIENTE

if TYPE_CHECKING:  # pragma: no cover
    from .acoes import ControladorRobo

log = logging.getLogger("garra_reachy_mini.robo.comportamento")

OUVINDO = "listening"
PENSANDO = "thinking"
FALANDO = "speaking"
OCIOSO = "idle"

# Intervalo entre micro-movimentos enquanto ouve. Longo de propósito: o robô tem
# de parecer atento, não inquieto.
MIN_INTERVALO_S = 6.0
MAX_INTERVALO_S = 14.0

# Amplitude dos micro-movimentos, em fração do envelope já limitado.
INTENSIDADE_MICRO = 0.12


class Comportamento:
    """Camada de vida do robô. Ligável e desligável a quente."""

    def __init__(
        self,
        controlador: "ControladorRobo",
        *,
        ativo: bool = True,
        tracking_ambiente: bool = False,
        peso_tracking: float = 0.35,
        wobbling: bool = True,
    ) -> None:
        self.ctrl = controlador
        self.ativo = ativo
        self.tracking_ambiente = tracking_ambiente
        self.peso_tracking = peso_tracking
        self.wobbling = wobbling

        self._estado = OCIOSO
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._acordar = threading.Event()
        self._proximo_micro = 0.0
        self._thread = threading.Thread(
            target=self._laco, name="comportamento", daemon=True
        )

    # ── ciclo de vida ───────────────────────────────────────────────────────
    def iniciar(self) -> None:
        self._thread.start()
        if self.tracking_ambiente:
            self.ctrl.tracking_pedir(True, self.peso_tracking, "ambiente")

    def encerrar(self) -> None:
        self._parar.set()
        self._acordar.set()
        try:
            self.ctrl.tracking_pedir(False, self.peso_tracking, "ambiente")
            if self.wobbling:
                self.ctrl.wobbling_pedir(False)
        except Exception:  # pragma: no cover
            pass

    # ── estados, chamados pelo loop de voz ──────────────────────────────────
    @property
    def estado(self) -> str:
        with self._lock:
            return self._estado

    def ouvindo(self) -> None:
        self._mudar(OUVINDO)
        if self.wobbling:
            self.ctrl.wobbling_pedir(False)
        self._gesto({"action": "move_antennas", "preset": "neutral", "duration": 0.4})

    def pensando(self) -> None:
        self._mudar(PENSANDO)
        if self.wobbling:
            self.ctrl.wobbling_pedir(False)
        self._gesto({"action": "move_antennas", "left": -0.6, "right": -0.6, "duration": 0.4})

    def falando(self) -> None:
        self._mudar(FALANDO)
        if self.wobbling:
            # Quem sincroniza com a fala é o daemon, que ouve o próprio
            # alto-falante. Nós só ligamos e saímos da frente.
            self.ctrl.wobbling_pedir(True)

    def _mudar(self, novo: str) -> None:
        with self._lock:
            anterior, self._estado = self._estado, novo
        if anterior != novo:
            self.ctrl.eventos.publicar("voice.state", state=novo, previous=anterior)
        self._agendar_micro()
        self._acordar.set()

    # ── micro-movimentos ────────────────────────────────────────────────────
    def _agendar_micro(self) -> None:
        atraso = self.ctrl._aleatorio.uniform(MIN_INTERVALO_S, MAX_INTERVALO_S)
        self._proximo_micro = time.monotonic() + atraso

    def _gesto(self, params: dict) -> None:
        """Enfileira em prioridade ambiente e NÃO espera.

        Não esperar é o ponto: o loop de voz chama isto entre um turno e outro,
        e não pode ficar preso atrás de um movimento. Se a ação for descartada
        por conflito, tudo bem — era só enfeite.
        """
        if not self.ativo:
            return
        nome = params.pop("action")
        try:
            self.ctrl.submeter(
                nome, params, prioridade=PRIO_AMBIENTE, source="ambiente", esperar=False
            )
        except Exception:  # pragma: no cover
            log.debug("gesto ambiente descartado", exc_info=True)

    def _laco(self) -> None:
        while not self._parar.is_set():
            self._acordar.wait(timeout=1.0)
            self._acordar.clear()
            if self._parar.is_set():
                return
            if not self.ativo or self.estado != OUVINDO:
                continue
            if time.monotonic() < self._proximo_micro:
                continue
            self._agendar_micro()
            # Um olhar curto para o lado e volta ao centro: o suficiente para
            # não parecer congelado, pequeno o bastante para não distrair.
            lado = self.ctrl._aleatorio.choice(["left", "right", "up"])
            self._gesto({
                "action": "turn_head", "direction": lado,
                "intensity": INTENSIDADE_MICRO, "duration": 1.2,
            })

    # ── ajustes a quente (usados pelo painel) ───────────────────────────────
    def configurar(
        self,
        *,
        ativo: bool | None = None,
        tracking_ambiente: bool | None = None,
        wobbling: bool | None = None,
    ) -> dict[str, bool]:
        if ativo is not None:
            self.ativo = ativo
        if wobbling is not None:
            self.wobbling = wobbling
            if not wobbling:
                self.ctrl.wobbling_pedir(False)
        if tracking_ambiente is not None:
            self.tracking_ambiente = tracking_ambiente
            self.ctrl.tracking_pedir(tracking_ambiente, self.peso_tracking, "ambiente")
        return {
            "ativo": self.ativo,
            "tracking_ambiente": self.tracking_ambiente,
            "wobbling": self.wobbling,
        }
