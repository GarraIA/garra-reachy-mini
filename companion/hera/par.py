"""O par WebRTC do companion com a OpenAI Live — o "navegador sem tela".

No site quem segura a sessão de voz é o navegador: microfone, alto-falante e o
canal de dados `oai-events`. Para o robô esse papel é do companion. O som do robô
chega aqui em PCM 16 kHz mono (o formato do SDK do Reachy), sobe por WebRTC, e o
que a OpenAI fala desce por WebRTC e sai daqui de novo em PCM 16 kHz mono.

WebRTC, e não o WebSocket da mesma API, porque é o transporte em que a
interrupção funciona sem lógica no cliente (o servidor entrega em tempo real e
simplesmente para) — é assim no site — e é o que o fornecedor recomenda inclusive
servidor a servidor.

A chave da OpenAI não passa por aqui: `trocar_sdp` leva a oferta à ponte da Hera,
que é quem fala com a OpenAI, e devolve a resposta.

`aiortc` é dependência opcional do companion: só este módulo o importa.
"""

from __future__ import annotations

import asyncio
import fractions
import json
import logging
import time
from typing import Any, Awaitable, Callable, Iterable

from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                    RTCSessionDescription)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av import AudioFrame, AudioResampler

log = logging.getLogger(__name__)

TAXA = 16000                      # a do SDK do Reachy, nos dois sentidos
QUADRO_S = 0.020
AMOSTRAS_POR_QUADRO = int(TAXA * QUADRO_S)
CANAL_DE_EVENTOS = "oai-events"   # o nome que a API Live espera
# Sem STUN por padrão. Medido em 25/11/2026 contra a OpenAI: com o STUN do site a
# abertura levou 6,4 s (o aiortc espera a coleta de candidatos terminar); sem
# ele, 0,6 s, e a sessão conectou igual — o lado de lá tem endereço público e
# os testes de conectividade partem daqui. Fica como opção para rede que precise.
STUN_DO_SITE = ("stun:stun.l.google.com:19302",)


class FilaDeSom:
    """O som do robô à espera de subir. Limitada: o que passa do teto sai pelo começo.

    O robô manda blocos de 64 ms por TCP, que às vezes chegam em rajada. Guardar
    tudo acumularia atraso para o resto da conversa; numa conversa, um pedaço
    perdido custa menos do que ficar meio segundo atrás.
    """

    def __init__(self, maximo_ms: int = 300) -> None:
        self._maximo = int(TAXA * maximo_ms / 1000) * 2
        self._dados = bytearray()
        self.descartado_ms = 0.0

    @property
    def pendente_ms(self) -> float:
        return len(self._dados) / 2 / TAXA * 1000

    def por(self, pcm: bytes) -> None:
        self._dados += pcm[: len(pcm) - (len(pcm) % 2)]
        sobra = len(self._dados) - self._maximo
        if sobra > 0:
            sobra += sobra % 2
            del self._dados[:sobra]
            self.descartado_ms += sobra / 2 / TAXA * 1000

    def tirar(self, amostras: int) -> bytes:
        """Exatamente `amostras`: o que houver, completado com silêncio."""
        pedaco = bytes(self._dados[: amostras * 2])
        del self._dados[: amostras * 2]
        return pedaco + b"\x00" * (amostras * 2 - len(pedaco))


class _Microfone(MediaStreamTrack):
    """A trilha de áudio que sobe: um quadro de 20 ms a cada 20 ms, haja som ou não."""

    kind = "audio"

    def __init__(self, fila: FilaDeSom) -> None:
        super().__init__()
        self._fila = fila
        self._inicio: float | None = None
        self._pts = 0

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        if self._inicio is None:
            self._inicio = time.monotonic()
        else:
            espera = self._inicio + self._pts / TAXA - time.monotonic()
            if espera > 0:
                await asyncio.sleep(espera)
        quadro = AudioFrame(format="s16", layout="mono", samples=AMOSTRAS_POR_QUADRO)
        quadro.planes[0].update(self._fila.tirar(AMOSTRAS_POR_QUADRO))
        quadro.sample_rate = TAXA
        quadro.pts = self._pts
        quadro.time_base = fractions.Fraction(1, TAXA)
        self._pts += AMOSTRAS_POR_QUADRO
        return quadro


class Par:
    """Uma sessão WebRTC: nasce em `abrir`, morre em `fechar`. Uma por conversa."""

    def __init__(self, trocar_sdp: Callable[[str], Awaitable[str]] | None, *,
                 ao_audio: Callable[[bytes], Any], ao_evento: Callable[[dict], Any],
                 ao_cair: Callable[[str], Any] | None = None,
                 servidores_ice: Iterable[str] = ()) -> None:
        self._trocar_sdp = trocar_sdp
        self._ao_audio = ao_audio
        self._ao_evento = ao_evento
        self._ao_cair = ao_cair
        self._ice = tuple(servidores_ice)
        self._fila = FilaDeSom()
        self._pc: RTCPeerConnection | None = None
        self._canal = None
        self._escuta: asyncio.Task | None = None
        self.fechado = False

    @property
    def pronto(self) -> bool:
        """O canal de eventos está aberto: dá para falar com a sessão."""
        return bool(not self.fechado and self._canal is not None
                    and self._canal.readyState == "open")

    @property
    def microfone_descartado_ms(self) -> float:
        return self._fila.descartado_ms

    async def abrir(self) -> None:
        """Monta o par, troca o SDP pela ponte e conecta. Erro aqui fecha tudo."""
        config = RTCConfiguration(iceServers=[RTCIceServer(urls=u) for u in self._ice])
        pc = self._pc = RTCPeerConnection(configuration=config)
        try:
            pc.addTrack(_Microfone(self._fila))
            canal = self._canal = pc.createDataChannel(CANAL_DE_EVENTOS)
            canal.on("message", self._mensagem)
            canal.on("close", lambda: self._caiu("channel_closed"))
            pc.on("track", self._trilha)
            pc.on("connectionstatechange", self._estado_mudou)
            # No aiortc `setLocalDescription` já espera a coleta de candidatos:
            # a oferta sai completa, como o `waitForIce` do navegador.
            await pc.setLocalDescription(await pc.createOffer())
            resposta = await self._trocar_sdp(pc.localDescription.sdp)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=resposta, type="answer"))
        except BaseException:
            await self.fechar()
            raise

    def microfone(self, pcm: bytes) -> None:
        """Som do robô (PCM s16le mono 16 kHz), em blocos de qualquer tamanho."""
        self._fila.por(pcm)

    def enviar(self, evento: dict) -> bool:
        """Um evento para a sessão, pelo canal de dados. `False` se ele não está aberto."""
        if not self.pronto:
            return False
        try:
            self._canal.send(json.dumps(evento, ensure_ascii=False))
            return True
        except Exception:
            log.debug("evento não enviado à sessão ao vivo", exc_info=True)
            return False

    async def fechar(self) -> None:
        if self.fechado:
            return
        self.fechado = True
        if self._escuta is not None:
            self._escuta.cancel()
        if self._pc is not None:
            try:
                await self._pc.close()
            except Exception:
                log.debug("falha ao fechar o par WebRTC", exc_info=True)

    # ── o que vem do outro lado ──────────────────────────────────────────────
    def _mensagem(self, bruto: Any) -> None:
        try:
            evento = json.loads(bruto)
        except (TypeError, ValueError):
            return
        if isinstance(evento, dict):
            self._ao_evento(evento)

    def _trilha(self, trilha: MediaStreamTrack) -> None:
        if trilha.kind == "audio" and self._escuta is None:
            self._escuta = asyncio.ensure_future(self._escutar(trilha))

    async def _escutar(self, trilha: MediaStreamTrack) -> None:
        # O decodificador entrega 48 kHz estéreo; o robô toca 16 kHz mono.
        para_o_robo = AudioResampler(format="s16", layout="mono", rate=TAXA)
        try:
            while True:
                quadro = await trilha.recv()
                for convertido in para_o_robo.resample(quadro):
                    self._ao_audio(bytes(convertido.planes[0])[: convertido.samples * 2])
        except MediaStreamError:
            self._caiu("media_ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("a escuta da sessão ao vivo parou")
            self._caiu("media_error")

    def _estado_mudou(self) -> None:
        if self._pc is not None and self._pc.connectionState in ("failed", "closed"):
            self._caiu(f"connection_{self._pc.connectionState}")

    def _caiu(self, motivo: str) -> None:
        if not self.fechado and self._ao_cair is not None:
            self._ao_cair(motivo)
