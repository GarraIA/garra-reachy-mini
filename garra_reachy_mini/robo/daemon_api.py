"""Cliente HTTP do daemon do Reachy Mini (porta 8000).

O SDK (`ReachyMini`) cobre movimento e mídia, mas várias coisas só existem no
REST do daemon — e algumas existem nos dois com comportamentos diferentes:

  • **moves gravados** (85 emoções + 19 danças): o SDK só sabe tocar um `Move`
    que ele mesmo carregou, rodando o loop de 100 Hz no NOSSO processo. Por
    Wi-Fi isso treme. O REST manda o daemon carregar e tocar localmente;
  • **tracking**: `ReachyMini.start_head_tracking()` é fire-and-forget sobre o
    WebSocket, sem confirmação nenhuma — falha em silêncio se não houver
    servidor de mídia. O REST devolve `{"status": "unavailable", "enabled": false}`,
    que é o que precisamos para não mentir na interface;
  • **apps, motores, specs da câmera**: só existem no REST.

Tudo aqui é síncrono (`requests`) porque quem chama é a thread executora, que é
síncrona de propósito. As rotas async do FastAPI usam `asyncio.to_thread`.

A API do daemon **não tem autenticação** — é por isso que a nossa fica no
loopback por padrão (ver `web/seguranca.py`).
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import quote

import requests

# Bibliotecas oficiais de movimento gravado, baixadas pelo daemon do HuggingFace.
DATASET_EMOCOES = "pollen-robotics/reachy-mini-emotions-library"
DATASET_DANCAS = "pollen-robotics/reachy-mini-dances-library"
DATASETS = (DATASET_EMOCOES, DATASET_DANCAS)

# A lista de moves só muda quando o daemon rebaixa o dataset (padrão: 24 h).
TTL_CATALOGO_S = 900.0


class ErroDaemon(RuntimeError):
    """Falha ao falar com o daemon. Carrega o status HTTP quando houve um."""

    def __init__(self, mensagem: str, status: int | None = None) -> None:
        super().__init__(mensagem)
        self.status = status


class DaemonAPI:
    """Cliente fino do daemon. Sem estado do robô — só transporte."""

    def __init__(self, base_url: str, timeout: float = 6.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._http = requests.Session()
        self._cache_moves: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    # ── transporte ──────────────────────────────────────────────────────────
    def _pedir(
        self,
        metodo: str,
        caminho: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base}{caminho}"
        try:
            r = self._http.request(
                metodo, url, json=json, timeout=timeout or self.timeout
            )
        except requests.RequestException as e:
            raise ErroDaemon(f"{metodo} {caminho}: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise ErroDaemon(
                f"{metodo} {caminho}: HTTP {r.status_code} {r.text[:200]}",
                status=r.status_code,
            )
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def alcancavel(self) -> bool:
        try:
            self._pedir("GET", "/api/daemon/status", timeout=2.5)
            return True
        except ErroDaemon:
            return False

    # ── estado ──────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        return self._pedir("GET", "/api/daemon/status") or {}

    def motores(self) -> str:
        """"enabled" | "disabled" | "gravity_compensation"."""
        return (self._pedir("GET", "/api/motors/status") or {}).get("mode", "unknown")

    def definir_motores(self, modo: str) -> None:
        if modo not in ("enabled", "disabled", "gravity_compensation"):
            raise ErroDaemon(f"modo de motor inválido: {modo}")
        self._pedir("POST", f"/api/motors/set_mode/{modo}")

    def estado_completo(self) -> dict[str, Any]:
        return self._pedir("GET", "/api/state/full") or {}

    def specs_camera(self) -> dict[str, Any]:
        return self._pedir("GET", "/api/camera/specs") or {}

    def status_midia(self) -> dict[str, Any]:
        return self._pedir("GET", "/api/media/status") or {}

    # ── moves gravados ──────────────────────────────────────────────────────
    def listar_moves(self, dataset: str, forcar: bool = False) -> list[str]:
        """Nomes dos moves de um dataset, com cache.

        Nunca embutimos os 85 nomes no código: o daemon é a fonte de verdade e a
        biblioteca é atualizada por fora.
        """
        agora = time.monotonic()
        with self._lock:
            cache = self._cache_moves.get(dataset)
            if cache and not forcar and agora - cache[0] < TTL_CATALOGO_S:
                return list(cache[1])
        nomes = self._pedir(
            "GET", f"/api/move/recorded-move-datasets/list/{dataset}", timeout=25.0
        )
        nomes = [str(n) for n in (nomes or [])]
        with self._lock:
            self._cache_moves[dataset] = (agora, nomes)
        return list(nomes)

    def tocar_move(self, dataset: str, nome: str) -> str:
        """Manda o daemon tocar o move; devolve o uuid da tarefa."""
        caminho = f"/api/move/play/recorded-move-dataset/{dataset}/{quote(nome, safe='')}"
        resposta = self._pedir("POST", caminho, timeout=25.0) or {}
        uuid_ = resposta.get("uuid")
        if not uuid_:
            raise ErroDaemon(f"o daemon não devolveu uuid ao tocar {nome!r}")
        return str(uuid_)

    def moves_rodando(self) -> list[str]:
        itens = self._pedir("GET", "/api/move/running") or []
        return [str(i.get("uuid")) for i in itens if isinstance(i, dict) and i.get("uuid")]

    def parar_move(self, uuid_: str) -> None:
        self._pedir("POST", "/api/move/stop", json={"uuid": uuid_})

    def parar_todos_moves(self) -> int:
        """Para tudo que estiver rodando. Devolve quantos foram parados.

        Best-effort de propósito: num e-stop, um move que já terminou sozinho e
        devolve 404/400 não pode abortar a parada dos outros.
        """
        parados = 0
        try:
            rodando = self.moves_rodando()
        except ErroDaemon:
            return 0
        for u in rodando:
            try:
                self.parar_move(u)
                parados += 1
            except ErroDaemon:
                continue
        return parados

    def tocar_wake_up(self) -> str:
        return str((self._pedir("POST", "/api/move/play/wake_up") or {}).get("uuid", ""))

    def tocar_goto_sleep(self) -> str:
        return str((self._pedir("POST", "/api/move/play/goto_sleep") or {}).get("uuid", ""))

    # ── tracking de rosto (YuNet nativo, dentro do daemon) ──────────────────
    def tracking_ligar(self, peso: float = 1.0) -> dict[str, Any]:
        """Devolve o status REAL: `{"status": "ok"|"unavailable", "enabled": bool}`."""
        return self._pedir(
            "POST", "/api/media/tracking/enable", json={"weight": peso}
        ) or {}

    def tracking_desligar(self) -> dict[str, Any]:
        return self._pedir("POST", "/api/media/tracking/disable") or {}

    def rosto_alvo(self) -> dict[str, Any]:
        resposta = self._pedir("GET", "/api/media/tracking/face") or {}
        return resposta.get("face_target") or {}

    # ── wobbling (balanço da cabeça reativo ao áudio, no daemon) ────────────
    def wobbling_ligar(self) -> None:
        self._pedir("POST", "/api/media/wobbling/enable")

    def wobbling_desligar(self) -> None:
        self._pedir("POST", "/api/media/wobbling/disable")

    # ── apps ────────────────────────────────────────────────────────────────
    def apps_instalados(self) -> list[dict[str, Any]]:
        return self._pedir("GET", "/api/apps/list-available/installed", timeout=20.0) or []

    def app_status(self) -> dict[str, Any] | None:
        return self._pedir("GET", "/api/apps/current-app-status")

    def app_iniciar(self, nome: str) -> dict[str, Any]:
        return self._pedir("POST", f"/api/apps/start-app/{nome}", timeout=30.0) or {}

    def app_parar(self) -> None:
        self._pedir("POST", "/api/apps/stop-current-app", timeout=30.0)

    def app_reiniciar(self) -> dict[str, Any]:
        return self._pedir("POST", "/api/apps/restart-current-app", timeout=40.0) or {}
