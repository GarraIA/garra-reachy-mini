"""Backend falso controlável, para testar concorrência sem robô."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from garra_reachy_mini.robo.backends import BackendSimulado
from garra_reachy_mini.robo.daemon_api import DATASET_DANCAS, DATASET_EMOCOES


class BackendFalso(BackendSimulado):
    """Finge ser hardware real e registra tudo o que foi pedido.

    `modo = "real"` de propósito: sem isso não dá para testar que uma ação
    concluída devolve `executed=True`, que é metade do contrato de honestidade.
    """

    modo = "real"

    def __init__(self, duracao_move: float = 0.4) -> None:
        super().__init__()
        self.registro: list[tuple[str, Any]] = []
        self.duracao_move = duracao_move
        self.parar_chamado = 0
        self.preparar_chamado = 0
        self._reg_lock = threading.Lock()

    def _reg(self, op: str, dado: Any = None) -> None:
        with self._reg_lock:
            self.registro.append((op, dado))

    def ops(self, nome: str) -> list[Any]:
        with self._reg_lock:
            return [d for o, d in self.registro if o == nome]

    def disponivel(self) -> bool:
        return True

    def preparar(self) -> None:
        self.preparar_chamado += 1
        self._reg("preparar")

    def estado(self) -> dict[str, Any]:
        return {
            "modo": self.modo, "conectado": True, "motores": self._motores,
            "movendo": False, "simulacao": False, "rosto_detectado": False,
        }

    def goto(self, **kwargs: Any) -> str:
        self._reg("goto", kwargs)
        return super().goto(**kwargs)

    def tocar_move(self, dataset: str, nome: str) -> str:
        self._reg("move", nome)
        return self._novo_id(self.duracao_move)

    def parar_moves(self) -> int:
        self.parar_chamado += 1
        self._reg("parar_moves")
        return 1

    def parar_e_aguardar(self, timeout: float = 2.0) -> bool:
        self._reg("parar_e_aguardar")
        return True

    def tracking(self, ligado: bool, peso: float = 1.0) -> dict[str, Any]:
        self._reg("tracking", (ligado, peso))
        return super().tracking(ligado, peso)

    def wobbling(self, ligado: bool) -> bool:
        self._reg("wobbling", ligado)
        return super().wobbling(ligado)

    def definir_motores(self, modo: str) -> None:
        self._reg("motores", modo)
        super().definir_motores(modo)

    def frame_jpeg(self) -> bytes | None:
        return b"\xff\xd8falso\xff\xd9"

    def moves_disponiveis(self, dataset: str) -> list[str]:
        return list(self.MOVES_FALSOS.get(dataset, []))

    def app_iniciar(self, nome: str) -> dict[str, Any]:
        self._reg("app_iniciar", nome)
        return {"state": "running", "info": {"name": nome}}


@pytest.fixture
def backend() -> BackendFalso:
    return BackendFalso()


@pytest.fixture
def controlador(backend: BackendFalso):
    from garra_reachy_mini.robo.acoes import ControladorRobo

    ctrl = ControladorRobo(backend, semente=1234)
    ctrl.iniciar()
    yield ctrl
    ctrl.encerrar(timeout=2.0)


@pytest.fixture
def controlador_simulado():
    from garra_reachy_mini.robo.acoes import ControladorRobo

    ctrl = ControladorRobo(BackendSimulado(), semente=1234)
    ctrl.iniciar()
    yield ctrl
    ctrl.encerrar(timeout=2.0)


__all__ = ["BackendFalso", "DATASET_DANCAS", "DATASET_EMOCOES"]
