"""O par WebRTC do companion — o "navegador sem tela" da conversa ao vivo.

No site, quem fala WebRTC com a OpenAI é o navegador. Para o robô é o companion:
ele recebe o som do robô em PCM 16 kHz, entrega à OpenAI por WebRTC, e devolve ao
robô o som que vier de lá, também em PCM 16 kHz. Os eventos da sessão passam pelo
canal de dados `oai-events`, como no navegador.

Aqui o outro lado é um par de mentira no mesmo processo (sem rede, sem OpenAI):
o que se confere é que som e eventos atravessam nos dois sentidos, no formato
que cada ponta espera.
"""

from __future__ import annotations

import asyncio
import json
import math

import numpy as np
import pytest

aiortc = pytest.importorskip("aiortc")

from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: E402
from aiortc.mediastreams import AudioStreamTrack, MediaStreamError  # noqa: E402
from av import AudioFrame  # noqa: E402

from companion.hera import par  # noqa: E402


def tom(segundos: float, hz: float = 440.0, taxa: int = 16000, amplitude: float = 0.5) -> bytes:
    t = np.arange(int(segundos * taxa)) / taxa
    return (np.sin(2 * math.pi * hz * t) * amplitude * 32767).astype("<i2").tobytes()


def rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    return float(np.sqrt(np.mean(x ** 2)))


class _Tom(AudioStreamTrack):
    """O que o "outro lado" fala: um tom contínuo, em quadros de 20 ms a 48 kHz."""

    def __init__(self) -> None:
        super().__init__()
        self._n = 0

    async def recv(self) -> AudioFrame:
        quadro = await super().recv()          # silêncio no ritmo certo; só troca o conteúdo
        amostras = quadro.samples
        t = (np.arange(amostras) + self._n) / quadro.sample_rate
        self._n += amostras
        onda = (np.sin(2 * math.pi * 660.0 * t) * 0.5 * 32767).astype("<i2")
        novo = AudioFrame.from_ndarray(onda.reshape(1, -1), format="s16", layout="mono")
        novo.sample_rate, novo.pts, novo.time_base = quadro.sample_rate, quadro.pts, quadro.time_base
        return novo


class OutroLado:
    """Faz o papel da OpenAI: responde o SDP, abre o canal de eventos, fala e escuta."""

    def __init__(self) -> None:
        self.pc = RTCPeerConnection()
        self.canal = None
        self.recebidos: list[dict] = []
        self.ouvido = bytearray()
        self.aberto = asyncio.Event()
        self._tarefas: list[asyncio.Task] = []

        @self.pc.on("datachannel")
        def _canal(canal) -> None:
            self.canal = canal
            canal.on("message", lambda m: self.recebidos.append(json.loads(m)))
            self.aberto.set()

        @self.pc.on("track")
        def _trilha(trilha) -> None:
            self._tarefas.append(asyncio.ensure_future(self._escutar(trilha)))

    async def _escutar(self, trilha) -> None:
        try:
            while True:
                quadro = await trilha.recv()
                self.ouvido += quadro.to_ndarray().astype("<i2").tobytes()
        except MediaStreamError:
            pass

    async def trocar_sdp(self, oferta: str) -> str:
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=oferta, type="offer"))
        self.pc.addTrack(_Tom())
        await self.pc.setLocalDescription(await self.pc.createAnswer())
        return self.pc.localDescription.sdp

    async def fechar(self) -> None:
        for tarefa in self._tarefas:
            tarefa.cancel()
        await self.pc.close()


async def _esperar(condicao, segundos: float = 8.0) -> bool:
    fim = asyncio.get_running_loop().time() + segundos
    while asyncio.get_running_loop().time() < fim:
        if condicao():
            return True
        await asyncio.sleep(0.05)
    return False


def test_som_e_eventos_atravessam_nos_dois_sentidos():
    async def cena():
        outro = OutroLado()
        do_outro = bytearray()
        eventos: list[dict] = []
        p = par.Par(outro.trocar_sdp, ao_audio=do_outro.extend, ao_evento=eventos.append,
                    servidores_ice=())
        try:
            await p.abrir()
            assert await _esperar(outro.aberto.is_set), "o canal oai-events não abriu"
            assert outro.canal.label == "oai-events"

            # eventos: de lá para cá e de cá para lá
            await _esperar(lambda: outro.canal.readyState == "open")
            outro.canal.send(json.dumps({"type": "session.started", "session": {"id": "s1"}}))
            assert await _esperar(lambda: eventos), "o evento da sessão não chegou"
            assert eventos[0]["type"] == "session.started"
            assert await _esperar(lambda: p.pronto)
            assert p.enviar({"type": "session.commentary.append", "content": "oi"}) is True
            assert await _esperar(lambda: outro.recebidos)
            assert outro.recebidos[0] == {"type": "session.commentary.append", "content": "oi"}

            # som do "robô" para o outro lado: 1 s de tom, em blocos de 64 ms como o SDK entrega
            bloco = 1024 * 2
            pcm = tom(1.2)
            for i in range(0, len(pcm), bloco):
                p.microfone(pcm[i:i + bloco])
                await asyncio.sleep(0.064)
            assert await _esperar(lambda: rms(bytes(outro.ouvido[-48000:])) > 0.05), \
                "o som do robô não chegou ao outro lado"

            # som do outro lado para o "robô": PCM 16 kHz mono, com energia
            assert await _esperar(lambda: len(do_outro) > 16000 * 2 and rms(bytes(do_outro[-16000:])) > 0.05), \
                "o som do outro lado não voltou em PCM"
            assert len(do_outro) % 2 == 0
        finally:
            await p.fechar()
            await outro.fechar()
        assert p.pronto is False

    asyncio.run(cena())


def test_sem_som_do_robo_o_microfone_manda_silencio_e_nao_trava():
    async def cena():
        outro = OutroLado()
        p = par.Par(outro.trocar_sdp, ao_audio=lambda _: None, ao_evento=lambda _: None,
                    servidores_ice=())
        try:
            await p.abrir()
            assert await _esperar(lambda: len(outro.ouvido) > 48000), "nenhum quadro chegou"
            assert rms(bytes(outro.ouvido)) < 0.001
        finally:
            await p.fechar()
            await outro.fechar()

    asyncio.run(cena())


def test_o_microfone_nao_acumula_atraso():
    """Som que chega mais rápido do que sai (rede em rajada) descarta o mais antigo:
    numa conversa, atraso acumulado é pior do que um pedaço perdido."""
    fila = par.FilaDeSom(maximo_ms=300)
    for _ in range(100):
        fila.por(tom(0.064))
    assert fila.pendente_ms <= 300
    assert fila.descartado_ms > 5000
    quadro = fila.tirar(320)
    assert len(quadro) == 640 and rms(quadro) > 0.1
    vazia = par.FilaDeSom(maximo_ms=300)
    assert vazia.tirar(320) == b"\x00" * 640           # sem som: silêncio, nunca bloqueio


def test_enviar_antes_de_abrir_nao_estoura():
    p = par.Par(None, ao_audio=lambda _: None, ao_evento=lambda _: None)
    assert p.pronto is False and p.enviar({"type": "x"}) is False


def test_falha_na_troca_de_sdp_fecha_o_par():
    async def cena():
        async def recusa(_oferta: str) -> str:
            raise RuntimeError("a ponte recusou a sessão")

        p = par.Par(recusa, ao_audio=lambda _: None, ao_evento=lambda _: None, servidores_ice=())
        with pytest.raises(RuntimeError):
            await p.abrir()
        assert p.pronto is False and p.fechado is True

    asyncio.run(cena())
