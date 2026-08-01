"""Cérebros do robô: gateway do Garra (completo) com reservas stateless.

Ordem por turno: gateway (Garra completo — memória, ferramentas, delegação a
agentes) → garra ask (binário local, LLM puro) → OpenRouter HTTP (LLM puro) →
mensagem honesta de indisponibilidade. Os modos reserva nunca prometem
executar tarefas (PERSONA_BASICA) e mantêm memória client-side reenviando os
últimos turnos.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import requests

from . import armazenamento
from .config import BREVIDADE, PERSONA_BASICA, Config, chave_real
from .eventos import EventoCerebro

FALHA_GENERICA = "Desculpe, me embananei aqui. Pode repetir?"
FALHA_TIMEOUT = "Desculpe, demorei demais para pensar nessa."
FALHA_PROVEDOR = (
    "Estou com problema para falar com o provedor de inteligência. "
    "Verifique a chave de API nos meus registros."
)
AVISO_MODO_BASICO = (
    "Aviso: meu modo completo está fora do ar, então estou no chat básico, "
    "sem executar tarefas. "
)
AVISO_MODO_COMPLETO = "Meu modo completo voltou. "
AVISO_SEM_CEREBRO = (
    "Não consegui falar com nenhum cérebro agora: nem o gateway do Garra, nem "
    "um modo reserva. Verifique se o garra start está rodando."
)


@dataclass
class RespostaCerebro:
    ok: bool
    texto: str                 # sempre falável (resposta ou aviso amigável)
    modo: str                  # "gateway" | "ask" | "openrouter" | "nenhum"
    tipo: str = "final"        # "final" | "task_accepted" (futuro, ver ESPEC) | "erro"
    task_id: str | None = None
    erro: str | None = None    # "timeout" | "provider" | "parse" | "gateway_off" | ...
    marca: int | None = None   # marca d'água a confirmar DEPOIS de falar


class GatewayIndisponivel(Exception):
    """O gateway não respondeu — tentar um modo reserva."""


def _prompt_com_historia(texto: str, historia) -> str:
    if not historia:
        return texto + BREVIDADE
    # Quem fala é `GARRA_USUARIO`, ou simplesmente "Usuário". Fixar um nome
    # próprio aqui faria o app instalado da loja chamar todo mundo pelo nome
    # do autor — e, pior, dizê-lo em voz alta.
    eu = (os.environ.get("GARRA_USUARIO") or "").strip() or "Usuário"
    linhas = ["Conversa até agora:"]
    for usuario, robo in historia:
        linhas.append(f"{eu}: {usuario[:500]}")
        linhas.append(f"GarraIA: {robo[:500]}")
    linhas.append(f"{eu}: {texto}")
    return "\n".join(linhas) + BREVIDADE


def descobrir_binario(cfg: Config) -> str | None:
    """Acha o executável do Garra (`garra`, ou `garraia` como o installer nomeia)."""
    candidatos: list[Path] = []
    if cfg.garra_bin:
        candidatos.append(Path(cfg.garra_bin).expanduser())
    raiz_app = Path(__file__).resolve().parent.parent
    for pasta in (raiz_app / "bin", armazenamento.diretorio() / "bin",
                  Path("~/.local/bin").expanduser()):
        candidatos += [pasta / "garra", pasta / "garraia"]
    for nome in ("garra", "garraia"):
        achado = shutil.which(nome)
        if achado:
            candidatos.append(Path(achado))
    for c in candidatos:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


class GatewayBrain:
    """Sessões reais do gateway do Garra (POST /api/sessions + .../messages).

    O /ping sozinho não garante nada: o handshake só é considerado bom depois
    de uma sessão utilizável (201 na criação, ou history 200 ao retomar).
    O agent_id vai em CADA mensagem — o gateway resolve o agente por mensagem,
    não pela sessão.

    Duas marcas d'água sobre o histórico, de propósito:
      cursor        — até onde já foi BUSCADO (só em memória, dedup do poll);
      cursor_falado — até onde o robô já FALOU (persistido em estado.json).
    O loop principal confirma a fala via confirmar_falado(), então uma
    notificação buscada mas não falada volta a ser enfileirada no próximo
    início em vez de se perder em silêncio.
    """

    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.log = logger
        self.http = requests.Session()
        if cfg.gateway_key:
            # Inofensivo sem lockdown; obrigatório com GARRAIA_LOCK_LEGACY.
            self.http.headers["Authorization"] = f"Bearer {cfg.gateway_key}"
        self.session_id: str | None = None
        self.cursor = 0          # marca de busca (dedup provisório por posição)
        self.cursor_falado = 0   # marca de fala (persistida)
        self._ultima_resposta: str | None = None  # dedup por conteúdo do turno síncrono
        # Reentrante: perguntar() segura a trava e pode chamar _criar_sessao()
        # e _sincronizar_cursor(), que também a tomam.
        self._trava = threading.RLock()

    def _url(self, caminho: str) -> str:
        return f"{self.cfg.gateway_url}{caminho}"

    def disponivel(self) -> bool:
        try:
            return self.http.get(self._url("/ping"), timeout=2).ok
        except requests.RequestException:
            return False

    def conectar(self) -> bool:
        if not self.disponivel():
            return False
        with self._trava:
            if self.session_id is not None:
                return True
            estado = armazenamento.carregar_estado()
            salvo = estado.get("session_id")
            if salvo and self._retomar(salvo, self._cursor_salvo(estado)):
                return True
            return self._criar_sessao()

    @staticmethod
    def _cursor_salvo(estado: dict) -> int:
        # "cursor" é o nome legado, de estado.json gravado por versões antigas.
        for chave in ("cursor_falado", "cursor"):
            try:
                return max(0, int(estado[chave]))
            except (KeyError, TypeError, ValueError):
                continue
        return 0

    def _retomar(self, session_id: str, cursor: int) -> bool:
        try:
            r = self.http.get(self._url(f"/api/sessions/{session_id}/history"), timeout=5)
            if r.status_code != 200:
                return False
            total = len(r.json().get("messages", []))
        except requests.RequestException:
            return False
        except (ValueError, AttributeError, TypeError):
            self.log.error("Histórico ilegível ao retomar a sessão %s: %r",
                           session_id, r.text[:200])
            return False
        self.session_id = session_id
        # Mensagens além do cursor FALADO viram notificações (chegaram com o app
        # desligado, ou foram buscadas e não faladas antes do encerramento);
        # um cursor maior que o histórico atual é realinhado.
        self.cursor = self.cursor_falado = min(cursor, total)
        self._salvar_estado()
        self.log.info("Sessão do gateway retomada: %s (histórico %d, cursor %d)",
                      session_id, total, self.cursor_falado)
        return True

    def _criar_sessao(self) -> bool:
        try:
            r = self.http.post(self._url("/api/sessions"),
                               json={"agent_id": self.cfg.agent_id}, timeout=10)
        except requests.RequestException:
            return False
        if r.status_code != 201:
            self.log.error("Gateway recusou criar sessão: HTTP %s %s",
                           r.status_code, r.text[:200])
            return False
        try:
            self.session_id = r.json().get("session_id")
        except (ValueError, AttributeError, TypeError):
            self.log.error("Gateway devolveu corpo ilegível ao criar sessão: %r",
                           r.text[:200])
            return False
        self.cursor = self.cursor_falado = 0
        self._ultima_resposta = None
        self._salvar_estado()
        self.log.info("Sessão nova no gateway: %s", self.session_id)
        return bool(self.session_id)

    def nova_sessao(self) -> str | None:
        """Abandona a sessão atual e abre uma limpa. Devolve o id novo.

        A retomada de sessão entre arranques é o comportamento certo para uso
        normal — conversar com o robô não devia recomeçar do zero toda vez que
        o app reinicia. Mas ela não tem escape, e um teste precisa de escape:
        um turno anterior em que o modelo olhou pela câmera fica no histórico e
        contamina a medição seguinte.

        Não apaga nada do lado do gateway. O histórico antigo continua lá para
        quem quiser lê-lo; o que muda é para onde este app passa a escrever.
        (`DELETE /api/sessions/{id}` do gateway é logout, não remoção: ele
        revoga tokens e desconecta, e o histórico permanece.)
        """
        with self._trava:
            anterior = self.session_id
            self.session_id = None
            self.cursor = self.cursor_falado = 0
            self._ultima_resposta = None
            if not self._criar_sessao():
                # Não deixa o app sem sessão: sem gateway, a antiga ainda serve.
                self.session_id = anterior
                self._salvar_estado()
                return None
            self.log.info("Sessão trocada: %s → %s", anterior, self.session_id)
            return self.session_id

    def _salvar_estado(self) -> None:
        armazenamento.salvar_estado({"session_id": self.session_id,
                                     "cursor_falado": self.cursor_falado})

    def confirmar_falado(self, marca: int) -> None:
        """Chamado pelo loop principal DEPOIS de falar — só então persiste.

        O pior caso é refalar uma notificação se o app cair entre a fala e a
        persistência; o contrário (perder a mensagem em silêncio) é pior.
        """
        with self._trava:
            if marca <= self.cursor_falado:
                return
            self.cursor_falado = marca
            self.cursor = max(self.cursor, marca)
            self._salvar_estado()

    def perguntar(self, texto: str) -> RespostaCerebro:
        if self.session_id is None and not self.conectar():
            raise GatewayIndisponivel
        corpo = {"content": texto, "agent_id": self.cfg.agent_id}
        if self.cfg.gateway_model:
            corpo["model"] = self.cfg.gateway_model
        # Turno inteiro sob trava: se o poller ler o histórico entre o POST e a
        # atualização do cursor, ele enfileira a resposta síncrona e o robô a
        # repete em voz alta no próximo silêncio.
        with self._trava:
            r = None
            for tentativa in (1, 2):
                try:
                    r = self.http.post(
                        self._url(f"/api/sessions/{self.session_id}/messages"),
                        json=corpo, timeout=self.cfg.timeout_gateway_s,
                    )
                except requests.Timeout:
                    return RespostaCerebro(False, FALHA_TIMEOUT, "gateway", "erro",
                                           erro="timeout")
                except requests.RequestException as e:
                    raise GatewayIndisponivel from e
                if r.status_code == 404 and tentativa == 1:
                    # Sessão morreu (gateway reiniciado) — recria uma vez e repete.
                    if not self._criar_sessao():
                        raise GatewayIndisponivel
                    continue
                break
            if r is None or r.status_code != 200:
                codigo = r.status_code if r is not None else "?"
                self.log.error("Gateway erro no turno: HTTP %s %s", codigo,
                               r.text[:300] if r is not None else "")
                return RespostaCerebro(False, FALHA_GENERICA, "gateway", "erro",
                                       erro=f"http_{codigo}")
            try:
                conteudo = (r.json().get("content") or "").strip()
            except (ValueError, AttributeError, TypeError):
                self.log.error("Gateway devolveu corpo ilegível no turno: %r",
                               r.text[:300])
                return RespostaCerebro(False, FALHA_GENERICA, "gateway", "erro",
                                       erro="parse")
            marca = self._sincronizar_cursor()
            if not conteudo:
                return RespostaCerebro(False, FALHA_GENERICA, "gateway", "erro",
                                       erro="vazio", marca=marca)
            self._ultima_resposta = conteudo
            return RespostaCerebro(True, conteudo, "gateway", marca=marca)

    def _sincronizar_cursor(self) -> int:
        """Marca o histórico atual como BUSCADO e devolve a marca d'água.

        Quem persiste é o loop principal, via confirmar_falado(), depois de
        realmente falar a resposta.
        """
        with self._trava:
            try:
                r = self.http.get(
                    self._url(f"/api/sessions/{self.session_id}/history"), timeout=5)
                if r.status_code == 200:
                    self.cursor = max(self.cursor, len(r.json().get("messages", [])))
                    return self.cursor
                self.log.warning("Histórico HTTP %s ao sincronizar o cursor",
                                 r.status_code)
            except requests.RequestException as e:
                self.log.warning("Não consegui sincronizar o cursor: %s", e)
            except (ValueError, AttributeError, TypeError):
                self.log.warning("Histórico ilegível ao sincronizar o cursor")
            # Falhou: avança otimista pelas duas mensagens deste turno (pergunta
            # + resposta) para o poller não refalar o que acabamos de dizer. Um
            # excesso é realinhado pelo ramo "histórico encolheu" abaixo.
            self.cursor += 2
            return self.cursor

    def novas_mensagens(self) -> list[EventoCerebro]:
        """Mensagens do agente além do cursor (resultados de tarefas delegadas).

        Dedup provisório por posição — o gateway ainda não expõe id/seq por
        mensagem nem uma API de tarefas assíncronas (ver ESPEC_GATEWAY_TAREFAS.md).
        Hoje /messages é síncrono; este poll cobre mensagens gravadas na sessão
        por outros caminhos e fica pronto para a API de tarefas.
        """
        if self.session_id is None:
            return []
        # Sem fila de espera atrás de um turno em voo (o POST pode levar minutos).
        if not self._trava.acquire(timeout=0.5):
            return []
        try:
            try:
                r = self.http.get(
                    self._url(f"/api/sessions/{self.session_id}/history"), timeout=4)
                if r.status_code != 200:
                    return []
                mensagens = r.json().get("messages", [])
            except requests.RequestException:
                return []
            except (ValueError, AttributeError, TypeError):
                self.log.warning("Histórico ilegível no poll de notificações")
                return []
            if len(mensagens) < self.cursor:
                # Histórico encolheu (reinício/truncamento) — realinha sem refalar.
                self.cursor = len(mensagens)
                if self.cursor_falado > self.cursor:
                    self.cursor_falado = self.cursor
                    self._salvar_estado()
                return []
            novas = mensagens[self.cursor:]
            if not novas:
                return []
            base = self.cursor
            self.cursor = len(mensagens)   # marca de BUSCA: não persiste aqui
            ultima = self._ultima_resposta
            self._ultima_resposta = None
        finally:
            self._trava.release()
        eventos = []
        for i, m in enumerate(novas):
            if not isinstance(m, dict):
                continue
            texto = (m.get("content") or "").strip()
            if m.get("role") != "assistant" or not texto:
                continue
            if ultima is not None and texto == ultima:
                # Cinto e suspensório: é a resposta síncrona que o robô já falou.
                ultima = None
                continue
            eventos.append(EventoCerebro("notificacao", texto, seq=base + i))
        return eventos


class AskBrain:
    """Reserva 1: binário `garra ask --json` (stateless, LLM puro, sem ferramentas)."""

    def __init__(self, binario: str, cfg: Config, logger: logging.Logger) -> None:
        self.binario = binario
        self.cfg = cfg
        self.log = logger

    def perguntar(self, texto: str, historia) -> RespostaCerebro:
        cmd = [self.binario, "ask", "--json",
               "-p", self.cfg.provider, "-m", self.cfg.model,
               "--timeout-secs", str(self.cfg.timeout_ask_s),
               "--system-prompt", PERSONA_BASICA,
               # "--": a transcrição do Whisper pode começar com hífen ("- oi"),
               # e o clap trataria isso como flag desconhecida.
               "--", _prompt_com_historia(texto, historia)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.cfg.timeout_ask_s + 15)
        except subprocess.TimeoutExpired:
            return RespostaCerebro(False, FALHA_TIMEOUT, "ask", "erro", erro="timeout")
        except OSError as e:
            self.log.error("Não consegui executar %s: %s", self.binario, e)
            return RespostaCerebro(False, FALHA_GENERICA, "ask", "erro", erro="exec")
        if proc.returncode == 124:
            return RespostaCerebro(False, FALHA_TIMEOUT, "ask", "erro", erro="timeout")
        if proc.returncode == 69:
            self.log.error("garra ask: erro de provedor: %s",
                           (proc.stderr or proc.stdout)[:300])
            return RespostaCerebro(False, FALHA_PROVEDOR, "ask", "erro", erro="provider")
        if proc.returncode != 0:
            self.log.error("garra ask saiu com código %d: %s", proc.returncode,
                           (proc.stderr or proc.stdout)[:300])
            return RespostaCerebro(False, FALHA_GENERICA, "ask", "erro",
                                   erro=f"exit_{proc.returncode}")
        linhas = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        try:
            envelope = json.loads(linhas[-1]) if linhas else None
        except ValueError:
            envelope = None
        if not isinstance(envelope, dict) or envelope.get("schema") != "garra.ask.v1":
            self.log.error("garra ask: envelope inesperado: %r", (proc.stdout or "")[:300])
            return RespostaCerebro(False, FALHA_GENERICA, "ask", "erro", erro="parse")
        if not envelope.get("ok"):
            kind = (envelope.get("error") or {}).get("kind", "provider")
            self.log.error("garra ask falhou: %s", envelope.get("error"))
            texto_falha = FALHA_TIMEOUT if kind == "timeout" else FALHA_PROVEDOR
            return RespostaCerebro(False, texto_falha, "ask", "erro", erro=kind)
        resposta = (envelope.get("answer") or "").strip()
        if not resposta:
            return RespostaCerebro(False, FALHA_GENERICA, "ask", "erro", erro="vazio")
        return RespostaCerebro(True, resposta, "ask")


class OpenRouterBrain:
    """Reserva 2: OpenRouter por HTTP puro — portátil (funciona sem o binário
    Rust, inclusive no Pi do Reachy wireless, onde não há garra aarch64)."""

    def __init__(self, api_key: str, cfg: Config, logger: logging.Logger) -> None:
        self.api_key = api_key
        self.cfg = cfg
        self.log = logger

    def perguntar(self, texto: str, historia) -> RespostaCerebro:
        mensagens = [{"role": "system", "content": PERSONA_BASICA}]
        for usuario, robo in historia:
            mensagens.append({"role": "user", "content": usuario[:500]})
            mensagens.append({"role": "assistant", "content": robo[:500]})
        mensagens.append({"role": "user", "content": texto + BREVIDADE})
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.cfg.model, "messages": mensagens, "max_tokens": 400},
                timeout=self.cfg.timeout_ask_s,
            )
        except requests.Timeout:
            return RespostaCerebro(False, FALHA_TIMEOUT, "openrouter", "erro",
                                   erro="timeout")
        except requests.RequestException as e:
            self.log.error("OpenRouter inacessível: %s", e)
            return RespostaCerebro(False, FALHA_PROVEDOR, "openrouter", "erro",
                                   erro="provider")
        if r.status_code != 200:
            self.log.error("OpenRouter HTTP %s: %s", r.status_code, r.text[:300])
            return RespostaCerebro(False, FALHA_PROVEDOR, "openrouter", "erro",
                                   erro="provider")
        try:
            resposta = (r.json()["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, ValueError, TypeError):
            self.log.error("OpenRouter: resposta inesperada: %r", r.text[:300])
            return RespostaCerebro(False, FALHA_GENERICA, "openrouter", "erro",
                                   erro="parse")
        if not resposta:
            return RespostaCerebro(False, FALHA_GENERICA, "openrouter", "erro",
                                   erro="vazio")
        return RespostaCerebro(True, resposta, "openrouter")


def sondar(cfg: Config, timeout: float = 2.0) -> tuple[bool, str, str]:
    """Estado do cérebro sem construir nada — `(disponível, código, descrição)`.

    Existe porque `Cerebro(...)` + `iniciar()` fazem bem mais do que olhar: o
    `conectar()` retoma ou CRIA uma sessão no gateway, gastando até 12 s de
    chamadas bloqueantes. Isso é certo uma vez, ao entrar no loop de voz; usar
    o mesmo caminho como health check periódico criava sessão a cada 20 s e
    segurava o encerramento do app — o SIGINT chegava no meio de um `requests`
    e o daemon acabava matando o processo no limite de 20 s.
    """
    ok = False
    try:
        # COM a credencial. Sem ela a ponte responde 401 e o app conclui que o
        # gateway sumiu — foi o que deixou o robô em modo limitado com tudo
        # funcionando por trás. A ponte também responde /ping sem token, mas
        # depender disso amarraria a sonda a um detalhe do outro lado.
        cabecalhos = ({"Authorization": f"Bearer {cfg.gateway_key}"}
                      if cfg.gateway_key else {})
        ok = requests.get(f"{cfg.gateway_url.rstrip('/')}/ping",
                          headers=cabecalhos, timeout=timeout).ok
    except requests.RequestException:
        pass
    if ok:
        return True, "gateway", f"Garra gateway ({cfg.gateway_url})"
    if descobrir_binario(cfg):
        return True, "garra_bin", "garra ask (local binary)"
    if chave_real(os.environ.get("OPENROUTER_API_KEY")):
        return True, "openrouter", f"OpenRouter ({cfg.model})"
    return False, "none", "no brain configured"


class Cerebro:
    """Orquestra os cérebros e os avisos únicos de queda/retorno do modo completo."""

    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.log = logger
        self.gateway = GatewayBrain(cfg, logger)
        self.historia: deque = deque(maxlen=cfg.janela_turnos)
        self.reserva = None
        binario = descobrir_binario(cfg)
        chave_or = chave_real(os.environ.get("OPENROUTER_API_KEY"))
        if binario:
            self.reserva = AskBrain(binario, cfg, logger)
            self.log.info("Modo reserva disponível: garra ask (%s)", binario)
        elif chave_or:
            self.reserva = OpenRouterBrain(chave_or, cfg, logger)
            self.log.info("Modo reserva disponível: OpenRouter HTTP")
        else:
            self.log.warning("Nenhum modo reserva disponível (sem binário garra "
                             "e sem OPENROUTER_API_KEY).")
        self.modo = "gateway"
        self._gateway_ok = False
        self._avisou_basico = False

    def iniciar(self) -> None:
        self._gateway_ok = self.gateway.conectar()
        if self._gateway_ok:
            self.modo = "gateway"
            self.log.info("Cérebro: gateway do Garra em %s (agente %s)",
                          self.cfg.gateway_url, self.cfg.agent_id)
        else:
            self.modo = "reserva"
            self.log.warning("Gateway do Garra indisponível em %s; começando no "
                             "modo reserva.", self.cfg.gateway_url)

    @property
    def disponivel(self) -> bool:
        """Há algum cérebro capaz de responder?

        Instalado da loja, sem gateway do Garra e sem chave de API, a resposta é
        não — e o app precisa dizer isso uma vez, não repetir "não consegui
        falar com nenhum cérebro" a cada frase que alguém disser ao robô.
        """
        return self._gateway_ok or self.reserva is not None

    def descrever(self) -> tuple[str, str]:
        """(código, descrição legível) do cérebro em uso. Sem segredo nenhum."""
        if self._gateway_ok:
            return "gateway", f"Garra gateway ({self.cfg.gateway_url})"
        if isinstance(self.reserva, AskBrain):
            return "garra_bin", "garra ask (local binary)"
        if isinstance(self.reserva, OpenRouterBrain):
            return "openrouter", f"OpenRouter ({self.cfg.model})"
        return "none", "no brain configured"

    def perguntar(self, texto: str) -> RespostaCerebro:
        prefixo = ""
        if self.modo != "gateway" and self.gateway.conectar():
            self.modo = "gateway"
            self._gateway_ok = True
            self._avisou_basico = False
            prefixo = AVISO_MODO_COMPLETO
        if self.modo == "gateway":
            try:
                resposta = self.gateway.perguntar(texto)
                if prefixo and resposta.ok:
                    resposta.texto = prefixo + resposta.texto
                return resposta
            except GatewayIndisponivel:
                self.log.warning("Gateway caiu no meio do turno; usando modo reserva.")
                self.modo = "reserva"
                self._gateway_ok = False
        if self.reserva is None:
            return RespostaCerebro(False, AVISO_SEM_CEREBRO, "nenhum", "erro",
                                   erro="gateway_off")
        resposta = self.reserva.perguntar(texto, list(self.historia))
        if resposta.ok:
            self.historia.append((texto, resposta.texto))
            if not self._avisou_basico:
                self._avisou_basico = True
                resposta.texto = AVISO_MODO_BASICO + resposta.texto
        return resposta

    def novas_mensagens(self) -> list[EventoCerebro]:
        if self.modo != "gateway":
            return []
        return self.gateway.novas_mensagens()

    def confirmar_falado(self, marca: int | None) -> None:
        """O loop principal chama isto DEPOIS de falar (ver GatewayBrain)."""
        if marca is not None:
            self.gateway.confirmar_falado(marca)

    def nova_sessao(self) -> str | None:
        """Recomeça a conversa. Só o gateway tem sessão para trocar.

        Nas reservas (`garra ask`, OpenRouter) o histórico é a lista local
        `self.historia`, e limpá-la é o equivalente honesto: nenhum turno
        anterior atravessa para o próximo. Aí devolve `""` — recomeçou, mas não
        há id de sessão para citar. `None` fica reservado para *falhou*, que é
        outra coisa e merece outra resposta HTTP.
        """
        self.historia.clear()
        if self.modo != "gateway":
            return ""
        return self.gateway.nova_sessao()
