"""A sentinela: o computador ouve, ele mesmo, o "pare" e o "Fala Garra".

O mesmo som do robô que sobe para a nuvem passa pelo detector de fala do Garra,
e cada trecho curto é transcrito pelo Whisper desta máquina. Aqui o Whisper é
um dublê; o detector é o de verdade.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import pytest

from companion.hera import comandos, sentinela

SR = 16000
BLOCO = 1024                      # 64 ms, como o robô manda


def _som(segundos: float, amplitude: float, semente: int = 0) -> np.ndarray:
    gerador = np.random.default_rng(semente)
    return (gerador.standard_normal(int(SR * segundos)) * amplitude).astype(np.float32)


def _pcm(onda: np.ndarray) -> bytes:
    return (np.clip(onda, -1, 1) * 32767).astype("<i2").tobytes()


def _roteiro(*partes: tuple[float, float]) -> np.ndarray:
    """(segundos, amplitude) em sequência: ruído baixo é sala, alto é alguém falando."""
    return np.concatenate([_som(s, a, i) for i, (s, a) in enumerate(partes)])


def _alimentar(sen: sentinela.Sentinela, onda: np.ndarray, t0_ms: int = 5000) -> None:
    for i in range(0, onda.size - BLOCO + 1, BLOCO):
        sen.ouvir(_pcm(onda[i:i + BLOCO]), t0_ms + i * 1000 // SR)


class Whisper:
    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.ouvidos: list[float] = []

    def __call__(self, audio):
        self.ouvidos.append(audio.size / SR)
        resposta = self.respostas.pop(0) if self.respostas else ("", None)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def _rodar(sen: sentinela.Sentinela, onda: np.ndarray, espera_s: float = 0.3, t0_ms: int = 5000):
    async def cena():
        tarefa = asyncio.ensure_future(sen.rodar())
        _alimentar(sen, onda, t0_ms)
        await asyncio.sleep(espera_s)
        tarefa.cancel()
    asyncio.run(cena())


# ─── o que conta como comando ────────────────────────────────────────────────
@pytest.mark.parametrize("texto,confianca,esperado", [
    ("Pare.", None, "stop"),
    ("Hera, para!", [0.9, 0.8], "stop"),
    ("Fala Garra.", None, "handoff_garra"),
    ("Fala Garra, que horas são?", [0.9] * 5, "handoff_garra"),
    # A despedida fica com a nuvem: o Whisper escreve "Tchau." para quase-silêncio.
    ("Tchau.", [0.99], None),
    ("pode desligar", None, None),
    ("obrigado", None, None),
    ("para de brincadeira e me conta", None, None),
    ("", None, None),
    # Pouca certeza do Whisper: não é comando.
    ("Pare.", [0.2], None),
])
def test_decidir(texto, confianca, esperado):
    assert sentinela.decidir(texto, confianca) == esperado


# ─── o trecho, do microfone ao comando ───────────────────────────────────────
def test_pare_dito_vira_comando_com_o_trecho_no_relogio_do_robo():
    vistos = []
    sen = sentinela.Sentinela(Whisper(("Pare.", [0.95])), lambda nome, trecho: vistos.append((nome, trecho)))
    # 2 s de sala, 0,6 s de fala (de 2,0 a 2,6 s), 1,5 s de sala
    _rodar(sen, _roteiro((2.0, 0.001), (0.6, 0.2), (1.5, 0.001)))
    ((nome, (ini, fim)),) = vistos
    assert nome == "stop"
    # O trecho cobre a fala, no relógio que o robô carimbou (começou em 5000 ms).
    assert ini <= 5000 + 2000 and fim >= 5000 + 2600
    assert fim - ini < 3000


def _fala_comprida(segundos: float) -> np.ndarray:
    """Fala de verdade tem vãos entre as sílabas; ruído constante vira piso da sala."""
    partes = []
    for i in range(int(segundos / 0.33)):
        partes += [_som(0.25, 0.2, 100 + i), _som(0.08, 0.001, 200 + i)]
    return np.concatenate(partes)


def test_trecho_comprido_nao_vai_ao_whisper():
    whisper = Whisper(("Pare.", None))
    vistos = []
    sen = sentinela.Sentinela(whisper, lambda *a: vistos.append(a))
    onda = np.concatenate([_som(2.0, 0.001), _fala_comprida(sentinela.DURACAO_MAX_S + 1.0),
                           _som(1.5, 0.001, 9)])
    _rodar(sen, onda)
    assert whisper.ouvidos == [] and vistos == []


def test_eco_da_propria_hera_nao_conta():
    vistos = []
    sen = sentinela.Sentinela(Whisper(("Pare.", None)), lambda *a: vistos.append(a),
                              eco=lambda nome: True)
    _rodar(sen, _roteiro((2.0, 0.001), (0.6, 0.2), (1.5, 0.001)))
    assert vistos == []


def test_whisper_fora_do_ar_nao_derruba_e_o_proximo_trecho_vale():
    vistos = []
    sen = sentinela.Sentinela(Whisper(RuntimeError("caiu"), ("Pare.", None)),
                              lambda nome, trecho: vistos.append(nome))
    _rodar(sen, _roteiro((2.0, 0.001), (0.6, 0.2), (1.5, 0.001), (0.6, 0.2), (1.5, 0.001)), espera_s=0.5)
    assert vistos == ["stop"]


def test_o_que_foi_dito_nao_vai_para_o_registro(caplog):
    caplog.set_level(logging.DEBUG)
    sen = sentinela.Sentinela(Whisper(("minha senha é 1234", None), ("Pare.", None)), lambda *a: None)
    _rodar(sen, _roteiro((2.0, 0.001), (0.6, 0.2), (1.5, 0.001), (0.6, 0.2), (1.5, 0.001)), espera_s=0.5)
    assert "1234" not in caplog.text and "Pare" not in caplog.text


def test_sem_carimbo_de_tempo_o_trecho_e_none():
    vistos = []
    sen = sentinela.Sentinela(Whisper(("Fala Garra.", None)), lambda nome, trecho: vistos.append((nome, trecho)))

    async def cena():
        tarefa = asyncio.ensure_future(sen.rodar())
        onda = _roteiro((2.0, 0.001), (0.6, 0.2), (1.5, 0.001))
        for i in range(0, onda.size - BLOCO + 1, BLOCO):
            sen.ouvir(_pcm(onda[i:i + BLOCO]))
        await asyncio.sleep(0.3)
        tarefa.cancel()

    asyncio.run(cena())
    assert vistos == [("handoff_garra", None)]


# ─── o Whisper desta máquina ─────────────────────────────────────────────────
def test_transcrever_local_usa_o_servidor_de_voz_por_loopback_e_sem_dica(monkeypatch, tmp_path):
    import requests
    monkeypatch.setenv("GARRA_REACHY_CONF", str(tmp_path))
    pedidos = []

    class Resposta:
        def raise_for_status(self):
            pass

        def json(self):
            return {"texto": "Pare.", "confianca": [0.93]}

    def post(url, data=None, headers=None, timeout=None):
        pedidos.append((url, len(data), headers, timeout))
        return Resposta()

    monkeypatch.setattr(requests, "post", post)
    texto, confianca = sentinela.transcrever_local(np.zeros(1600, np.float32))
    assert (texto, confianca) == ("Pare.", [0.93])
    ((url, tamanho, cabecalhos, prazo),) = pedidos
    assert url.startswith("http://127.0.0.1:") and url.endswith("/transcrever?sr=16000")
    assert "dica" not in url
    assert tamanho == 1600 * 4 and cabecalhos["Authorization"].startswith("Bearer ")
    assert prazo == sentinela.PRAZO_WHISPER_S


def test_a_ponte_registra_a_hera_com_a_sentinela(monkeypatch):
    from companion import ponte
    from companion.hera import canal
    visto = {}
    monkeypatch.setattr(canal, "registrar", lambda app, **kw: visto.update(kw))
    ponte._registrar_hera(object())
    assert visto["transcrever"] is sentinela.transcrever_local
    assert visto["descrever"] is ponte._descrever_para_hera


# ─── eco: o que ela acabou de dizer ──────────────────────────────────────────
@pytest.mark.parametrize("nome,fala_dela,eco", [
    ("stop", "Se quiser que eu pare, é só dizer.", True),
    ("stop", "Hoje vai chover.", False),
    ("handoff_garra", "O Garra cuida das luzes.", True),
    ("handoff_garra", "Hoje vai chover.", False),
    ("farewell", "Tchau, até mais!", True),
])
def test_eco_pelo_que_ela_disse(nome, fala_dela, eco):
    assert comandos.eco(nome, fala_dela) is eco
