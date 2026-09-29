"""O detector de fala do laço de voz, com os números que o robô mediu.

Em 29/09/2026 o robô parou de responder. A medição, feita pelo que chegava ao
servidor de voz (só duração e energia, nunca o conteúdo):

* um "trecho de fala" de **182 segundos** sem uma palavra dentro — o detector
  ficou aberto três minutos, e nesse tempo o robô não ouviu ninguém;
* o ruído de fundo do microfone indo de 0,007 a 0,045 sozinho. A placa de áudio
  tem ganho automático: depois de alguém falar baixo ela amplifica tudo, o
  ruído junto;
* o limiar era um número só, medido em 1,2 s na partida (0,026). Ruído acima
  dele = "fala" para sempre, porque a trava de duração máxima só era conferida
  nos blocos ABAIXO do limiar.

Cada sinal abaixo imita uma dessas medidas.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from garra_reachy_mini.config import FALA_MAXIMA_S, FIM_DE_FALA_S, PRE_ROLL_S
from garra_reachy_mini.segmentador import Segmentador

SR = 16000
BLOCO = 320                     # 20 ms
PISO = 0.007                    # o ruído de fundo medido com o ganho baixo
PISO_ALTO = 0.045               # o mesmo cômodo, com o ganho automático alto


def ruido(nivel: float, segundos: float, semente: int = 1) -> np.ndarray:
    """Ruído estacionário com o RMS pedido."""
    g = np.random.default_rng(semente)
    return (g.standard_normal(int(segundos * SR)) * nivel).astype(np.float32)


def rampa(de: float, ate: float, segundos: float, semente: int = 2) -> np.ndarray:
    g = np.random.default_rng(semente)
    n = int(segundos * SR)
    return (g.standard_normal(n) * np.linspace(de, ate, n)).astype(np.float32)


def fala(nivel: float, segundos: float, semente: int = 3) -> np.ndarray:
    """Parecido com fala no que importa aqui: sobe e desce quatro vezes por
    segundo, e entre uma sílaba e outra cai a um décimo do volume."""
    g = np.random.default_rng(semente)
    n = int(segundos * SR)
    t = np.arange(n) / SR
    silabas = 0.1 + 0.9 * np.abs(np.sin(2 * np.pi * 2.0 * t))
    return (g.standard_normal(n) * nivel * silabas).astype(np.float32)


def passar(seg: Segmentador, sinal: np.ndarray, t0: float = 0.0,
           bloco: int = BLOCO) -> list:
    """Entrega o sinal em blocos, no ritmo do relógio, e junta o que saiu."""
    saida = []
    for i in range(0, len(sinal) - bloco + 1, bloco):
        trecho = seg.bloco(sinal[i:i + bloco], t0 + (i + bloco) / SR)
        if trecho is not None:
            saida.append(trecho)
    return saida


def segundos(trecho) -> float:
    return trecho.audio.size / SR


def novo(**kw) -> Segmentador:
    kw.setdefault("piso_inicial", PISO)
    return Segmentador(SR, **kw)


# ─── o caso comum não pode piorar ────────────────────────────────────────────
def test_uma_fala_sobre_ruido_baixo_vira_um_trecho_so() -> None:
    sinal = np.concatenate([ruido(PISO, 2.0), fala(0.10, 1.5), ruido(PISO, 2.0)])
    trechos = passar(novo(), sinal)
    assert len(trechos) == 1
    # O que veio antes (o ataque da palavra), a fala e o silêncio que a fechou.
    assert 1.5 + FIM_DE_FALA_S <= segundos(trechos[0]) <= 1.5 + FIM_DE_FALA_S + PRE_ROLL_S + 0.2


def test_o_trecho_comeca_antes_de_a_energia_subir() -> None:
    sinal = np.concatenate([ruido(PISO, 2.0), fala(0.10, 1.5), ruido(PISO, 2.0)])
    trecho = passar(novo(), sinal)[0]
    comeco = float(np.sqrt(np.mean(trecho.audio[:int(0.2 * SR)] ** 2)))
    assert comeco < 0.02, "o começo do trecho tem de ser o silêncio de antes da fala"


def test_silencio_nao_vira_trecho() -> None:
    seg = novo()
    assert passar(seg, ruido(PISO, 30.0)) == []
    assert not seg.em_fala


# ─── o defeito medido ────────────────────────────────────────────────────────
def test_ruido_que_sobe_devagar_nao_abre_o_detector() -> None:
    """O ganho automático subindo: 0,007 → 0,045 em vinte segundos, e fica."""
    sinal = np.concatenate([ruido(PISO, 3.0), rampa(PISO, PISO_ALTO, 20.0),
                            ruido(PISO_ALTO, 60.0)])
    seg = novo()
    assert passar(seg, sinal) == []
    # Lista vazia sozinha não prova nada: o detector PRESO aberto também não
    # entrega trecho nenhum. Foi assim que ele passou três minutos surdo.
    assert not seg.em_fala


def test_ruido_que_salta_nao_deixa_o_robo_surdo() -> None:
    """De uma hora para outra o ruído passa do limiar — e alguém fala depois."""
    sinal = np.concatenate([ruido(PISO, 3.0), ruido(PISO_ALTO, 120.0),
                            fala(0.45, 1.5), ruido(PISO_ALTO, 3.0)])
    trechos = passar(novo(), sinal)
    assert trechos, "a fala não saiu"
    # O susto do salto pode virar um trecho; o que não pode é durar minutos.
    assert max(segundos(t) for t in trechos) < 12.0
    ultimo = trechos[-1]
    assert 1.5 <= segundos(ultimo) <= 4.0
    assert float(np.max(np.abs(ultimo.audio))) > 0.3, "o último trecho tem de ser a fala"


def test_depois_do_ruido_alto_volta_a_ouvir_quem_fala_baixo() -> None:
    """O ganho desce de novo. Uma fala de 0,05 está acima do ruído de agora
    (0,007), e quase no nível do ruído de um minuto atrás (0,045)."""
    sinal = np.concatenate([ruido(PISO_ALTO, 60.0), ruido(PISO, 3.0),
                            fala(0.05, 1.5), ruido(PISO, 2.0)])
    trechos = passar(novo(piso_inicial=PISO_ALTO), sinal)
    assert len(trechos) == 1
    assert 1.5 <= segundos(trechos[0]) <= 4.0


def test_nenhum_trecho_passa_da_duracao_maxima() -> None:
    """A trava valia só nos blocos abaixo do limiar — ou seja, nunca quando
    fazia falta."""
    sinal = np.concatenate([ruido(PISO, 2.0), fala(0.2, 70.0), ruido(PISO, 2.0)])
    trechos = passar(novo(), sinal)
    assert trechos
    assert max(segundos(t) for t in trechos) <= FALA_MAXIMA_S + 0.1
    assert any(t.motivo == "max_duration" for t in trechos)


def test_fala_longa_com_pausas_curtas_sai_inteira() -> None:
    """Acompanhar o ruído não pode confundir fala comprida com ruído."""
    partes = [ruido(PISO, 2.0)]
    for i in range(4):
        partes += [fala(0.08, 2.0, semente=10 + i), ruido(PISO, 0.3, semente=20 + i)]
    partes.append(ruido(PISO, 2.0))
    trechos = passar(novo(), np.concatenate(partes))
    assert len(trechos) == 1
    assert segundos(trechos[0]) >= 4 * 2.3


def test_o_fim_da_frase_dito_mais_baixo_entra_no_trecho() -> None:
    """Abrir pede mais energia do que continuar: quem baixa a voz no fim da
    frase não pode perder as últimas palavras."""
    sinal = np.concatenate([ruido(PISO, 2.0), fala(0.10, 1.0), ruido(0.022, 0.7, semente=5),
                            ruido(PISO, 2.0)])
    trechos = passar(novo(), sinal)
    assert len(trechos) == 1
    assert segundos(trechos[0]) >= 1.0 + 0.7 + FIM_DE_FALA_S


def test_estalo_nao_vai_para_a_transcricao() -> None:
    sinal = np.concatenate([ruido(PISO, 2.0), ruido(0.2, 0.08, semente=7), ruido(PISO, 3.0)])
    seg = novo()
    assert passar(seg, sinal) == []
    assert seg.medida()["dropped_short"] + seg.medida()["segments"] <= 1
    assert seg.medida()["segments"] == 0


def test_um_bloco_isolado_acima_do_limiar_nao_abre() -> None:
    sinal = np.concatenate([ruido(PISO, 2.0), ruido(0.2, 0.02, semente=8), ruido(PISO, 3.0)])
    seg = novo()
    assert passar(seg, sinal) == []
    assert seg.medida()["dropped_short"] == 0, "um bloco só nem chega a abrir"


# ─── limiar fixo: quem configurou sabe o que quer ────────────────────────────
def test_limiar_fixo_da_configuracao_e_respeitado() -> None:
    sinal = np.concatenate([ruido(0.05, 10.0), fala(0.4, 1.5), ruido(0.05, 3.0)])
    seg = novo(limiar_fixo=0.12)
    trechos = passar(seg, sinal)
    assert len(trechos) == 1
    m = seg.medida()
    assert m["fixed_threshold"] is True
    assert m["open_threshold"] == pytest.approx(0.12)


def test_limiar_fixo_baixo_demais_ainda_tem_a_trava_de_duracao() -> None:
    trechos = passar(novo(limiar_fixo=0.01), ruido(0.05, 80.0))
    assert trechos and max(segundos(t) for t in trechos) <= FALA_MAXIMA_S + 0.1


# ─── quando o áudio para de chegar ───────────────────────────────────────────
def falar_e_calar(seg: Segmentador, t0: float) -> list:
    """Seis blocos de 100 ms e depois nada: nem silêncio, só ausência."""
    saida, t = [], t0
    for _ in range(6):
        t += 0.1
        r = seg.bloco(np.full(SR // 10, 0.2, np.float32), t)
        if r is not None:
            saida.append(r)
    for _ in range(80):
        t += 0.015
        r = seg.sem_audio(t)
        if r is not None:
            saida.append(r)
    return saida


def test_audio_que_para_de_chegar_fecha_o_trecho() -> None:
    trechos = falar_e_calar(novo(piso_inicial=None), 100.0)
    assert len(trechos) == 1
    assert trechos[0].motivo == "no_audio"
    assert segundos(trechos[0]) == pytest.approx(0.6, abs=0.01)


def test_so_fala_sem_silencio_nenhum_nao_ensurdece() -> None:
    """Sem ver silêncio não há como medir ruído; o piso não pode ser inventado
    a partir da fala."""
    seg = novo(piso_inicial=None)
    for i in range(12):
        assert len(falar_e_calar(seg, 100.0 + 10.0 * i)) == 1, f"fala {i + 1} não saiu"


# ─── o que se pode mostrar ───────────────────────────────────────────────────
def test_a_medida_so_tem_numeros() -> None:
    seg = novo()
    passar(seg, np.concatenate([ruido(PISO, 2.0), fala(0.1, 1.5), ruido(PISO, 2.0)]))
    m = seg.medida()
    assert set(m) == {"floor", "open_threshold", "close_threshold", "fixed_threshold",
                      "in_speech", "segments", "dropped_short", "cut_by_max", "block_ms"}
    assert m["segments"] == 1 and m["in_speech"] is False
    assert m["floor"] == pytest.approx(PISO, rel=0.35)
    assert m["block_ms"] == pytest.approx(20.0, abs=0.5)
    assert all(isinstance(v, (int, float, bool)) for v in m.values())


def test_a_medida_acompanha_o_ruido() -> None:
    seg = novo()
    passar(seg, np.concatenate([ruido(PISO, 3.0), rampa(PISO, PISO_ALTO, 20.0),
                                ruido(PISO_ALTO, 10.0)]))
    m = seg.medida()
    assert m["floor"] == pytest.approx(PISO_ALTO, rel=0.25)
    assert m["open_threshold"] > PISO_ALTO * 2


def test_descartar_esquece_o_trecho_aberto_mas_nao_o_ruido() -> None:
    """Depois de o robô falar, o que estava no buffer é eco dele."""
    seg = novo()
    passar(seg, np.concatenate([ruido(PISO_ALTO, 20.0, semente=4)]))
    piso = seg.medida()["floor"]
    passar(seg, np.concatenate([fala(0.5, 0.5)]), t0=20.0)
    assert seg.em_fala
    seg.descartar()
    assert not seg.em_fala
    assert seg.medida()["floor"] == pytest.approx(piso, rel=0.3)
    assert passar(seg, ruido(PISO_ALTO, 3.0, semente=9), t0=20.5) == []


# ─── o ruído medido, como ele foi ────────────────────────────────────────────
DEGRAUS = " ▁▂▃▄▅▆▇█"
MEDIDO = pathlib.Path(__file__).parent / "dados" / "ruido_182s.txt"


def ruido_medido() -> np.ndarray:
    """Os 182 s de 29/09/2026, refeitos como ruído com o mesmo volume."""
    linha = next(l for l in MEDIDO.read_text(encoding="utf-8").splitlines()
                 if l and not l.startswith("#"))
    g = np.random.default_rng(5)
    return np.concatenate([g.standard_normal(SR // 10) * (DEGRAUS.index(c) + 0.5) / 8 * 0.05
                           for c in linha]).astype(np.float32)


def test_o_ruido_medido_tem_o_tamanho_que_se_mediu() -> None:
    assert ruido_medido().size / SR == pytest.approx(182.0, abs=0.1)


def test_com_o_ruido_medido_o_detector_nao_fica_preso() -> None:
    """O limiar da partida era 0,026 (quatro vezes 0,0065). Com a regra antiga
    este sinal é um trecho só, de três minutos."""
    seg = novo(piso_inicial=0.0065)
    trechos = passar(seg, ruido_medido())
    assert not seg.em_fala
    assert len(trechos) <= 1, "no máximo o susto do começo"
    assert all(segundos(t) < 6.0 for t in trechos)


def test_depois_do_ruido_medido_quem_fala_e_ouvido() -> None:
    seg = novo(piso_inicial=0.0065)
    passar(seg, ruido_medido())
    # A placa amplifica a fala junto com o ruído: 25 dB acima do piso, como a
    # voz do Michel estava (0,17 sobre 0,007).
    sinal = np.concatenate([ruido(0.03, 1.0, semente=31), fala(0.6, 1.5),
                            ruido(0.03, 2.0, semente=32)])
    trechos = passar(seg, sinal, t0=182.0)
    assert len(trechos) == 1
    assert 1.5 <= segundos(trechos[0]) <= 4.0
