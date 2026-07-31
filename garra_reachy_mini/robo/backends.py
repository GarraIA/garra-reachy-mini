"""Backends do robô: a única fronteira entre a nossa lógica e o hardware.

`ControladorRobo` não sabe o que é REST nem o que é SDK. Ele fala com um
`RoboBackend`, e é aqui que se decide qual caminho usar para cada coisa.

## Por que o movimento vai pelo REST do daemon, e não pelo SDK

`ReachyMini.goto_target()` **bloqueia** até a trajetória acabar e **não é
cancelável**: `ReachyMini.cancel_move()` só levanta uma flag local do loop
client-side de `play_move`, e `CancelMoveCmd` só atinge moves enviados por
upload (`protocol.py:661-669`). Um e-stop no meio de um `goto_target` de 6 s não
teria efeito nenhum.

`POST /api/move/goto` do daemon devolve um `uuid` imediatamente
(`create_move_task` → `asyncio.create_task`) e `POST /api/move/stop` faz
`task.cancel()` de verdade (`routers/move.py:111-129`). Isso nos dá as três
coisas que a camada de ações precisa: não bloqueia, é cancelável no meio, e roda
no robô (sem tremer por Wi-Fi). Mesma máquina para os moves gravados.

## A armadilha do "movimento ignorado"

`Backend.play_move` começa com:

    if not self._try_start_move():
        self.logger.warning("Ignoring play_move request: another move is running.")
        return

Ou seja: pedir um movimento novo enquanto outro roda **não enfileira e não
preempta — some em silêncio**. Como `goto_target` também passa por `play_move`,
isso vale para tudo. Por isso `parar_e_aguardar()` existe e o controlador SEMPRE
o chama antes de começar um movimento explícito.

O SDK continua sendo usado para o que o REST não cobre: frames da câmera
(a sessão de mídia é dele) e a matemática de `look_at` (que precisa da
calibração da câmera).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Protocol

import numpy as np
import numpy.typing as npt

from . import limites
from .daemon_api import DATASET_DANCAS, DATASET_EMOCOES, DaemonAPI, ErroDaemon

log = logging.getLogger("garra_reachy_mini.robo.backends")

# Intervalo de sondagem de `/api/move/running`. 0,2 s dá ~5 req/s durante uma
# dança — barato — e mantém a latência de detecção de fim abaixo de um piscar.
INTERVALO_SONDA_S = 0.2

# Um move recém-criado leva alguns milissegundos para aparecer em
# `/api/move/running`. Se nunca aparecer dentro dessa janela, tratamos como já
# terminado em vez de esperar o timeout inteiro.
GRACA_APARECER_S = 1.5

CANCELADO = "cancelled"
CONCLUIDO = "completed"
EXPIRADO = "timeout"


class RoboBackend(Protocol):
    """Contrato síncrono. Quem chama é a thread executora."""

    modo: str  # "real" | "simulated"

    def disponivel(self) -> bool: ...
    def preparar(self) -> None: ...
    def estado(self) -> dict[str, Any]: ...

    # movimento (não bloqueia; devolve um identificador cancelável)
    def goto(
        self,
        *,
        pose: npt.NDArray[np.float64] | None = None,
        antenas: list[float] | None = None,
        body_yaw: float | None = 0.0,
        duracao: float = limites.DURACAO_PADRAO_S,
        interpolacao: str = "minjerk",
    ) -> str: ...
    def tocar_move(self, dataset: str, nome: str) -> str: ...
    def wake_up(self) -> str: ...
    def goto_sleep(self) -> str: ...
    def esperar(
        self, ident: str, timeout: float, cancelado: Callable[[], bool]
    ) -> str: ...
    def moves_rodando(self) -> list[str]: ...
    def parar_ident(self, ident: str) -> bool: ...
    def parar_moves(self) -> int: ...
    def parar_e_aguardar(self, timeout: float = 2.0) -> bool: ...

    # estado auxiliar
    def tracking(self, ligado: bool, peso: float = 1.0) -> dict[str, Any]: ...
    def wobbling(self, ligado: bool) -> bool: ...
    def motores(self) -> str: ...
    def definir_motores(self, modo: str) -> None: ...
    def pose_atual(self) -> npt.NDArray[np.float64] | None: ...
    def pose_olhar_mundo(self, x: float, y: float, z: float) -> npt.NDArray[np.float64]: ...
    def pose_olhar_imagem(self, u: float, v: float) -> npt.NDArray[np.float64] | None: ...
    def frame_jpeg(self) -> bytes | None: ...
    def moves_disponiveis(self, dataset: str) -> list[str]: ...

    # apps do robô
    def apps_listar(self) -> list[dict[str, Any]]: ...
    def app_status(self) -> dict[str, Any] | None: ...
    def app_iniciar(self, nome: str) -> dict[str, Any]: ...
    def app_parar(self) -> None: ...
    def app_reiniciar(self) -> dict[str, Any]: ...


# ─────────────────────────────────────────────────────────────────────────────
class BackendSdk:
    """Robô de verdade: movimento pelo REST do daemon, mídia pelo SDK."""

    modo = "real"

    def __init__(self, reachy: Any, daemon: DaemonAPI) -> None:
        self.reachy = reachy
        self.daemon = daemon
        self._lock_frame = threading.Lock()

    # ── disponibilidade ─────────────────────────────────────────────────────
    def disponivel(self) -> bool:
        try:
            estado = self.daemon.status()
        except ErroDaemon:
            return False
        return estado.get("state") == "running"

    def preparar(self) -> None:
        """Garante torque ligado.

        Ordem importa: `enable_motors()` fixa os alvos na pose atual antes de
        ligar o torque, então qualquer `set_target`/`goto` tem de vir DEPOIS
        (docstring do SDK em `reachy_mini.py:539-543`).
        """
        try:
            if self.daemon.motores() != "enabled":
                self.daemon.definir_motores("enabled")
        except ErroDaemon as e:
            log.warning("não consegui ligar os motores: %s", e)

    def estado(self) -> dict[str, Any]:
        dados: dict[str, Any] = {"modo": self.modo}
        try:
            st = self.daemon.status()
            dados["conectado"] = st.get("state") == "running"
            dados["versao"] = st.get("version")
            dados["ip"] = st.get("wlan_ip")
            dados["midia_liberada"] = bool(st.get("media_released"))
            dados["sem_midia"] = bool(st.get("no_media"))
            dados["simulacao"] = bool(st.get("simulation_enabled") or st.get("mockup_sim_enabled"))
            alvo = st.get("face_target") or {}
            dados["rosto_detectado"] = bool(alvo.get("detected"))
        except ErroDaemon as e:
            dados["conectado"] = False
            dados["erro"] = str(e)
        try:
            dados["motores"] = self.daemon.motores()
        except ErroDaemon:
            dados["motores"] = "unknown"
        try:
            dados["movendo"] = bool(self.daemon.moves_rodando())
        except ErroDaemon:
            dados["movendo"] = False
        return dados

    # ── movimento ───────────────────────────────────────────────────────────
    def goto(
        self,
        *,
        pose: npt.NDArray[np.float64] | None = None,
        antenas: list[float] | None = None,
        body_yaw: float | None = 0.0,
        duracao: float = limites.DURACAO_PADRAO_S,
        interpolacao: str = "minjerk",
    ) -> str:
        corpo: dict[str, Any] = {
            "duration": float(duracao),
            "interpolation": interpolacao,
            "body_yaw": body_yaw,
        }
        if pose is not None:
            corpo["head_pose"] = {"m": [float(v) for v in np.asarray(pose).flatten()]}
        if antenas is not None:
            corpo["antennas"] = [float(antenas[0]), float(antenas[1])]
        resposta = self.daemon._pedir("POST", "/api/move/goto", json=corpo) or {}
        ident = resposta.get("uuid")
        if not ident:
            raise ErroDaemon("o daemon não devolveu uuid no goto")
        return str(ident)

    def tocar_move(self, dataset: str, nome: str) -> str:
        return self.daemon.tocar_move(dataset, nome)

    def wake_up(self) -> str:
        return self.daemon.tocar_wake_up()

    def goto_sleep(self) -> str:
        return self.daemon.tocar_goto_sleep()

    def esperar(self, ident: str, timeout: float, cancelado: Callable[[], bool]) -> str:
        """Sonda até o move sumir de `/api/move/running`, cancelar ou expirar."""
        inicio = time.monotonic()
        apareceu = False
        while True:
            if cancelado():
                return CANCELADO
            try:
                rodando = self.daemon.moves_rodando()
            except ErroDaemon:
                # Daemon oscilou. Não trava a fila por causa disso: trata como
                # fim e deixa o controlador reportar o estado real na próxima.
                return CONCLUIDO
            decorrido = time.monotonic() - inicio
            if ident in rodando:
                apareceu = True
            elif apareceu or decorrido > GRACA_APARECER_S:
                return CONCLUIDO
            if decorrido > timeout:
                return EXPIRADO
            time.sleep(INTERVALO_SONDA_S)

    def moves_rodando(self) -> list[str]:
        try:
            return self.daemon.moves_rodando()
        except ErroDaemon:
            return []

    def parar_ident(self, ident: str) -> bool:
        """Um único POST, sem perguntar antes o que está rodando.

        É o caminho quente da parada de emergência: cada ida e volta ao robô
        pelo Wi-Fi custa dezenas de milissegundos, e aqui só cabe uma.
        """
        try:
            self.daemon.parar_move(ident)
            return True
        except ErroDaemon:
            # 500/404 = o move já tinha acabado. Para o e-stop isso é sucesso.
            return False

    def parar_moves(self) -> int:
        return self.daemon.parar_todos_moves()

    def parar_e_aguardar(self, timeout: float = 2.0) -> bool:
        """Para tudo e espera o daemon soltar o `_play_move_lock`.

        Sem essa espera, o movimento seguinte cairia no `_try_start_move()` do
        daemon e seria descartado em silêncio.
        """
        self.parar_moves()
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if not self.moves_rodando():
                return True
            time.sleep(0.05)
        return not self.moves_rodando()

    # ── estado auxiliar ─────────────────────────────────────────────────────
    def tracking(self, ligado: bool, peso: float = 1.0) -> dict[str, Any]:
        try:
            if ligado:
                return self.daemon.tracking_ligar(peso)
            return self.daemon.tracking_desligar()
        except ErroDaemon as e:
            return {"status": "error", "enabled": False, "erro": str(e)}

    def wobbling(self, ligado: bool) -> bool:
        try:
            if ligado:
                self.daemon.wobbling_ligar()
            else:
                self.daemon.wobbling_desligar()
            return True
        except ErroDaemon as e:
            log.debug("wobbling indisponível: %s", e)
            return False

    def motores(self) -> str:
        try:
            return self.daemon.motores()
        except ErroDaemon:
            return "unknown"

    def definir_motores(self, modo: str) -> None:
        self.daemon.definir_motores(modo)

    def pose_atual(self) -> npt.NDArray[np.float64] | None:
        try:
            return self.reachy.get_current_head_pose()
        except Exception:
            return None

    def pose_olhar_mundo(self, x: float, y: float, z: float) -> npt.NDArray[np.float64]:
        from reachy_mini.vision.look_at import look_at_world_pose

        return look_at_world_pose(x, y, z)

    def pose_olhar_imagem(self, u: float, v: float) -> npt.NDArray[np.float64] | None:
        """Pose para olhar um pixel. Precisa de câmera calibrada.

        `perform_movement=False` faz o SDK só calcular — quem move é o daemon,
        via `goto`, para o movimento continuar cancelável.
        """
        try:
            return self.reachy.look_at_image(int(u), int(v), perform_movement=False)
        except Exception as e:
            log.debug("look_at_image indisponível: %s", e)
            return None

    def frame_jpeg(self) -> bytes | None:
        # Lock porque o FrameHub e um snapshot avulso podem cair aqui juntos.
        with self._lock_frame:
            try:
                return self.reachy.media.get_frame_jpeg()
            except Exception as e:
                log.debug("frame indisponível: %s", e)
                return None

    def moves_disponiveis(self, dataset: str) -> list[str]:
        try:
            return self.daemon.listar_moves(dataset)
        except ErroDaemon as e:
            log.warning("não consegui listar %s: %s", dataset, e)
            return []

    # ── apps ────────────────────────────────────────────────────────────────
    def apps_listar(self) -> list[dict[str, Any]]:
        return self.daemon.apps_instalados()

    def app_status(self) -> dict[str, Any] | None:
        return self.daemon.app_status()

    def app_iniciar(self, nome: str) -> dict[str, Any]:
        return self.daemon.app_iniciar(nome)

    def app_parar(self) -> None:
        self.daemon.app_parar()

    def app_reiniciar(self) -> dict[str, Any]:
        return self.daemon.app_reiniciar()


# ─────────────────────────────────────────────────────────────────────────────
class BackendSimulado:
    """Sem robô. Aceita tudo, executa nada, e diz isso com todas as letras.

    Existe para que o app (voz, painel, API) continue de pé sem hardware — e,
    principalmente, para que `executed=False` chegue até o modelo. Um simulador
    que responde "ok" indistinguível do real faria o Garra afirmar que virou a
    cabeça sem ter virado.
    """

    modo = "simulated"

    # Catálogo mínimo para a interface ter o que mostrar offline. Os nomes reais
    # vêm do daemon quando ele existe; estes são só os canônicos.
    MOVES_FALSOS = {
        DATASET_EMOCOES: [
            "cheerful1", "sad1", "curious1", "surprised1", "confused1",
            "enthusiastic1", "tired1", "attentive1", "welcoming1", "yes1", "no1",
        ],
        DATASET_DANCAS: ["simple_nod", "side_to_side_sway", "head_tilt_roll"],
    }

    def __init__(self) -> None:
        self._contador = 0
        self._lock = threading.Lock()
        self._pose = np.eye(4)
        self._tracking = False
        self._wobbling = False
        self._motores = "enabled"
        self._duracoes: dict[str, float] = {}

    def _novo_id(self, duracao: float) -> str:
        with self._lock:
            self._contador += 1
            ident = f"sim-{self._contador}"
            self._duracoes[ident] = duracao
        return ident

    def disponivel(self) -> bool:
        return False

    def preparar(self) -> None:
        return None

    def estado(self) -> dict[str, Any]:
        return {
            "modo": self.modo,
            "conectado": False,
            "motores": self._motores,
            "movendo": False,
            "simulacao": True,
            "rosto_detectado": False,
        }

    def goto(self, *, pose=None, antenas=None, body_yaw=0.0, duracao=limites.DURACAO_PADRAO_S, interpolacao="minjerk") -> str:  # noqa: ANN001
        if pose is not None:
            self._pose = np.asarray(pose)
        return self._novo_id(duracao)

    def tocar_move(self, dataset: str, nome: str) -> str:
        return self._novo_id(2.0)

    def wake_up(self) -> str:
        return self._novo_id(2.0)

    def goto_sleep(self) -> str:
        return self._novo_id(2.0)

    def esperar(self, ident: str, timeout: float, cancelado: Callable[[], bool]) -> str:
        # Dorme a duração de verdade: sem isso, testes de preempção e de fila
        # passariam por acidente (tudo terminaria antes de haver concorrência).
        alvo = min(self._duracoes.pop(ident, 0.3), timeout)
        fim = time.monotonic() + alvo
        while time.monotonic() < fim:
            if cancelado():
                return CANCELADO
            time.sleep(0.02)
        return CONCLUIDO

    def moves_rodando(self) -> list[str]:
        return []

    def parar_ident(self, ident: str) -> bool:
        return True

    def parar_moves(self) -> int:
        return 0

    def parar_e_aguardar(self, timeout: float = 2.0) -> bool:
        return True

    def tracking(self, ligado: bool, peso: float = 1.0) -> dict[str, Any]:
        self._tracking = ligado
        return {"status": "simulated", "enabled": ligado}

    def wobbling(self, ligado: bool) -> bool:
        self._wobbling = ligado
        return True

    def motores(self) -> str:
        return self._motores

    def definir_motores(self, modo: str) -> None:
        self._motores = modo

    def pose_atual(self) -> npt.NDArray[np.float64] | None:
        return self._pose

    def pose_olhar_mundo(self, x: float, y: float, z: float) -> npt.NDArray[np.float64]:
        from reachy_mini.vision.look_at import look_at_world_pose

        return look_at_world_pose(x, y, z)

    def pose_olhar_imagem(self, u: float, v: float) -> npt.NDArray[np.float64] | None:
        return None

    def frame_jpeg(self) -> bytes | None:
        return None

    def moves_disponiveis(self, dataset: str) -> list[str]:
        return list(self.MOVES_FALSOS.get(dataset, []))

    def apps_listar(self) -> list[dict[str, Any]]:
        return []

    def app_status(self) -> dict[str, Any] | None:
        return None

    def app_iniciar(self, nome: str) -> dict[str, Any]:
        return {"state": "simulated", "info": {"name": nome}}

    def app_parar(self) -> None:
        return None

    def app_reiniciar(self) -> dict[str, Any]:
        return {"state": "simulated"}
