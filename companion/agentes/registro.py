"""Quem pode atender o robô, e quem atende agora.

O registro junta três fontes:

* **descoberta automática** — Hermes, Garra, Pi, Claude e o Ollama, cada um
  procurado onde costuma estar (executável, porta);
* **agentes do dono** — receitas de linha de comando e servidores HTTP gravados
  em `agentes.json`;
* **a escolha** — qual deles é o agente ativo, as opções de cada um e o
  agente reserva.

Tudo em `~/.config/garra-reachy/agentes.json`, 0600, gravado de forma atômica.

**O Hermes é o padrão** de uma instalação nova. Uma escolha feita pelo dono é
`explicit` e nunca é desfeita por atualização; sem escolha e sem Hermes na
máquina, vale o primeiro disponível da ordem de preferência — para que o robô
de quem só tem o Garra continue conversando sem ninguém abrir uma tela.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import httpx

from . import comando, http_openai, persona
from .base import LIVRE, Adaptador, Conversa, ErroDoAgente, Saude, achar_executavel
from .garra import Garra
from .hermes import Hermes
from .pi import Pi

log = logging.getLogger("companion.agentes")

PADRAO = "hermes"
# Quem assume quando não há escolha do dono e o padrão não está na máquina.
PREFERENCIA = ("hermes", "garra", "pi", "claude", "ollama")
SAUDE_VALE_S = 10.0            # quanto dura uma saúde em cache
PERSONALIZADOS_MAX = 12
# De quanto em quanto se procura de novo um agente ESCOLHIDO que não foi achado.
PROCURAR_AUSENTE_S = 60.0
FRASE_DE_TESTE = "Responda apenas com a palavra: pronto"
OLLAMA = "http://127.0.0.1:11434"
# Teto do modelo local escolhido sozinho: a GPU é dividida com a voz.
OLLAMA_TETO_BYTES = 12 * 1024 ** 3


def arquivo_padrao() -> Path:
    pasta = Path(os.environ.get("GARRA_REACHY_CONF", "~/.config/garra-reachy")).expanduser()
    return pasta / "agentes.json"


def _ollama() -> http_openai.HttpOpenAI | None:
    """O Ollama desta máquina, com o maior modelo de conversa que cabe na GPU."""
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=1.0)
        modelos = r.json().get("models") if r.is_success else None
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(modelos, list):
        return None
    conversam = [m for m in modelos if isinstance(m, dict) and m.get("name")
                 and not any(p in str(m["name"]).lower() for p in http_openai._NAO_CONVERSA)
                 and int(m.get("size") or 0) <= OLLAMA_TETO_BYTES]
    if not conversam:
        return None
    maior = max(conversam, key=lambda m: int(m.get("size") or 0))
    return http_openai.HttpOpenAI(
        {"id": "ollama", "name": "Ollama (modelo local)", "base_url": OLLAMA,
         "model": str(maior["name"]), "timeout_s": 120}, descoberto=True)


class Ausente(Adaptador):
    """O agente que o dono escolheu e que não foi encontrado.

    Existe para a escolha não se desfazer sozinha: se o Ollama escolhido estava
    desligado quando o companion subiu, o robô tem de dizer "o Ollama não
    responde" — e não passar a mandar a conversa da casa, em silêncio, a um
    agente de nuvem. O registro volta a procurá-lo de tempos em tempos.
    """

    interface = "unknown"

    def __init__(self, ident: str) -> None:
        self.id = ident
        self.nome = ident.capitalize()

    def saude(self) -> Saude:
        return Saude(False, "not_installed", "o agente escolhido não foi encontrado")

    def responder(self, conversa, texto, *, instrucoes, ao_trecho=None) -> str:
        raise ErroDoAgente("unavailable", "o agente escolhido não foi encontrado")


class Registro:
    def __init__(self, arquivo: Path | None = None, *, chave_gateway: str | None = None,
                 modelo_do_garra: Callable[[], str | None] | None = None,
                 identidade: Callable[[], dict[str, Any]] | None = None,
                 descobrir: Callable[[], list[Adaptador]] | None = None) -> None:
        self.arquivo = arquivo or arquivo_padrao()
        self._chave_gateway = chave_gateway
        self._modelo_do_garra = modelo_do_garra
        # Nome do robô, de quem ele é e o jeito de falar. Do dono, não do agente.
        self._identidade = identidade or (lambda: {})
        self._descobrir = descobrir or self._descobrir_instalados
        self._trava = threading.RLock()
        self._agentes: dict[str, Adaptador] = {}
        self._saude: dict[str, tuple[float, Saude]] = {}
        self._estado: dict[str, Any] = {}
        # Chamado quando um agente muda de um jeito que invalida as conversas
        # abertas com ele (opções, remoção). A ponte liga isto às sessões.
        self.ao_mudar: Callable[[str], None] = lambda _id: None
        self._visto: float | None = None
        self._ilegivel = False
        self._procurado_em = 0.0
        self.recarregar()

    def _carimbo(self) -> float | None:
        try:
            return self.arquivo.stat().st_mtime
        except OSError:
            return None

    def sincronizar(self) -> None:
        """Relê `agentes.json` se ele mudou por fora deste processo.

        É o que faz valer, sem reiniciar o companion, o que o dono muda pela
        linha de comando (`python -m companion.agentes`) ou editando o arquivo.
        """
        escolhido = self._agentes.get(self._estado.get("active") or "")
        procurar = (isinstance(escolhido, Ausente)
                    and time.monotonic() - self._procurado_em > PROCURAR_AUSENTE_S)
        if self._carimbo() != self._visto or procurar:
            antes = set(self._agentes)
            self.recarregar()
            for ident in antes | set(self._agentes):
                self.ao_mudar(ident)

    # ── descoberta e carga ───────────────────────────────────────────────────
    def _descobrir_instalados(self) -> list[Adaptador]:
        achados: list[Adaptador] = [
            Hermes(), Garra(self._chave_gateway, modelo=self._modelo_do_garra), Pi()]
        if achar_executavel("claude", ("~/.local/bin/claude",)):
            achados.append(comando.Comando(comando.CLAUDE, descoberto=True))
        ollama = _ollama()
        if ollama is not None:
            achados.append(ollama)
        return achados

    def _ler_arquivo(self) -> dict[str, Any]:
        self._ilegivel = False
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("agentes.json ilegível — usando os padrões; o arquivo fica "
                        "como está, e uma cópia é guardada antes de qualquer gravação")
            self._ilegivel = True
            return {}
        if not isinstance(dados, dict):
            self._ilegivel = True
            return {}
        return dados

    def recarregar(self) -> None:
        """Procura de novo os agentes e relê `agentes.json`."""
        with self._trava:
            estado = self._ler_arquivo()
            agentes = {a.id: a for a in self._descobrir()}
            proprios = []
            for definicao in (estado.get("custom") or [])[:PERSONALIZADOS_MAX]:
                da_rede = isinstance(definicao, dict) and definicao.get("origin") == "network"
                try:
                    if isinstance(definicao, dict) and definicao.get("kind") == "http":
                        # O que entrou pela rede segue valendo as regras da rede
                        # a cada carga (nada de loopback, nada de credencial).
                        novo: Adaptador = http_openai.HttpOpenAI(definicao, local=not da_rede)
                    elif da_rede:
                        raise ErroDoAgente("refused", "receita de comando marcada como da rede")
                    else:
                        novo = comando.Comando(definicao)
                except ErroDoAgente as e:
                    log.warning("agente do dono ignorado (%s)", e.detalhe)
                    continue
                if novo.id in agentes and agentes[novo.id].descoberto:
                    log.warning("agente do dono `%s` tem o id de um descoberto — ignorado", novo.id)
                    continue
                agentes[novo.id] = novo
                # De onde veio: o que o dono criou neste computador, a rede não
                # troca nem apaga. Sem a marca (arquivo escrito à mão), é do dono.
                origem = "network" if definicao.get("origin") == "network" else "local"
                proprios.append({**definicao, "origin": origem})
            opcoes = estado.get("options") if isinstance(estado.get("options"), dict) else {}
            for ident, valores in opcoes.items():
                if ident in agentes and isinstance(valores, dict):
                    try:
                        # O arquivo é do dono e está neste computador: vale tudo.
                        agentes[ident].configurar(valores, local=True)
                    except ErroDoAgente as e:
                        log.warning("opções de `%s` ignoradas (%s)", ident, e.detalhe)
            ativo = str(estado.get("active") or "")
            if estado.get("explicit") and ativo and ativo not in agentes:
                # Escolhido e não encontrado: fica como AUSENTE, não é trocado.
                agentes[ativo] = Ausente(ativo)
                self._procurado_em = time.monotonic()
            self._agentes = agentes
            self._saude = {}
            self._estado = {
                "active": ativo,
                "explicit": bool(estado.get("explicit")),
                "fallback": str(estado.get("fallback") or ""),
                "options": {i: dict(v) for i, v in opcoes.items() if isinstance(v, dict)},
                "custom": proprios,
            }
            self._visto = self._carimbo()

    def _gravar(self) -> None:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        if self._ilegivel:
            # O arquivo que não deu para ler pode ser do dono com um erro de
            # digitação: gravar por cima apagaria as receitas e opções dele.
            copia = self.arquivo.with_name(
                f"{self.arquivo.name}.ilegivel-{time.strftime('%Y%m%dT%H%M%S')}")
            try:
                os.replace(self.arquivo, copia)
                log.warning("agentes.json ilegível guardado como %s", copia.name)
            except OSError:
                raise ErroDoAgente("refused", "agentes.json está ilegível e não pude guardá-lo")
            self._ilegivel = False
        dados = {"version": 1, **self._estado}
        fd, temporario = tempfile.mkstemp(dir=str(self.arquivo.parent), prefix=".agentes-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(dados, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.chmod(temporario, 0o600)
            os.replace(temporario, self.arquivo)
            self._visto = self._carimbo()
        except OSError:
            try:
                os.unlink(temporario)
            except OSError:
                pass
            raise

    # ── leitura ──────────────────────────────────────────────────────────────
    def obter(self, ident: str) -> Adaptador | None:
        with self._trava:
            return self._agentes.get(ident)

    def saude(self, ident: str, *, fresca: bool = False) -> Saude:
        agente = self.obter(ident)
        if agente is None:
            return Saude(False, "not_installed", "agente desconhecido")
        agora = time.monotonic()
        with self._trava:
            guardada = self._saude.get(ident)
        if guardada and not fresca and agora - guardada[0] < SAUDE_VALE_S:
            return guardada[1]
        try:
            medida = agente.saude()
        except Exception as e:  # um adaptador com defeito não derruba a lista
            medida = Saude(False, "error", f"{type(e).__name__}")
        with self._trava:
            self._saude[ident] = (agora, medida)
        return medida

    @property
    def escolhido(self) -> str:
        """O id que o dono escolheu — ou o padrão, se nunca escolheu."""
        with self._trava:
            ativo = self._estado.get("active") or ""
            return ativo if ativo in self._agentes else PADRAO

    @property
    def explicito(self) -> bool:
        with self._trava:
            return bool(self._estado.get("explicit")) and (
                self._estado.get("active") in self._agentes)

    def efetivo(self) -> Adaptador | None:
        """Quem atende o próximo turno.

        A escolha do dono vale sempre, disponível ou não: um agente escolhido
        que caiu tem de aparecer como caído, e não ser trocado em silêncio por
        outro com outra memória. Só quando ninguém escolheu é que o robô
        procura sozinho quem está de pé.
        """
        with self._trava:
            escolhido = self._agentes.get(self.escolhido)
            if self.explicito and escolhido is not None:
                return escolhido
            candidatos = [self.escolhido] + [i for i in PREFERENCIA if i != self.escolhido]
        for ident in candidatos:
            if self.obter(ident) is not None and self.saude(ident).disponivel:
                return self.obter(ident)
        return escolhido

    def reserva(self) -> Adaptador | None:
        with self._trava:
            return self._agentes.get(self._estado.get("fallback") or "")

    def listar(self) -> dict[str, Any]:
        with self._trava:
            agentes = list(self._agentes.values())
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="saude-agente") as pool:
            saudes = list(pool.map(lambda a: self.saude(a.id), agentes))
        efetivo = self.efetivo()
        ordem = {i: n for n, i in enumerate(PREFERENCIA)}
        itens = []
        for agente, saude in zip(agentes, saudes):
            itens.append({**agente.descrever(), "health": saude.json(),
                          "available": saude.disponivel,
                          "active": efetivo is not None and agente.id == efetivo.id})
        itens.sort(key=lambda a: (ordem.get(a["id"], 50), a["name"].lower()))
        with self._trava:
            return {"agents": itens, "active": efetivo.id if efetivo else None,
                    "selected": self.escolhido, "explicit": self.explicito,
                    "default": PADRAO, "fallback": self._estado.get("fallback") or None}

    # ── escrita ──────────────────────────────────────────────────────────────
    def _so_daqui(self, agente: Adaptador, local: bool) -> None:
        """Agente sem restrição só entra em uso por decisão tomada NESTE computador.

        O dono pode ter cadastrado um agente com ferramentas só para testar; da
        rede, ninguém o põe para atender (nem como reserva) a fala da sala.
        """
        if not local and agente.seguranca == LIVRE:
            raise ErroDoAgente(
                "refused", "agente sem restrição de ferramentas só se ativa neste computador")

    def escolher(self, ident: str, *, local: bool = False) -> Adaptador:
        with self._trava:
            agente = self._agentes.get(ident)
            if agente is None or isinstance(agente, Ausente):
                raise ErroDoAgente("refused", "agente desconhecido")
            self._so_daqui(agente, local)
            self._estado["active"], self._estado["explicit"] = ident, True
            if self._estado.get("fallback") == ident:
                self._estado["fallback"] = ""
            self._gravar()
        log.info("agente ativo: %s", ident)
        return agente

    def definir_reserva(self, ident: str | None, *, local: bool = False) -> None:
        efetivo = self.efetivo()
        with self._trava:
            if ident and ident not in self._agentes:
                raise ErroDoAgente("refused", "agente desconhecido")
            if ident and (ident == self.escolhido or (efetivo and ident == efetivo.id)):
                raise ErroDoAgente("refused", "o reserva não pode ser o próprio agente ativo")
            if ident:
                self._so_daqui(self._agentes[ident], local)
            self._estado["fallback"] = ident or ""
            self._gravar()

    def configurar(self, ident: str, opcoes: dict[str, Any], *, local: bool = False) -> None:
        with self._trava:
            agente = self._agentes.get(ident)
            if agente is None:
                raise ErroDoAgente("refused", "agente desconhecido")
            conhecidas = {o["key"]: o for o in agente.opcoes()}
            if not isinstance(opcoes, dict) or not opcoes or set(opcoes) - set(conhecidas):
                raise ErroDoAgente("refused", "opção desconhecida para este agente")
            for chave, valor in opcoes.items():
                escolhas = conhecidas[chave].get("choices")
                if escolhas is not None:
                    alvo = next((e for e in escolhas if e["value"] == valor), None)
                    if alvo is None:
                        raise ErroDoAgente("refused", f"valor inválido para {chave}")
                    if alvo.get("local_only") and not local:
                        raise ErroDoAgente("refused",
                                           "esta opção só pode ser mudada neste computador")
            agente.configurar(opcoes, local=local)
            guardadas = self._estado.setdefault("options", {}).setdefault(ident, {})
            guardadas.update(opcoes)
            self._saude.pop(ident, None)
            self._gravar()
        # Conversa aberta não herda opção nova: a restrição de ferramentas de
        # uma sessão é a de quando ela nasceu.
        self.ao_mudar(ident)

    def adicionar(self, definicao: dict[str, Any], *, local: bool = False) -> Adaptador:
        """Um agente do dono. Receita de comando só entra com `local=True`."""
        tipo = definicao.get("kind") if isinstance(definicao, dict) else None
        if tipo == "http":
            novo: Adaptador = http_openai.HttpOpenAI(definicao, local=local)
            limpa = dict(novo.definicao)
        elif tipo == "command":
            if not local:
                raise ErroDoAgente(
                    "refused", "agente de linha de comando só se cria neste computador")
            novo = comando.Comando(definicao)
            limpa = dict(novo.receita)
        else:
            raise ErroDoAgente("refused", "kind: `http` ou `command`")
        limpa["origin"] = "local" if local else "network"
        with self._trava:
            existente = self._agentes.get(novo.id)
            if existente is not None and existente.descoberto:
                raise ErroDoAgente("refused", "já existe um agente descoberto com este id")
            if not local:
                # Pela rede não se toma o nome de um agente conhecido, nem se
                # troca um agente que o dono cadastrou neste computador.
                if novo.id in PREFERENCIA:
                    raise ErroDoAgente("refused", "este id é reservado")
                anterior = next((c for c in self._estado["custom"]
                                 if c.get("id") == novo.id), None)
                if anterior is not None and anterior.get("origin") != "network":
                    raise ErroDoAgente(
                        "refused", "já existe um agente com este id, criado neste computador")
            proprios = [c for c in self._estado["custom"] if c.get("id") != novo.id]
            if len(proprios) >= PERSONALIZADOS_MAX:
                raise ErroDoAgente("refused", f"no máximo {PERSONALIZADOS_MAX} agentes do dono")
            self._estado["custom"] = proprios + [limpa]
            self._agentes[novo.id] = novo
            self._saude.pop(novo.id, None)
            self._gravar()
        self.ao_mudar(novo.id)
        return novo

    def remover(self, ident: str, *, local: bool = False) -> None:
        with self._trava:
            agente = self._agentes.get(ident)
            if agente is None or agente.descoberto:
                raise ErroDoAgente("refused", "só agentes do dono podem ser removidos")
            origem = next((c.get("origin") for c in self._estado["custom"]
                           if c.get("id") == ident), "local")
            if not local and origem != "network":
                # Pela rede só se desfaz o que pela rede se fez.
                raise ErroDoAgente("refused", "este agente foi criado neste computador")
            del self._agentes[ident]
            self._estado["custom"] = [c for c in self._estado["custom"] if c.get("id") != ident]
            self._estado.get("options", {}).pop(ident, None)
            if self._estado.get("active") == ident:
                self._estado["active"], self._estado["explicit"] = "", False
            if self._estado.get("fallback") == ident:
                self._estado["fallback"] = ""
            self._gravar()
        self.ao_mudar(ident)

    # ── o papel e o teste ────────────────────────────────────────────────────
    def instrucoes(self, agente: Adaptador, idioma: str = "pt") -> str:
        try:
            quem = self._identidade() or {}
        except Exception:  # sem identidade, o robô ainda tem nome
            quem = {}
        return persona.instrucoes(
            str(quem.get("assistant_name") or "Garra"), str(quem.get("operator_name") or ""),
            str(quem.get("persona_prompt") or ""), seguranca=agente.seguranca, idioma=idioma)

    def testar(self, ident: str) -> dict[str, Any]:
        """Uma ida e volta de verdade: é o que prova a conversa, não a saúde."""
        agente = self.obter(ident)
        if agente is None:
            return {"ok": False, "error": {"code": "refused", "detail": "agente desconhecido"}}
        conversa = Conversa(id=str(uuid.uuid4()), agente=ident)
        t0 = time.monotonic()
        primeiro: list[float] = []
        try:
            resposta = agente.responder(
                conversa, FRASE_DE_TESTE, instrucoes=self.instrucoes(agente),
                ao_trecho=lambda _t: primeiro or primeiro.append(time.monotonic() - t0))
        except ErroDoAgente as e:
            with self._trava:
                self._saude.pop(ident, None)
            return {"ok": False, "agent": ident, "error": {"code": e.codigo, "detail": e.detalhe},
                    "latency_ms": int((time.monotonic() - t0) * 1000)}
        except Exception as e:  # defeito do adaptador: dito, não propagado
            log.exception("teste do agente %s falhou", ident)
            return {"ok": False, "agent": ident,
                    "error": {"code": "bad_response", "detail": type(e).__name__}}
        finally:
            try:
                agente.encerrar(conversa)
            except Exception:
                pass
        return {"ok": True, "agent": ident, "latency_ms": int((time.monotonic() - t0) * 1000),
                "first_chunk_ms": int(primeiro[0] * 1000) if primeiro else None,
                "reply": " ".join(resposta.split())[:80], "safety": agente.seguranca}
