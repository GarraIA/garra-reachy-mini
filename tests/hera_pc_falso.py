"""Um computador de mentira para o robô: faz o papel da ponte `/api/hera/ao-vivo`.

Substitui `websockets.sync.client.connect` (o `ao_vivo.conectar`): responde ao
aperto de mão com a prova de verdade, abre a "sessão", manda fala da Hera e
obedece a `parar`. O teste empurra o resto (`estado`, `estop`, `fim`) quando quer.

`cair()` derruba a conexão; o robô que volta com a senha da conversa é aceito
(`aceita_volta`) e segue na mesma sessão, como na ponte de verdade.
"""

from __future__ import annotations

import collections
import json
import threading
import time

import numpy as np

from garra_reachy_mini import ao_vivo

SEGREDO = "segredo-do-dispositivo-de-teste-0123456789"
SENHA = "senha-de-retomada-da-conversa"
HOST = "10.0.0.24:8126"
DISPOSITIVO = "garra-sala"
# 0,2 s da "voz da Hera": um tom, em dez quadros de 20 ms
FALA_DA_HERA = (np.sin(np.arange(3200) * 2 * np.pi * 440 / 16000) * 0.4 * 32767).astype("<i2").tobytes()


class Fechada(Exception):
    """O que a conexão levanta depois de fechada (como o `ConnectionClosed`)."""


class PcFalso:
    def __init__(self, segredo: str = SEGREDO, *, recusa: str | None = None,
                 prova_errada: bool = False, abrir: bool = True, falar: bool = True,
                 pings: bool = True, senha: str | None = SENHA, aceita_volta: bool = True) -> None:
        self.segredo = segredo
        self.recusa = recusa
        self.prova_errada = prova_errada
        self.abrir, self.falar, self.pings = abrir, falar, pings
        self.senha, self.aceita_volta = senha, aceita_volta
        self.recebidos: list[dict] = []
        self.som: list[bytes] = []
        self.carimbos: list[int] = []        # t_ms de cada quadro de som, como o robô carimbou
        self.conectou: list[str] = []
        self.fechado = False
        self._fila: collections.deque = collections.deque()
        self._trava = threading.Lock()
        self._ola: dict | None = None
        self._nonce_pc = "nonce-do-pc-falso"
        self._ultimo_ping = 0.0

    # ── o que o robô chama ───────────────────────────────────────────────────
    def __call__(self, url: str, **_kw):           # faz o papel de `connect(...)`
        self.conectou.append(url)
        if self.fechado:
            if not self.aceita_volta:
                raise ConnectionRefusedError("o computador não atende")
            # uma conexão nova: a fila velha morreu com a anterior
            self.fechado, self._ola = False, None
            with self._trava:
                self._fila.clear()
        return self

    def send(self, mensagem) -> None:
        if self.fechado:
            raise Fechada()
        if isinstance(mensagem, (bytes, bytearray)):
            lido = ao_vivo.ler_quadro(bytes(mensagem))
            assert lido is not None and lido[0] == ao_vivo.MICROFONE, "quadro de som torto"
            self.som.append(lido[3])
            self.carimbos.append(lido[2])
            return
        m = json.loads(mensagem)
        self.recebidos.append(m)
        if m["t"] == "ola":
            self._ola = m
            if m.get("retomar") is not None and m["retomar"] != self.senha:
                self.empurrar({"t": "fim", "motivo": "not_resumable", "segundos": 0})
                return
            if self.recusa and self.recusa != "bad_proof":
                self.empurrar({"t": "fim", "motivo": self.recusa, "segundos": 0})
                return
            dados = self._dados()
            prova = ao_vivo.prova("pc", **{**dados, "segredo": "outro" if self.prova_errada else self.segredo})
            self.empurrar({"t": "desafio", "nonce": self._nonce_pc, "prova": prova})
        elif m["t"] == "prova":
            certa = ao_vivo.prova("robo", **self._dados())
            if m.get("prova") != certa or self.recusa == "bad_proof":
                self.empurrar({"t": "fim", "motivo": "bad_proof", "segundos": 0})
                return
            self.empurrar({"t": "aceito"})
            if self._ola.get("retomar") is not None:
                # volta do robô: a mesma sessão, sem abrir de novo
                self.empurrar({"t": "estado", "fase": "ouvindo"})
                return
            if self.abrir:
                self.empurrar({"t": "aberta", **({"retomar": self.senha} if self.senha else {})})
                self.empurrar({"t": "estado", "fase": "ouvindo"})
                if self.falar:
                    self.empurrar({"t": "estado", "fase": "falando"})
                    for i in range(0, len(FALA_DA_HERA), 640):
                        self.empurrar(ao_vivo.quadro(ao_vivo.FALA, i, 0, FALA_DA_HERA[i:i + 640]))
        elif m["t"] == "parar":
            self.empurrar({"t": "fim", "motivo": "robot_stop", "segundos": 7})

    def recv(self, timeout: float | None = None):
        fim = time.monotonic() + (timeout or 0.0)
        while True:
            with self._trava:
                if self._fila:
                    item = self._fila.popleft()
                    return item if isinstance(item, (bytes, bytearray)) else json.dumps(item)
            if self.fechado:
                raise Fechada()
            agora = time.monotonic()
            if self.pings and self._ola is not None and agora - self._ultimo_ping > 0.5:
                self._ultimo_ping = agora
                return json.dumps({"t": "ping"})
            if agora >= fim:
                raise TimeoutError
            time.sleep(0.005)

    def close(self) -> None:
        self.fechado = True

    def cair(self) -> None:
        """O Wi-Fi caiu: a conexão morre sem fim nem aviso."""
        self.fechado = True

    # ── o que o teste chama ──────────────────────────────────────────────────
    def empurrar(self, item) -> None:
        with self._trava:
            self._fila.append(item)

    def tipos(self) -> list[str]:
        return [m["t"] for m in self.recebidos]

    def _dados(self) -> dict:
        assert self._ola is not None
        return {"segredo": self.segredo, "nonce_robo": self._ola["nonce"], "nonce_pc": self._nonce_pc,
                "host": self._ola["host"], "dispositivo": self._ola["dispositivo"]}
