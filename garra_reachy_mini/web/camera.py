"""`FrameHub`: um único produtor de quadros, vários consumidores.

Motivo de existir: a sessão de mídia do robô é exclusiva. Se o MJPEG do painel,
o botão de instantâneo e a captura para a visão do Garra cada um abrisse a sua
leitura, teríamos três consumidores disputando o mesmo `MediaManager` (e, pior,
alguém tentaria abrir uma segunda conexão WebRTC — que o robô não dá).

Então: uma thread lê `backend.frame_jpeg()` e guarda o último quadro; todo mundo
lê de lá. Como `bytes` é imutável em Python, "distribuir cópias" é só entregar a
mesma referência — nenhum consumidor consegue estragar o quadro de outro.

Economia: sem ninguém assistindo, a captura cai para `fps_ocioso`. Assim o hub
pode ficar sempre ligado (o que mantém o instantâneo instantâneo) sem queimar CPU
e banda o dia inteiro.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from ..robo.imagem import dimensoes_jpeg

log = logging.getLogger("garra_reachy_mini.web.camera")

# Acima disso o quadro guardado é velho o bastante para a interface avisar.
IDADE_OBSOLETA_S = 3.0


@dataclass(frozen=True)
class Quadro:
    jpeg: bytes
    seq: int
    ts: float
    largura: int | None
    altura: int | None

    @property
    def idade_s(self) -> float:
        return time.monotonic() - self.ts

    @property
    def obsoleto(self) -> bool:
        return self.idade_s > IDADE_OBSOLETA_S

    def info(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "age_s": round(self.idade_s, 2),
            "stale": self.obsoleto,
            "bytes": len(self.jpeg),
            "width": self.largura,
            "height": self.altura,
        }


class FrameHub:
    def __init__(
        self,
        backend,
        *,
        fps_ativo: float = 12.0,
        fps_ocioso: float = 1.0,
        max_clientes: int = 4,
    ) -> None:
        self.backend = backend
        self.fps_ativo = max(1.0, fps_ativo)
        self.fps_ocioso = max(0.2, fps_ocioso)
        self.max_clientes = max_clientes

        self._lock = threading.Lock()
        self._quadro: Quadro | None = None
        self._seq = 0
        self._clientes = 0
        self._novo = threading.Condition(self._lock)
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None
        self._falhas = 0

    # ── ciclo de vida ───────────────────────────────────────────────────────
    def iniciar(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._laco, name="frame-hub", daemon=True
        )
        self._thread.start()

    def encerrar(self, timeout: float = 2.0) -> None:
        self._parar.set()
        with self._novo:
            self._novo.notify_all()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    # ── produtor ────────────────────────────────────────────────────────────
    def _laco(self) -> None:
        while not self._parar.is_set():
            with self._lock:
                ativo = self._clientes > 0
            periodo = 1.0 / (self.fps_ativo if ativo else self.fps_ocioso)
            inicio = time.monotonic()
            self._capturar()
            resto = periodo - (time.monotonic() - inicio)
            if resto > 0:
                self._parar.wait(resto)

    def _capturar(self) -> Quadro | None:
        try:
            jpeg = self.backend.frame_jpeg()
        except Exception as e:  # pragma: no cover - backend já engole quase tudo
            log.debug("falha ao ler quadro: %s", e)
            jpeg = None
        if not jpeg:
            self._falhas += 1
            return None
        self._falhas = 0
        dims = dimensoes_jpeg(jpeg)
        with self._novo:
            self._seq += 1
            self._quadro = Quadro(
                jpeg=jpeg,
                seq=self._seq,
                ts=time.monotonic(),
                largura=dims[0] if dims else None,
                altura=dims[1] if dims else None,
            )
            self._novo.notify_all()
            return self._quadro

    # ── consumidores ────────────────────────────────────────────────────────
    def ultimo(self) -> Quadro | None:
        with self._lock:
            return self._quadro

    def instantaneo(self, idade_maxima_s: float = 0.5) -> Quadro | None:
        """Quadro recente. Captura na hora se o guardado já envelheceu."""
        atual = self.ultimo()
        if atual is not None and atual.idade_s <= idade_maxima_s:
            return atual
        return self._capturar() or atual

    def esperar_proximo(self, depois_de: int, timeout: float) -> Quadro | None:
        """Bloqueia até existir um quadro com `seq > depois_de`."""
        limite = time.monotonic() + timeout
        with self._novo:
            while True:
                if self._quadro is not None and self._quadro.seq > depois_de:
                    return self._quadro
                resto = limite - time.monotonic()
                if resto <= 0:
                    return None
                self._novo.wait(resto)

    def entrar(self) -> bool:
        """Registra um consumidor de stream. False se já bateu o teto."""
        with self._lock:
            if self._clientes >= self.max_clientes:
                return False
            self._clientes += 1
            return True

    def sair(self) -> None:
        with self._lock:
            self._clientes = max(0, self._clientes - 1)

    # ── introspecção ────────────────────────────────────────────────────────
    def status(self) -> dict[str, object]:
        with self._lock:
            quadro = self._quadro
            clientes = self._clientes
        base: dict[str, object] = {
            "available": quadro is not None and not quadro.obsoleto,
            "clients": clientes,
            "max_clients": self.max_clientes,
            "fps": self.fps_ativo if clientes else self.fps_ocioso,
            "consecutive_failures": self._falhas,
        }
        if quadro is not None:
            base.update(quadro.info())
        return base
