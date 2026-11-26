"""A conversa ao vivo, do lado do robô.

Quem decide que um enunciado é chamado é `roteador.py`, dentro do turno. Quem
ATENDE o chamado é o laço de voz, fora do turno, por aqui — é ele o dono do
microfone, e uma conversa em tempo real precisa do microfone o tempo todo.

O robô é só o terminal de áudio da conversa. Ele abre um WebSocket na ponte do
computador (o mesmo endereço da ponte de chat, `:8126`), prova quem é depois de
o computador provar primeiro, e daí troca som: o do microfone sobe, a voz da Hera
desce e sai pelo `CoordenadorAudio`. Chave da OpenAI, tetos, Hermes e o que foi
dito ficam no computador; aqui chegam só a fase, o "pare", o fim e — quando
pedem para a Hera olhar — o pedido de UM quadro da câmera, sem a pergunta. Quem
descreve a imagem é o modelo de visão do computador, o mesmo do "Fala Garra".

O corpo segue o nível escolhido no painel (`live_motion`). Medido em 25/11/2026:
fora da fala dela, os servos aparecem no microfone (antenas ~17 dB acima do
silêncio da sala, a cabeça até 28 dB), e iriam para a nuvem como se fosse
alguém falando; durante a fala dela o chip abafa o microfone, e o balanço da
cabeça não subiu o nível. Daí: "parado" (o padrão), "ao falar" (o balanço só
enquanto ela fala) e "expressivo" (também as antenas ao pensar, com o microfone
calado enquanto elas se mexem).

Quando o computador some no meio, o robô volta UMA vez pela mesma conversa (a
senha dela veio no `aberta`, e a prova de posse é feita de novo). Quando alguém
diz "Fala Garra", o computador devolve só o intervalo do que foi dito, no
relógio daqui: o robô guarda o som que mandou e o ouve de novo, ele mesmo.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import logging
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import numpy as np

from .conversa import MOVIMENTOS_AO_VIVO
from .roteador import Chamado

log = logging.getLogger(__name__)

# Dito pela voz local do robô quando a conversa ao vivo não pode começar.
AVISO_INDISPONIVEL = "A Hera não está disponível agora."
# O aviso falado não se repete a cada chamado: quem insiste já ouviu.
AVISO_INTERVALO_S = 30.0

TAXA = 16000                     # a do canal, nos dois sentidos
CAMINHO = "/api/hera/ao-vivo"
PRAZO_APERTO_S = 5.0
PRAZO_ABERTURA_S = 15.0          # do `aceito` ao `aberta`
PC_MUDO_S = 3.0                  # sem nada do computador: a conversa caiu
PRAZO_FIM_S = 3.0                # depois de pedir o fim, quanto se espera a resposta
# A volta depois de perder o computador. Ele espera o robô por 8 s.
RETOMAR_S = 8.0
PAUSA_RETOMAR_S = 0.5
AVISO_QUEDA = "A conversa com a Hera caiu."
# Sem voz dela por isto, "falando" volta a "ouvindo" (a tela e o balanço da cabeça).
CALADA_S = 1.0
# O corpo durante a conversa (ver o topo).
NIVEIS_DE_MOVIMENTO = MOVIMENTOS_AO_VIVO
MUDO_GESTO_S = 0.9               # antenas: 0,4 s de movimento + o servo assentar
# O som mandado fica guardado por um tempo: é dele que sai o trecho do "Fala Garra".
GUARDA_S = 15.0
TRECHO_MAX_MS = 15_000
MARGEM_TRECHO_MS = 250
_QUEDAS = ("pc_lost", "connection_lost")
# Recusas que nenhuma insistência muda: a conversa acabou lá, ou não é o computador.
_RECUSAS_DEFINITIVAS = ("not_resumable", "bad_pc_proof", "unknown_device", "wrong_host",
                        "disabled", "not_paired", "bad_hello", "bad_version", "bad_proof")


# ─── a prova de posse (gêmea de companion/hera/credencial.py) ────────────────
def prova(papel: str, *, segredo: str, nonce_robo: str, nonce_pc: str,
          host: str, dispositivo: str) -> str:
    """Cada lado prova que conhece o segredo do dispositivo sem mandá-lo: HMAC
    sobre os dois desafios, o endereço que o robô discou e o dispositivo."""
    if papel not in ("pc", "robo"):
        raise ValueError("papel desconhecido")
    mensagem = "\n".join(("garra-hera-v1", papel, nonce_robo, nonce_pc, host, dispositivo))
    return hmac.new(segredo.encode("utf-8"), mensagem.encode("utf-8"), hashlib.sha256).hexdigest()


# ─── quadros de som (gêmeos de companion/hera/canal.py) ──────────────────────
MICROFONE, FALA = 1, 2
VERSAO_QUADRO = 1
_CABECALHO = struct.Struct("<BBHII")


def quadro(tipo: int, seq: int, t_ms: int, pcm: bytes) -> bytes:
    """12 bytes de cabeçalho (tipo, versão, flags, sequência, tempo em ms) + PCM s16le."""
    return _CABECALHO.pack(tipo, VERSAO_QUADRO, 0, seq & 0xFFFFFFFF, t_ms & 0xFFFFFFFF) + pcm


def ler_quadro(dados: bytes) -> tuple[int, int, int, bytes] | None:
    if len(dados) < _CABECALHO.size:
        return None
    tipo, versao, _flags, seq, t_ms = _CABECALHO.unpack_from(dados)
    pcm = dados[_CABECALHO.size:]
    if versao != VERSAO_QUADRO or tipo not in (MICROFONE, FALA) or len(pcm) % 2:
        return None
    return tipo, seq, t_ms, pcm


# ─── som ─────────────────────────────────────────────────────────────────────
def _reamostrar(onda: np.ndarray, de: int, para: int) -> np.ndarray:
    if de == para or onda.size == 0:
        return onda
    n = max(1, int(round(onda.size * para / de)))
    return np.interp(np.linspace(0, onda.size - 1, n), np.arange(onda.size), onda).astype(np.float32)


def para_pcm16(onda: np.ndarray, sr: int) -> bytes:
    """O microfone do robô (float32 mono, em `sr`) no formato do canal."""
    onda = _reamostrar(np.asarray(onda, dtype=np.float32), sr, TAXA)
    return (np.clip(onda, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def de_pcm16(pcm: bytes, sr: int) -> np.ndarray:
    """A voz da Hera (PCM do canal) no formato do alto-falante do robô."""
    onda = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return _reamostrar(onda, TAXA, sr)


def sinal(sr: int, subindo: bool, volume: float = 0.22) -> np.ndarray:
    """Três notas: subindo ao abrir a conversa, descendo ao fechar. Diferentes do
    bipe do "Fala Garra" (duas notas), para quem ouve saber com quem fala."""
    notas = (659.3, 880.0, 1174.7) if subindo else (1174.7, 880.0, 659.3)
    partes = []
    for frequencia in notas:
        t = np.arange(int(sr * 0.09)) / sr
        nota = np.sin(2 * np.pi * frequencia * t) * volume
        borda = int(sr * 0.01)
        nota[:borda] *= np.linspace(0, 1, borda)
        nota[-borda:] *= np.linspace(1, 0, borda)
        partes += [nota, np.zeros(int(sr * 0.02))]
    return np.concatenate(partes).astype(np.float32)


class Saida:
    """A voz da Hera a caminho do alto-falante.

    O Wi-Fi entrega aos solavancos (medido: p99 de 120 ms). Para não picotar, o
    começo de cada fala espera uma folga; com a fala andando, o que chega vai
    direto para a fila do player, que toca os pedaços em sequência.
    """

    def __init__(self, tocar: Callable[[np.ndarray], Any], sr: int, *, folga_s: float = 0.12,
                 relogio: Callable[[], float] = time.monotonic) -> None:
        self._tocar = tocar
        self._sr = sr
        self._folga = int(TAXA * folga_s) * 2
        self._relogio = relogio
        self._pendente = bytearray()
        self._chegou_em = 0.0
        self._acaba_em = 0.0

    @property
    def tocando(self) -> bool:
        return self._relogio() < self._acaba_em

    @property
    def acaba_em(self) -> float:
        """Quando o que já foi para o alto-falante termina de tocar."""
        return self._acaba_em

    def por(self, pcm: bytes) -> None:
        self._pendente += pcm
        self._chegou_em = self._relogio()

    def avancar(self) -> None:
        if not self._pendente:
            return
        agora = self._relogio()
        if not self.tocando and len(self._pendente) < self._folga and agora - self._chegou_em < 0.06:
            return
        pcm, self._pendente = bytes(self._pendente), bytearray()
        onda = de_pcm16(pcm, self._sr)
        self._tocar(onda)
        self._acaba_em = max(agora, self._acaba_em) + onda.size / self._sr

    def cortar(self) -> None:
        self._pendente = bytearray()
        self._acaba_em = self._relogio()


# ─── credenciais e canal ─────────────────────────────────────────────────────
def credenciais(salva: dict, gateway_url: str) -> dict | None:
    """Endereço e segredo do canal, ou `None` sem pareamento.

    O canal fica na ponte do computador, a mesma do chat: o robô já sabe onde ela
    está, e mudar o endereço dela exige a assinatura do computador. O segredo e o
    nome do dispositivo são gravados pelo computador (`python -m companion.hera
    parear`), também com assinatura.
    """
    segredo, dispositivo = salva.get("hera_token"), salva.get("hera_device")
    if not (isinstance(segredo, str) and segredo and isinstance(dispositivo, str) and dispositivo):
        return None
    partes = urlsplit(gateway_url or "")
    if partes.scheme not in ("http", "https") or not partes.hostname:
        return None
    if partes.hostname in ("127.0.0.1", "localhost", "::1"):
        return None                          # o padrão de fábrica: ninguém pareou
    esquema = "wss" if partes.scheme == "https" else "ws"
    return {"url": f"{esquema}://{partes.netloc}{CAMINHO}", "host": partes.netloc,
            "segredo": segredo, "dispositivo": dispositivo}


def _conectar_padrao(url: str, **kw: Any):
    from websockets.sync.client import connect
    return connect(url, **kw)


# Trocado nos testes por um computador de mentira.
conectar: Callable[..., Any] = _conectar_padrao


class Recusado(Exception):
    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


class CanalCaiu(Exception):
    """A conexão com o computador acabou no meio da conversa."""


class Canal:
    """O WebSocket com o computador. Síncrono, de propósito: vive na thread do laço."""

    def __init__(self, url: str, host: str, segredo: str, dispositivo: str, *,
                 conectar: Callable[..., Any] | None = None, prazo_s: float = PRAZO_APERTO_S,
                 t0: float | None = None) -> None:
        self._url, self._host = url, host
        self._segredo, self._dispositivo = segredo, dispositivo
        self._conectar = conectar
        self._prazo = prazo_s
        self._ws = None
        self._seq = 0
        # O relógio dos carimbos de som. Uma conexão de volta usa o da primeira:
        # o trecho do "Fala Garra" vem nele.
        self._t0 = time.monotonic() if t0 is None else t0

    def outro(self) -> "Canal":
        """Uma conexão nova para o mesmo computador, no mesmo relógio."""
        return Canal(self._url, self._host, self._segredo, self._dispositivo,
                     conectar=self._conectar, prazo_s=self._prazo, t0=self._t0)

    def _receber_json(self) -> dict:
        try:
            bruto = self._ws.recv(timeout=self._prazo)
        except Exception:
            raise Recusado("unreachable") from None
        try:
            mensagem = json.loads(bruto)
        except (TypeError, ValueError):
            raise Recusado("bad_response") from None
        if not isinstance(mensagem, dict):
            raise Recusado("bad_response")
        return mensagem

    def abrir(self, como: str, pedido: str, retomar: str | None = None) -> None:
        """Conecta e faz o aperto de mão. Levanta `Recusado` com o motivo.
        `retomar`: a senha da conversa a que o robô volta depois de cair."""
        try:
            fabrica = self._conectar or conectar
            self._ws = fabrica(self._url, open_timeout=self._prazo, close_timeout=2.0,
                               max_size=2 ** 20, compression=None, proxy=None)
        except Exception:
            raise Recusado("unreachable") from None
        try:
            nonce = secrets.token_urlsafe(24)
            ola = {"t": "ola", "v": 1, "dispositivo": self._dispositivo, "host": self._host,
                   "nonce": nonce, "como": como, "pedido": pedido[:500]}
            if retomar is not None:
                ola["retomar"] = retomar
            self._ws.send(json.dumps(ola))
            desafio = self._receber_json()
            if desafio.get("t") == "fim":
                raise Recusado(str(desafio.get("motivo") or "refused"))
            dados = {"segredo": self._segredo, "nonce_robo": nonce,
                     "nonce_pc": str(desafio.get("nonce") or ""), "host": self._host,
                     "dispositivo": self._dispositivo}
            esperado = prova("pc", **dados)
            if desafio.get("t") != "desafio" or not hmac.compare_digest(
                    str(desafio.get("prova") or ""), esperado):
                # Quem respondeu não é o computador pareado: nem a prova do robô
                # sai daqui, e muito menos som.
                raise Recusado("bad_pc_proof")
            self._ws.send(json.dumps({"t": "prova", "prova": prova("robo", **dados)}))
            resposta = self._receber_json()
            if resposta.get("t") != "aceito":
                raise Recusado(str(resposta.get("motivo") or "refused"))
        except BaseException:
            self.fechar()
            raise

    def enviar_som(self, pcm: bytes) -> int:
        """Manda um bloco do microfone. Devolve o carimbo (ms) que ele levou."""
        self._seq += 1
        t_ms = int((time.monotonic() - self._t0) * 1000)
        try:
            self._ws.send(quadro(MICROFONE, self._seq, t_ms, pcm))
        except Exception:
            raise CanalCaiu() from None
        return t_ms

    def enviar(self, mensagem: dict) -> None:
        try:
            self._ws.send(json.dumps(mensagem))
        except Exception:
            raise CanalCaiu() from None

    def receber(self) -> list[tuple[str, Any]]:
        """Tudo o que já chegou, sem esperar: `("som", pcm)` e `("json", dict)`."""
        itens: list[tuple[str, Any]] = []
        while True:
            try:
                bruto = self._ws.recv(timeout=0)
            except TimeoutError:
                return itens
            except Exception:
                if itens:
                    return itens
                raise CanalCaiu() from None
            if isinstance(bruto, (bytes, bytearray)):
                lido = ler_quadro(bytes(bruto))
                if lido and lido[0] == FALA:
                    itens.append(("som", lido[3]))
                continue
            try:
                mensagem = json.loads(bruto)
            except ValueError:
                continue
            if isinstance(mensagem, dict):
                itens.append(("json", mensagem))

    def fechar(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                log.debug("canal ao vivo não fechou limpo", exc_info=True)
            self._ws = None


# ─── o atendimento ───────────────────────────────────────────────────────────
FASE_CONECTANDO = "live_connecting"
FASE_OUVINDO = "live_listening"
FASE_PENSANDO = "live_thinking"
FASE_FALANDO = "live_speaking"
_FASES_DO_PC = {"ouvindo": FASE_OUVINDO, "pensando": FASE_PENSANDO, "falando": FASE_FALANDO,
                "conectando": FASE_CONECTANDO}


@dataclass
class Portas:
    """O que o laço de voz empresta à conversa ao vivo enquanto ela dura."""

    ler_mono: Callable[[], np.ndarray]
    sr_in: int
    sr_out: int
    tocar: Callable[[np.ndarray], Any]          # pelo CoordenadorAudio
    cortar: Callable[[], Any]                   # idem: limpa a fila do player
    fase: Callable[[str], None]
    sem_movimento: Callable[[], None]
    estop: Callable[[], None]
    deve_parar: Callable[[], bool]
    credenciais: Callable[[], dict | None]
    saida_habilitada: Callable[[], bool]
    trava_fala: Any                             # o mesmo lock do `falar` do laço
    # Um quadro da câmera: `(jpeg, prefs, idade_s)` ou algo com `.motivo` (visão
    # desligada, sem câmera, quadro velho). É o `Olho.quadro` do laço.
    olhar: Callable[[], Any] | None = None
    # O corpo: "speaking" (balanço da cabeça), "thinking" (antenas para trás) ou
    # "quiet" (sem balanço). Só se pede o que o nível de `movimento()` permite.
    corpo: Callable[[str], Any] | None = None
    movimento: Callable[[], str] | None = None


class Atendente:
    """Atende os chamados da conversa ao vivo. Um por laço de voz; roda na thread dele."""

    def __init__(self, publicar: Callable[..., Any], falar: Callable[[str], Any],
                 relogio: Callable[[], float] = time.monotonic, *,
                 portas: Portas | None = None) -> None:
        self._publicar = publicar
        self._falar = falar
        self._relogio = relogio
        self._portas = portas
        self._avisou_em: float | None = None
        self._parar = threading.Event()
        self._motivo_parada = "robot_stop"
        self.ativo = False
        # Depois de um "Fala Garra": `{"trecho": som | None}` para o laço passar
        # ao Garra. `None` quando a conversa acabou por outro motivo.
        self.ao_garra: dict | None = None
        self._canal: Canal | None = None
        self._nivel = "still"
        self._fase_viva = FASE_CONECTANDO
        self._falando_desde = 0.0
        self._mudo_ate = 0.0

    def pedir_parada(self, motivo: str = "panel") -> None:
        """PARAR do painel (de outra thread): encerra a conversa em curso."""
        if self.ativo:
            self._motivo_parada = motivo
            self._parar.set()

    def atender(self, chamado: Chamado, reenvio: np.ndarray | None = None) -> bool:
        """Bloqueia até a conversa acabar. `True` se ela chegou a acontecer.

        `reenvio`: o áudio do próprio chamado, quando o pedido veio junto ("Oi
        Hera, que horas são?"). Sobe para a sessão logo que ela abre, no lugar do
        microfone, como se a pessoa tivesse acabado de dizer. Medido: só o texto
        no histórico fez a Hera responder em 1 de 4 sessões; com o som, 4 de 4.
        """
        self.ao_garra = None
        p = self._portas
        if p is None:
            self.indisponivel(chamado, "unsupported")
            return False
        cred = p.credenciais()
        if cred is None:
            self.indisponivel(chamado, "not_paired")
            return False
        if not p.saida_habilitada():
            self.indisponivel(chamado, "speech_output_disabled")
            return False
        if not p.trava_fala.acquire(blocking=False):
            self.indisponivel(chamado, "busy")
            return False
        motivo, segundos, aberta = "unavailable", 0, False
        self._parar.clear()
        self.ativo = True
        canal = self._canal = Canal(cred["url"], cred["host"], cred["segredo"], cred["dispositivo"])
        try:
            p.sem_movimento()
            p.fase(FASE_CONECTANDO)
            self._publicar("voice.live.connecting", destination=chamado.destino, match=chamado.como)
            try:
                canal.abrir(chamado.como, chamado.resto)
            except Recusado as recusa:
                motivo = recusa.motivo
            else:
                motivo, segundos, aberta = self._conduzir(canal, p, reenvio, chamado.como)
        except Exception:
            log.exception("a conversa ao vivo falhou")
            motivo = "robot_error"
        finally:
            (self._canal or canal).fechar()
            self._canal = None
            self.ativo = False
            p.trava_fala.release()
        if not aberta:
            self.indisponivel(chamado, motivo)
            return False
        if motivo not in ("stop", "estop", "handoff_garra"):
            # Três notas descendo: a conversa acabou, o microfone voltou para casa.
            # Na troca para o Garra quem fala é o bipe dele.
            p.tocar(sinal(p.sr_out, subindo=False))
        self._publicar("voice.live.ended", destination=chamado.destino, reason=motivo,
                       seconds=segundos)
        if motivo in _QUEDAS:
            try:
                self._falar(AVISO_QUEDA)
            except Exception:
                log.debug("aviso de queda da conversa ao vivo não foi falado", exc_info=True)
        return True

    # ── o corpo e a tela ─────────────────────────────────────────────────────
    @staticmethod
    def _nivel_de(p: Portas) -> str:
        try:
            nivel = p.movimento() if p.movimento is not None else "still"
        except Exception:
            nivel = "still"
        return nivel if nivel in NIVEIS_DE_MOVIMENTO else "still"

    def _mudar_fase(self, p: Portas, nova: str, agora: float) -> None:
        """A fase da conversa (tela) e o que o corpo faz nela, pelo nível."""
        if nova == self._fase_viva:
            return
        anterior, self._fase_viva = self._fase_viva, nova
        p.fase(nova)
        if nova == FASE_FALANDO:
            self._falando_desde = agora
        if self._nivel == "still" or p.corpo is None:
            return
        if nova == FASE_FALANDO:
            acao = "speaking"           # o chip abafa o microfone enquanto ela fala
        elif nova == FASE_PENSANDO and self._nivel == "expressive":
            acao = "thinking"
            # O servo das antenas é ouvido: o microfone não sobe enquanto elas se mexem.
            self._mudo_ate = agora + MUDO_GESTO_S
        elif anterior == FASE_FALANDO:
            acao = "quiet"
        else:
            return
        try:
            p.corpo(acao)
        except Exception:
            log.debug("o corpo não atendeu na conversa ao vivo", exc_info=True)

    # ── o som mandado, para o "Fala Garra" ───────────────────────────────────
    @staticmethod
    def _guardar(guardado: collections.deque, t_ms: int, onda: np.ndarray) -> None:
        guardado.append((t_ms, np.asarray(onda, dtype=np.float32).copy()))
        while guardado and t_ms - guardado[0][0] > GUARDA_S * 1000:
            guardado.popleft()

    @staticmethod
    def _recortar(guardado: collections.deque, trecho: Any) -> np.ndarray | None:
        """O som do intervalo `[a, b]` (ms, relógio daqui). Intervalo torto: nada."""
        if not (isinstance(trecho, (list, tuple)) and len(trecho) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in trecho)):
            return None
        a, b = trecho
        if b <= a or b - a > TRECHO_MAX_MS:
            return None
        blocos = [onda for t, onda in guardado if a - MARGEM_TRECHO_MS <= t <= b + MARGEM_TRECHO_MS]
        return np.concatenate(blocos) if blocos else None

    # ── a volta depois de perder o computador ────────────────────────────────
    def _voltar(self, canal: Canal, p: Portas, como: str, senha: str) -> Canal | None:
        """Tenta, por `RETOMAR_S`, voltar à mesma conversa. A prova de posse é
        feita de novo; a senha só diz a qual conversa. `None` se não deu."""
        canal.fechar()
        log.info("a conversa ao vivo perdeu o computador; tentando voltar")
        prazo = self._relogio() + RETOMAR_S
        while self._relogio() < prazo:
            if self._parar.is_set() or p.deve_parar():
                return None
            novo = canal.outro()
            try:
                novo.abrir(como, "", retomar=senha)
            except Recusado as recusa:
                if recusa.motivo in _RECUSAS_DEFINITIVAS:
                    log.info("o computador não aceitou a volta (%s)", recusa.motivo)
                    return None
            except Exception:
                log.debug("a volta à conversa ao vivo falhou", exc_info=True)
                novo.fechar()
            else:
                log.info("a conversa ao vivo voltou")
                self._publicar("voice.live.resumed", destination="hera")
                return novo
            time.sleep(PAUSA_RETOMAR_S)
        return None

    def _conduzir(self, canal: Canal, p: Portas, reenvio: np.ndarray | None = None,
                  como: str = "alone") -> tuple[str, float, bool]:
        """O laço da conversa: microfone sobe, voz desce. `(motivo, segundos, abriu)`."""
        saida = Saida(p.tocar, p.sr_out, relogio=self._relogio)
        a_reenviar = np.asarray(reenvio, dtype=np.float32) if reenvio is not None else np.empty(0, np.float32)
        bloco_reenvio = max(1, int(p.sr_in * 0.064))
        proximo_reenvio: float | None = None
        aberta = False
        prazo_abrir = self._relogio() + PRAZO_ABERTURA_S
        ultimo_contato = self._relogio()
        pediu_fim_em: float | None = None
        motivo_local = "robot_stop"
        inicio = 0.0
        senha: str | None = None
        voltou = False
        guardado: collections.deque = collections.deque()
        self._nivel = self._nivel_de(p)
        self._fase_viva = FASE_CONECTANDO
        self._falando_desde = 0.0
        self._mudo_ate = 0.0

        def duracao(agora: float) -> float:
            return round(agora - inicio, 1) if aberta else 0

        def perdeu(agora: float) -> bool:
            """O computador sumiu. `True` se voltou à mesma conversa."""
            nonlocal canal, voltou, ultimo_contato
            saida.cortar()
            p.cortar()
            if not aberta or pediu_fim_em is not None or voltou or not senha:
                return False
            voltou = True                # uma volta por conversa
            self._mudar_fase(p, FASE_CONECTANDO, agora)
            novo = self._voltar(canal, p, como, senha)
            if novo is None:
                return False
            canal = self._canal = novo
            ultimo_contato = self._relogio()
            return True

        while True:
            agora = self._relogio()
            if pediu_fim_em is None and (self._parar.is_set() or p.deve_parar()):
                motivo_local = self._motivo_parada if self._parar.is_set() else "listening_stopped"
                pediu_fim_em = agora
                saida.cortar()
                p.cortar()
                try:
                    canal.enviar({"t": "parar"})
                except CanalCaiu:
                    return motivo_local, duracao(agora), aberta
            mono = p.ler_mono()
            reenviar = np.empty(0, np.float32)
            if aberta and a_reenviar.size and pediu_fim_em is None:
                # O chamado sobe em tempo real, um bloco de 64 ms por vez, no lugar
                # do microfone — o que a sala diz nesse meio-tempo não se mistura.
                proximo_reenvio = agora if proximo_reenvio is None else proximo_reenvio
                if agora >= proximo_reenvio:
                    reenviar, a_reenviar = a_reenviar[:bloco_reenvio], a_reenviar[bloco_reenvio:]
                    proximo_reenvio += 0.064
                mono = np.empty(0, np.float32)
            if mono.size and agora < self._mudo_ate:
                mono = np.zeros_like(mono)       # as antenas se mexendo: silêncio no lugar
            try:
                if reenviar.size:
                    self._guardar(guardado, canal.enviar_som(para_pcm16(reenviar, p.sr_in)), reenviar)
                if mono.size and aberta and pediu_fim_em is None:
                    self._guardar(guardado, canal.enviar_som(para_pcm16(mono, p.sr_in)), mono)
                itens = canal.receber()
            except CanalCaiu:
                if perdeu(agora):
                    continue
                return ("pc_lost" if pediu_fim_em is None else motivo_local), duracao(agora), aberta
            for tipo, item in itens:
                ultimo_contato = agora
                if tipo == "som":
                    if pediu_fim_em is None:
                        saida.por(item)
                    continue
                t = item.get("t")
                if t == "aberta":
                    nova_senha = item.get("retomar")
                    if isinstance(nova_senha, str) and 0 < len(nova_senha) <= 64:
                        senha = nova_senha
                    # Três notas subindo: daqui em diante o som da sala vai para a nuvem.
                    # De novo quando a sessão caiu e o computador abriu outra.
                    p.tocar(sinal(p.sr_out, subindo=True))
                    self._mudar_fase(p, FASE_OUVINDO, agora)
                    if not aberta:
                        aberta, inicio = True, agora
                        self._publicar("voice.live.started", destination="hera")
                elif t == "estado" and item.get("fase") in _FASES_DO_PC:
                    self._mudar_fase(p, _FASES_DO_PC[item["fase"]], agora)
                elif t == "cortar":
                    # Falaram por cima dela: o servidor já a calou. Sai o que ainda
                    # não foi para o alto-falante — sem `clear_player`, que pausaria
                    # o microfone com a pessoa falando.
                    saida.cortar()
                elif t == "estop":
                    saida.cortar()
                    p.cortar()
                    p.estop()
                elif t == "ping":
                    try:
                        canal.enviar({"t": "pong"})
                    except CanalCaiu:
                        pass
                elif t == "olhar":
                    self._mandar_imagem(canal, p, item.get("id"))
                elif t == "fim":
                    motivo = str(item.get("motivo") or "closed")
                    if motivo == "handoff_garra":
                        # Quem fala agora é o Garra: ela para onde estiver.
                        saida.cortar()
                        p.cortar()
                        self.ao_garra = {"trecho": self._recortar(guardado, item.get("trecho_ms"))}
                    elif pediu_fim_em is None:
                        self._esperar_acabar(saida)
                    segundos = item.get("segundos")
                    return (motivo, segundos if isinstance(segundos, (int, float)) else duracao(agora),
                            aberta)
            saida.avancar()
            if (self._fase_viva == FASE_FALANDO and not saida.tocando
                    and agora - max(saida.acaba_em, self._falando_desde) > CALADA_S):
                self._mudar_fase(p, FASE_OUVINDO, agora)
            if not aberta and agora > prazo_abrir:
                return "unavailable", 0, False
            if agora - ultimo_contato > PC_MUDO_S:
                if perdeu(agora):
                    continue
                return "pc_lost", duracao(agora), aberta
            if pediu_fim_em is not None and agora - pediu_fim_em > PRAZO_FIM_S:
                return motivo_local, duracao(agora), aberta
            if mono.size == 0:
                time.sleep(0.005)

    def _mandar_imagem(self, canal: Canal, p: Portas, ident: Any) -> None:
        """Pediram para a Hera olhar: UM quadro e as preferências de visão daqui.

        O computador, que já provou ser o pareado, só pede o quadro — o que a
        pessoa perguntou fica lá. As regras são as do "Fala Garra": visão ligada
        no painel, quadro fresco, nuvem só com o interruptor dela, nada guardado.
        """
        if not isinstance(ident, str) or not 0 < len(ident) <= 64:
            return
        resposta: dict[str, Any] = {"t": "imagem", "id": ident}
        try:
            pego = p.olhar() if p.olhar is not None else None
        except Exception:
            log.exception("o quadro para a Hera falhou")
            pego = None
        if isinstance(pego, tuple):
            jpeg, prefs, idade = pego
            resposta.update(jpeg=base64.b64encode(jpeg).decode("ascii"), prefs=prefs, idade_s=idade)
        else:
            resposta["motivo"] = str(getattr(pego, "motivo", "") or "camera_unavailable")
        try:
            canal.enviar(resposta)
        except CanalCaiu:
            pass                     # a próxima volta do laço percebe a queda

    def _esperar_acabar(self, saida: Saida, teto_s: float = 6.0) -> None:
        """Fim pedido pelo computador: deixa a última frase dela terminar."""
        saida.avancar()
        fim = self._relogio() + teto_s
        while saida.tocando and self._relogio() < fim and not self._parar.is_set():
            time.sleep(0.02)

    def indisponivel(self, chamado: Chamado, motivo: str) -> None:
        """Diz, com evento e (de vez em quando) com a voz local, que não deu.

        O evento sai sempre e não leva nada do que foi dito — nem o pedido que
        veio junto do chamado. O robô nunca finge que atendeu, e o Garra não
        assume a conversa em silêncio.
        """
        self._publicar("voice.live.unavailable", destination=chamado.destino,
                       reason=motivo, match=chamado.como)
        agora = self._relogio()
        if self._avisou_em is not None and agora - self._avisou_em < AVISO_INTERVALO_S:
            return
        self._avisou_em = agora
        try:
            self._falar(AVISO_INDISPONIVEL)
        except Exception:
            # O alto-falante ocupado ou a síntese fora do ar não podem derrubar
            # o laço: o evento já contou o que houve.
            log.debug("aviso de conversa ao vivo indisponível não foi falado", exc_info=True)
