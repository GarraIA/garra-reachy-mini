"""Spotify, do lado do PC: conectar a conta e comandar a reprodução.

O que as regras da Spotify vigentes em 30/09/2026 decidem por nós
(developer.spotify.com — redirect_uri, authorization, quota-modes, policy):

- **Redirect** só por HTTPS ou por IP de loopback literal; `localhost` e IP da
  LAN por HTTP são recusados. Então a conta é conectada no navegador DESTE PC,
  em `http://127.0.0.1:8125/spotify/callback`. O painel do robô, no celular,
  não consegue fazer isso — e diz isso em vez de fingir.
- **Authorization Code com PKCE**, sem client secret. O que se guarda é o
  refresh token, em arquivo 0600 fora do repositório. Access token dura 1 h e
  é renovado antes de vencer; refresh revogado desconecta e avisa.
- **Development mode**: até 5 usuários, cada um na allowlist do app no
  dashboard, e o dono precisa de Premium. Os comandos do player exigem
  Premium (403 `PREMIUM_REQUIRED`).
- **A Web API comanda um aparelho que já toca Spotify** (celular, computador).
  O robô NÃO é aparelho de reprodução — isso pede o eSDK, só para parceiros —
  e cada estado devolvido diz onde a música está tocando.
- **Aceito não é tocando**: um 204 diz que o comando entrou; o estado se lê
  depois (`GET /me/player`). Comando não idempotente não é repetido.
- **Voz não controla** (Developer Policy III.3) e nada da Spotify vai ao
  modelo (III.14). O robô reconhece o pedido de música por voz só para
  recusá-lo com a razão e apontar o painel.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable

import httpx

log = logging.getLogger("companion.musica")

CONTAS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
PORTA_ADMIN = 8125
REDIRECT_URI = f"http://127.0.0.1:{PORTA_ADMIN}/spotify/callback"
CONNECT_URL = f"http://127.0.0.1:{PORTA_ADMIN}/spotify/conectar"
ESCOPOS = ("user-read-playback-state", "user-modify-playback-state")
STATE_VALIDADE_S = 600
RENOVAR_ANTES_S = 60
ARQUIVO = Path(os.environ.get("GARRA_REACHY_CONF", "~/.config/garra-reachy")).expanduser() / "spotify.json"
ACOES = ("play", "pause", "next", "previous", "volume", "transfer")
CODIGOS = ("invalid_client_id", "not_configured", "not_connected", "invalid_state", "auth_failed",
           "auth_expired", "premium_required", "no_active_device", "restricted_device",
           "rate_limited", "bad_request", "unreachable", "timeout", "spotify_error")
_CLIENT_ID = re.compile(r"[A-Za-z0-9]{32}")

RECUSA_POR_VOZ = ("Por voz eu não controlo o Spotify: a política da Spotify não permite "
                  "assistente de voz. Use o painel do robô, na aba de música.")
_MUSICA = re.compile(
    r"\b(spotify|musica|música|faixa|playlist|album|álbum|cancao|canção)\b|"
    r"\b(toca|toque|tocar|pausa|pause|pausar|despausa|proxima|próxima|anterior|pula|pular|"
    r"aumenta o volume|abaixa o volume|diminui o volume|volume da musica|volume da música)\b|"
    r"\b(play|pause|next|skip) (some |the |a )?(music|song|track)\b|\bplay some music\b")
_NAO_MUSICA = re.compile(r"\b(campainha|tanque|alarme)\b")


def pede_controle(texto: str) -> bool:
    """Parece pedido de música? Serve para RECUSAR por voz, não para agir."""
    t = (texto or "").lower()
    return bool(t) and bool(_MUSICA.search(t)) and not _NAO_MUSICA.search(t)


class ErroMusica(Exception):
    def __init__(self, codigo: str, detalhe: str = "", retry_after_s: int | None = None) -> None:
        assert codigo in CODIGOS, codigo
        super().__init__(f"{codigo}: {detalhe}" if detalhe else codigo)
        self.codigo = codigo
        self.detalhe = detalhe
        self.retry_after_s = retry_after_s


def _b64url(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode("ascii")


class Spotify:
    def __init__(self, http: httpx.Client | None = None, arquivo: Path = ARQUIVO,
                 agora: Callable[[], float] = time.time, espera_s: float = 0.6) -> None:
        self.http = http or httpx.Client(timeout=15.0)
        self.arquivo = arquivo
        self.agora = agora
        self.espera_s = espera_s
        # As conexões começadas e ainda não concluídas: state → (verificador, quando).
        self._pendentes: dict[str, tuple[str, float]] = {}

    # ── o arquivo ────────────────────────────────────────────────────────────
    def _ler(self) -> dict[str, Any]:
        try:
            dados = json.loads(self.arquivo.read_text(encoding="utf-8"))
            return dados if isinstance(dados, dict) else {}
        except (OSError, ValueError):
            return {}

    def _gravar(self, dados: dict[str, Any]) -> None:
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.arquivo.with_name(f".{self.arquivo.name}.tmp-{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(dados))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.arquivo)

    # ── estado ───────────────────────────────────────────────────────────────
    def estado(self) -> dict[str, Any]:
        d = self._ler()
        conectado = bool(d.get("refresh_token"))
        return {
            "configured": bool(d.get("client_id")),
            "connected": conectado,
            "user": d.get("user") if conectado else None,
            "premium": (d.get("user") or {}).get("product") == "premium" if conectado else None,
            "scopes": list(ESCOPOS) if conectado else [],
            "expires_in_s": max(0, int(d.get("expires_at", 0) - self.agora())) if conectado else None,
            "redirect_uri": REDIRECT_URI,
            "connect_url": CONNECT_URL,
            "voice_control": "blocked_by_policy",
        }

    def configurar(self, client_id: str) -> dict[str, Any]:
        client_id = str(client_id or "").strip()
        if not _CLIENT_ID.fullmatch(client_id):
            raise ErroMusica("invalid_client_id")
        d = self._ler()
        if d.get("client_id") != client_id:
            # Client ID novo: os tokens antigos são de outro app.
            d = {"client_id": client_id}
        self._gravar(d)
        return self.estado()

    # ── conectar (PKCE) ──────────────────────────────────────────────────────
    def iniciar_conexao(self) -> dict[str, str]:
        d = self._ler()
        if not d.get("client_id"):
            raise ErroMusica("not_configured")
        verificador = _b64url(secrets.token_bytes(64))
        desafio = _b64url(hashlib.sha256(verificador.encode("ascii")).digest())
        state = secrets.token_urlsafe(24)
        agora = self.agora()
        self._pendentes = {s: v for s, v in self._pendentes.items() if agora - v[1] < STATE_VALIDADE_S}
        self._pendentes[state] = (verificador, agora)
        params = {"client_id": d["client_id"], "response_type": "code", "redirect_uri": REDIRECT_URI,
                  "scope": " ".join(ESCOPOS), "code_challenge_method": "S256",
                  "code_challenge": desafio, "state": state}
        return {"url": f"{CONTAS}/authorize?{httpx.QueryParams(params)}", "state": state}

    def concluir(self, code: str, state: str) -> dict[str, Any]:
        pendente = self._pendentes.pop(str(state or ""), None)
        if pendente is None or self.agora() - pendente[1] >= STATE_VALIDADE_S:
            raise ErroMusica("invalid_state")
        d = self._ler()
        if not d.get("client_id"):
            raise ErroMusica("not_configured")
        resposta = self._token({"grant_type": "authorization_code", "code": str(code or ""),
                                "redirect_uri": REDIRECT_URI, "client_id": d["client_id"],
                                "code_verifier": pendente[0]})
        if resposta is None:
            raise ErroMusica("auth_failed")
        d.update(access_token=resposta["access_token"],
                 expires_at=self.agora() + int(resposta.get("expires_in", 3600)),
                 refresh_token=resposta.get("refresh_token") or d.get("refresh_token"))
        self._gravar(d)
        d["user"] = self._quem_sou(d["access_token"])
        self._gravar(d)
        log.info("spotify: conta conectada (%s)", (d["user"] or {}).get("product"))
        return self.estado()

    def desconectar(self) -> dict[str, Any]:
        d = self._ler()
        self._gravar({"client_id": d.get("client_id")} if d.get("client_id") else {})
        log.info("spotify: conta desconectada")
        return self.estado()

    # ── tokens ───────────────────────────────────────────────────────────────
    def _token(self, campos: dict[str, str]) -> dict[str, Any] | None:
        try:
            r = self.http.post(f"{CONTAS}/api/token", data=campos, timeout=15.0)
        except httpx.TimeoutException as e:
            raise ErroMusica("timeout", "token") from e
        except httpx.HTTPError as e:
            raise ErroMusica("unreachable", type(e).__name__) from e
        if r.status_code != 200:
            return None
        try:
            dados = r.json()
        except ValueError:
            return None
        return dados if isinstance(dados, dict) and dados.get("access_token") else None

    def _acesso(self) -> str:
        d = self._ler()
        if not d.get("client_id"):
            raise ErroMusica("not_configured")
        if not d.get("refresh_token"):
            raise ErroMusica("not_connected")
        if d.get("access_token") and self.agora() < d.get("expires_at", 0) - RENOVAR_ANTES_S:
            return d["access_token"]
        resposta = self._token({"grant_type": "refresh_token", "refresh_token": d["refresh_token"],
                                "client_id": d["client_id"]})
        if resposta is None:
            # Refresh recusado: a conta foi desconectada do lado de lá (ou o
            # token venceu). Não há como continuar sem a pessoa conectar de novo.
            self._gravar({"client_id": d["client_id"]})
            log.info("spotify: renovação recusada; conta desconectada")
            raise ErroMusica("auth_expired")
        d.update(access_token=resposta["access_token"],
                 expires_at=self.agora() + int(resposta.get("expires_in", 3600)))
        if resposta.get("refresh_token"):
            d["refresh_token"] = resposta["refresh_token"]
        self._gravar(d)
        return d["access_token"]

    # ── a API ────────────────────────────────────────────────────────────────
    def _api(self, metodo: str, caminho: str, **kw) -> httpx.Response:
        token = self._acesso()
        try:
            r = self.http.request(metodo, f"{API}{caminho}", headers={"Authorization": f"Bearer {token}"},
                                  timeout=15.0, **kw)
        except httpx.TimeoutException as e:
            raise ErroMusica("timeout", caminho) from e
        except httpx.HTTPError as e:
            raise ErroMusica("unreachable", type(e).__name__) from e
        if r.status_code == 429:
            try:
                espera = int(r.headers.get("Retry-After", "0"))
            except ValueError:
                espera = None
            raise ErroMusica("rate_limited", retry_after_s=espera)
        if r.status_code in (401,):
            raise ErroMusica("auth_expired")
        if r.status_code in (403, 404):
            razao = ""
            try:
                razao = str(((r.json() or {}).get("error") or {}).get("reason") or "")
            except ValueError:
                pass
            if razao == "PREMIUM_REQUIRED":
                raise ErroMusica("premium_required")
            if razao == "NO_ACTIVE_DEVICE":
                raise ErroMusica("no_active_device")
            if r.status_code == 403:
                raise ErroMusica("restricted_device" if "restrict" in razao.lower() else "spotify_error",
                                 razao or "403")
            raise ErroMusica("spotify_error", razao or "404")
        if r.status_code >= 400:
            raise ErroMusica("spotify_error", f"HTTP {r.status_code}")
        return r

    def _quem_sou(self, token: str) -> dict[str, Any] | None:
        try:
            r = self.http.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"}, timeout=15.0)
            if r.status_code != 200:
                return None
            d = r.json()
            return {"id": d.get("id"), "display_name": d.get("display_name"), "product": d.get("product")}
        except (httpx.HTTPError, ValueError):
            return None

    @staticmethod
    def _aparelho(d: dict | None) -> dict[str, Any] | None:
        if not isinstance(d, dict):
            return None
        return {"id": d.get("id"), "name": d.get("name"), "type": d.get("type"),
                "volume_percent": d.get("volume_percent")}

    def dispositivos(self) -> list[dict[str, Any]]:
        r = self._api("GET", "/me/player/devices")
        saida = []
        for d in (r.json().get("devices") or []):
            saida.append({"id": d.get("id"), "name": d.get("name"), "type": d.get("type"),
                          "is_active": bool(d.get("is_active")), "volume_percent": d.get("volume_percent"),
                          "is_restricted": bool(d.get("is_restricted")),
                          "supports_volume": bool(d.get("supports_volume", True))})
        return saida

    def reproducao(self) -> dict[str, Any] | None:
        """O que está tocando e ONDE. `None` quando não há reprodução ativa."""
        r = self._api("GET", "/me/player")
        if r.status_code == 204 or not r.content:
            return None
        d = r.json()
        item = d.get("item") if isinstance(d.get("item"), dict) else None
        aparelho = self._aparelho(d.get("device"))
        imagens = ((item or {}).get("album") or {}).get("images") or []
        return {
            "is_playing": bool(d.get("is_playing")),
            "device": aparelho,
            "plays_on": (aparelho or {}).get("name"),
            "plays_on_robot": False,
            "progress_ms": d.get("progress_ms"),
            "shuffle": bool(d.get("shuffle_state")),
            "repeat": d.get("repeat_state"),
            "item": None if not item else {
                "name": item.get("name"),
                "artists": [a.get("name") for a in item.get("artists") or [] if isinstance(a, dict)],
                "album": (item.get("album") or {}).get("name"),
                "duration_ms": item.get("duration_ms"),
                "image": imagens[0].get("url") if imagens and isinstance(imagens[0], dict) else None,
            },
        }

    def acionar(self, acao: str, **args: Any) -> dict[str, Any]:
        """Manda o comando UMA vez e lê o estado depois. Aceito não é tocando."""
        if acao not in ACOES:
            raise ErroMusica("bad_request", f"ação {acao!r}")
        params: dict[str, Any] = {}
        corpo: dict[str, Any] | None = None
        metodo, caminho = "PUT", f"/me/player/{acao}"
        if acao in ("next", "previous"):
            metodo = "POST"
        elif acao == "volume":
            pct = args.get("percent")
            if not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100:
                raise ErroMusica("bad_request", "percent")
            params = {"volume_percent": pct}
        elif acao == "transfer":
            alvo = args.get("device_id")
            if not isinstance(alvo, str) or not alvo:
                raise ErroMusica("bad_request", "device_id")
            caminho, corpo = "/me/player", {"device_ids": [alvo], "play": bool(args.get("play", True))}
        self._api(metodo, caminho, params=params or None, json=corpo)
        if self.espera_s:
            time.sleep(self.espera_s)
        try:
            estado = self.reproducao()
        except ErroMusica as e:
            estado = {"unknown": e.codigo}
        return {"requested": acao, "accepted": True, "state": estado}


__all__ = ["ACOES", "CODIGOS", "CONNECT_URL", "ErroMusica", "ESCOPOS", "RECUSA_POR_VOZ",
           "REDIRECT_URI", "STATE_VALIDADE_S", "Spotify", "pede_controle"]
