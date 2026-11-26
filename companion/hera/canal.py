"""O canal da conversa ao vivo no computador: o aperto de mão e a condução.

O robô abre um WebSocket na ponte (`/api/hera/ao-vivo`, porta 8126, a que o
firewall já libera). O protocolo:

    robô → {"t": "ola", "v": 1, "dispositivo", "host" (o que discou), "nonce",
            "como": "alone"|"leading", "pedido" (o que veio junto do chamado),
            "retomar"?: a senha da conversa a que ele volta depois de cair}
    PC   → {"t": "desafio", "nonce", "prova"}        o computador prova PRIMEIRO
    robô → {"t": "prova", "prova"}
    PC   → {"t": "aceito"}  ou  {"t": "fim", "motivo", "segundos": 0}
    PC   → {"t": "aberta", "retomar"} quando a sessão começou (de novo, se ela
           caiu e outra abriu); depois {"t": "estado", "fase"}, {"t": "cortar"}
           (falaram por cima dela), {"t": "estop"}, {"t": "ping"}, {"t": "olhar",
           "id"} e, no fim, {"t": "fim", "motivo", "segundos", "trecho_ms"?}
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
import secrets
import struct
import time
import uuid
from typing import Any, Callable

# No topo, e não dentro de `registrar`: o FastAPI resolve as anotações da rota
# pelo módulo (este arquivo usa `from __future__ import annotations`).
from fastapi import WebSocket, WebSocketDisconnect

from . import comandos, credencial, livro as livro_mod, olhar, registro as registro_mod
from . import sentinela as sentinela_mod
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
        self.retomar: str | None = None

    def responder(self, ola: Any) -> dict:
        if not isinstance(ola, dict) or ola.get("t") != "ola":
            raise Recusa("bad_hello")
        if ola.get("v") != VERSAO_PROTOCOLO:
            raise Recusa("bad_version")
        nonce, como, pedido = ola.get("nonce"), ola.get("como", "alone"), ola.get("pedido", "")
        retomar = ola.get("retomar")
        if (not isinstance(nonce, str) or not 8 <= len(nonce) <= 128
                or como not in ("alone", "leading")
                or not isinstance(pedido, str) or len(pedido) > PEDIDO_MAX
                or (retomar is not None and (not isinstance(retomar, str) or not 0 < len(retomar) <= 64))):
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
        self.como, self.pedido, self.retomar = como, pedido.strip(), retomar
        return {"t": "desafio", "nonce": self._dados["nonce_pc"],
                "prova": credencial.prova("pc", **self._dados)}

    def conferir(self, mensagem: Any) -> bool:
        if self._dados is None or not isinstance(mensagem, dict) or mensagem.get("t") != "prova":
            return False
        return credencial.confere(mensagem.get("prova"), "robo", **self._dados)


# ─── o que se diz durante a conversa ─────────────────────────────────────────
# Os comandos moram em `comandos.py`: a sentinela local os reconhece também.
comando = comandos.comando
_ECO_S = 2.0                    # quanto tempo a fala dela conta como possível eco
# Para a sentinela, mais: o trecho dela só chega depois do fim da fala (0,8 s de
# silêncio) e da transcrição.
_ECO_LOCAL_S = 4.0


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
# "Fala Garra" ouvido pela nuvem espera um pouco o trecho da sentinela, que
# deixa o robô ouvir de novo o pedido feito ao Garra.
ESPERA_TRECHO_S = 1.5
# Fala da pessoa até este tempo depois da última fala dela é interrupção: o
# servidor a cala (medido: 0,36 s), e o que dela ainda não saiu não deve sair.
INTERROMPE_S = 1.5
ESPERA_INICIO_S = 12.0
# Reconexão. Caiu a sessão da OpenAI: abre-se outra UMA vez, com o histórico
# montado aqui (a API não retoma sessão, e o armazenamento da OpenAI está
# desligado, então não há "fork"). Caiu o robô: a conversa espera a volta dele.
RETOMAR_S = 8.0
# Um envio ao robô que não sai em tanto tempo é de uma conexão morta: o TCP de um
# Wi-Fi que caiu não avisa, e um envio preso seguraria o robô que voltou.
ENVIO_MAX_S = 2.0
QUEDAS_DA_SESSAO = ("connection_lost", "remote_hangup")
HISTORIA_MAX = 40                  # mensagens
HISTORIA_MAX_CARACTERES = 6000     # o teto da API é ~8 mil tokens
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
                 descrever: Callable[[bytes, str, Any], Any] | None = None,
                 transcrever: Callable[[Any], tuple[str, list[float] | None]] | None = None) -> None:
        """`descrever(jpeg, pergunta, prefs)`: o modelo de visão da casa (o da
        ponte). Sem ele, pedir para olhar segue para o Hermes como antes.
        `transcrever(audio)`: o Whisper desta máquina, para a sentinela. Sem ele,
        os comandos dependem só da transcrição da nuvem."""
        self._robo = robo
        self._descrever = descrever
        self._transcrever = transcrever
        self._sentinela: sentinela_mod.Sentinela | None = None
        self._trecho_garra: tuple[int, int] | None = None
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
        self._ultima_fala_dela = 0.0
        self._cortada = False
        self._interrupcoes = 0
        self._pendentes = 0
        self._delegacoes = 0
        self._fase = ""
        self._tarefas: set[asyncio.Task] = set()
        # reconexão
        self.retomada = secrets.token_urlsafe(18)
        self._robo_caiu_em: float | None = None
        self._terminou = asyncio.Event()
        self._geracao = 0                  # qual par é o da sessão atual
        self._reabrir: str | None = None
        self._reaberturas = 0
        self._historia: list[list[str]] = []   # [papel, texto], para a sessão seguinte
        self._inicio_sessao: float | None = None
        self._segundos_anteriores = 0.0

    # ── chamado pelo canal ───────────────────────────────────────────────────
    def microfone(self, pcm: bytes, t_ms: int | None = None) -> None:
        """Som do robô: sobe para a sessão e passa pela sentinela. `t_ms` é o
        carimbo do próprio robô (o do cabeçalho do quadro)."""
        if self._par is not None:
            self._par.microfone(pcm)
        if self._sentinela is not None:
            self._sentinela.ouvir(pcm, t_ms)

    def parar(self, motivo: str) -> None:
        if self._motivo is None:
            self._motivo = motivo

    def robo_caiu(self, robo: Any) -> None:
        """A conexão com `robo` acabou. Se ele é o desta conversa, ela fica à
        espera da volta dele por `RETOMAR_S`; o que ela falar enquanto isso se perde."""
        if robo is None or robo is not self._robo:
            return
        self._robo = None
        self._robo_caiu_em = time.monotonic()
        log.info("o robô saiu da conversa ao vivo; esperando a volta por %.0f s", RETOMAR_S)

    def pode_retomar(self, senha: Any) -> bool:
        """`senha` é a desta conversa, e ela ainda está viva? O robô pode voltar
        antes de o computador perceber que ele caiu (o Wi-Fi não avisa)."""
        return (self._motivo is None and not self._terminou.is_set()
                and isinstance(senha, str) and secrets.compare_digest(senha, self.retomada))

    def reatar(self, robo: Any) -> None:
        """O robô voltou (outra conexão, com prova e senha): a conversa segue nele."""
        self._robo, self._robo_caiu_em = robo, None
        log.info("o robô voltou para a conversa ao vivo")
        self._ao_robo("json", {"t": "estado", "fase": self._fase or "ouvindo"})

    async def terminou(self) -> None:
        await self._terminou.wait()

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
            robo = self._robo
            if robo is None:
                continue                   # robô fora: o que era para ele se perde
            try:
                envio = robo.enviar_json(item) if tipo == "json" else robo.enviar_audio(item)
                await asyncio.wait_for(envio, ENVIO_MAX_S)
            except Exception:
                log.debug("o robô não recebeu uma mensagem da conversa ao vivo", exc_info=True)
                self.robo_caiu(robo)

    # ── o que vem da sessão ──────────────────────────────────────────────────
    def _historico_inicial(self) -> list[dict] | None:
        if self._como == "leading" and self._pedido:
            return [{"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": self._pedido[:PEDIDO_MAX]}]}]
        return None

    def _anotar(self, papel: str, delta: str) -> None:
        """O que foi dito, por quem: a matéria do histórico de uma sessão nova."""
        if not delta:
            return
        if self._historia and self._historia[-1][0] == papel:
            self._historia[-1][1] += delta
        else:
            self._historia.append([papel, delta])
            if len(self._historia) > HISTORIA_MAX:
                del self._historia[0]

    def _historico(self) -> list[dict] | None:
        """A conversa até aqui, das falas mais novas para trás, dentro do teto."""
        itens: list[dict] = []
        total = 0
        for papel, texto in reversed(self._historia):
            texto = " ".join(texto.split())
            if not texto:
                continue
            if total + len(texto) > HISTORIA_MAX_CARACTERES:
                break
            total += len(texto)
            tipo = "input_text" if papel == "user" else "output_text"
            itens.append({"type": "message", "role": papel, "content": [{"type": tipo, "text": texto}]})
        return list(reversed(itens)) or None

    async def _trocar_sdp(self, oferta: str, historico: list[dict] | None = None) -> str:
        sessao = await asyncio.to_thread(self._ponte.sessao, oferta, historico)
        if self._sid is not None:
            self._livro.anotar(self._sid, sessao_openai=sessao.id)
        return sessao.sdp

    async def _abrir_sessao(self, historico: list[dict] | None) -> None:
        """Um par novo, e a sessão dele começada. O que um par antigo ainda disser
        (ele pode estar morrendo devagar) não conta: cada um tem sua geração."""
        self._geracao += 1
        g = self._geracao
        self._comecou.clear()
        self._fechada.clear()
        self._uso = None
        self._inicio_sessao = None

        async def trocar(oferta: str) -> str:
            return await self._trocar_sdp(oferta, historico)

        par = self._abrir_par(trocar, ao_audio=lambda pcm: self._do_par_audio(pcm, g),
                              ao_evento=lambda evento: self._do_par_evento(evento, g),
                              ao_cair=lambda motivo: self._caiu(motivo, g))
        await par.abrir()
        self._par = par
        await asyncio.wait_for(self._comecou.wait(), ESPERA_INICIO_S)

    def _segundos_da_sessao(self) -> float:
        """Os segundos cobrados da sessão atual: os que ela informou ao fechar ou,
        sem isso (caiu), o relógio daqui."""
        if self._inicio_sessao is None:
            return 0.0
        if self._uso is not None:
            return self._uso
        return time.monotonic() - self._inicio_sessao

    async def _reabrir_sessao(self) -> bool:
        """A sessão caiu: outra, com o histórico. `False` se não deu."""
        queda, self._reabrir = self._reabrir, None
        self._reaberturas += 1
        log.info("a sessão ao vivo caiu (%s); abrindo outra com o histórico", queda)
        self._estado("conectando")
        velho, self._par = self._par, None
        self._segundos_anteriores += self._segundos_da_sessao()
        if velho is not None:
            await velho.fechar()
        try:
            await self._abrir_sessao(self._historico())
        except asyncio.CancelledError:
            raise
        except Exception as erro:
            log.info("a sessão ao vivo não reabriu (%s)", getattr(erro, "codigo", type(erro).__name__))
            return False
        # Para o robô: três notas subindo de novo, e "pode falar".
        self._ao_robo("json", {"t": "aberta", "retomar": self.retomada})
        self._estado("ouvindo")
        return True

    def _vigiar(self, corrotina: Any) -> asyncio.Task:
        """Uma tarefa da conversa: cancelada no fim, junto com as outras."""
        tarefa = asyncio.ensure_future(corrotina)
        self._tarefas.add(tarefa)
        tarefa.add_done_callback(self._tarefas.discard)
        return tarefa

    def _do_par_audio(self, pcm: bytes, geracao: int | None = None) -> None:
        if geracao is not None and geracao != self._geracao:
            return
        self._ao_robo("audio", pcm)

    def _caiu(self, motivo: str, geracao: int | None = None) -> None:
        if geracao is not None and geracao != self._geracao:
            return
        self._queda_da_sessao(motivo)

    def _queda_da_sessao(self, motivo: str) -> None:
        """A sessão da OpenAI caiu: uma reabertura por conversa; na segunda, acabou."""
        if self._motivo is not None or self._reabrir is not None:
            return
        if self._inicio_sessao is not None and self._reaberturas < 1:
            self._reabrir = motivo
        else:
            log.info("a conexão da conversa ao vivo caiu (%s)", motivo)
            self.parar("connection_lost")

    def _do_par_evento(self, evento: dict, geracao: int | None = None) -> None:
        if geracao is not None and geracao != self._geracao:
            return
        tipo = evento.get("type")
        agora = time.monotonic()
        if tipo == "session.started":
            self._inicio_sessao = agora
            self._comecou.set()
        elif tipo == "session.input_transcript.delta":
            if (not self._cortada and self._fase == "falando"
                    and agora - self._ultima_fala_dela <= INTERROMPE_S):
                self._interrompida()
            delta = str(evento.get("delta") or "")
            self._anotar("user", delta)
            self._ouviu(delta, agora)
        elif tipo == "session.output_transcript.delta":
            delta = str(evento.get("delta") or "")
            self._anotar("assistant", delta)
            self._fala_dela.append((agora, delta))
            self._ultima_atividade = self._ultima_fala_dela = agora
            self._cortada = False
            self._estado("falando")
        elif tipo == "session.delegation.created":
            ident = (evento.get("delegation") or {}).get("id")
            if isinstance(ident, str) and ident:
                self._vigiar(self._delegar(ident))
        elif tipo == "session.closed":
            uso = (evento.get("usage") or {}).get("seconds")
            if isinstance(uso, (int, float)):
                self._uso = float(uso)
            self._fechada.set()
            razao = evento.get("reason")
            if razao in QUEDAS_DA_SESSAO:
                self._queda_da_sessao(str(razao))
            elif self._motivo is None and razao not in (None, "close_requested"):
                self.parar(str(razao))
        elif tipo == "error":
            erro = evento.get("error") or {}
            log.info("a sessão ao vivo avisou um erro: %s", erro.get("code") or "sem código")

    def _interrompida(self) -> None:
        """A pessoa falou por cima dela. O servidor já a cala; aqui sai da fila o
        som dela que ainda não foi para o robô, e o robô joga fora o que tem."""
        self._cortada = True
        self._interrupcoes += 1
        if self._saida is not None:
            restantes = []
            while not self._saida.empty():
                item = self._saida.get_nowait()
                if item[0] != "audio":
                    restantes.append(item)
            for item in restantes:
                self._saida.put_nowait(item)
        self._ao_robo("json", {"t": "cortar"})

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
        if pedido in ("stop", "handoff_garra") and self._eco(pedido, agora, _ECO_S):
            pedido = None
        if pedido == "farewell":
            if self._despedida_em is None:
                self._despedida_em = agora + self._tolerancia_despedida
            self._comando = None
        else:
            self._comando = (pedido, agora) if pedido else None

    def _eco(self, nome: str, agora: float, janela_s: float) -> bool:
        """O comando que acabou de sair da boca dela, voltando pelo microfone."""
        recente = " ".join(t for (q, t) in self._fala_dela if agora - q <= janela_s)
        return comandos.eco(nome, recente)

    def _comando_local(self, nome: str, trecho: tuple[int, int] | None) -> None:
        """A sentinela ouviu: o trecho já é uma frase inteira, vale na hora."""
        if nome == "handoff_garra":
            if trecho is not None:
                self._trecho_garra = trecho
            self.parar("handoff_garra")
        elif nome == "stop":
            self.parar("stop")

    async def _delegar(self, ident: str) -> None:
        texto = self._dito.strip()
        self._dito = ""
        if not texto and self._delegacoes == 0 and self._pedido:
            texto = self._pedido
        par = self._par
        if par is None:
            return
        if not texto:
            for pedaco in pedacos(NAO_OUVI):
                par.enviar({"type": "session.commentary.append", "delegation_id": ident, "content": pedaco})
            return
        ver = self._descrever is not None and olhar.pede_visao(texto)
        self._pendentes += 1
        self._delegacoes += 1
        self._estado("pensando")
        par.enviar({"type": "session.thinking.append", "delegation_id": ident,
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
        if self._par is not par:
            # A sessão em que ela delegou caiu; a nova não conhece esta delegação.
            log.info("a resposta de uma delegação chegou depois da queda da sessão e ficou de fora")
            return
        for pedaco in pedacos(resposta):
            par.enviar({"type": "session.commentary.append", "delegation_id": ident, "content": pedaco})

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
        if self._robo_caiu_em is not None and agora - self._robo_caiu_em >= RETOMAR_S:
            return "robot_lost"
        if self._comando:
            # Um "pare" à espera de confirmação vence o silêncio: a frase só precisa
            # acabar ali para valer.
            nome, quando = self._comando
            if agora - max(quando, self._ultimo_delta) < CONFIRMAR_COMANDO_S:
                return None
            if (nome == "handoff_garra" and self._sentinela is not None
                    and self._trecho_garra is None and agora - quando < ESPERA_TRECHO_S):
                return None
            return nome
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
        """Abre, conduz e fecha. Devolve `{"motivo", "segundos", "delegacoes", "interrupcoes"}`."""
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
            await self._abrir_sessao(self._historico_inicial())
            inicio = time.monotonic()
            if self._transcrever is not None:
                self._sentinela = sentinela_mod.Sentinela(
                    self._transcrever, self._comando_local,
                    eco=lambda nome: self._eco(nome, time.monotonic(), _ECO_LOCAL_S))
                self._vigiar(self._sentinela.rodar())
            self._ao_robo("json", {"t": "aberta", "retomar": self.retomada})
            self._estado("ouvindo")
            # À parte do laço: o ping segue enquanto uma sessão nova abre.
            self._vigiar(self._pingar())
            while True:
                agora = time.monotonic()
                vencido = self._vencido(inicio, agora)
                if vencido:
                    motivo = vencido
                    break
                if self._reabrir is not None and not await self._reabrir_sessao():
                    motivo = "connection_lost"
                    break
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
        segundos = await self._encerrar()
        self._livro.fechar(self._sid, segundos=segundos, motivo=motivo,
                           delegacoes=self._delegacoes, agora=time.time())
        fim: dict[str, Any] = {"t": "fim", "motivo": motivo, "segundos": segundos}
        if motivo == "handoff_garra" and self._trecho_garra is not None:
            # Só o intervalo, no relógio do robô: ele guarda o som que mandou e
            # ouve de novo, ele mesmo, o que foi dito ao Garra.
            fim["trecho_ms"] = list(self._trecho_garra)
        self._ao_robo("json", fim)
        self._ao_robo("fim", None)
        await enviador
        self._terminou.set()
        log.info("conversa ao vivo encerrada: %s, %s s, %d delegações, %d interrupções",
                 motivo, segundos, self._delegacoes, self._interrupcoes)
        return {"motivo": motivo, "segundos": segundos, "delegacoes": self._delegacoes,
                "interrupcoes": self._interrupcoes}

    async def _pingar(self) -> None:
        while True:
            self._ao_robo("json", {"t": "ping"})
            await asyncio.sleep(1.0)

    async def _encerrar(self) -> float:
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
        total = self._segundos_anteriores + self._segundos_da_sessao()
        return int(total) if total == int(total) else round(total, 1)


# ─── a rota ──────────────────────────────────────────────────────────────────
ESPERA_APERTO_S = 5.0
ROBO_MUDO_S = 5.0
# Depois do `fim`, quem fecha é o robô, depois de lê-lo. Fechando primeiro, o envio
# de microfone que estava a caminho falhava lá, e o robô tomava o fim por queda.
ESPERA_DESPEDIDA_S = 2.0


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
              descrever: Callable[[bytes, str, Any], Any] | None = None,
              transcrever: Callable[[Any], tuple[str, list[float] | None]] | None = None) -> None:
    """Põe `/api/hera/ao-vivo` no app da ponte do robô. `descrever` é o olho da
    ponte (`ponte._descrever_para_hera`); sem ele, a Hera não olha. `transcrever`
    é o Whisper desta máquina, para a sentinela (`sentinela.transcrever_local`)."""
    ponte = ponte or PonteHera()
    uma_por_vez = asyncio.Lock()
    ativas: dict[str, Conversa] = {}      # senha de retomada → conversa viva
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
            if aperto.retomar is not None:
                return await _retomar(ws, ativas.get(aperto.retomar), aperto.retomar)
            if uma_por_vez.locked():
                return await _recusar(ws, "busy")
            async with uma_por_vez:
                livro = livro_mod.Livro()
                pode, motivo = livro.pode_abrir(atual["limits"], agora=time.time())
                if not pode:
                    return await _recusar(ws, motivo)
                await ws.send_text(json.dumps({"t": "aceito"}))
                robo = _RoboWS(ws)
                conversa = Conversa(robo, ponte=ponte, livro=livro,
                                    limites={**atual["limits"], **(limites_de_teste or {})},
                                    dispositivo=atual["device"]["id"], nivel=atual["level"],
                                    como=aperto.como, pedido=aperto.pedido, abrir_par=abrir_par,
                                    descrever=descrever, transcrever=transcrever)
                ativas[conversa.retomada] = conversa
                try:
                    conduzindo = asyncio.ensure_future(conversa.conduzir())
                    ouvindo = asyncio.ensure_future(_escutar_robo(ws, conversa, robo))
                    await conduzindo
                    await _despedir(ouvindo)
                finally:
                    ativas.pop(conversa.retomada, None)
            await ws.close()
        except (WebSocketDisconnect, asyncio.TimeoutError):
            return
        except RuntimeError:
            # o robô fechou no meio do envio
            return

    async def _retomar(ws: WebSocket, conversa: Conversa | None, senha: str) -> None:
        """O robô caiu e voltou, com prova e com a senha da conversa: segue nela,
        sem abrir sessão nova nem passar pelos tetos de abertura."""
        if conversa is None or not conversa.pode_retomar(senha):
            return await _recusar(ws, "not_resumable")
        await ws.send_text(json.dumps({"t": "aceito"}))
        robo = _RoboWS(ws)
        conversa.reatar(robo)
        ouvindo = asyncio.ensure_future(_escutar_robo(ws, conversa, robo))
        await conversa.terminou()
        await _despedir(ouvindo)
        await ws.close()

    async def _despedir(ouvindo: asyncio.Task) -> None:
        """O `fim` já foi; espera o robô fechar do lado dele (o ouvinte acaba)."""
        try:
            await asyncio.wait_for(asyncio.shield(ouvindo), ESPERA_DESPEDIDA_S)
        except asyncio.TimeoutError:
            pass
        ouvindo.cancel()

    async def _escutar_robo(ws: WebSocket, conversa: Conversa, robo: _RoboWS) -> None:
        while True:
            try:
                mensagem = await asyncio.wait_for(ws.receive(), ROBO_MUDO_S)
            except asyncio.TimeoutError:
                conversa.robo_caiu(robo)
                return
            if mensagem.get("type") == "websocket.disconnect":
                conversa.robo_caiu(robo)
                return
            if mensagem.get("bytes"):
                lido = ler_quadro(mensagem["bytes"])
                if lido and lido[0] == MICROFONE:
                    conversa.microfone(lido[3], lido[2])
            elif mensagem.get("text"):
                try:
                    pedido = json.loads(mensagem["text"])
                except ValueError:
                    continue
                if isinstance(pedido, dict) and pedido.get("t") == "parar":
                    conversa.parar("robot_stop")
                elif isinstance(pedido, dict) and pedido.get("t") == "imagem":
                    conversa.imagem(pedido)
