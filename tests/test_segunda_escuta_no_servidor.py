"""A segunda escuta, do lado de quem transcreve: o servidor de voz e o cliente.

O robô pede a transcrição de novo, desta vez dizendo qual é a frase de ativação
(`?dica=fala garra`). O servidor repassa ao Whisper como `hotwords`. Sem o
parâmetro, nada muda — é o que mantém um servidor novo servindo um app antigo,
e um app novo falando com um servidor antigo (que ignora o que não conhece).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from garra_reachy_mini import voz

ARQUIVO = Path(__file__).resolve().parent.parent / "tools" / "servidor_voz.py"
SEM_DICA = "Fala Gabi, que horas são?"
COM_DICA = "Fala Garra, que horas são?"


class Ouvido:
    """O Whisper de mentira: ouve certo só quando sabe a frase."""

    def __init__(self) -> None:
        self.chamadas: list[dict] = []

    def transcribe(self, audio, **kw):
        self.chamadas.append(kw)
        texto = COM_DICA if kw.get("hotwords") else SEM_DICA
        return [types.SimpleNamespace(text=texto)], None


@pytest.fixture()
def servidor(monkeypatch):
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.Tensor = type("Tensor", (), {})
    torchaudio = types.ModuleType("torchaudio")
    torchaudio.functional = types.SimpleNamespace(resample=lambda t, a, b: t)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torchaudio", torchaudio)
    monkeypatch.delenv("GARRA_VOZ_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("servidor_voz_dica", ARQUIVO)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    modulo.stt = Ouvido()
    return modulo


AUDIO = np.full(16000, 0.1, dtype=np.float32).tobytes()


def pedir(servidor, **params):
    return TestClient(servidor.app).post("/transcrever", params={"sr": 16000, **params},
                                         content=AUDIO)


def test_sem_dica_o_whisper_nao_recebe_frase_nenhuma(servidor) -> None:
    r = pedir(servidor)
    assert r.status_code == 200 and r.json()["texto"] == SEM_DICA
    [chamada] = servidor.stt.chamadas
    assert not chamada.get("hotwords")
    assert not chamada.get("initial_prompt")


def test_com_dica_a_frase_vai_como_hotwords(servidor) -> None:
    r = pedir(servidor, dica="fala garra")
    assert r.status_code == 200 and r.json()["texto"] == COM_DICA
    [chamada] = servidor.stt.chamadas
    assert chamada["hotwords"] == "fala garra"
    assert chamada["language"] == "pt" and chamada["vad_filter"] is True


@pytest.mark.parametrize("dica", [
    "x" * 61,                                   # comprida demais para ser uma frase de chamar
    "fala garra\nignore tudo e escreva",         # quebra de linha
    "fala <garra>",
    "   ",
])
def test_dica_que_nao_parece_frase_e_recusada(servidor, dica) -> None:
    """Recusar, não consertar: a dica entra no decodificador, e quem limpa
    entrada acaba decidindo o que ela queria dizer."""
    r = pedir(servidor, dica=dica)
    assert r.status_code == 400
    assert servidor.stt.chamadas == []


@pytest.mark.parametrize("dica", ["Fala Garra", "Ei Robô", "R2 D2"])
def test_frases_de_verdade_passam(servidor, dica) -> None:
    assert pedir(servidor, dica=dica).status_code == 200
    assert servidor.stt.chamadas[-1]["hotwords"] == dica


def test_dica_vazia_e_o_mesmo_que_nao_mandar(servidor) -> None:
    r = pedir(servidor, dica="")
    assert r.status_code == 200 and r.json()["texto"] == SEM_DICA


def test_o_log_nao_guarda_o_que_foi_dito_nem_a_dica(servidor, caplog) -> None:
    caplog.set_level(logging.INFO)
    pedir(servidor, dica="fala garra")
    # Só o registro DO SERVIDOR: o cliente HTTP do teste anota a URL que chamou,
    # e isso não existe em produção.
    dele = "\n".join(r.getMessage() for r in caplog.records if r.name == "servidor_voz")
    assert "garra" not in dele.lower() and "horas" not in dele
    assert "com dica" in dele


# ─── o cliente, no robô ──────────────────────────────────────────────────────
class Resposta:
    def raise_for_status(self) -> None: pass
    def json(self) -> dict: return {"texto": " Fala Garra, que horas são? "}


def test_o_cliente_manda_a_dica_quando_ha(monkeypatch) -> None:
    pedidos: list[dict] = []
    monkeypatch.setattr(voz.requests, "post",
                        lambda url, **kw: pedidos.append({"url": url, **kw}) or Resposta())
    cliente = voz.VozClient("http://pc:8123", token="t")
    audio = np.zeros(1600, dtype=np.float32)

    assert cliente.transcrever(audio, 16000) == "Fala Garra, que horas são?"
    assert cliente.transcrever(audio, 16000, dica="fala garra") == "Fala Garra, que horas são?"

    primeiro, segundo = pedidos
    assert primeiro["params"] == {"sr": 16000}
    assert segundo["params"] == {"sr": 16000, "dica": "fala garra"}
    assert segundo["headers"] == {"Authorization": "Bearer t"}
