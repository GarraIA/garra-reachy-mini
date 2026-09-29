"""O começo da fala tem de chegar ao STT.

A rodada no hardware falhou por aqui, e não pelo modelo: "Qual é a capital da
França?" chegou transcrito como "Ó a capital da França." O detector abria o
buffer só no primeiro bloco acima do limiar, e ainda o esvaziava na transição —
o ataque do "Qual", que é justamente a parte de menor energia, ficava de fora.

Estes testes cobrem o anel de pre-roll e, no fim, olham o próprio laço de voz
para garantir que ele continua ligado ao anel.
"""

from __future__ import annotations

import pathlib

import numpy as np

from garra_reachy_mini.config import FIM_DE_FALA_S, PRE_ROLL_S
from garra_reachy_mini.segmentador import Segmentador
from garra_reachy_mini.voz import PreRoll

SR = 16000
BLOCO = int(SR * 0.02)          # 20 ms, a granularidade que o SDK entrega


def _blocos(sinal: np.ndarray) -> list[np.ndarray]:
    return [sinal[i:i + BLOCO] for i in range(0, len(sinal), BLOCO)]


# ── o anel ────────────────────────────────────────────────────────────────────
def test_guarda_no_maximo_a_janela_pedida():
    anel = PreRoll(int(0.1 * SR))            # 100 ms = 5 blocos
    for b in _blocos(np.ones(SR, np.float32)):   # 1 s inteiro
        anel.guardar(b)
    # Um bloco de folga é aceitável (a poda para antes de esvaziar); 1 s não é.
    assert anel.amostras <= int(0.1 * SR) + BLOCO


def test_drenar_entrega_e_esvazia():
    anel = PreRoll(SR)
    for b in _blocos(np.ones(BLOCO * 3, np.float32)):
        anel.guardar(b)
    assert anel.amostras == BLOCO * 3
    saida = anel.drenar()
    assert sum(b.size for b in saida) == BLOCO * 3
    assert anel.amostras == 0
    assert anel.drenar() == []


def test_janela_zero_nao_guarda_nada():
    """`PRE_ROLL_S = 0` tem de desligar o recurso, não estourar."""
    anel = PreRoll(0)
    anel.guardar(np.ones(BLOCO, np.float32))
    assert anel.amostras == 0 and anel.drenar() == []


def test_nunca_esvazia_com_bloco_maior_que_a_janela():
    """Meio pre-roll é melhor que nenhum — e `drenar()` não pode voltar vazio."""
    anel = PreRoll(10)
    anel.guardar(np.ones(BLOCO, np.float32))
    assert anel.amostras == BLOCO
    assert len(anel.drenar()) == 1


def test_blocos_vazios_sao_ignorados():
    anel = PreRoll(SR)
    anel.guardar(np.empty(0, np.float32))
    assert anel.amostras == 0


# ── o defeito, reproduzido ────────────────────────────────────────────────────
def _fala_com_ataque_suave() -> np.ndarray:
    """Silêncio, uma subida gradual (a consoante) e a vogal em volume cheio."""
    ruido = np.full(int(0.30 * SR), 0.001, np.float32)
    ataque = np.linspace(0.001, 0.20, int(0.12 * SR), dtype=np.float32)
    vogal = np.full(int(0.50 * SR), 0.20, np.float32)
    return np.concatenate([ruido, ataque, vogal])


def _capturar(sinal: np.ndarray, limiar: float, anel: PreRoll | None) -> np.ndarray:
    """A regra que o `_laco_voz` tinha: RMS por bloco decide abrir e fechar."""
    buffer: list[np.ndarray] = []
    em_fala = False
    silencio = 0.0
    for bloco in _blocos(sinal):
        rms = float(np.sqrt(np.mean(bloco ** 2)))
        if rms >= limiar:
            if not em_fala:
                em_fala = True
                buffer.clear()
                if anel is not None:
                    buffer.extend(anel.drenar())
            silencio = 0.0
            buffer.append(bloco)
        elif em_fala:
            buffer.append(bloco)
            silencio += 0.02
            if silencio > FIM_DE_FALA_S:
                break
        elif anel is not None:
            anel.guardar(bloco)
    return np.concatenate(buffer) if buffer else np.empty(0, np.float32)


def test_sem_pre_roll_o_ataque_da_palavra_e_perdido():
    """O comportamento antigo, fixado como referência do que se corrigiu."""
    sinal = _fala_com_ataque_suave()
    capturado = _capturar(sinal, limiar=0.10, anel=None)
    # A captura começa já no meio da subida: sobra menos que a fala real.
    assert capturado.size < int(0.62 * SR)
    # E o primeiro bloco já entra em volume alto — não há ataque nenhum.
    assert float(np.sqrt(np.mean(capturado[:BLOCO] ** 2))) >= 0.10


def test_com_pre_roll_o_ataque_chega_inteiro():
    sinal = _fala_com_ataque_suave()
    anel = PreRoll(int(PRE_ROLL_S * SR))
    capturado = _capturar(sinal, limiar=0.10, anel=anel)
    # Agora entra a subida inteira e mais um pedaço do silêncio anterior.
    assert capturado.size > int(0.62 * SR)
    # E o começo é baixo: é o ataque, que antes se perdia.
    assert float(np.sqrt(np.mean(capturado[:BLOCO] ** 2))) < 0.10


def test_o_pre_roll_nao_arrasta_a_frase_anterior():
    """Meio segundo é curto de propósito: não pode colar duas falas numa."""
    assert PRE_ROLL_S < FIM_DE_FALA_S


# ── o laço continua ligado ao anel ────────────────────────────────────────────
LACO = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini" / "main.py"


def _pelo_detector(sinal: np.ndarray, limiar: float) -> np.ndarray:
    """O mesmo sinal, agora por quem o laço usa de verdade."""
    seg = Segmentador(SR, limiar_fixo=limiar)
    for i, bloco in enumerate(_blocos(sinal)):
        trecho = seg.bloco(bloco, (i + 1) * BLOCO / SR)
        if trecho is not None:
            return trecho.audio
    return np.empty(0, np.float32)


def test_o_detector_do_laco_entrega_o_ataque_inteiro():
    """A regra saiu de `_laco_voz` para `segmentador.py`. O que ela garantia
    tem de ter ido junto."""
    calado = np.full(int(1.2 * SR), 0.001, np.float32)
    capturado = _pelo_detector(np.concatenate([_fala_com_ataque_suave(), calado]), 0.10)
    assert capturado.size > int(0.62 * SR)
    assert float(np.sqrt(np.mean(capturado[:BLOCO] ** 2))) < 0.10


def test_o_laco_de_voz_entrega_o_microfone_ao_detector():
    """Um teste de unidade do detector não prova que o laço o usa. Este prova."""
    fonte = LACO.read_text(encoding="utf-8")
    assert "detector = Segmentador(" in fonte
    assert "detector.bloco(mono, agora)" in fonte
    assert "detector.sem_audio(agora)" in fonte
    # E nenhum limiar próprio no laço: duas regras divergiriam com o tempo.
    assert "rms >= limiar" not in fonte
