"""O contrato de um agente de IA — o que a ponte precisa de cada um, e mais nada.

O robô não sabe com quem fala. Ele manda a fala transcrita à ponte e lê a
resposta em voz alta; quem decide se a resposta vem do Hermes, do Garra, do Pi
ou de um modelo local é o computador, e cada um deles entra por um adaptador
com esta mesma forma.

Três coisas que todo adaptador garante:

1. **Saúde barata.** `saude()` roda a cada poucos segundos e nunca gasta token:
   confere binário, porta ou rota de vida. Quem prova a conversa é `responder`.
2. **Conversa com dono.** O adaptador recebe uma `Conversa` e decide como
   continuar o assunto: guardando o id da sessão do agente (`ref`), ou
   reenviando o histórico quando o agente não tem memória.
3. **Erro com nome.** Falha sai como `ErroDoAgente` com um código que a tela e a
   voz sabem explicar — nunca como exceção crua, nunca como texto do agente.

O que o agente devolve é **texto não confiável**. Nada aqui o interpreta: ele
segue para o robô, que o limpa, fala e — no máximo — lê as diretivas do caderno.
Movimento, comando e hardware passam pelas interfaces validadas do robô.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

# Quanto do que um agente imprimiu pode entrar numa mensagem de erro ou num log.
# O bastante para reconhecer o problema; curto para não carregar conversa.
TRECHO_DE_ERRO = 160

# Como a fala captada pelo microfone é impedida de virar comando na máquina:
SEM_FERRAMENTAS = "no_tools"     # o agente só conversa
RESTRITO = "restricted"          # ferramentas escolhidas (ex.: só busca na web)
LIVRE = "unrestricted"           # o agente executa o que quiser — só por opção do dono

Trecho = Callable[[str], None]


class ErroDoAgente(RuntimeError):
    """Falha esperada de um agente, com código estável.

    `unavailable` (não instalado, fora do ar), `timeout`, `auth` (sem login ou
    sem chave), `bad_response` (respondeu algo que não dá para usar), `refused`
    (o adaptador recusou o pedido) e `session_lost` (a conversa não existe mais
    do lado do agente — quem chama abre outra).
    """

    def __init__(self, codigo: str, detalhe: str = "", interno: str = "") -> None:
        super().__init__(f"{codigo}: {detalhe}" if detalhe else codigo)
        self.codigo = codigo
        # `detalhe` atravessa a ponte e chega ao painel do robô, que não pede
        # senha: só o que pode ser lido por qualquer aparelho da casa.
        self.detalhe = detalhe[:TRECHO_DE_ERRO]
        # `interno` é o que o programa imprimiu ao falhar. Fica neste
        # computador: serve ao adaptador para entender a falha e ao log.
        self.interno = interno[:400]


@dataclass(frozen=True)
class Saude:
    disponivel: bool
    codigo: str = "ok"       # ok | not_installed | not_running | auth | error | timeout
    detalhe: str = ""
    versao: str | None = None

    def json(self) -> dict[str, Any]:
        return {"available": self.disponivel, "code": self.codigo,
                "detail": self.detalhe[:TRECHO_DE_ERRO], "version": self.versao}


@dataclass
class Conversa:
    """Um assunto em andamento entre o robô e UM agente."""

    id: str
    agente: str
    # Como o agente chama esta conversa do lado dele (id de sessão). `None`
    # enquanto não houve o primeiro turno, ou quando o agente não tem memória.
    ref: str | None = None
    mensagens: list[dict[str, str]] = field(default_factory=list)
    criada_em: float = field(default_factory=time.time)
    usada_em: float = field(default_factory=time.time)
    # Um turno por vez: dois pedidos na mesma conversa embaralhariam a ordem.
    trava: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # A mesma conversa, do lado do agente reserva (por id de agente). Cada
    # reserva tem o SEU id de sessão: reaproveitar o desta faria um agente
    # recusar "sessão já existe", ou retomar a de outro.
    reservas: dict[str, "Conversa"] = field(default_factory=dict, repr=False)
    # Até quando (relógio monotônico) o turno em curso pode durar. É a ponte
    # que marca: o robô desiste em 120 s, e o agente reserva precisa caber no
    # que sobrar depois de o escolhido não responder.
    ate: float | None = field(default=None, repr=False)


class Adaptador:
    """Base de todo agente. Subclasses definem os atributos e três métodos."""

    id: str = ""
    nome: str = ""
    interface: str = ""            # "cli" | "http" | "gateway"
    # O agente guarda a conversa do lado dele (senão, o histórico é reenviado).
    sessoes: bool = False
    # A resposta chega aos pedaços (a ponte mede o primeiro trecho).
    streaming: bool = False
    seguranca: str = SEM_FERRAMENTAS
    # Veio da descoberta automática (e não de `agentes.json`).
    descoberto: bool = True
    # A conversa deste agente atravessa o proxy travado do gateway, em vez de
    # ser atendida aqui (só o Garra).
    pelo_gateway: bool = False
    prazo_s: float = 90.0

    def saude(self) -> Saude:
        raise NotImplementedError

    def prazo(self, conversa: Conversa) -> float:
        """Quanto este turno pode durar: o prazo do agente, ou o que sobrar."""
        if conversa.ate is None:
            return self.prazo_s
        return max(5.0, min(self.prazo_s, conversa.ate - time.monotonic()))

    def responder(self, conversa: Conversa, texto: str, *, instrucoes: str,
                  ao_trecho: Trecho | None = None) -> str:
        """A resposta do agente a `texto`, dentro de `conversa`.

        `instrucoes` é o papel do agente nesta conversa (voz de um robô, frases
        curtas, sem markdown). Levanta `ErroDoAgente`; nunca devolve vazio.
        """
        raise NotImplementedError

    def encerrar(self, conversa: Conversa) -> None:
        """A conversa acabou. Padrão: nada a liberar."""

    # ── o que a tela mostra, e o que o dono pode ajustar ─────────────────────
    def opcoes(self) -> list[dict[str, Any]]:
        """Opções ajustáveis: `[{key, label, type, value, choices?, local_only?}]`.

        `local_only` marca o que só o dono, NESTE computador, pode mudar — o
        painel do robô atende a rede da casa, e de lá ninguém afrouxa a
        segurança de um agente.
        """
        return []

    def configurar(self, opcoes: dict[str, Any], *, local: bool = False) -> None:
        """Aplica opções já validadas contra `opcoes()`. Padrão: nenhuma."""

    def descrever(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.nome, "interface": self.interface,
            "capabilities": {"sessions": self.sessoes, "streaming": self.streaming},
            "safety": self.seguranca, "discovered": self.descoberto,
            "custom": not self.descoberto, "timeout_s": self.prazo_s,
            "options": self.opcoes(),
        }


# ─── utilidades de quem fala com um executável ──────────────────────────────
def achar_executavel(nome: str, extras: tuple[str, ...] = ()) -> str | None:
    """O caminho ABSOLUTO do executável, ou `None`.

    O companion roda pelo systemd do usuário, cujo PATH não é o do terminal:
    por isso, além do `which`, os lugares onde cada agente costuma se instalar.
    O caminho absoluto é guardado para que uma mudança de PATH não troque, em
    silêncio, o programa que a voz do robô alcança.
    """
    # Os lugares conhecidos primeiro: o mesmo executável, com o PATH do
    # terminal ou com o do systemd.
    candidatos = [str(Path(c).expanduser()) for c in extras]
    candidatos.append(shutil.which(nome, path=ambiente_do_agente()["PATH"]))
    for caminho in candidatos:
        if caminho and os.path.isfile(caminho) and os.access(caminho, os.X_OK):
            return str(Path(caminho).absolute())
    return None


# Variáveis do companion que nenhum agente precisa conhecer: os segredos da
# própria ponte. As chaves de provedor (OPENROUTER_API_KEY, …) seguem, porque
# há agente que só fala com o modelo através delas.
_NAO_HERDAR = ("GARRA_VOZ_TOKEN", "GARRA_GATEWAY_KEY", "GARRA_REACHY_TOKEN")


def ambiente_do_agente() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _NAO_HERDAR}
    # Os instaladores de agente põem os binários aqui; o systemd não sabe.
    extras = [str(Path(p).expanduser()) for p in
              ("~/.local/bin", "~/.local/share/node24/bin", "~/.opencode/bin")]
    atual = env.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(os.pathsep)
    env["PATH"] = os.pathsep.join(extras + [p for p in atual if p not in extras])
    env.setdefault("NO_COLOR", "1")
    env["TERM"] = "dumb"
    return env


def pasta_de_trabalho(agente: str) -> Path:
    """Uma pasta vazia por agente, para ele rodar longe de qualquer projeto.

    Um agente com ferramenta de arquivo trabalha a partir do diretório atual.
    Rodá-lo de dentro do repositório do companion daria à voz do robô um
    caminho para o código que a atende.
    """
    base = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    pasta = base / "garra-reachy" / "agentes" / agente
    pasta.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(pasta, 0o700)
    except OSError:
        pass
    return pasta


def executar(argv: list[str], *, entrada: str | None = None, prazo_s: float,
             cwd: Path | None = None) -> Iterator[str]:
    """Roda `argv` (sem shell) e entrega o stdout linha a linha.

    Sem shell de propósito: o texto do usuário vai como UM argumento, e nada do
    que ele disser vira sintaxe. O processo nasce no próprio grupo, para que o
    estouro do prazo mate também o que ele tiver lançado. Estouro levanta
    `ErroDoAgente("timeout")`; saída diferente de zero, `unavailable` com o fim
    do stderr.
    """
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if entrada is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", cwd=str(cwd) if cwd else None,
            env=ambiente_do_agente(), start_new_session=True)
    except OSError as e:
        raise ErroDoAgente("unavailable", f"{type(e).__name__}: {e}") from e

    erros: list[str] = []
    leitor = threading.Thread(
        target=lambda: erros.extend(proc.stderr.readlines()[-20:]), daemon=True)
    leitor.start()
    estourou = threading.Event()

    def matar() -> None:
        estourou.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    relogio = threading.Timer(prazo_s, matar)
    relogio.daemon = True
    relogio.start()
    try:
        if entrada is not None:
            try:
                proc.stdin.write(entrada)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        for linha in proc.stdout:
            yield linha.rstrip("\n")
        codigo = proc.wait()
    finally:
        relogio.cancel()
        if proc.poll() is None:
            matar()
            proc.wait()
        for canal in (proc.stdout, proc.stderr, proc.stdin):
            try:
                if canal is not None:
                    canal.close()
            except OSError:
                pass
    leitor.join(timeout=2.0)
    if estourou.is_set():
        raise ErroDoAgente("timeout", f"sem resposta em {prazo_s:.0f} s")
    if codigo != 0:
        fim = " ".join(" ".join(erros[-3:]).split())
        # O que o programa imprimiu não atravessa a ponte: pode trazer caminho,
        # nome de usuário ou um pedaço do pedido.
        raise ErroDoAgente("unavailable", f"saiu com código {codigo}", interno=fim)


def versao_de(executavel: str, prazo_s: float = 6.0) -> str | None:
    """A primeira linha de `--version`, ou `None`. Nunca levanta."""
    try:
        r = subprocess.run([executavel, "--version"], capture_output=True, text=True,
                           timeout=prazo_s, env=ambiente_do_agente(),
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    linhas = (r.stdout or r.stderr or "").strip().splitlines()
    return linhas[0].strip()[:60] if linhas and r.returncode == 0 else None


# As redes que são "da casa". Lista explícita, e não `ip.is_private`: para o
# Python, "privado" é tudo que não é roteável na internet — o que inclui faixas
# de documentação (203.0.113.0/24), de teste de desempenho e o 6to4 (2002::/16).
_REDES_DA_CASA = tuple(ipaddress.ip_network(r) for r in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",     # RFC 1918
    "169.254.0.0/16",                                    # cabo direto, sem DHCP
    "fc00::/7",                                          # IPv6 local único
))


def da_rede_da_casa(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """`ip` é de uma rede local (e não deste computador, nem da internet)?"""
    return any(ip.version == rede.version and ip in rede for rede in _REDES_DA_CASA)


def neutralizar(texto: str) -> str:
    """Tira do texto o que as CLIs de agente tratam como referência a arquivo.

    Pi, Claude e Hermes expandem `@caminho` (e o Hermes, `@url:`) ANTES de o
    modelo ver o pedido: medido no Pi 0.87.1, um pedido `@/etc/hostname` vira
    "anexe este arquivo", com ou sem `--no-tools`. O pedido vem de um microfone
    ou do chat do painel — de qualquer um na casa. O arroba vira o arroba de
    largura inteira (＠): o modelo lê igual, e CLI nenhuma o trata como marca.
    """
    return texto.replace("@", "＠")


def nome_do_executavel(caminho: str | None) -> str:
    """Só o nome: o caminho inteiro traz o nome do usuário, e vai ao painel."""
    return Path(caminho).name if caminho else ""


def com_historico(conversa: Conversa, texto: str, turnos: int = 8) -> str:
    """O pedido precedido dos últimos turnos — para agente sem memória própria."""
    anteriores = conversa.mensagens[-2 * turnos:]
    if not anteriores:
        return texto
    linhas = ["Conversa até agora:"]
    for m in anteriores:
        quem = "Usuário" if m.get("role") == "user" else "Você"
        linhas.append(f"{quem}: {str(m.get('content') or '')[:500]}")
    linhas.append(f"Usuário: {texto}")
    return "\n".join(linhas)
