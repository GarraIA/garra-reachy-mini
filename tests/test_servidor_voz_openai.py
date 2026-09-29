"""O servidor de voz também fala o dialeto da OpenAI, para o gateway do Garra.

O gateway só sabe pedir voz de três jeitos, e o que serve aqui é o
"OpenAI-compatível": `POST /v1/audio/speech` com `response_format: "wav"`, mais
um `GET /` que a sonda de saúde dele chama. Sem estas duas rotas o console em
`:3888` mostra "TTS server: quebrado" — e a saída era subir um SEGUNDO
Chatterbox na GPU só para o console.

Os modelos de verdade não entram aqui: `torch` nem existe no ambiente dos
testes. O que se prova é o contrato HTTP.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

ARQUIVO = Path(__file__).resolve().parent.parent / "tools" / "servidor_voz.py"
TOKEN = "token-de-teste-0123456789"


@pytest.fixture()
def servidor(monkeypatch):
    """O módulo carregado com `torch` de mentira e um TTS que devolve um tom."""
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.Tensor = type("Tensor", (), {})
    torchaudio = types.ModuleType("torchaudio")
    torchaudio.functional = types.SimpleNamespace(resample=lambda t, a, b: t)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)
    monkeypatch.setenv("GARRA_VOZ_TOKEN", TOKEN)

    spec = importlib.util.spec_from_file_location("servidor_voz_teste", ARQUIVO)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)

    pedidos: list[tuple[str, int]] = []

    def tom(texto: str, sr: int) -> np.ndarray:
        pedidos.append((texto, sr))
        t = np.arange(int(sr * 0.25), dtype=np.float32) / sr
        return (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)

    monkeypatch.setattr(modulo, "sintetizar", tom, raising=False)
    modulo.pedidos = pedidos
    return modulo


def cliente(servidor) -> TestClient:
    return TestClient(servidor.app)


AUT = {"Authorization": f"Bearer {TOKEN}"}
PEDIDO = {"model": "chatterbox", "input": "Bom dia, tudo bem?", "voice": "alloy",
          "response_format": "wav"}


def test_a_raiz_responde_para_a_sonda_de_saude_do_gateway(servidor) -> None:
    """O gateway faz `GET <endpoint>/` sem credencial e só olha o status."""
    r = cliente(servidor).get("/")
    assert r.status_code == 200
    assert r.json()["service"] == "garra-voz"


def test_a_lista_de_modelos_responde_para_a_sonda_do_provedor(servidor) -> None:
    """Com `tts_provider: lmstudio` a sonda do gateway é `GET /v1/models`."""
    r = cliente(servidor).get("/v1/models")
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "chatterbox-pt"


def test_a_fala_sai_como_wav_de_verdade(servidor) -> None:
    r = cliente(servidor).post("/v1/audio/speech", json=PEDIDO, headers=AUT)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth()) == (1, 2)
        assert w.getframerate() == servidor.SR_FALA
        amostras = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert amostras.size == int(servidor.SR_FALA * 0.25)
    assert 15000 < int(np.abs(amostras).max()) <= 32767, "o tom de 0,5 tem de sobreviver"
    assert servidor.pedidos == [("Bom dia, tudo bem?", servidor.SR_FALA)]


def test_onda_fora_da_faixa_e_cortada_e_nao_da_a_volta(servidor, monkeypatch) -> None:
    """Sem o corte, 1.2 em int16 vira negativo: um estalo no alto-falante."""
    monkeypatch.setattr(servidor, "sintetizar",
                        lambda texto, sr: np.array([1.2, -1.2, 0.0], dtype=np.float32))
    r = cliente(servidor).post("/v1/audio/speech", json=PEDIDO, headers=AUT)
    with wave.open(io.BytesIO(r.content), "rb") as w:
        amostras = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert amostras.tolist() == [32767, -32767, 0]


@pytest.mark.parametrize("corpo", [
    {"model": "x", "input": ""},
    {"model": "x", "input": "   "},
    {"model": "x"},
])
def test_pedido_sem_texto_e_recusado(servidor, corpo) -> None:
    """Um WAV vazio com 200 faria o console tocar silêncio e chamar de sucesso."""
    r = cliente(servidor).post("/v1/audio/speech", json=corpo, headers=AUT)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert servidor.pedidos == []


def test_formato_que_nao_e_wav_e_recusado(servidor) -> None:
    r = cliente(servidor).post(
        "/v1/audio/speech", json={**PEDIDO, "response_format": "mp3"}, headers=AUT)
    assert r.status_code == 400
    assert "wav" in r.json()["error"]["message"]
    assert servidor.pedidos == []


def test_da_rede_sem_token_nao_entra(servidor) -> None:
    """A rota nova não pode ser a porta dos fundos da GPU."""
    r = cliente(servidor).post("/v1/audio/speech", json=PEDIDO)
    assert r.status_code == 401
    assert servidor.pedidos == []


def test_do_loopback_entra_sem_token(servidor, monkeypatch) -> None:
    """É de onde o gateway chama, e ele não manda credencial nenhuma."""
    monkeypatch.setattr(servidor, "_e_loopback", lambda request: True)
    r = cliente(servidor).post("/v1/audio/speech", json=PEDIDO)
    assert r.status_code == 200


def test_o_texto_falado_nao_vai_para_o_log(servidor, caplog) -> None:
    """Este log já guardou conversa de casa uma vez."""
    caplog.set_level(logging.INFO)
    cliente(servidor).post("/v1/audio/speech", json=PEDIDO, headers=AUT)
    assert "Bom dia" not in caplog.text
