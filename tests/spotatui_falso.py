"""Um spotatui de mentira, para os testes do companion.

Emula a CLI do spotatui 0.43.0 no que o adaptador usa: os subcomandos, as
flags longas, a troca dos especificadores do `--format` e as mensagens de erro
que o binário de verdade imprime (lidas dele em 30/09/2026). O relógio é o do
teste: o progresso da faixa só anda quando o teste manda o tempo andar.

Nada aqui toca rede, processo ou arquivo.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field

LOG = "Logging to: /tmp/x/spotatui_logs/spotatuilog4242\n"


@dataclass
class Faixa:
    uri: str
    titulo: str
    artista: str
    album: str = "Álbum"
    duracao_s: int = 200


@dataclass
class Tocando:
    faixa: Faixa
    aparelho: str
    tocando: bool = True
    base_s: float = 0.0          # progresso quando `desde` foi marcado
    desde: float = 0.0
    playlist: str | None = None
    lista: list[Faixa] = field(default_factory=list)   # contexto (playlist) para próxima/anterior


def _progresso(s: int) -> str:
    return f"{s // 60}:{s % 60:02d}"


class Relogio:
    def __init__(self) -> None:
        self.t = 1000.0

    def agora(self) -> float:
        return self.t

    def dormir(self, s: float) -> None:
        self.t += max(0.0, s)


class SpotatuiFalso:
    def __init__(self, relogio: Relogio | None = None) -> None:
        self.relogio = relogio or Relogio()
        self.aparelhos: dict[str, int] = {"spotatui": 60, "michel-PC": 100}
        self.catalogo = [
            Faixa("spotify:track:hotel1", "Hotel California - 2013 Remaster", "Eagles", "Hotel California", 391),
            Faixa("spotify:track:hotel2", "Hotel California - Live", "Eagles", "Hell Freezes Over", 430),
            Faixa("spotify:track:evid01", "Evidências", "Chitãozinho & Xororó", "Cowboy do Asfalto", 280),
            Faixa("spotify:track:bohe01", "Bohemian Rhapsody", "Queen", "A Night at the Opera", 354),
        ]
        self.curtidas = [
            Faixa("spotify:track:chop01", "Chop Suey!", "System Of A Down", "Toxicity", 210),
            Faixa("spotify:track:onde01", "Onde Você Mora?", "Cidade Negra", "Sobre Todas as Forças", 250),
            Faixa("spotify:track:60seg1", "60 Segundos", "CPM 22", "CPM22", 148),
        ]
        rock = [self.catalogo[3], self.catalogo[0], self.curtidas[0]]
        self.playlists: dict[str, tuple[str, list[Faixa]]] = {
            "spotify:playlist:rock01": ("Rock Nacional", [self.curtidas[2], self.curtidas[1]]),
            "spotify:playlist:toad01": ("Toad sings", rock),
            "spotify:playlist:jota01": ("Jota Quest - As Melhores  💥 💥 💥 ", [self.curtidas[1]]),
        }
        self.agora: Tocando | None = None
        self.fila: list[Faixa] = []
        self.chamadas: list[list[str]] = []
        self.envs: list[dict] = []
        # Quantas leituras de estado ainda mostram o estado ANTERIOR a um
        # comando: a Spotify leva um instante para refletir o que mudou.
        self.atraso_leituras = 0
        self._pendente: Tocando | None | str = "nada"
        self.falha: str | None = None          # próxima(s) chamada(s) falham assim
        self.falhas_restantes = 0               # 0 = todas enquanto `falha` estiver definida
        self.ignorar: set[str] = set()          # comandos aceitos que não surtem efeito
        # Faixa avulsa, fila vazia: "próxima" pausa a mesma faixa no 0:00 (medido).
        self.fim_de_contexto = False
        self.versao = "spotatui 0.43.0"
        self.trava = threading.Lock()
        self.icone_tocando, self.icone_pausado = "▶", "⏸"

    # ── estado que o teste arma ───────────────────────────────────────────
    def por_para_tocar(self, faixa: Faixa, aparelho: str = "spotatui", tocando: bool = True,
                       progresso_s: float = 20.0, playlist: str | None = None) -> None:
        self.agora = Tocando(faixa, aparelho, tocando, progresso_s, self.relogio.agora(), playlist)

    def progresso_s(self) -> int:
        a = self.agora
        if a is None:
            return 0
        extra = self.relogio.agora() - a.desde if a.tocando else 0.0
        return int(min(a.faixa.duracao_s, a.base_s + extra))

    # ── a CLI ─────────────────────────────────────────────────────────────
    def __call__(self, argv: list[str], timeout_s: float, env: dict) -> tuple[int, str, str]:
        with self.trava:
            self.chamadas.append(list(argv))
            self.envs.append(dict(env))
            if self.falha:
                falha = self.falha
                if self.falhas_restantes:
                    self.falhas_restantes -= 1
                    if not self.falhas_restantes:
                        self.falha = None
                return self._falhar(falha, timeout_s)
            return self._executar(argv[1:])

    def _falhar(self, falha: str, timeout_s: float) -> tuple[int, str, str]:
        if falha == "timeout":
            import subprocess
            raise subprocess.TimeoutExpired(["spotatui"], timeout_s)
        if falha == "ausente":
            raise FileNotFoundError("spotatui")
        if falha == "offline":
            return 1, "", LOG + "Error: Failed to get response: error sending request: dns error\n"
        if falha == "auth":
            return 1, "", LOG + "Error: Failed to get response: 401 Unauthorized: invalid_grant\n"
        return 1, "", LOG + f"Error: {falha}\n"

    def _fmt(self, fmt: str, **campos: str) -> str:
        saida, i = [], 0
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt) and fmt[i + 1] in campos:
                saida.append(campos[fmt[i + 1]])
                i += 2
            else:
                saida.append(fmt[i])
                i += 1
        return "".join(saida)

    def _visivel(self) -> Tocando | None:
        if self.atraso_leituras > 0 and self._pendente != "nada":
            self.atraso_leituras -= 1
            return self._pendente  # type: ignore[return-value]
        self._pendente = "nada"
        return self.agora

    def _status(self, fmt: str) -> tuple[int, str, str]:
        a = self._visivel()
        if a is None:
            return 1, "", LOG + "Error: no context available\n"
        prog = self.progresso_s() if a is self.agora else int(a.base_s)
        f = a.faixa
        linha = self._fmt(fmt, s=self.icone_tocando if a.tocando else self.icone_pausado, t=f.titulo,
                          a=f.artista, b=f.album, d=a.aparelho, v=str(self.aparelhos.get(a.aparelho, 0)),
                          u=f.uri, p=a.playlist or "None", f="",
                          r=f"{_progresso(prog)}/{_progresso(f.duracao_s)} (-{_progresso(f.duracao_s - prog)})",
                          h="None")
        return 0, linha + "\n", LOG

    def _mudar(self, novo: Tocando | None) -> None:
        """O comando entrou; as próximas `atraso_leituras` leituras ainda veem o antigo."""
        self._pendente = self.agora
        self.agora = novo

    def _opcoes(self, args: list[str]) -> tuple[dict, list[str]]:
        com_valor = {"--device", "--format", "--uri", "--volume", "--limit", "--transfer", "--name", "--seek"}
        opcoes: dict = {"--previous": 0, "--next": 0}
        posicionais: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                posicionais.extend(args[i + 1:])
                break
            if a in com_valor:
                opcoes[a] = args[i + 1]
                i += 2
                continue
            if a in ("--previous", "--next"):
                opcoes[a] += 1
            elif a.startswith("-"):
                opcoes[a] = True
            else:
                posicionais.append(a)
            i += 1
        return opcoes, posicionais

    def _executar(self, args: list[str]) -> tuple[int, str, str]:
        if args == ["--version"]:
            return 0, self.versao + "\n", ""
        sub, resto = args[0], args[1:]
        o, pos = self._opcoes(resto)
        fmt = o.get("--format", "%f %s %t - %a")
        aparelho = o.get("--device")
        if aparelho is not None and aparelho not in self.aparelhos:
            return 1, "", LOG + f"Error: no device with name '{aparelho}'\n"
        if sub == "list":
            return self._listar(o, fmt)
        if sub == "search":
            return self._buscar(o, pos, fmt)
        if sub == "play":
            return self._tocar(o, fmt, aparelho)
        if sub == "playback":
            return self._reproducao(o, fmt, aparelho)
        return 2, "", LOG + f"error: unrecognized subcommand '{sub}'\n"

    def _listar(self, o: dict, fmt: str) -> tuple[int, str, str]:
        limite = int(o.get("--limit", 20))
        if o.get("--devices"):
            linhas = [self._fmt(fmt, d=n, v=str(v), s="None") for n, v in self.aparelhos.items()]
        elif o.get("--playlists"):
            linhas = [self._fmt(fmt, p=nome, u=uri) for uri, (nome, _) in self.playlists.items()]
        elif o.get("--liked"):
            linhas = [self._fmt(fmt, u=f.uri, t=f.titulo, a=f.artista, b=f.album) for f in self.curtidas]
        else:
            return 2, "", LOG + "error: one of --devices --playlists --liked is required\n"
        return 0, "".join(linha + "\n" for linha in linhas[:limite]), LOG

    def _buscar(self, o: dict, pos: list[str], fmt: str) -> tuple[int, str, str]:
        if len(pos) != 1:
            extra = pos[1] if len(pos) > 1 else ""
            return 2, "", LOG + f"error: unexpected argument '{extra}' found\n"
        termos = [t for t in pos[0].lower().split() if t]
        achados = [f for f in self.catalogo + self.curtidas
                   if all(t in f"{f.titulo} {f.artista}".lower() for t in termos)]
        if not achados and "califórnia" in pos[0].lower():
            # A busca da Spotify devolve ALGUMA coisa para quase tudo (medido:
            # "até o Califórnia" → "Cachimbo da Paz").
            achados = self.catalogo[:1]
        if not achados:
            return 1, "", LOG + f'Error: No tracks found for "{pos[0]}"\n'
        limite = int(o.get("--limit", 5))
        return 0, "".join(self._fmt(fmt, u=f.uri, t=f.titulo, a=f.artista, b=f.album) + "\n"
                          for f in achados[:limite]), LOG

    def _faixa(self, uri: str) -> Faixa | None:
        for f in self.catalogo + self.curtidas:
            if f.uri == uri:
                return f
        return None

    def _tocar(self, o: dict, fmt: str, aparelho: str | None) -> tuple[int, str, str]:
        uri = o.get("--uri", "")
        destino = aparelho or (self.agora.aparelho if self.agora else None)
        if destino is None:
            return 1, "", LOG + "Error: no active device\n"
        if o.get("--queue"):
            f = self._faixa(uri)
            if f is None:
                return 1, "", LOG + f"Error: Could not find: {uri}\n"
            if "queue" not in self.ignorar:
                self.fila.append(f)
            return 0, "", LOG
        if uri.startswith("spotify:playlist:"):
            if uri not in self.playlists:
                return 1, "", LOG + f"Error: Could not find: {uri}\n"
            nome, faixas = self.playlists[uri]
            f = random.choice(faixas) if o.get("--random") else faixas[0]
            novo = Tocando(f, destino, True, 0.0, self.relogio.agora(), nome, list(faixas))
        else:
            f = self._faixa(uri)
            if f is None:
                return 1, "", LOG + f"Error: Could not find: {uri}\n"
            novo = Tocando(f, destino, True, 0.0, self.relogio.agora())
        if "play" not in self.ignorar:
            self._mudar(novo)
        return self._status(fmt) if self.agora else (0, "", LOG)

    def _reproducao(self, o: dict, fmt: str, aparelho: str | None) -> tuple[int, str, str]:
        a = self.agora
        if o.get("--toggle"):
            if a is None:
                return 1, "", LOG + "Error: No playback to resume on device\n"
            if "toggle" not in self.ignorar:
                novo = Tocando(a.faixa, a.aparelho, not a.tocando, self.progresso_s(), self.relogio.agora(),
                               a.playlist, a.lista)
                self._mudar(novo)
        if o.get("--next") or o.get("--previous"):
            if a is None:
                return 1, "", LOG + "Error: Nothing is playing (no context)\n"
            if "skip" not in self.ignorar:
                if o["--next"]:
                    if self.fim_de_contexto and not self.fila:
                        self._mudar(Tocando(a.faixa, a.aparelho, False, 0.0, self.relogio.agora(), a.playlist,
                                            a.lista))
                        return self._status(fmt)
                    if self.fila:
                        f = self.fila.pop(0)
                    else:
                        lista = a.lista or self.catalogo
                        i = lista.index(a.faixa) if a.faixa in lista else -1
                        f = lista[(i + 1) % len(lista)]
                    self._mudar(Tocando(f, a.aparelho, True, 0.0, self.relogio.agora(), a.playlist, a.lista))
                else:
                    lista = a.lista or self.catalogo
                    i = lista.index(a.faixa) if a.faixa in lista else 0
                    f = lista[i - 1] if o["--previous"] >= 2 else a.faixa
                    self._mudar(Tocando(f, a.aparelho, True, 0.0, self.relogio.agora(), a.playlist, a.lista))
        if "--seek" in o:
            if a is None:
                return 1, "", LOG + "Error: Nothing is playing (no context)\n"
            self.agora = Tocando(a.faixa, a.aparelho, a.tocando, float(int(o["--seek"])), self.relogio.agora(),
                                 a.playlist, a.lista)
        if "--volume" in o:
            alvo = aparelho or (a.aparelho if a else None)
            if alvo is None:
                return 1, "", LOG + "Error: no active device\n"
            if "volume" not in self.ignorar:
                self.aparelhos[alvo] = int(o["--volume"])
        return self._status(fmt)
