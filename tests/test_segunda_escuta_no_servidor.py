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


def palavra(texto: str, p: float):
    return types.SimpleNamespace(word=" " + texto, probability=p, start=0.0, end=0.3)


class Ouvido:
    """O Whisper de mentira: ouve certo só quando sabe a frase, e duvida do
    "Gabi" que escreveu quando não sabe."""

    def __init__(self) -> None:
        self.chamadas: list[dict] = []

    def transcribe(self, audio, **kw):
        self.chamadas.append(kw)
        texto = COM_DICA if kw.get("hotwords") else SEM_DICA
        confianca = [0.95, 0.30 if "Gabi" in texto else 0.12, 0.98, 0.99, 0.97]
        palavras = [palavra(w, p) for w, p in zip(texto.split(), confianca)]
        if not kw.get("word_timestamps"):
            palavras = None
        # Em dois pedaços, como o Whisper faz com pausa no meio.
        return [types.SimpleNamespace(text=" Fala " + texto.split()[1],
                                      words=palavras[:2] if palavras else None),
                types.SimpleNamespace(text=" " + " ".join(texto.split()[2:]),
                                      words=palavras[2:] if palavras else None)], None


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


def test_a_resposta_traz_a_confianca_de_cada_palavra(servidor) -> None:
    """Uma por palavra do texto, na ordem. É com ela que o robô separa "fala
    gabi" (o Whisper em dúvida) de "fala galera" (o Whisper com certeza)."""
    corpo = pedir(servidor).json()
    assert corpo["texto"] == SEM_DICA
    assert corpo["confianca"] == [0.95, 0.30, 0.98, 0.99, 0.97]
    assert len(corpo["confianca"]) == len(corpo["texto"].split())
    assert servidor.stt.chamadas[0]["word_timestamps"] is True


def test_sem_fala_a_confianca_e_uma_lista_vazia(servidor) -> None:
    servidor.stt.transcribe = lambda audio, **kw: ([], None)
    assert pedir(servidor).json() == {"texto": "", "confianca": []}


def test_whisper_que_nao_da_palavras_nao_inventa_confianca(servidor) -> None:
    servidor.stt.transcribe = lambda audio, **kw: (
        [types.SimpleNamespace(text=" Fala Gabi", words=None)], None)
    corpo = pedir(servidor).json()
    assert corpo["texto"] == "Fala Gabi" and corpo["confianca"] is None


def test_contagem_que_nao_bate_com_o_texto_nao_vira_confianca(servidor) -> None:
    """Quem recebe alinha pela posição. Lista torta poria a confiança de uma
    palavra em outra — melhor dizer que não se sabe."""
    servidor.stt.transcribe = lambda audio, **kw: (
        [types.SimpleNamespace(text=" Fala Gabi, que horas são?",
                               words=[palavra("Fala", 0.9), palavra("Gabi,", 0.3)])], None)
    assert pedir(servidor).json()["confianca"] is None


def test_pedaco_sem_palavras_invalida_a_lista_inteira(servidor) -> None:
    """A contagem até fecharia — com as confianças nas palavras erradas."""
    cinco = [palavra(w, 0.9) for w in ("a", "b", "c", "d", "e")]
    servidor.stt.transcribe = lambda audio, **kw: (
        [types.SimpleNamespace(text=" Fala Gabi,", words=None),
         types.SimpleNamespace(text=" que horas são?", words=cinco)], None)
    assert pedir(servidor).json()["confianca"] is None


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
    def __init__(self, corpo: dict | None = None) -> None:
        self.corpo = corpo or {"texto": " Fala Garra, que horas são? ",
                               "confianca": [0.9, 0.2, 0.9, 0.9, 0.9]}
    def raise_for_status(self) -> None: pass
    def json(self) -> dict: return self.corpo


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


def test_ouvir_devolve_o_texto_e_a_confianca(monkeypatch) -> None:
    monkeypatch.setattr(voz.requests, "post", lambda url, **kw: Resposta())
    ouvido = voz.VozClient("http://pc:8123").ouvir(np.zeros(1600, dtype=np.float32), 16000)
    assert ouvido.texto == "Fala Garra, que horas são?"
    assert ouvido.confianca == (0.9, 0.2, 0.9, 0.9, 0.9)


@pytest.mark.parametrize("corpo", [
    {"texto": "Fala Gabi"},                              # servidor de voz antigo
    {"texto": "Fala Gabi", "confianca": None},
    {"texto": "Fala Gabi", "confianca": "alta"},
    {"texto": "Fala Gabi", "confianca": [0.9, "x"]},
])
def test_servidor_que_nao_diz_a_confianca_deixa_ela_desconhecida(monkeypatch, corpo) -> None:
    monkeypatch.setattr(voz.requests, "post", lambda url, **kw: Resposta(corpo))
    ouvido = voz.VozClient("http://pc:8123").ouvir(np.zeros(1600, dtype=np.float32), 16000)
    assert ouvido.texto == "Fala Gabi" and ouvido.confianca is None


def test_a_transcricao_nao_mostra_o_texto_no_repr(monkeypatch) -> None:
    monkeypatch.setattr(voz.requests, "post", lambda url, **kw: Resposta())
    ouvido = voz.VozClient("http://pc:8123").ouvir(np.zeros(1600, dtype=np.float32), 16000)
    assert "Garra" not in repr(ouvido) and "horas" not in repr(ouvido)
