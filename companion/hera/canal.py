"""O canal da conversa ao vivo no computador: o aperto de mão e a condução.

O robô abre um WebSocket na ponte (`/api/hera/ao-vivo`, porta 8126, a que o
firewall já libera). O protocolo:

    robô → {"t": "ola", "v": 1, "dispositivo", "host" (o que discou), "nonce",
            "como": "alone"|"leading", "pedido" (o que veio junto do chamado)}
    PC   → {"t": "desafio", "nonce", "prova"}        o computador prova PRIMEIRO
    robô → {"t": "prova", "prova"}
    PC   → {"t": "aceito"}  ou  {"t": "fim", "motivo", "segundos": 0}
    PC   → {"t": "aberta"} quando a sessão começou; depois {"t": "estado", "fase"},
           {"t": "estop"}, {"t": "ping"}, {"t": "olhar", "id"} e, no fim,
           {"t": "fim", "motivo", "segundos"}
    robô → {"t": "parar"} | {"t": "pong"}
           | {"t": "imagem", "id", "jpeg" (base64), "prefs", "idade_s"}
           | {"t": "imagem", "id", "motivo"}     (resposta a um `olhar`)
    som  → quadros binários: 12 bytes de cabeçalho + PCM s16le mono 16 kHz
           (MICROFONE do robô para cá, FALA daqui para o robô)

Nada do que foi dito atravessa para o robô: o painel dele não pede senha. Nem a
pergunta de quem pede para a Hera olhar: o robô recebe só o pedido do quadro.

`Conversa` conduz uma sessão do começo ao fim e decide quando ela acaba: silêncio,
teto, "pare", "Fala Garra", despedida, queda. Os tetos que valem ANTES de abrir
estão no `livro`.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import secrets
import struct
import time
import unicodedata
import uuid
from typing import Any, Callable

# No topo, e não dentro de `registrar`: o FastAPI resolve as anotações da rota
# pelo módulo (este arquivo usa `from __future__ import annotations`).
from fastapi import WebSocket, WebSocketDisconnect

from . import credencial, livro as livro_mod, olhar, registro as registro_mod
from .ponte_hera import ErroDaPonte, PonteHera

log = logging.getLogger(__name__)

# ─── quadros de som ──────────────────────────────────────────────────────────
MICROFONE, FALA = 1, 2
VERSAO_QUADRO = 1
_CABECALHO = struct.Struct("<BBHII")       # tipo, versão, flags, sequência, tempo (ms)


def quadro(tipo: int, seq: int, t_ms: int, pcm: bytes) -> bytes:
    """Gêmea de `garra_reachy_mini/ao_vivo.py::quadro`; um teste as compara."""
    return _CABECALHO.pack(tipo, VERSAO_QUADRO, 0, seq & 0xFFFFFFFF, t_ms & 0xFFFFFFFF) + pcm


def ler_quadro(dados: bytes) -> tuple[int, int, int, bytes] | None:
    if len(dados) < _CABECALHO.size:
        return None
    tipo, versao, _flags, seq, t_ms = _CABECALHO.unpack_from(dados)
    pcm = dados[_CABECALHO.size:]
    if versao != VERSAO_QUADRO or tipo not in (MICROFONE, FALA) or len(pcm) % 2:
        return None
    return tipo, seq, t_ms, pcm


# ─── o aperto de mão ─────────────────────────────────────────────────────────
VERSAO_PROTOCOLO = 1
PEDIDO_MAX = 500


class Recusa(Exception):
    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


class Aperto:
    """Prova de posse nos dois sentidos (ver `credencial.py`). Uma por conexão."""

    def __init__(self, *, segredo: str, dispositivo: str,
                 host_e_meu: Callable[[str], bool]) -> None:
        self._segredo = segredo
        self._dispositivo = dispositivo
        self._host_e_meu = host_e_meu
        self._dados: dict[str, str] | None = None
        self.como = "alone"
        self.pedido = ""

    def responder(self, ola: Any) -> dict:
        if not isinstance(ola, dict) or ola.get("t") != "ola":
            raise Recusa("bad_hello")
        if ola.get("v") != VERSAO_PROTOCOLO:
            raise Recusa("bad_version")
        nonce, como, pedido = ola.get("nonce"), ola.get("como", "alone"), ola.get("pedido", "")
        if (not isinstance(nonce, str) or not 8 <= len(nonce) <= 128
                or como not in ("alone", "leading")
                or not isinstance(pedido, str) or len(pedido) > PEDIDO_MAX):
            raise Recusa("bad_hello")
        if ola.get("dispositivo") != self._dispositivo:
            raise Recusa("unknown_device")
        host = ola.get("host")
        # O endereço que o robô discou tem de ser ESTE computador: uma prova tirada
        # por um intermediário fica presa ao endereço dele, e não vale aqui.
        if not isinstance(host, str) or not self._host_e_meu(host):
            raise Recusa("wrong_host")
        self._dados = {"segredo": self._segredo, "nonce_robo": nonce,
                       "nonce_pc": secrets.token_urlsafe(18), "host": host,
                       "dispositivo": self._dispositivo}
        self.como, self.pedido = como, pedido.strip()
        return {"t": "desafio", "nonce": self._dados["nonce_pc"],
                "prova": credencial.prova("pc", **self._dados)}

    def conferir(self, mensagem: Any) -> bool:
        if self._dados is None or not isinstance(mensagem, dict) or mensagem.get("t") != "prova":
            return False
        return credencial.confere(mensagem.get("prova"), "robo", **self._dados)


# ─── o que se diz durante a conversa ─────────────────────────────────────────
def _normal(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto.lower())
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", sem_acento).split())


_VOCATIVO = r"(?:(?:hera|era|oi|ei|ola|ok|garra)\s+)*"
_PARE = re.compile(rf"^{_VOCATIVO}(?:pare|para|parar|chega|stop)(?:\s+(?:agora|ja|por favor|tudo))*$")
_FALA_GARRA = re.compile(r"^(?:oi\s+|ei\s+)?fala\s+garra(?:ia)?\b")
_DESPEDIDA = re.compile(r"\b(?:tchau|ate logo|ate mais|pode desligar|e so isso|era so isso|"
                        r"pode encerrar|encerrar|encerra a conversa)\b")
_ECO_S = 2.0                    # quanto tempo a fala dela conta como possível eco


def comando(segmento: str) -> str | None:
    """O que o trecho dito pede: "stop", "handoff_garra", "farewell" ou nada."""
    n = _normal(segmento)
    if not n:
        return None
    if _PARE.match(n):
        return "stop"
    if _FALA_GARRA.match(n):
        return "handoff_garra"
    if _DESPEDIDA.search(n):
        return "farewell"
    return None


# ─── a condução ──────────────────────────────────────────────────────────────
FALHA_DO_HERMES = "Não consegui consultar isso agora. Tente de novo em instantes."
_DIAS = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")


def agora_local(momento: float | None = None) -> str:
    """Data e hora deste computador, para acompanhar cada turno delegado. Sem isso,
    num nível sem ferramentas o Hermes respondeu que não conseguia ver o relógio."""
    lt = time.localtime(momento)
    return (f"Data e hora locais: {_DIAS[lt.tm_wday]}, {lt.tm_mday:02d}/{lt.tm_mon:02d}/{lt.tm_year}, "
            f"{lt.tm_hour:02d}:{lt.tm_min:02d} ({time.strftime('%Z', lt)}).")
NAO_OUVI = "Não consegui ouvir a pergunta. Pode repetir?"
PENSANDO = "Consultando."
PEDACO_MAX = 1400              # por `session.commentary.append` (≤ 500 tokens)
SEGMENTO_S = 1.2               # pausa entre deltas que separa um trecho do outro
CONFIRMAR_COMANDO_S = 0.7      # um "pare" só vale se a frase acabou ali
ESPERA_INICIO_S = 12.0
ESPERA_FECHAR_S = 3.0
ESPERA_IMAGEM_S = 4.0          # do pedido de quadro à imagem do robô


def pedacos(texto: str, maximo: int = PEDACO_MAX) -> list[str]:
    texto = " ".join(texto.split())
    saida: list[str] = []
    while len(texto) > maximo:
        corte = max(texto.rfind(s, 0, maximo) for s in (". ", "! ", "? ", "; ", ", "))
        corte = corte + 1 if corte > maximo // 3 else texto.rfind(" ", 0, maximo)
        if corte <= 0:
            corte = maximo
        saida.append(texto[:corte].strip())
        texto = texto[corte:].strip()
    if texto:
        saida.append(texto)
    return saida


class Conversa:
    """Uma sessão ao vivo, do pedido de abertura ao fim. Vive no laço da ponte."""

    def __init__(self, robo: Any, *, ponte: Any, livro: livro_mod.Livro, limites: dict,
                 dispositivo: str, nivel: str, como: str = "alone", pedido: str = "",
                 abrir_par: Callable[..., Any] | None = None,
                 tolerancia_despedida_s: float = 4.0,
                 descrever: Callable[[bytes, str, Any], Any] | None = None) -> None:
        """`descrever(jpeg, pergunta, prefs)`: o modelo de visão da casa (o da
        ponte). Sem ele, pedir para olhar segue para o Hermes como antes."""
        self._robo = robo
        self._descrever = descrever
        self._imagens: dict[str, asyncio.Future] = {}
        self._ponte = ponte
        self._livro = livro
        self._lim = limites
        self._dispositivo, self._nivel, self._como = dispositivo, nivel, como
        self._pedido = pedido
        self._abrir_par = abrir_par
        self._tolerancia_despedida = tolerancia_despedida_s
        self._id = uuid.uuid4().hex
        self._motivo: str | None = None
        self._saida: asyncio.Queue | None = None
        self._comecou = asyncio.Event()
        self._fechada = asyncio.Event()
        self._uso: float | None = None
        self._sid: int | None = None
        self._par = None
        # o que a pessoa disse e quando
        self._falou = bool(pedido)
        self._dito = ""
        self._segmento = ""
        self._ultimo_delta = 0.0
        self._comando: tuple[str, float] | None = None
        self._despedida_em: float | None = None
        self._ultima_atividade = 0.0
        self._fala_dela: collections.deque = collections.deque(maxlen=40)
        self._pendentes = 0
        self._delegacoes = 0
        self._fase = ""
        self._tarefas: set[asyncio.Task] = set()

    # ── chamado pelo canal ───────────────────────────────────────────────────
    def microfone(self, pcm: bytes) -> None:
        if self._par is not None:
            self._par.microfone(pcm)

    def parar(self, motivo: str) -> None:
        if self._motivo is None:
            self._motivo = motivo

    def imagem(self, mensagem: dict) -> None:
        """A resposta do robô a um pedido de quadro. Só vale a do pedido em aberto."""
        futuro = self._imagens.get(mensagem.get("id"))
        if futuro is not None and not futuro.done():
            futuro.set_result(mensagem)

    # ── para o robô, em ordem ────────────────────────────────────────────────
    def _ao_robo(self, tipo: str, item: Any) -> None:
        if self._saida is not None:
            self._saida.put_nowait((tipo, item))

    def _estado(self, fase: str) -> None:
        if fase != self._fase:
            self._fase = fase
            self._ao_robo("json", {"t": "estado", "fase": fase})

    async def _enviar_ao_robo(self) -> None:
        assert self._saida is not None
        while True:
            tipo, item = await self._saida.get()
            if tipo == "fim":
                return
            try:
                if tipo == "json":
                    await self._robo.enviar_json(item)
                else:
                    await self._robo.enviar_audio(item)
            except Exception:
                log.debug("o robô não recebeu uma mensagem da conversa ao vivo", exc_info=True)
                self.parar("robot_lost")

    # ── o que vem da sessão ──────────────────────────────────────────────────
    async def _trocar_sdp(self, oferta: str) -> str:
        historico = None
        if self._como == "leading" and self._pedido:
            historico = [{"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": self._pedido[:PEDIDO_MAX]}]}]
        sessao = await asyncio.to_thread(self._ponte.sessao, oferta, historico)
        if self._sid is not None:
            self._livro.anotar(self._sid, sessao_openai=sessao.id)
        return sessao.sdp

    def _do_par_audio(self, pcm: bytes) -> None:
        self._ao_robo("audio", pcm)

    def _caiu(self, motivo: str) -> None:
        log.info("a conexão da conversa ao vivo caiu (%s)", motivo)
        self.parar("connection_lost")

    def _do_par_evento(self, evento: dict) -> None:
        tipo = evento.get("type")
        agora = time.monotonic()
        if tipo == "session.started":
            self._comecou.set()
        elif tipo == "session.input_transcript.delta":
            self._ouviu(str(evento.get("delta") or ""), agora)
        elif tipo == "session.output_transcript.delta":
            delta = str(evento.get("delta") or "")
            self._fala_dela.append((agora, delta))
            self._ultima_atividade = agora
            self._estado("falando")
        elif tipo == "session.delegation.created":
            ident = (evento.get("delegation") or {}).get("id")
            if isinstance(ident, str) and ident:
                tarefa = asyncio.ensure_future(self._delegar(ident))
                self._tarefas.add(tarefa)
                tarefa.add_done_callback(self._tarefas.discard)
        elif tipo == "session.closed":
            uso = (evento.get("usage") or {}).get("seconds")
            if isinstance(uso, (int, float)):
                self._uso = float(uso)
            self._fechada.set()
            if self._motivo is None and evento.get("reason") not in (None, "close_requested"):
                self.parar(str(evento.get("reason")))
        elif tipo == "error":
            erro = evento.get("error") or {}
            log.info("a sessão ao vivo avisou um erro: %s", erro.get("code") or "sem código")

    def _ouviu(self, delta: str, agora: float) -> None:
        self._falou = True
        self._ultima_atividade = agora
        self._estado("ouvindo")
        if agora - self._ultimo_delta > SEGMENTO_S:
            self._segmento = ""
        self._ultimo_delta = agora
        self._segmento += delta
        self._dito = (self._dito + delta)[-2000:]
        pedido = comando(self._segmento)
        if pedido == "stop" and self._eco_de_pare(agora):
            pedido = None
        if pedido == "farewell":
            if self._despedida_em is None:
                self._despedida_em = agora + self._tolerancia_despedida
            self._comando = None
        else:
            self._comando = (pedido, agora) if pedido else None

    def _eco_de_pare(self, agora: float) -> bool:
        """O "pare" que acabou de sair da boca dela, voltando pelo microfone."""
        recente = " ".join(t for (q, t) in self._fala_dela if agora - q <= _ECO_S)
        return bool(re.search(r"\b(?:pare|para|parar|chega)\b", _normal(recente)))

    async def _delegar(self, ident: str) -> None:
        texto = self._dito.strip()
        self._dito = ""
        if not texto and self._delegacoes == 0 and self._pedido:
            texto = self._pedido
        if self._par is None:
            return
        if not texto:
            for pedaco in pedacos(NAO_OUVI):
                self._par.enviar({"type": "session.commentary.append", "delegation_id": ident, "content": pedaco})
            return
        ver = self._descrever is not None and olhar.pede_visao(texto)
        self._pendentes += 1
        self._delegacoes += 1
        self._estado("pensando")
        self._par.enviar({"type": "session.thinking.append", "delegation_id": ident,
                          "content": olhar.OLHANDO if ver else PENSANDO})
        try:
            if ver:
                resposta = await self._olhar(texto)
            else:
                turno = await asyncio.to_thread(self._ponte.turno, self._id, texto, agora_local())
                resposta = turno.texto.strip() or FALHA_DO_HERMES
        except ErroDaPonte as erro:
            log.info("o Hermes não atendeu a delegação (%s)", erro.codigo)
            resposta = FALHA_DO_HERMES
        except Exception:
            log.exception("a delegação ao Hermes falhou")
            resposta = FALHA_DO_HERMES
        finally:
            self._pendentes -= 1
            self._ultima_atividade = time.monotonic()
        if self._par is not None:
            for pedaco in pedacos(resposta):
                self._par.enviar({"type": "session.commentary.append", "delegation_id": ident,
                                  "content": pedaco})

    async def _olhar(self, pergunta: str) -> str:
        """UM quadro do robô, descrito aqui. Devolve o que ela vai falar — a
        descrição ou o porquê de não ter visto. Nunca levanta (salvo cancelamento)."""
        pedido = uuid.uuid4().hex[:16]
        futuro = asyncio.get_running_loop().create_future()
        self._imagens[pedido] = futuro
        self._ao_robo("json", {"t": "olhar", "id": pedido})
        try:
            resposta = await asyncio.wait_for(futuro, ESPERA_IMAGEM_S)
        except asyncio.TimeoutError:
            log.info("a Hera pediu um quadro e o robô não respondeu")
            return olhar.sem_visao("camera_silent")
        finally:
            self._imagens.pop(pedido, None)
        lido = olhar.ler_imagem(resposta)
        if isinstance(lido, str):
            log.info("a Hera pediu um quadro e o robô não mandou imagem (%s)", lido)
            return olhar.sem_visao(lido)
        jpeg, prefs = lido
        try:
            observado = await asyncio.to_thread(self._descrever, jpeg, pergunta, prefs)
        except Exception as erro:
            codigo = getattr(erro, "codigo", None)
            if codigo is None:
                log.exception("a descrição do quadro para a Hera falhou")
            else:
                log.info("a Hera não conseguiu ver: %s", codigo)
            return olhar.sem_visao(codigo or "provider_error")
        descricao = " ".join(str(getattr(observado, "texto", "") or "").split())
        if not descricao:
            return olhar.sem_visao("provider_error")
        log.info("a Hera olhou: %s/%s em %s ms (%d bytes de imagem)", getattr(observado, "provedor", "?"),
                 getattr(observado, "modelo", "?"), getattr(observado, "ms", "?"), len(jpeg))
        return olhar.vista(descricao)

    # ── o relógio da conversa ────────────────────────────────────────────────
    def _vencido(self, inicio: float, agora: float) -> str | None:
        if self._motivo:
            return self._motivo
        if self._comando:
            # Um "pare" à espera de confirmação vence o silêncio: a frase só precisa
            # acabar ali para valer.
            if agora - max(self._comando[1], self._ultimo_delta) >= CONFIRMAR_COMANDO_S:
                return self._comando[0]
            return None
        if self._despedida_em is not None and agora >= self._despedida_em:
            return "farewell"
        if agora - inicio >= self._lim["session_max_s"]:
            return "max"
        if self._pendentes:
            return None
        if not self._falou and agora - inicio >= self._lim["no_speech_s"]:
            return "no_speech"
        if self._falou and agora - max(self._ultima_atividade, inicio) >= self._lim["idle_close_s"]:
            return "idle"
        return None

    async def conduzir(self) -> dict:
        """Abre, conduz e fecha. Devolve `{"motivo", "segundos", "delegacoes"}`."""
        self._saida = asyncio.Queue()
        enviador = asyncio.ensure_future(self._enviar_ao_robo())
        self._sid = self._livro.abrir(dispositivo=self._dispositivo, nivel=self._nivel,
                                      como=self._como, agora=time.time())
        inicio: float | None = None
        motivo = "unavailable"
        if self._abrir_par is None:
            from .par import Par
            self._abrir_par = Par
        try:
            par = self._abrir_par(self._trocar_sdp, ao_audio=self._do_par_audio,
                                  ao_evento=self._do_par_evento, ao_cair=self._caiu)
            await par.abrir()
            self._par = par
            await asyncio.wait_for(self._comecou.wait(), ESPERA_INICIO_S)
            inicio = time.monotonic()
            self._ao_robo("json", {"t": "aberta"})
            self._estado("ouvindo")
            proximo_ping = inicio
            while True:
                agora = time.monotonic()
                vencido = self._vencido(inicio, agora)
                if vencido:
                    motivo = vencido
                    break
                if agora >= proximo_ping:
                    self._ao_robo("json", {"t": "ping"})
                    proximo_ping = agora + 1.0
                await asyncio.sleep(0.05)
        except ErroDaPonte as erro:
            motivo = erro.codigo
        except asyncio.TimeoutError:
            motivo = "unavailable"
        except Exception:
            log.exception("a conversa ao vivo não abriu")
            motivo = "unavailable"
        if motivo == "stop":
            self._ao_robo("json", {"t": "estop"})
        segundos = await self._encerrar(inicio)
        self._livro.fechar(self._sid, segundos=segundos, motivo=motivo,
                           delegacoes=self._delegacoes, agora=time.time())
        self._ao_robo("json", {"t": "fim", "motivo": motivo, "segundos": segundos})
        self._ao_robo("fim", None)
        await enviador
        log.info("conversa ao vivo encerrada: %s, %s s, %d delegações", motivo, segundos, self._delegacoes)
        return {"motivo": motivo, "segundos": segundos, "delegacoes": self._delegacoes}

    async def _encerrar(self, inicio: float | None) -> float:
        par, self._par = self._par, None
        for tarefa in list(self._tarefas):
            tarefa.cancel()
        if par is not None:
            if par.pronto and par.enviar({"type": "session.close"}):
                try:
                    await asyncio.wait_for(self._fechada.wait(), ESPERA_FECHAR_S)
                except asyncio.TimeoutError:
                    pass
            await par.fechar()
        if inicio is None:
            return 0
        if self._uso is not None:
            return int(self._uso) if self._uso == int(self._uso) else round(self._uso, 1)
        return round(time.monotonic() - inicio, 1)


# ─── a rota ──────────────────────────────────────────────────────────────────
ESPERA_APERTO_S = 5.0
ROBO_MUDO_S = 5.0


def _host_e_meu(host: str) -> bool:
    """`ip:porta` aponta para esta máquina, na porta da ponte?"""
    from .. import ponte, vigia
    ip, _, porta = host.rpartition(":")
    return bool(ip) and porta == str(ponte.PORTA) and vigia._ipv4(ip) is not None \
        and vigia._e_deste_computador(ip)


class _RoboWS:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._seq = 0
        self._t0 = time.monotonic()

    async def enviar_json(self, mensagem: dict) -> None:
        await self._ws.send_text(json.dumps(mensagem, ensure_ascii=False))

    async def enviar_audio(self, pcm: bytes) -> None:
        self._seq += 1
        await self._ws.send_bytes(quadro(FALA, self._seq, int((time.monotonic() - self._t0) * 1000), pcm))


def registrar(app: Any, *, ponte: Any = None, abrir_par: Callable[..., Any] | None = None,
              host_e_meu: Callable[[str], bool] = _host_e_meu,
              limites_de_teste: dict | None = None,
              descrever: Callable[[bytes, str, Any], Any] | None = None) -> None:
    """Põe `/api/hera/ao-vivo` no app da ponte do robô. `descrever` é o olho da
    ponte (`ponte._descrever_para_hera`); sem ele, a Hera não olha."""
    ponte = ponte or PonteHera()
    uma_por_vez = asyncio.Lock()
    try:
        orfas = livro_mod.Livro().fechar_orfas(agora=time.time())
        if orfas:
            log.info("conversa ao vivo: %d sessão(ões) sem fim marcada(s) como órfã(s)", len(orfas))
    except Exception:
        log.debug("não consegui conferir as sessões órfãs", exc_info=True)

    async def _receber_texto(ws: WebSocket) -> Any:
        bruto = await asyncio.wait_for(ws.receive_text(), ESPERA_APERTO_S)
        try:
            return json.loads(bruto)
        except ValueError:
            return None

    async def _recusar(ws: WebSocket, motivo: str) -> None:
        await ws.send_text(json.dumps({"t": "fim", "motivo": motivo, "segundos": 0}))
        await ws.close()

    @app.websocket("/api/hera/ao-vivo")
    async def hera_ao_vivo(ws: WebSocket) -> None:
        await ws.accept()
        try:
            ola = await _receber_texto(ws)
            reg = registro_mod.Registro()
            if not reg.liberada():
                return await _recusar(ws, "disabled")
            if not credencial.existe():
                return await _recusar(ws, "not_paired")
            atual = reg.ler()
            aperto = Aperto(segredo=credencial.segredo(), dispositivo=atual["device"]["id"],
                            host_e_meu=host_e_meu)
            try:
                await ws.send_text(json.dumps(aperto.responder(ola)))
            except Recusa as recusa:
                return await _recusar(ws, recusa.motivo)
            if not aperto.conferir(await _receber_texto(ws)):
                log.warning("conversa ao vivo: prova do robô recusada")
                return await _recusar(ws, "bad_proof")
            if uma_por_vez.locked():
                return await _recusar(ws, "busy")
            async with uma_por_vez:
                livro = livro_mod.Livro()
                pode, motivo = livro.pode_abrir(atual["limits"], agora=time.time())
                if not pode:
                    return await _recusar(ws, motivo)
                await ws.send_text(json.dumps({"t": "aceito"}))
                conversa = Conversa(_RoboWS(ws), ponte=ponte, livro=livro,
                                    limites={**atual["limits"], **(limites_de_teste or {})},
                                    dispositivo=atual["device"]["id"], nivel=atual["level"],
                                    como=aperto.como, pedido=aperto.pedido, abrir_par=abrir_par,
                                    descrever=descrever)
                conduzindo = asyncio.ensure_future(conversa.conduzir())
                ouvindo = asyncio.ensure_future(_escutar_robo(ws, conversa))
                await conduzindo
                ouvindo.cancel()
            await ws.close()
        except (WebSocketDisconnect, asyncio.TimeoutError):
            return
        except RuntimeError:
            # o robô fechou no meio do envio
            return

    async def _escutar_robo(ws: WebSocket, conversa: Conversa) -> None:
        while True:
            try:
                mensagem = await asyncio.wait_for(ws.receive(), ROBO_MUDO_S)
            except asyncio.TimeoutError:
                conversa.parar("robot_lost")
                return
            if mensagem.get("type") == "websocket.disconnect":
                conversa.parar("robot_lost")
                return
            if mensagem.get("bytes"):
                lido = ler_quadro(mensagem["bytes"])
                if lido and lido[0] == MICROFONE:
                    conversa.microfone(lido[3])
            elif mensagem.get("text"):
                try:
                    pedido = json.loads(mensagem["text"])
                except ValueError:
                    continue
                if isinstance(pedido, dict) and pedido.get("t") == "parar":
                    conversa.parar("robot_stop")
                elif isinstance(pedido, dict) and pedido.get("t") == "imagem":
                    conversa.imagem(pedido)
