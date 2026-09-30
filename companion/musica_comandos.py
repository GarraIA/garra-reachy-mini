"""A ferramenta de música do Garra: pedido tipado → player → estado conferido.

Quem chama é o robô (pedido de voz ou do chat, já interpretado lá) e o painel.
Quem executa é o spotatui deste PC (`spotatui.py`). Entre os dois, as regras
que fazem a confirmação falada ser verdade:

- "Tocando" só com a faixa pedida tocando NO APARELHO ALVO e o tempo andando
  entre duas leituras. É o estado técnico do player, não prova de que alguém
  ouviu o som — e o resultado diz qual dos dois é.
- Pausa e retomada nunca alternam às cegas: lê o estado, alterna só se ele for
  o oposto, e confere. Já pausada é "pausada", sem comando.
- Comando aceito que não surtiu efeito vira falha, e não é repetido: repetir
  um play recomeça a música; repetir um toggle desfaz o primeiro.
- O mesmo `request_id` executa uma vez só. Quem chegar de novo (reconexão,
  reenvio) recebe o mesmo resultado, marcado `duplicate`.
- O aparelho é o configurado. Música tocando em outro (celular, TV) não é
  mexida nem puxada para cá.
- Abaixar a música enquanto o robô fala devolve o volume de antes, e só se
  ninguém mexeu nele nesse meio-tempo.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from .spotatui import ErroSpotatui, Spotatui

log = logging.getLogger("companion.musica_comandos")

OPERACOES = ("play_generic", "play_track", "play_playlist", "play_found", "queue_track", "search_track",
             "pause", "resume", "next", "previous", "now_playing", "set_volume", "change_volume")
STATUS = ("pending", "playing", "paused", "queued", "found", "not_found", "volume_set", "idle",
          "unavailable", "failed")
_COM_CONSULTA = ("play_track", "play_playlist", "queue_track", "search_track")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_PLAYLIST = re.compile(r"spotify:playlist:[A-Za-z0-9]{1,64}")
_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")
_CONECTIVOS = {"de", "do", "da", "dos", "das", "by"}
_INDISPONIVEL = {"not_installed", "player_off", "auth_expired", "unreachable"}

VERIFICAR_S = 8.0
INTERVALO_S = 0.4
FILA_REPETIDA_S = 600.0
FILA_EXTRA = 3
ESPERA_DUPLICADO_S = 30.0
TRAVA_S = 20.0
ABAFAR_MAX_S = 40.0
ACHADO_VALIDADE_S = 600.0
PEDIDOS_MAX = 200
ARQUIVO = Path(os.environ.get("GARRA_REACHY_CONF", "~/.config/garra-reachy")).expanduser() / "musica.json"


class PedidoInvalido(ValueError):
    pass


def _norm(texto: str | None) -> str:
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


def _titulo_base(titulo: str | None) -> str:
    """"Hotel California - 2013 Remaster" e "Hotel California" são a mesma música."""
    t = re.split(r"\s+-\s+|\s+\(", titulo or "", maxsplit=1)[0]
    return _norm(t)


class Preferencias:
    """Aparelho alvo, playlist padrão e quanto abaixar a música — num arquivo 0600."""

    PADRAO = {"target_device": "spotatui", "default_playlist": None, "duck_percent": 30}

    def __init__(self, arquivo: Path = ARQUIVO) -> None:
        self.arquivo = Path(arquivo)
        self._trava = threading.Lock()

    def ler(self) -> dict[str, Any]:
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dados = {}
        saida = dict(self.PADRAO)
        if isinstance(dados, dict):
            for k in self.PADRAO:
                if k in dados:
                    saida[k] = dados[k]
        return saida

    @staticmethod
    def _validar(dados: Any) -> dict[str, Any]:
        if not isinstance(dados, dict) or not dados or set(dados) - set(Preferencias.PADRAO):
            raise PedidoInvalido("preferências")
        if "target_device" in dados:
            v = dados["target_device"]
            if (not isinstance(v, str) or not v.strip() or len(v) > 64 or _CONTROLE.search(v)
                    or v.startswith("-")):
                raise PedidoInvalido("target_device")
        if "default_playlist" in dados:
            v = dados["default_playlist"]
            if v is not None and (not isinstance(v, str) or not _PLAYLIST.fullmatch(v)):
                raise PedidoInvalido("default_playlist")
        if "duck_percent" in dados:
            v = dados["duck_percent"]
            if not isinstance(v, int) or isinstance(v, bool) or not 5 <= v <= 100:
                raise PedidoInvalido("duck_percent")
        return dados

    def gravar(self, dados: dict[str, Any]) -> dict[str, Any]:
        dados = self._validar(dados)
        with self._trava:
            novo = {**self.ler(), **dados}
            self.arquivo.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.arquivo.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(novo, f, ensure_ascii=False)
            os.replace(tmp, self.arquivo)
            os.chmod(self.arquivo, 0o600)
        return novo


class Comandos:
    def __init__(self, provedor: Spotatui, preferencias: Preferencias | None = None,
                 agora: Callable[[], float] = time.monotonic, dormir: Callable[[float], None] = time.sleep,
                 sorteio: random.Random | None = None, temporizador: Callable | None = None,
                 segundo_plano: Callable | None = None) -> None:
        self.provedor = provedor
        self.preferencias = preferencias or Preferencias()
        self.agora = agora
        self.dormir = dormir
        self.sorteio = sorteio or random.Random()
        self.temporizador = temporizador or self._timer_real
        self.segundo_plano = segundo_plano or self._thread
        self._trava = threading.Lock()                 # um comando no player por vez
        self._trava_pedidos = threading.Lock()
        self._pedidos: OrderedDict[str, dict] = OrderedDict()
        self._achado: tuple[dict, float] | None = None
        self._enfileiradas: dict[str, float] = {}
        self._depois: list[tuple] = []                  # tarefas para quando a trava soltar
        self._abafado: dict | None = None
        self._timer_abafar = None
        self._arquivo_abafado = self.preferencias.arquivo.with_name("musica-abafado.json")
        self._recuperou = False

    @staticmethod
    def _thread(funcao: Callable, *args: Any) -> None:
        threading.Thread(target=funcao, args=args, daemon=True, name="musica-fundo").start()

    @staticmethod
    def _timer_real(segundos: float, funcao: Callable[[], None]):
        t = threading.Timer(segundos, funcao)
        t.daemon = True
        t.start()
        return t

    @property
    def alvo(self) -> str:
        return self.preferencias.ler()["target_device"]

    # ── entrada ───────────────────────────────────────────────────────────
    @staticmethod
    def validar(pedido: Any) -> dict[str, Any]:
        if not isinstance(pedido, dict):
            raise PedidoInvalido("pedido")
        rid, op = pedido.get("request_id"), pedido.get("operation")
        if not isinstance(rid, str) or not _ID.fullmatch(rid):
            raise PedidoInvalido("request_id")
        if op not in OPERACOES:
            raise PedidoInvalido("operation")
        limpo: dict[str, Any] = {"request_id": rid, "operation": op,
                                 "source": pedido.get("source") if pedido.get("source") in ("voz", "chat", "painel")
                                 else "painel", "duck": pedido.get("duck") is True}
        if op in _COM_CONSULTA:
            q = pedido.get("query")
            if not isinstance(q, str) or _CONTROLE.search(q) or not q.strip() or len(q) > 120:
                raise PedidoInvalido("query")
            limpo["query"] = q.strip()
        if op == "set_volume":
            v = pedido.get("volume_percent")
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100:
                raise PedidoInvalido("volume_percent")
            limpo["volume_percent"] = v
        if op == "change_volume":
            v = pedido.get("volume_delta")
            if not isinstance(v, int) or isinstance(v, bool) or v == 0 or not -100 <= v <= 100:
                raise PedidoInvalido("volume_delta")
            limpo["volume_delta"] = v
        return limpo

    def executar(self, pedido: Any) -> dict[str, Any]:
        """Valida, executa uma vez por `request_id` e devolve o resultado conferido."""
        pedido = self.validar(pedido)
        self._recuperar_abafado()
        rid = pedido["request_id"]
        with self._trava_pedidos:
            reg = self._pedidos.get(rid)
            dono = reg is None
            if dono:
                reg = {"feito": threading.Event(), "resultado": None}
                self._pedidos[rid] = reg
                while len(self._pedidos) > PEDIDOS_MAX:
                    self._pedidos.popitem(last=False)
        if not dono:
            if reg["feito"].wait(ESPERA_DUPLICADO_S) and reg["resultado"] is not None:
                return {**reg["resultado"], "duplicate": True}
            return {**self._base(pedido), "status": "pending", "duplicate": True}
        try:
            if not self._trava.acquire(timeout=TRAVA_S):
                resultado = self._falha(pedido, "busy")
            else:
                try:
                    resultado = self._executar(pedido)
                    if pedido["duck"]:
                        # Quem vai falar a confirmação pediu a música baixa: DEPOIS
                        # de conferido, com o volume que a conferência acabou de ler.
                        resultado["ducked"] = self._abafar_pelo_resultado(rid, resultado)
                finally:
                    depois, self._depois = self._depois, []
                    self._trava.release()
                for tarefa in depois:
                    self.segundo_plano(*tarefa)
        except ErroSpotatui as e:
            resultado = self._falha(pedido, e.codigo)
        except Exception:
            log.exception("musica: erro inesperado em %s", pedido["operation"])
            resultado = self._falha(pedido, "provider_error")
        reg["resultado"] = resultado
        reg["feito"].set()
        log.info("musica %s %s → %s%s", rid, pedido["operation"], resultado["status"],
                 f" ({resultado['reason']})" if resultado.get("reason") else "")
        return resultado

    # ── resultado ─────────────────────────────────────────────────────────
    def _base(self, pedido: dict) -> dict[str, Any]:
        return {"request_id": pedido["request_id"], "provider": "spotatui", "operation": pedido["operation"],
                "status": "pending", "reason": None, "device": self.alvo, "track": None, "playlist": None,
                "observed": None, "verified": False, "evidence": None, "already": False, "duplicate": False}

    def _falha(self, pedido: dict, codigo: str) -> dict[str, Any]:
        if codigo == "no_playback":
            status = "idle"
        elif codigo == "not_found":
            status = "not_found"
        elif codigo in _INDISPONIVEL or codigo == "other_device":
            status = "unavailable"
        else:
            status = "failed"
        return {**self._base(pedido), "status": status, "reason": codigo}

    @staticmethod
    def _observado(est: dict | None, avancou: bool | None = None) -> dict[str, Any] | None:
        if est is None:
            return {"state": "idle", "device": None, "progress_s": None, "volume_percent": None,
                    "advancing": False}
        return {"state": est["state"], "device": est["device"], "progress_s": est["progress_s"],
                "volume_percent": est["volume_percent"], "advancing": bool(avancou)}

    @staticmethod
    def _faixa(est: dict | None) -> dict[str, Any] | None:
        if not est:
            return None
        t = est["track"]
        return {"name": t["name"], "artists": list(t["artists"]), "album": t["album"], "uri": t["uri"]}

    def _pronto(self, pedido: dict, status: str, est: dict | None, avancou: bool | None = None,
                **extra: Any) -> dict[str, Any]:
        r = {**self._base(pedido), "status": status, "observed": self._observado(est, avancou),
             "track": self._faixa(est)}
        r.update(extra)
        if r["verified"] and r["evidence"] is None:
            # Lido do player: estado técnico, não prova de que o som saiu na sala.
            r["evidence"] = "player_state"
        return r

    # ── conferência ───────────────────────────────────────────────────────
    def _esperar(self, cond: Callable[[dict | None, dict | None], bool],
                 prazo_s: float = VERIFICAR_S) -> tuple[bool, dict | None, bool]:
        """Lê o estado até `cond(atual, anterior)` valer ou o prazo vencer.

        Devolve (valeu, último estado, o tempo andou entre as duas últimas leituras).
        """
        fim = self.agora() + prazo_s
        anterior = atual = None
        while True:
            try:
                atual = self.provedor.reproducao()
            except ErroSpotatui as e:
                if e.codigo in ("auth_expired", "not_installed"):
                    raise
                atual = None
            if cond(atual, anterior):
                return True, atual, self._avancou(anterior, atual)
            if self.agora() >= fim:
                return False, atual, self._avancou(anterior, atual)
            anterior = atual
            self.dormir(INTERVALO_S)

    @staticmethod
    def _avancou(antes: dict | None, depois: dict | None) -> bool:
        return bool(antes and depois and antes["track"]["uri"] == depois["track"]["uri"]
                    and depois["progress_s"] > antes["progress_s"])

    def _no_alvo(self, est: dict | None, alvo: str) -> bool:
        return bool(est and est["device"] == alvo)

    @staticmethod
    def _mesma_faixa(est: dict | None, faixa: dict) -> bool:
        if not est:
            return False
        t = est["track"]
        if t["uri"] == faixa["uri"]:
            return True
        # A Spotify pode trocar o URI por outro lançamento da mesma faixa.
        return (_titulo_base(t["name"]) == _titulo_base(faixa["name"])
                and {_norm(a) for a in t["artists"]} & {_norm(a) for a in faixa["artists"]} != set())

    def _tocar_conferido(self, pedido: dict, alvo: str, uri: str, confere: Callable[[dict], bool],
                         aleatorio: bool = False, **extra: Any) -> dict[str, Any]:
        """Um play, uma conferência: tocando no alvo, a coisa certa, o tempo andando."""
        def cond(atual, anterior):
            return (self._no_alvo(atual, alvo) and atual["state"] == "playing" and confere(atual)
                    and self._avancou(anterior, atual))
        try:
            self.provedor.tocar(uri, alvo, aleatorio=aleatorio)
        except ErroSpotatui as e:
            if e.codigo != "timeout":
                raise
            # O processo estourou o prazo, mas o comando pode ter chegado: confere
            # antes de dizer que falhou — e não manda de novo.
        ok, est, avancou = self._esperar(cond)
        if ok:
            return self._pronto(pedido, "playing", est, avancou, verified=True, **extra)
        return self._pronto(pedido, "failed", est, avancou, reason="not_verified", **extra)

    # ── as operações ──────────────────────────────────────────────────────
    def _executar(self, pedido: dict) -> dict[str, Any]:
        op = pedido["operation"]
        return getattr(self, f"_op_{op}")(pedido, self.alvo)

    def _buscar(self, consulta: str) -> list[dict]:
        termos = [t for t in consulta.split() if _norm(t) not in _CONECTIVOS]
        return self.provedor.pesquisar(" ".join(termos) or consulta, limite=5)

    @staticmethod
    def _em_paralelo(*funcoes: Callable[[], Any]) -> list[Any]:
        """Cada chamada à CLI custa ~1 s (medido): as leituras independentes vão juntas."""
        resultados: list[Any] = [None] * len(funcoes)
        erros: list[BaseException | None] = [None] * len(funcoes)

        def rodar(i: int, f: Callable[[], Any]) -> None:
            try:
                resultados[i] = f()
            except BaseException as e:  # noqa: BLE001 — devolvido a quem chamou, abaixo
                erros[i] = e

        threads = [threading.Thread(target=rodar, args=(i, f), daemon=True) for i, f in enumerate(funcoes)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for e in erros:
            if e is not None:
                raise e
        return resultados

    def _op_play_track(self, pedido: dict, alvo: str) -> dict[str, Any]:
        achadas, est = self._em_paralelo(lambda: self._buscar(pedido["query"]), self.provedor.reproducao)
        if not achadas:
            return self._falha(pedido, "not_found")
        return self._tocar_faixa(pedido, alvo, achadas[0], est)

    def _tocar_faixa(self, pedido: dict, alvo: str, faixa: dict, est: dict | None | bool = False) -> dict[str, Any]:
        if est is False:
            est = self.provedor.reproducao()
        if self._no_alvo(est, alvo) and est["state"] == "playing" and self._mesma_faixa(est, faixa):
            return self._pronto(pedido, "playing", est, None, verified=True, already=True)
        return self._tocar_conferido(pedido, alvo, faixa["uri"], lambda e: self._mesma_faixa(e, faixa))

    def _op_play_found(self, pedido: dict, alvo: str) -> dict[str, Any]:
        if self._achado is None or self.agora() - self._achado[1] > ACHADO_VALIDADE_S:
            return {**self._falha(pedido, "not_found"), "reason": "no_search"}
        return self._tocar_faixa(pedido, alvo, self._achado[0])

    def _op_search_track(self, pedido: dict, alvo: str) -> dict[str, Any]:
        achadas = self._buscar(pedido["query"])
        if not achadas:
            return self._falha(pedido, "not_found")
        self._achado = (achadas[0], self.agora())
        return {**self._base(pedido), "status": "found", "verified": True,
                "track": {k: achadas[0][k] for k in ("name", "artists", "album", "uri")},
                "results": [{k: a[k] for k in ("name", "artists", "uri")} for a in achadas[:3]]}

    def _op_play_generic(self, pedido: dict, alvo: str) -> dict[str, Any]:
        est = self.provedor.reproducao()
        if self._no_alvo(est, alvo):
            if est["state"] == "playing":
                return self._pronto(pedido, "playing", est, None, verified=True, already=True)
            if est["state"] == "paused":
                r = self._retomar(pedido, alvo, est)
                r["resumed"] = r["status"] == "playing"
                return r
        padrao = self.preferencias.ler()["default_playlist"]
        if padrao:
            return self._tocar_playlist(pedido, alvo, {"uri": padrao, "name": None}, aleatorio=True)
        curtidas = self.provedor.curtidas(50)
        if not curtidas:
            return self._falha(pedido, "not_found")
        faixa = self.sorteio.choice(curtidas)
        r = self._tocar_conferido(pedido, alvo, faixa["uri"], lambda e: self._mesma_faixa(e, faixa))
        if r["status"] == "playing":
            # Faixa avulsa não tem "próxima": mais algumas curtidas vão para a fila,
            # depois da confirmação e fora da trava do pedido.
            extras = [f for f in curtidas if f["uri"] != faixa["uri"]]
            self.sorteio.shuffle(extras)
            self._depois.append((self._encher_fila, [f["uri"] for f in extras[:FILA_EXTRA]], alvo))
        return r

    def _encher_fila(self, uris: list[str], alvo: str) -> None:
        for uri in uris:
            if not self._trava.acquire(timeout=5.0):
                return
            try:
                self.provedor.enfileirar(uri, alvo)
                self._enfileiradas[uri] = self.agora()
            except ErroSpotatui as e:
                log.info("musica: fila da biblioteca parou (%s)", e.codigo)
                return
            finally:
                self._trava.release()

    def _melhor_playlist(self, nome: str) -> dict | None:
        pedido = _norm(nome)
        if not pedido:
            return None
        listas = self.provedor.playlists(50)
        for criterio in (lambda n: n == pedido, lambda n: n.startswith(pedido), lambda n: pedido in n,
                         lambda n: set(pedido.split()) <= set(n.split())):
            for p in listas:
                if criterio(_norm(p["name"])):
                    return p
        return None

    def _op_play_playlist(self, pedido: dict, alvo: str) -> dict[str, Any]:
        escolhida = self._melhor_playlist(pedido["query"])
        if escolhida is None:
            return self._falha(pedido, "not_found")
        return self._tocar_playlist(pedido, alvo, escolhida)

    def _tocar_playlist(self, pedido: dict, alvo: str, playlist: dict, aleatorio: bool = False) -> dict[str, Any]:
        nome = playlist.get("name")
        if nome is None:
            # A playlist padrão é guardada pelo URI; o nome é para a confirmação.
            nome = next((p["name"] for p in self.provedor.playlists(50) if p["uri"] == playlist["uri"]), None)
        antes = self.provedor.reproducao()
        uri_antes = antes["track"]["uri"] if antes else None

        def confere(e: dict) -> bool:
            # A faixa mudou ou recomeçou, e — quando o player diz a playlist — é ela.
            nova = e["track"]["uri"] != uri_antes or e["progress_s"] < 10
            return nova and (e["playlist"] is None or nome is None or _norm(e["playlist"]) == _norm(nome))

        r = self._tocar_conferido(pedido, alvo, playlist["uri"], confere, aleatorio=aleatorio)
        r["playlist"] = {"name": nome, "uri": playlist["uri"]}
        return r

    def _op_queue_track(self, pedido: dict, alvo: str) -> dict[str, Any]:
        achadas = self._buscar(pedido["query"])
        if not achadas:
            return self._falha(pedido, "not_found")
        faixa = achadas[0]
        est = self.provedor.reproducao()
        if est is None:
            return self._falha(pedido, "no_playback")
        if not self._no_alvo(est, alvo):
            return {**self._falha(pedido, "other_device"), "observed": self._observado(est)}
        so_faixa = {k: faixa[k] for k in ("name", "artists", "album", "uri")}
        # A fila não se lê (a CLI não tem, e a do MCP não reflete o que entrou):
        # a repetição se evita pela memória do que ESTE companion enfileirou.
        quando = self._enfileiradas.get(faixa["uri"])
        if quando is not None and self.agora() - quando < FILA_REPETIDA_S:
            return {**self._base(pedido), "status": "queued", "already": True, "evidence": "api_accepted",
                    "track": so_faixa, "observed": self._observado(est)}
        self.provedor.enfileirar(faixa["uri"], alvo)
        self._enfileiradas[faixa["uri"]] = self.agora()
        return {**self._base(pedido), "status": "queued", "evidence": "api_accepted", "track": so_faixa,
                "observed": self._observado(est)}

    def _no_player(self, pedido: dict, alvo: str) -> tuple[dict | None, dict | None]:
        """O estado atual — ou o resultado pronto quando não há o que comandar no alvo."""
        est = self.provedor.reproducao()
        if est is None:
            return None, self._falha(pedido, "no_playback")
        if not self._no_alvo(est, alvo):
            return None, {**self._falha(pedido, "other_device"), "observed": self._observado(est),
                          "track": self._faixa(est)}
        return est, None

    def _op_pause(self, pedido: dict, alvo: str) -> dict[str, Any]:
        est, pronto = self._no_player(pedido, alvo)
        if pronto:
            return pronto
        if est["state"] == "paused":
            return self._pronto(pedido, "paused", est, False, verified=True, already=True)
        self.provedor.alternar(alvo)
        ok, fim, _ = self._esperar(lambda a, _ant: self._no_alvo(a, alvo) and a["state"] == "paused")
        if ok:
            return self._pronto(pedido, "paused", fim, False, verified=True)
        return self._pronto(pedido, "failed", fim, None, reason="not_verified")

    def _retomar(self, pedido: dict, alvo: str, est: dict) -> dict[str, Any]:
        self.provedor.alternar(alvo)
        ok, fim, avancou = self._esperar(lambda a, ant: self._no_alvo(a, alvo) and a["state"] == "playing"
                                         and self._avancou(ant, a))
        if ok:
            return self._pronto(pedido, "playing", fim, avancou, verified=True)
        return self._pronto(pedido, "failed", fim, avancou, reason="not_verified")

    def _op_resume(self, pedido: dict, alvo: str) -> dict[str, Any]:
        est, pronto = self._no_player(pedido, alvo)
        if pronto:
            return pronto
        if est["state"] == "playing":
            return self._pronto(pedido, "playing", est, None, verified=True, already=True)
        return self._retomar(pedido, alvo, est)

    def _op_next(self, pedido: dict, alvo: str) -> dict[str, Any]:
        est, pronto = self._no_player(pedido, alvo)
        if pronto:
            return pronto
        antes, posicao, tocava = est["track"]["uri"], est["progress_s"], est["state"] == "playing"

        def sem_proxima(a: dict) -> bool:
            # Medido: sem fila e sem contexto, a Spotify volta a MESMA faixa ao
            # começo (pausada) em vez de pular.
            return a["track"]["uri"] == antes and a["progress_s"] < 3 and (a["state"] == "paused" or posicao >= 5)

        self.provedor.proxima(alvo)
        ok, fim, avancou = self._esperar(lambda a, _ant: self._no_alvo(a, alvo)
                                         and (a["track"]["uri"] != antes or sem_proxima(a)))
        if ok and fim["track"]["uri"] == antes:
            if not tocava:
                return self._pronto(pedido, "paused", fim, False, verified=True, reason="no_next")
            # A música tocava: volta a tocar de onde estava, em vez de deixar o silêncio.
            try:
                if fim["state"] == "paused":
                    self.provedor.alternar(alvo)
                self.provedor.posicionar(posicao, alvo)
            except ErroSpotatui:
                return self._pronto(pedido, fim["state"], fim, False, verified=True, reason="no_next")
            ok2, fim2, avancou2 = self._esperar(lambda a, ant: self._no_alvo(a, alvo) and a["state"] == "playing"
                                                and a["track"]["uri"] == antes and self._avancou(ant, a))
            return self._pronto(pedido, "playing" if ok2 else (fim2 or fim)["state"], fim2 or fim, avancou2,
                                verified=True, reason="no_next")
        status = (fim["state"] if fim and fim["state"] in ("playing", "paused") else "playing") if ok else "failed"
        return self._pronto(pedido, status, fim, avancou, verified=ok, reason=None if ok else "not_verified")

    def _op_previous(self, pedido: dict, alvo: str) -> dict[str, Any]:
        est, pronto = self._no_player(pedido, alvo)
        if pronto:
            return pronto
        antes = est["track"]["uri"]
        self.provedor.anterior(alvo)
        ok, fim, avancou = self._esperar(lambda a, _ant: self._no_alvo(a, alvo)
                                         and (a["track"]["uri"] != antes or a["progress_s"] < 3))
        status = (fim["state"] if fim and fim["state"] in ("playing", "paused") else "playing") if ok else "failed"
        return self._pronto(pedido, status, fim, avancou, verified=ok, reason=None if ok else "not_verified")

    def _op_now_playing(self, pedido: dict, alvo: str) -> dict[str, Any]:
        est = self.provedor.reproducao()
        if est is None:
            return self._falha(pedido, "no_playback")
        status = est["state"] if est["state"] in ("playing", "paused") else "playing"
        return self._pronto(pedido, status, est, None, verified=True)

    def _volume_do_alvo(self, alvo: str) -> int | None:
        for d in self.provedor.dispositivos():
            if d["name"] == alvo:
                return d["volume_percent"]
        raise ErroSpotatui("player_off")

    def _definir_volume(self, pedido: dict, alvo: str, pct: int, **extra: Any) -> dict[str, Any]:
        self.provedor.volume(pct, alvo)
        fim = self.agora() + VERIFICAR_S
        atual = None
        while True:
            atual = self._volume_do_alvo(alvo)
            if atual == pct or self.agora() >= fim:
                break
            self.dormir(INTERVALO_S)
        ok = atual == pct
        return {**self._base(pedido), "status": "volume_set" if ok else "failed", "verified": ok,
                "evidence": "player_state" if ok else None, "reason": None if ok else "not_verified",
                "volume_percent": atual, **extra}

    def _op_set_volume(self, pedido: dict, alvo: str) -> dict[str, Any]:
        self._esquecer_abafado()
        return self._definir_volume(pedido, alvo, pedido["volume_percent"])

    def _op_change_volume(self, pedido: dict, alvo: str) -> dict[str, Any]:
        abafado = self._esquecer_abafado()
        base = abafado["antes"] if abafado else self._volume_do_alvo(alvo)
        if base is None:
            return self._falha(pedido, "provider_error")
        novo = max(0, min(100, base + pedido["volume_delta"]))
        if novo == base:
            return {**self._base(pedido), "status": "volume_set", "verified": True, "already": True,
                    "reason": "at_limit", "volume_percent": base}
        return self._definir_volume(pedido, alvo, novo)

    # ── abaixar enquanto o robô fala ──────────────────────────────────────
    def _gravar_abafado(self, dados: dict | None) -> None:
        try:
            if dados is None:
                self._arquivo_abafado.unlink(missing_ok=True)
            else:
                self._arquivo_abafado.parent.mkdir(parents=True, exist_ok=True)
                self._arquivo_abafado.write_text(json.dumps(dados), encoding="utf-8")
        except OSError:
            log.warning("musica: não deu para registrar o volume abaixado")

    def _esquecer_abafado(self) -> dict | None:
        ab, self._abafado = self._abafado, None
        if self._timer_abafar is not None:
            self._timer_abafar.cancel()
            self._timer_abafar = None
        if ab is not None:
            self._gravar_abafado(None)
        return ab

    def _recuperar_abafado(self) -> None:
        """Um companion que caiu com a música abaixada devolve o volume ao voltar."""
        if self._recuperou:
            return
        self._recuperou = True
        try:
            dados = json.loads(self._arquivo_abafado.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(dados, dict) and isinstance(dados.get("antes"), int) and isinstance(dados.get("abafado"), int):
            self._abafado = dados
            try:
                self.restaurar(dados.get("id"))
            except ErroSpotatui:
                pass
        else:
            self._gravar_abafado(None)

    def abafar(self, pedido_id: str | None = None) -> dict[str, Any]:
        self._recuperar_abafado()
        if not self._trava.acquire(timeout=1.0):
            return {"ducked": False, "reason": "busy"}
        try:
            return self._abafar(pedido_id, self.provedor.reproducao())
        finally:
            self._trava.release()

    def _abafar_pelo_resultado(self, pedido_id: str, resultado: dict) -> dict[str, Any]:
        visto = resultado.get("observed") or {}
        if resultado["status"] in ("unavailable", "failed", "idle", "not_found", "pending"):
            return {"ducked": False, "reason": "not_playing"}
        try:
            return self._abafar(pedido_id, {"state": visto.get("state"), "device": visto.get("device"),
                                            "volume_percent": visto.get("volume_percent")})
        except ErroSpotatui as e:
            return {"ducked": False, "reason": e.codigo}

    def _abafar(self, pedido_id: str | None, est: dict | None) -> dict[str, Any]:
        """Com a trava já tomada. Abaixa só a música que toca no alvo, e guarda o de antes."""
        if self._abafado is not None:
            return {"ducked": True, "already": True, "from": self._abafado["antes"], "to": self._abafado["abafado"]}
        alvo = self.alvo
        if not (self._no_alvo(est, alvo) and est["state"] == "playing"):
            return {"ducked": False, "reason": "not_playing"}
        v = est["volume_percent"]
        if v is None:
            return {"ducked": False, "reason": "no_volume"}
        d = max(3, round(v * self.preferencias.ler()["duck_percent"] / 100))
        if d >= v:
            return {"ducked": False, "reason": "already_low"}
        self.provedor.volume(d, alvo)
        self._abafado = {"id": pedido_id, "antes": v, "abafado": d, "alvo": alvo}
        self._gravar_abafado(self._abafado)
        self._timer_abafar = self.temporizador(ABAFAR_MAX_S, self._restaurar_sozinho)
        return {"ducked": True, "from": v, "to": d}

    def _restaurar_sozinho(self) -> None:
        try:
            self.restaurar(None)
        except ErroSpotatui:
            log.warning("musica: não deu para devolver o volume sozinho")

    def restaurar(self, pedido_id: str | None = None) -> dict[str, Any]:
        if not self._trava.acquire(timeout=5.0):
            return {"restored": False, "reason": "busy"}
        try:
            ab = self._esquecer_abafado()
            if ab is None:
                return {"restored": False, "reason": "not_ducked"}
            alvo = ab.get("alvo") or self.alvo
            atual = self._volume_do_alvo(alvo)
            if atual != ab["abafado"]:
                return {"restored": False, "reason": "changed_by_user", "volume_percent": atual}
            self.provedor.volume(ab["antes"], alvo)
            return {"restored": True, "volume_percent": ab["antes"]}
        finally:
            self._trava.release()

    # ── o painel ──────────────────────────────────────────────────────────
    def estado(self) -> dict[str, Any]:
        self._recuperar_abafado()
        prefs = self.preferencias.ler()
        alvo = prefs["target_device"]
        e: dict[str, Any] = {
            "provider": "spotatui", "installed": bool(self.provedor.executavel), "version": None,
            "state": "error", "reason": None, "target_device": alvo, "target_available": False,
            "devices": [], "playback": None, "default_playlist": prefs["default_playlist"],
            "duck_percent": prefs["duck_percent"], "operations": list(OPERACOES),
            "voice_control": "owner_opt_in", "ducked": self._abafado is not None,
        }
        if not e["installed"]:
            e["state"] = e["reason"] = "not_installed"
            return e
        e["version"] = self.provedor.versao()
        try:
            e["devices"] = self.provedor.dispositivos()
            e["target_available"] = any(d["name"] == alvo for d in e["devices"])
            e["playback"] = self.provedor.reproducao()
        except ErroSpotatui as erro:
            e["state"] = e["reason"] = erro.codigo
            return e
        e["state"] = "ready" if e["target_available"] else "player_off"
        return e


__all__ = ["ABAFAR_MAX_S", "Comandos", "OPERACOES", "PedidoInvalido", "Preferencias", "STATUS"]
