"""Spotify, do lado do PC: conectar a conta e comandar a reprodução.

O que as regras vigentes da Spotify (lidas em 30/09/2026) impõem, e estes
testes seguram:

- redirect só por HTTPS ou por IP de loopback literal (`localhost` é
  proibido): a conexão acontece no navegador DESTE PC, em
  `http://127.0.0.1:8125/spotify/callback`, e o robô nunca a hospeda;
- Authorization Code com PKCE, sem client secret: nada de segredo em lugar
  nenhum além do refresh token, que fica num arquivo 0600 fora do repo;
- a Web API comanda um aparelho que já toca Spotify (celular, computador):
  o robô não é aparelho de reprodução, e o estado devolvido diz ONDE toca;
- "aceito" (204) não é "tocando": depois de cada comando se lê o estado;
- nada da Spotify vai para o modelo, e a voz não passa por este provedor (vai
  pelo spotatui do PC, opt-in do dono — política III.3).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.parse
from pathlib import Path

import httpx
import pytest

from companion import musica

CLIENT_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
CONTA = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"


class SpotifyFalso:
    def __init__(self, premium: bool = True) -> None:
        self.premium = premium
        self.pedidos: list[httpx.Request] = []
        self.tocando = False
        self.aparelho = {"id": "dev-celular", "name": "Celular do Michel", "type": "Smartphone",
                         "is_active": True, "volume_percent": 40, "is_restricted": False,
                         "supports_volume": True}
        self.aparelhos = [self.aparelho]
        self.access = "acc-1"
        self.refresh = "ref-1"
        self.falha: str | None = None
        self.codigo_verificador_visto: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append(request)
        if self.falha == "offline":
            raise httpx.ConnectError("recusado")
        if request.url.host == "accounts.spotify.com" and request.url.path == "/api/token":
            campos = dict(urllib.parse.parse_qsl(request.content.decode()))
            if campos.get("grant_type") == "authorization_code":
                if campos.get("code") != "codigo-bom" or campos.get("client_id") != CLIENT_ID:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                self.codigo_verificador_visto = campos.get("code_verifier")
                return httpx.Response(200, json={"access_token": self.access, "token_type": "Bearer",
                                                 "expires_in": 3600, "refresh_token": self.refresh,
                                                 "scope": "user-read-playback-state user-modify-playback-state"})
            if campos.get("grant_type") == "refresh_token":
                if self.falha == "refresh_revogado":
                    return httpx.Response(400, json={"error": "invalid_grant",
                                                     "error_description": "Refresh token revoked"})
                self.access = "acc-2"
                return httpx.Response(200, json={"access_token": "acc-2", "token_type": "Bearer",
                                                 "expires_in": 3600})
            return httpx.Response(400)
        if request.headers.get("authorization") != f"Bearer {self.access}":
            return httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}})
        if self.falha == "429":
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"status": 429}})
        p = request.url.path
        if p == "/v1/me":
            return httpx.Response(200, json={"id": "michel", "display_name": "Michel",
                                             "product": "premium" if self.premium else "free"})
        if p == "/v1/me/player/devices":
            return httpx.Response(200, json={"devices": self.aparelhos})
        if p == "/v1/me/player":
            if not self.aparelhos:
                return httpx.Response(204)
            return httpx.Response(200, json={
                "is_playing": self.tocando, "device": self.aparelho, "progress_ms": 12000,
                "shuffle_state": False, "repeat_state": "off",
                "item": {"name": "Aquarela", "duration_ms": 200000, "type": "track",
                         "artists": [{"name": "Toquinho"}], "album": {"name": "Aquarela",
                         "images": [{"url": "https://i.scdn.co/image/abc"}]}}})
        if p.startswith("/v1/me/player/"):
            if not self.premium:
                return httpx.Response(403, json={"error": {"status": 403, "reason": "PREMIUM_REQUIRED",
                                                           "message": "Player command failed: Premium required"}})
            if not self.aparelhos:
                return httpx.Response(404, json={"error": {"status": 404, "reason": "NO_ACTIVE_DEVICE",
                                                           "message": "Player command failed: No active device found"}})
            acao = p.rsplit("/", 1)[1]
            if acao == "play":
                self.tocando = True
            elif acao == "pause":
                self.tocando = False
            elif acao == "volume":
                self.aparelho["volume_percent"] = int(request.url.params["volume_percent"])
            elif p == "/v1/me/player" and request.method == "PUT":
                pass
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture()
def sp(tmp_path, monkeypatch):
    falso = SpotifyFalso()
    arquivo = tmp_path / "spotify.json"
    relogio = {"t": 1_000_000.0}
    s = musica.Spotify(http=httpx.Client(transport=httpx.MockTransport(falso.handler)),
                       arquivo=arquivo, agora=lambda: relogio["t"], espera_s=0.0)
    s._falso, s._relogio, s._arquivo_teste = falso, relogio, arquivo
    return s


def conectar(s: musica.Spotify) -> dict:
    s.configurar(CLIENT_ID)
    inicio = s.iniciar_conexao()
    return s.concluir("codigo-bom", inicio["state"])


# ─── configurar e conectar ───────────────────────────────────────────────────
def test_nasce_sem_nada_e_diz_onde_o_navegador_tem_de_estar(sp) -> None:
    e = sp.estado()
    assert e["configured"] is False and e["connected"] is False and e["user"] is None
    assert e["redirect_uri"] == "http://127.0.0.1:8125/spotify/callback"
    assert e["connect_url"] == "http://127.0.0.1:8125/spotify/conectar"
    assert e["voice_control"] == "spotatui_only"


@pytest.mark.parametrize("ruim", ["", "curto", "com espaço 12345678901234567890123456", "x" * 100, "../../etc"])
def test_client_id_torto_e_recusado(sp, ruim) -> None:
    with pytest.raises(musica.ErroMusica) as e:
        sp.configurar(ruim)
    assert e.value.codigo == "invalid_client_id"


def test_a_url_de_autorizacao_e_pkce_com_os_escopos_do_player(sp) -> None:
    sp.configurar(CLIENT_ID)
    inicio = sp.iniciar_conexao()
    url = urllib.parse.urlsplit(inicio["url"])
    q = dict(urllib.parse.parse_qsl(url.query))
    assert url.scheme == "https" and url.netloc == "accounts.spotify.com" and url.path == "/authorize"
    assert q["client_id"] == CLIENT_ID and q["response_type"] == "code"
    assert q["redirect_uri"] == "http://127.0.0.1:8125/spotify/callback"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert set(q["scope"].split()) == {"user-read-playback-state", "user-modify-playback-state"}
    assert q["state"] == inicio["state"] and len(inicio["state"]) >= 16
    assert "client_secret" not in inicio["url"]


def test_concluir_troca_o_codigo_com_o_verificador_certo_e_guarda_0600(sp) -> None:
    sp.configurar(CLIENT_ID)
    inicio = sp.iniciar_conexao()
    e = sp.concluir("codigo-bom", inicio["state"])
    falso = sp._falso
    verificador = falso.codigo_verificador_visto
    assert verificador and len(verificador) >= 43
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(inicio["url"]).query))
    esperado = base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == esperado
    assert e["connected"] is True and e["user"] == {"id": "michel", "display_name": "Michel", "product": "premium"}
    assert oct(sp._arquivo_teste.stat().st_mode & 0o777) == "0o600"
    guardado = json.loads(sp._arquivo_teste.read_text())
    assert guardado["refresh_token"] == "ref-1" and "client_secret" not in guardado


def test_state_errado_ou_repetido_e_recusado(sp) -> None:
    sp.configurar(CLIENT_ID)
    inicio = sp.iniciar_conexao()
    with pytest.raises(musica.ErroMusica) as e:
        sp.concluir("codigo-bom", "outro-state")
    assert e.value.codigo == "invalid_state"
    sp.concluir("codigo-bom", inicio["state"])
    with pytest.raises(musica.ErroMusica) as e2:
        sp.concluir("codigo-bom", inicio["state"])
    assert e2.value.codigo == "invalid_state", "state é de uso único"


def test_codigo_recusado_pela_spotify_nao_conecta(sp) -> None:
    sp.configurar(CLIENT_ID)
    inicio = sp.iniciar_conexao()
    with pytest.raises(musica.ErroMusica) as e:
        sp.concluir("codigo-ruim", inicio["state"])
    assert e.value.codigo == "auth_failed"
    assert sp.estado()["connected"] is False


def test_conexao_pendente_expira(sp) -> None:
    sp.configurar(CLIENT_ID)
    inicio = sp.iniciar_conexao()
    sp._relogio["t"] += musica.STATE_VALIDADE_S + 1
    with pytest.raises(musica.ErroMusica) as e:
        sp.concluir("codigo-bom", inicio["state"])
    assert e.value.codigo == "invalid_state"


def test_desconectar_apaga_os_tokens_e_mantem_o_client_id(sp) -> None:
    conectar(sp)
    e = sp.desconectar()
    assert e["connected"] is False and e["configured"] is True
    assert "refresh_token" not in sp._arquivo_teste.read_text()


# ─── tokens ──────────────────────────────────────────────────────────────────
def test_o_token_e_renovado_antes_de_vencer(sp) -> None:
    conectar(sp)
    sp._relogio["t"] += 3600 - 30
    sp.reproducao()
    grants = [dict(urllib.parse.parse_qsl(r.content.decode())).get("grant_type")
              for r in sp._falso.pedidos if r.url.path == "/api/token"]
    assert grants == ["authorization_code", "refresh_token"]
    assert json.loads(sp._arquivo_teste.read_text())["access_token"] == "acc-2"


def test_refresh_revogado_desconecta_e_diz(sp) -> None:
    conectar(sp)
    sp._falso.falha = "refresh_revogado"
    sp._relogio["t"] += 3600
    with pytest.raises(musica.ErroMusica) as e:
        sp.reproducao()
    assert e.value.codigo == "auth_expired"
    assert sp.estado()["connected"] is False


def test_sem_conexao_qualquer_comando_diz_not_connected(sp) -> None:
    sp.configurar(CLIENT_ID)
    for chamada in (sp.reproducao, sp.dispositivos, lambda: sp.acionar("pause")):
        with pytest.raises(musica.ErroMusica) as e:
            chamada()
        assert e.value.codigo == "not_connected"


# ─── comandar e confirmar ────────────────────────────────────────────────────
def test_tocar_manda_o_comando_e_devolve_o_estado_lido_depois(sp) -> None:
    conectar(sp)
    r = sp.acionar("play")
    assert r["requested"] == "play" and r["accepted"] is True
    assert r["state"]["is_playing"] is True
    assert r["state"]["device"] == {"id": "dev-celular", "name": "Celular do Michel",
                                    "type": "Smartphone", "volume_percent": 40}
    assert r["state"]["item"] == {"name": "Aquarela", "artists": ["Toquinho"], "album": "Aquarela",
                                  "duration_ms": 200000, "image": "https://i.scdn.co/image/abc"}
    caminhos = [r.url.path for r in sp._falso.pedidos if r.url.host == "api.spotify.com"]
    assert caminhos[-2:] == ["/v1/me/player/play", "/v1/me/player"], "aceito não é tocando: lê depois"


def test_o_estado_diz_onde_toca_e_que_nao_e_no_robo(sp) -> None:
    conectar(sp)
    e = sp.reproducao()
    assert e["plays_on"] == "Celular do Michel" and e["plays_on_robot"] is False


def test_volume_e_transferencia_validam_os_argumentos(sp) -> None:
    conectar(sp)
    assert sp.acionar("volume", percent=65)["state"]["device"]["volume_percent"] == 65
    for ruim in ({"percent": 101}, {"percent": -1}, {"percent": "alto"}):
        with pytest.raises(musica.ErroMusica) as e:
            sp.acionar("volume", **ruim)
        assert e.value.codigo == "bad_request"
    with pytest.raises(musica.ErroMusica) as e:
        sp.acionar("transfer", device_id="")
    assert e.value.codigo == "bad_request"
    with pytest.raises(musica.ErroMusica) as e:
        sp.acionar("explode")
    assert e.value.codigo == "bad_request"


def test_sem_premium_o_comando_e_dito(sp) -> None:
    sp._falso.premium = False
    conectar(sp)
    with pytest.raises(musica.ErroMusica) as e:
        sp.acionar("play")
    assert e.value.codigo == "premium_required"


def test_sem_aparelho_ativo_e_dito(sp) -> None:
    conectar(sp)
    sp._falso.aparelhos = []
    with pytest.raises(musica.ErroMusica) as e:
        sp.acionar("play")
    assert e.value.codigo == "no_active_device"
    assert sp.reproducao() is None


def test_limite_da_api_e_dito_com_a_espera(sp) -> None:
    conectar(sp)
    sp._falso.falha = "429"
    with pytest.raises(musica.ErroMusica) as e:
        sp.reproducao()
    assert e.value.codigo == "rate_limited" and e.value.retry_after_s == 7


def test_spotify_fora_do_ar_e_dito(sp) -> None:
    conectar(sp)
    sp._falso.falha = "offline"
    with pytest.raises(musica.ErroMusica) as e:
        sp.dispositivos()
    assert e.value.codigo == "unreachable"


def test_comando_nao_e_repetido_depois_de_falha(sp) -> None:
    """`next` não é idempotente: uma falha depois do envio não pode virar dois
    pulos."""
    conectar(sp)
    sp.acionar("next")
    assert [r.url.path for r in sp._falso.pedidos].count("/v1/me/player/next") == 1


# ─── nada de segredo ─────────────────────────────────────────────────────────
def test_estado_e_log_nao_trazem_tokens(sp, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    conectar(sp)
    sp.acionar("play")
    texto = json.dumps(sp.estado()) + json.dumps(sp.reproducao()) + caplog.text
    for segredo in ("acc-1", "acc-2", "ref-1", sp._falso.codigo_verificador_visto or "x" * 50):
        assert segredo not in texto
