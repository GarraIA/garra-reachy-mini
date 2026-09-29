"""O detector de fala DENTRO do laço de voz, com um microfone que nunca cala.

`test_segmentador.py` prova a regra. Este prova que o laço a usa: roda
`_laco_voz` inteiro com uma fita de áudio no lugar do microfone — o ruído de
fundo que sobe sozinho, como o ganho automático da placa faz, e alguém chamando
o robô no meio dele.

Foi assim que o robô ficou surdo em 29/09/2026: o ruído passou do limiar medido
na partida e o laço entendeu três minutos seguidos como uma fala só.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from test_escuta_no_laco import Cena, Microfone, Voz
from test_segmentador import PISO, PISO_ALTO, SR, fala, ruido

BLOCO = 320
PEDIDO = "Fala Garra, que horas são?"


class MicrofoneDeSala(Microfone):
    """Uma fita contínua, bloco a bloco; acabada a fita, o ruído de fundo no
    ritmo do relógio. Nunca devolve silêncio absoluto: microfone de verdade não
    devolve."""

    def __init__(self, fita: np.ndarray, piso: float) -> None:
        super().__init__()
        self.fita, self.piso = fita, piso
        self._cursor = 0
        self._ultimo = 0.0
        self._g = np.random.default_rng(9)
        self.acabou = threading.Event()

    def get_audio_sample(self):
        self.lendo.set()
        with self._trava:
            if self._cursor + BLOCO <= self.fita.size:
                bloco = self.fita[self._cursor:self._cursor + BLOCO]
                self._cursor += BLOCO
                return bloco
        self.acabou.set()
        agora = time.monotonic()
        if agora - self._ultimo < BLOCO / SR:
            return None
        self._ultimo = agora
        return (self._g.standard_normal(BLOCO) * self.piso).astype(np.float32)


class VozQueOuve(Voz):
    """Transcreve pelo volume: trecho com som alto dentro é o pedido; ruído,
    nada. E anota a duração de tudo o que lhe mandaram."""

    def __init__(self, frase: str, corte: float = 0.3) -> None:
        super().__init__([])
        self.frase, self.corte = frase, corte
        self.duracoes: list[float] = []

    def transcrever(self, audio, sr) -> str:
        self.duracoes.append(audio.size / sr)
        self.transcritas += 1
        return self.frase if float(np.max(np.abs(audio))) > self.corte else ""


@pytest.fixture()
def montar_sala(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.delenv("GARRA_LIMIAR", raising=False)      # calibra, como no robô
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "0")
    monkeypatch.setenv("GARRA_NOTIFICACOES_S", "3600")
    cenas: list[Cena] = []

    def _montar(fita: np.ndarray, piso_final: float, respostas: list[str]) -> Cena:
        cenas.append(Cena(backend, tmp_path, {"wake_phrase_enabled": True}, [], respostas,
                          mic=MicrofoneDeSala(fita, piso_final), voz=VozQueOuve(PEDIDO)))
        return cenas[-1]

    yield _montar
    for c in cenas:
        c.encerrar()


def esperar(condicao, segundos: float = 30.0) -> bool:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.05)
    return False


def test_ruido_que_passa_do_limiar_da_partida_nao_ensurdece_o_robo(montar_sala) -> None:
    fita = np.concatenate([ruido(PISO, 3.0), ruido(PISO_ALTO, 60.0, semente=4),
                           fala(0.45, 1.5), ruido(PISO_ALTO, 3.0, semente=6)])
    cena = montar_sala(fita, PISO_ALTO, ["São três horas."])

    assert esperar(lambda: cena.voz.faladas), "o robô não respondeu a quem o chamou"

    assert cena.cerebro.recebidas == ["que horas são?"]
    assert cena.voz.faladas == ["São três horas."]
    # Nada do que foi para a transcrição pode ter minutos: o trecho gigante é o
    # robô surdo enquanto ele dura, e uma transcrição que ninguém pediu depois.
    assert max(cena.voz.duracoes) < 12.0
    assert len(cena.voz.duracoes) <= 2, "o susto do ruído e a fala; mais que isso é ruído virando trecho"


def test_sala_quieta_nao_manda_nada_para_a_transcricao(montar_sala) -> None:
    cena = montar_sala(ruido(PISO, 40.0), PISO, [])
    assert cena.mic.acabou.wait(20)
    time.sleep(1.5)
    assert cena.voz.duracoes == []


def test_o_app_sabe_dizer_o_que_o_detector_esta_medindo(montar_sala) -> None:
    cena = montar_sala(np.concatenate([ruido(PISO, 3.0), ruido(PISO_ALTO, 30.0, semente=4)]),
                       PISO_ALTO, [])
    assert cena.mic.acabou.wait(20)
    time.sleep(0.5)
    medida = cena.app.medida_do_detector()
    assert medida is not None
    assert medida["floor"] == pytest.approx(PISO_ALTO, rel=0.3)
    assert medida["in_speech"] is False
    assert medida["fixed_threshold"] is False
