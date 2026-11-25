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
from typing import TYPE_CHECKING, Callable

from .acoes import PRIO_AMBIENTE

if TYPE_CHECKING:  # pragma: no cover
    from .acoes import ControladorRobo

log = logging.getLogger("garra_reachy_mini.robo.comportamento")

OUVINDO = "listening"
PENSANDO = "thinking"
FALANDO = "speaking"
OCIOSO = "idle"
# Conversa ao vivo (Hera): o corpo fica parado. Os micro-movimentos só acontecem
# em OUVINDO, e o balanço da cabeça é desligado.
AO_VIVO = "live"

# ── fase da voz ─────────────────────────────────────────────────────────────
# Os quatro estados acima dirigem os GESTOS, e bastam para isso. A fase é o que
# a tela mostra a quem pergunta "por que ele não responde?": mais fina, e com o
# motivo quando o robô está parado. `offline` não aparece aqui de propósito — um
# app fora do ar não tem como dizer que está fora do ar; quem conclui isso é o
# painel, quando o status deixa de chegar.
FASE_CONECTANDO = "connecting"          # o app subiu e ainda confere os serviços
FASE_ESPERA = "standby"                 # sem escuta: voz desligada ou sem servidor
FASE_AGUARDANDO_FRASE = "wake_listening"  # ouvindo só a frase de ativação
FASE_OUVINDO = "listening"              # ouvindo a pessoa (sessão aberta)
FASE_TRANSCREVENDO = "transcribing"
FASE_PROCESSANDO = "processing"         # o agente está pensando
FASE_FALANDO = "speaking"
FASE_ERRO = "error"
# A conversa ao vivo tem as suas: quem olha a tela precisa saber com quem fala, e
# que nesse intervalo o som da sala vai para a nuvem.
FASE_AO_VIVO_CONECTANDO = "live_connecting"
FASE_AO_VIVO_OUVINDO = "live_listening"
FASE_AO_VIVO_PENSANDO = "live_thinking"
FASE_AO_VIVO_FALANDO = "live_speaking"
FASES = (FASE_CONECTANDO, FASE_ESPERA, FASE_AGUARDANDO_FRASE, FASE_OUVINDO,
         FASE_TRANSCREVENDO, FASE_PROCESSANDO, FASE_FALANDO, FASE_ERRO,
         FASE_AO_VIVO_CONECTANDO, FASE_AO_VIVO_OUVINDO, FASE_AO_VIVO_PENSANDO,
         FASE_AO_VIVO_FALANDO)

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
        self._fase = FASE_CONECTANDO
        self._fase_motivo: str | None = None
        self._fase_desde = time.time()
        self._ultimo_erro: dict | None = None
        # Quem sabe se o robô está esperando a frase ou já ouvindo a pessoa é o
        # laço de voz (é ele o dono da sessão de ativação). Ele deixa aqui como
        # perguntar; sem laço, "ouvindo" é só ouvindo.
        self.fase_ao_ouvir: Callable[[], tuple[str, str | None]] | None = None
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
        mudou = self._mudar(OUVINDO)
        self.atualizar_fase_de_escuta()
        if not mudou:
            # Já estava ouvindo: nada a mexer. Sem isto, cada barulho da sala
            # (que o laço ouve, transcreve e descarta) movia as antenas — e o
            # servo, ao lado dos microfones, era ouvido como um barulho novo.
            # Medido em 24/11/2026: um trecho falso a cada 2 s, em laço.
            return
        if self.wobbling:
            self.ctrl.wobbling_pedir(False)
        self._gesto({"action": "move_antennas", "preset": "neutral", "duration": 0.4})

    def ativado(self) -> None:
        """A frase de ativação casou: as antenas sobem um instante.

        Não muda o estado — quem ouviu a frase continua ouvindo. É só o sinal
        físico de "ouvi você"; o `ouvindo()` ou o `pensando()` que vem a seguir
        devolve as antenas ao lugar.
        """
        self._gesto({"action": "move_antennas", "preset": "up", "duration": 0.25})

    def ao_vivo(self) -> None:
        """Conversa ao vivo: o corpo para e fica parado até ela acabar.

        Sem gesto nenhum aqui — nem para "parar": os servos ficam ao lado dos
        microfones, e o barulho deles iria para a nuvem junto com a conversa. O
        `ouvindo()` do fim devolve o robô ao normal.
        """
        self._mudar(AO_VIVO)
        if self.wobbling:
            self.ctrl.wobbling_pedir(False)

    def atualizar_fase_de_escuta(self) -> None:
        """Esperando a frase, ou ouvindo a pessoa? Pergunta a quem sabe."""
        provedor = self.fase_ao_ouvir
        if provedor is None:
            self.fase(FASE_OUVINDO)
            return
        try:
            self.fase(*provedor())
        except Exception:  # pragma: no cover - a tela não pode derrubar a voz
            log.debug("fase de escuta indisponível", exc_info=True)

    def pensando(self) -> None:
        """O pedido foi ACEITO e segue para o agente: antenas para trás.

        Só para pedido aceito. Enquanto o laço ainda está transcrevendo — sem
        saber se aquilo foi a frase de ativação ou a televisão — o robô não se
        mexe: a fase muda (`transcribing`), o corpo não.
        """
        if not self._mudar(PENSANDO):
            return
        if self.wobbling:
            self.ctrl.wobbling_pedir(False)
        self._gesto({"action": "move_antennas", "left": -0.6, "right": -0.6, "duration": 0.4})

    def falando(self) -> None:
        self._mudar(FALANDO)
        self.fase(FASE_FALANDO)
        if self.wobbling:
            # Quem sincroniza com a fala é o daemon, que ouve o próprio
            # alto-falante. Nós só ligamos e saímos da frente.
            self.ctrl.wobbling_pedir(True)

    # ── fase, para a tela ───────────────────────────────────────────────────
    def fase(self, nova: str, motivo: str | None = None) -> None:
        """Registra o que a voz está fazendo agora. Publica só quando muda."""
        if nova not in FASES:
            return
        with self._lock:
            if self._fase == nova and self._fase_motivo == motivo:
                return
            anterior = self._fase
            self._fase, self._fase_motivo, self._fase_desde = nova, motivo, time.time()
        self.ctrl.eventos.publicar("voice.phase", phase=nova, previous=anterior,
                                   reason=motivo)

    def erro(self, codigo: str, detalhe: str = "") -> None:
        """Um tropeço da voz (STT, TTS, agente). Fica visível por um tempo.

        Não prende a fase em `error`: o laço segue ouvindo, e a tela mostra o
        último erro com a hora — que é o que responde "por que ele não falou?".
        """
        registro = {"code": codigo, "detail": detalhe[:160], "at": time.time()}
        with self._lock:
            self._ultimo_erro = registro
        self.ctrl.eventos.publicar("voice.error", code=codigo, detail=detalhe[:160])

    def estado_da_voz(self) -> dict:
        with self._lock:
            return {"phase": self._fase, "reason": self._fase_motivo,
                    "since": self._fase_desde,
                    "last_error": dict(self._ultimo_erro) if self._ultimo_erro else None}

    def _mudar(self, novo: str) -> bool:
        """Troca o estado. Devolve se ele de fato mudou."""
        with self._lock:
            anterior, self._estado = self._estado, novo
        if anterior != novo:
            self.ctrl.eventos.publicar("voice.state", state=novo, previous=anterior)
        self._agendar_micro()
        self._acordar.set()
        return anterior != novo

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
