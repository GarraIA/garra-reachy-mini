"""Spotify pelo spotatui deste PC: o caminho que o Claude Code já usava, agora do Garra.

O que se mediu em 30/09/2026 com o spotatui 0.43.0 instalado aqui:

- A CLI (`playback`, `play`, `list`, `search`) fala com a Web API da Spotify
  usando a sessão que o próprio spotatui guarda e renova. Nenhuma credencial
  nova, e nenhum token lido por nós: quem toca no cache é só o spotatui.
- O aparelho `spotatui` (o player embutido na TUI) só existe enquanto a TUI
  está aberta num terminal. Não há modo daemon nem headless, e a TUI se
  atualiza sozinha ao abrir — então o companion NÃO a lança. Sem o player, o
  estado é `player_off`, dito como tal.
- Não existe pause nem resume explícitos na CLI, só `--toggle`. Quem usa isto
  (`musica_comandos`) alterna apenas depois de LER o estado oposto e confere o
  resultado; nunca alterna às cegas.
- `playback --status` sem reprodução sai com 1 e "no context available".
- Cada execução grava um log em `$TMPDIR/spotatui_logs/`. TMPDIR vai para uma
  pasta nossa (0700), podada a cada chamada.
- O MCP (`spotatui mcp`) foi avaliado e ficou de fora: não pausa, não retoma,
  não muda volume nem lista aparelhos, e o que ele lê é a memória da TUI — o
  "agora" dele disse "pausado" com a música tocando, e a "fila" seguiu vazia
  com uma faixa enfileirada (a "próxima" foi para ela). A CLI lê a Spotify.
- Faixa tocada sozinha não tem "próxima": a Spotify pausa a mesma faixa no
  0:00 (medido). `--seek` existe, e é o que devolve a música ao ponto em que
  estava.

Todo texto vindo de fora (nome de música, de playlist, de aparelho) é um
elemento de argv, validado, depois de `--` quando é posicional. Nada passa por
shell.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("companion.spotatui")

CODIGOS = ("not_installed", "player_off", "no_playback", "auth_expired", "unreachable", "timeout",
           "not_found", "bad_request", "provider_error")
TIMEOUT_S = 10.0
MAX_LOGS = 20
CONSULTA_MAX = 120
# Separador de campos no `--format`: um caractere que não aparece em título.
SEP = "\x1f"
_FMT_STATUS = SEP.join(("%s", "%t", "%a", "%b", "%d", "%v", "%u", "%r", "%p"))
_FMT_FAIXA = SEP.join(("%u", "%t", "%a", "%b"))
_URI = re.compile(r"spotify:(track|playlist|album|artist):[A-Za-z0-9]{1,64}")
_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")
_TEMPO = re.compile(r"(?:(\d+):)?(\d+):(\d{2})")
_VERSAO = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_CANDIDATOS = ("~/.local/bin/spotatui", "~/.cargo/bin/spotatui", "/usr/local/bin/spotatui", "/usr/bin/spotatui")
CONFIG_YML = Path("~/.config/spotatui/config.yml").expanduser()
DIR_TEMP = Path(os.environ.get("GARRA_REACHY_CACHE", "~/.cache/garra-reachy")).expanduser() / "spotatui-tmp"

Executor = Callable[[list[str], float, dict], tuple[int, str, str]]
_AMBIENTE = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LANGUAGE", "SHELL", "SSL_CERT_FILE", "SSL_CERT_DIR",
             "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"}


class ErroSpotatui(Exception):
    def __init__(self, codigo: str, detalhe: str = "") -> None:
        assert codigo in CODIGOS, codigo
        super().__init__(f"{codigo}: {detalhe}" if detalhe else codigo)
        self.codigo = codigo
        self.detalhe = detalhe


def icones_do_config(caminho: Path = CONFIG_YML) -> tuple[str, str]:
    """Os ícones de tocando/pausado que o usuário escolheu (o `%s` os imprime)."""
    tocando, pausado = "▶", "⏸"
    try:
        texto = caminho.read_text(encoding="utf-8")
    except OSError:
        return tocando, pausado
    for chave, valor in re.findall(r"^\s+(playing_icon|paused_icon):\s*(.+?)\s*$", texto, re.M):
        valor = re.sub(r"\s+#.*$", "", valor).strip().strip("'\"")
        if valor:
            if chave == "playing_icon":
                tocando = valor
            else:
                pausado = valor
    return tocando, pausado


def _segundos(texto: str) -> int | None:
    m = _TEMPO.fullmatch(texto.strip())
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s)


def _executar_processo(argv: list[str], timeout_s: float, env: dict) -> tuple[int, str, str]:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, env=env,
                       stdin=subprocess.DEVNULL, shell=False, check=False)
    return r.returncode, r.stdout, r.stderr


def _erro_de(stderr: str) -> ErroSpotatui:
    """Traduz a mensagem da CLI num código. A mensagem bruta nunca sai daqui.

    Só a linha do erro conta: a do log ("Logging to: …spotatuilog4011") e o
    texto que a pessoa pediu não podem virar "conta desconectada" por conter 401.
    """
    linhas = [linha for linha in stderr.splitlines() if linha.strip() and not linha.startswith("Logging to:")]
    erros = [linha for linha in linhas if "error" in linha.lower()]
    t = "\n".join(erros or linhas).lower()
    if "no device with name" in t or "no active device" in t or "device not found" in t:
        return ErroSpotatui("player_off")
    if "no context available" in t:
        # É também o que a CLI imprime quando o comando entrou mas a exibição do
        # estado, logo depois, ainda não vê a reprodução.
        return ErroSpotatui("no_playback", "no_context")
    if "nothing is playing" in t or "no playback" in t:
        return ErroSpotatui("no_playback")
    # "Não achei" vem antes da conta: a mensagem traz o texto pedido, e um "401"
    # no nome da música não é conta desconectada.
    if "no tracks found" in t or "could not find" in t or "no tracks with name" in t \
            or "no playlists with name" in t:
        return ErroSpotatui("not_found")
    if re.search(r"\b401\b[^\n]*unauthorized|status(?: code)?:? 401\b", t) or "unauthorized" in t \
            or "invalid_grant" in t or "invalid access token" in t or "token expired" in t or "refresh token" in t:
        return ErroSpotatui("auth_expired")
    if ("failed to get response" in t or "error sending request" in t or "dns error" in t
            or "connection" in t or "unreachable" in t or "timed out" in t):
        return ErroSpotatui("unreachable")
    return ErroSpotatui("provider_error")


class Spotatui:
    def __init__(self, executavel: str | None = None, executar: Executor | None = None,
                 dir_temp: Path | None = None, icones: tuple[str, str] | None = None,
                 timeout_s: float = TIMEOUT_S, max_logs: int = MAX_LOGS) -> None:
        self.executavel = executavel or self._achar()
        self._executar = executar or _executar_processo
        self.dir_temp = Path(dir_temp or DIR_TEMP)
        self.icones = icones or icones_do_config()
        self.timeout_s = timeout_s
        self.max_logs = max_logs

    @staticmethod
    def _achar() -> str | None:
        """O binário, por caminho absoluto: o serviço do systemd não tem ~/.local/bin no PATH."""
        env = os.environ.get("GARRA_SPOTATUI")
        if env == "off":
            return None
        if env and os.access(env, os.X_OK):
            return env
        for c in _CANDIDATOS:
            p = os.path.expanduser(c)
            if os.access(p, os.X_OK):
                return p
        return shutil.which("spotatui")

    # ── o processo ────────────────────────────────────────────────────────
    def _ambiente(self) -> dict:
        """O mínimo que a CLI precisa. O ambiente do companion tem chave de API
        (OpenRouter), e o spotatui não tem nada que fazer com ela."""
        self.dir_temp.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir_temp, 0o700)
        self._podar()
        env = {k: v for k, v in os.environ.items() if k in _AMBIENTE or k.startswith(("LC_", "XDG_"))}
        return {**env, "TMPDIR": str(self.dir_temp), "NO_COLOR": "1"}

    def _podar(self) -> None:
        logs = self.dir_temp / "spotatui_logs"
        try:
            arquivos = sorted((p for p in logs.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for p in arquivos[:max(0, len(arquivos) - self.max_logs)]:
            try:
                p.unlink()
            except OSError:
                pass

    def _rodar(self, args: list[str], timeout_s: float | None = None) -> tuple[int, str, str]:
        if not self.executavel:
            raise ErroSpotatui("not_installed")
        argv = [self.executavel, *args]
        try:
            return self._executar(argv, timeout_s or self.timeout_s, self._ambiente())
        except subprocess.TimeoutExpired:
            raise ErroSpotatui("timeout") from None
        except FileNotFoundError:
            raise ErroSpotatui("not_installed") from None
        except OSError as e:
            raise ErroSpotatui("provider_error", type(e).__name__) from None

    def _cli(self, args: list[str]) -> str:
        rc, out, err = self._rodar(args)
        if rc != 0:
            e = _erro_de(err)
            log.info("spotatui %s → %s", args[0], e.codigo)
            raise e
        return out

    def _acao(self, args: list[str]) -> None:
        """Um comando que muda o player. Depois dele a CLI exibe o estado — e essa
        exibição pode falhar com "no context available" quando a Spotify ainda não
        refletiu o comando que ENTROU. Isso não é falha do comando: quem diz se
        funcionou é a conferência de quem chamou."""
        try:
            self._cli(args)
        except ErroSpotatui as e:
            if e.detalhe != "no_context":
                raise

    # ── validação ─────────────────────────────────────────────────────────
    @staticmethod
    def _consulta(texto: Any) -> str:
        if not isinstance(texto, str) or _CONTROLE.search(texto):
            raise ErroSpotatui("bad_request", "consulta")
        texto = texto.strip()
        if not texto or len(texto) > CONSULTA_MAX:
            raise ErroSpotatui("bad_request", "consulta")
        return texto

    @staticmethod
    def _uri(uri: Any, tipos: tuple[str, ...] = ("track", "playlist", "album", "artist")) -> str:
        if not isinstance(uri, str):
            raise ErroSpotatui("bad_request", "uri")
        m = _URI.fullmatch(uri)
        if not m or m.group(1) not in tipos:
            raise ErroSpotatui("bad_request", "uri")
        return uri

    @staticmethod
    def _aparelho(nome: Any) -> str:
        if (not isinstance(nome, str) or not nome.strip() or len(nome) > 64 or _CONTROLE.search(nome)
                or nome.startswith("-")):
            raise ErroSpotatui("bad_request", "aparelho")
        return nome

    # ── leituras ──────────────────────────────────────────────────────────
    def versao(self) -> str | None:
        try:
            rc, out, _ = self._rodar(["--version"], timeout_s=5.0)
        except ErroSpotatui:
            return None
        m = _VERSAO.search(out) if rc == 0 else None
        return m.group(1) if m else None

    def reproducao(self) -> dict[str, Any] | None:
        """O que o player diz agora. `None` quando não há reprodução nenhuma."""
        try:
            out = self._cli(["playback", "--status", "--format", _FMT_STATUS])
        except ErroSpotatui as e:
            if e.codigo == "no_playback":
                return None
            raise
        linha = out.rstrip("\n").split("\n")[-1]
        campos = linha.split(SEP)
        if len(campos) != 9:
            raise ErroSpotatui("provider_error", "formato")
        icone, titulo, artista, album, aparelho, volume, uri, progresso, playlist = campos
        agora, _, total = progresso.partition("/")
        total = total.split(" ")[0]
        estado = ("playing" if icone.strip() == self.icones[0] else
                  "paused" if icone.strip() == self.icones[1] else "unknown")
        return {
            "state": estado,
            "device": aparelho,
            "volume_percent": int(volume) if volume.isdigit() else None,
            "progress_s": _segundos(agora) or 0,
            "playlist": None if playlist in ("", "None") else playlist,
            "track": {"name": titulo, "artists": [artista] if artista and artista != "None" else [],
                      "album": None if album in ("", "None") else album, "uri": uri,
                      "duration_s": _segundos(total)},
        }

    def dispositivos(self) -> list[dict[str, Any]]:
        out = self._cli(["list", "--devices", "--format", f"%d{SEP}%v"])
        saida = []
        for linha in out.splitlines():
            nome, _, volume = linha.partition(SEP)
            if nome:
                saida.append({"name": nome, "volume_percent": int(volume) if volume.isdigit() else None})
        return saida

    def _faixas(self, out: str) -> list[dict[str, Any]]:
        saida = []
        for linha in out.splitlines():
            campos = linha.split(SEP)
            if len(campos) == 4 and _URI.fullmatch(campos[0]):
                uri, titulo, artista, album = campos
                saida.append({"uri": uri, "name": titulo, "artists": [artista] if artista else [],
                              "album": album or None})
        return saida

    def pesquisar(self, consulta: str, limite: int = 5) -> list[dict[str, Any]]:
        consulta = self._consulta(consulta)
        try:
            out = self._cli(["search", "--tracks", "--limit", str(max(1, min(10, limite))),
                             "--format", _FMT_FAIXA, "--", consulta])
        except ErroSpotatui as e:
            if e.codigo == "not_found":
                return []
            raise
        return self._faixas(out)

    def playlists(self, limite: int = 50) -> list[dict[str, str]]:
        out = self._cli(["list", "--playlists", "--limit", str(max(1, min(50, limite))),
                         "--format", f"%p{SEP}%u"])
        saida = []
        for linha in out.splitlines():
            nome, _, uri = linha.partition(SEP)
            if _URI.fullmatch(uri):
                saida.append({"name": nome, "uri": uri})
        return saida

    def curtidas(self, limite: int = 50) -> list[dict[str, Any]]:
        return self._faixas(self._cli(["list", "--liked", "--limit", str(max(1, min(50, limite))),
                                       "--format", _FMT_FAIXA]))

    # ── comandos (quem confere o efeito é `musica_comandos`) ─────────────
    def tocar(self, uri: str, aparelho: str, aleatorio: bool = False) -> None:
        args = ["play", "--uri", self._uri(uri), "--device", self._aparelho(aparelho)]
        if aleatorio:
            args.append("--random")
        self._acao(args)

    def enfileirar(self, uri: str, aparelho: str) -> None:
        self._acao(["play", "--uri", self._uri(uri, ("track",)), "--queue", "--device", self._aparelho(aparelho)])

    def alternar(self, aparelho: str) -> None:
        self._acao(["playback", "--toggle", "--device", self._aparelho(aparelho)])

    def proxima(self, aparelho: str) -> None:
        self._acao(["playback", "--next", "--device", self._aparelho(aparelho)])

    def anterior(self, aparelho: str) -> None:
        # Um `--previous` volta ao começo da faixa; dois voltam uma faixa.
        self._acao(["playback", "--previous", "--previous", "--device", self._aparelho(aparelho)])

    def posicionar(self, segundos: int, aparelho: str) -> None:
        if not isinstance(segundos, int) or isinstance(segundos, bool) or not 0 <= segundos <= 24 * 3600:
            raise ErroSpotatui("bad_request", "posição")
        self._acao(["playback", "--seek", str(segundos), "--device", self._aparelho(aparelho)])

    def volume(self, pct: int, aparelho: str) -> None:
        if not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100:
            raise ErroSpotatui("bad_request", "volume")
        self._acao(["playback", "--volume", str(pct), "--device", self._aparelho(aparelho)])

__all__ = ["CODIGOS", "ErroSpotatui", "SEP", "Spotatui", "icones_do_config"]
