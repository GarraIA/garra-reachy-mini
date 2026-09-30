"""Spotify pela ponte (o que o painel do robô usa) e pelo servidor do PC (onde
a conta é conectada, no navegador deste computador).

Nada aqui fala com a Spotify: `musica.Spotify` é substituído. O que se prova é
a fronteira — token na ponte, loopback no PC, e nenhum token da Spotify em
resposta ou log.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from companion import musica, ponte, servidor

TOKEN = "token-de-teste-nao-e-segredo"
AUT = {"Authorization": f"Bearer {TOKEN}"}
CLIENT_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


class SpotifyFalso:
    def __init__(self) -> None:
        self.chamadas: list[tuple] = []
        self.conectado = False
        self.configurado = False
        self.erro: str | None = None

    def _estado(self):
        return {"configured": self.configurado, "connected": self.conectado,
                "user": {"id": "michel", "display_name": "Michel", "product": "premium"} if self.conectado else None,
                "premium": True if self.conectado else None, "scopes": [], "expires_in_s": None,
                "redirect_uri": musica.REDIRECT_URI, "connect_url": musica.CONNECT_URL,
                "voice_control": "spotatui_only"}

    def estado(self):
        return self._estado()

    def configurar(self, client_id):
        self.chamadas.append(("configurar", client_id))
        if self.erro:
            raise musica.ErroMusica(self.erro)
        self.configurado = True
        return self._estado()

    def iniciar_conexao(self):
        self.chamadas.append(("iniciar",))
        if not self.configurado:
            raise musica.ErroMusica("not_configured")
        return {"url": "https://accounts.spotify.com/authorize?state=abc", "state": "abc"}

    def concluir(self, code, state):
        self.chamadas.append(("concluir", code, state))
        if state != "abc":
            raise musica.ErroMusica("invalid_state")
        self.conectado = True
        return self._estado()

    def desconectar(self):
        self.chamadas.append(("desconectar",))
        self.conectado = False
        return self._estado()

    def dispositivos(self):
        if self.erro:
            raise musica.ErroMusica(self.erro, retry_after_s=7 if self.erro == "rate_limited" else None)
        return [{"id": "dev-1", "name": "Celular", "type": "Smartphone", "is_active": True,
                 "volume_percent": 40, "is_restricted": False, "supports_volume": True}]

    def reproducao(self):
        if self.erro:
            raise musica.ErroMusica(self.erro)
        return {"is_playing": True, "plays_on": "Celular", "plays_on_robot": False, "device": None,
                "item": {"name": "Aquarela", "artists": ["Toquinho"]}}

    def acionar(self, acao, **args):
        self.chamadas.append(("acionar", acao, args))
        if self.erro:
            raise musica.ErroMusica(self.erro)
        return {"requested": acao, "accepted": True, "state": self.reproducao()}


@pytest.fixture()
def falso(monkeypatch):
    f = SpotifyFalso()
    monkeypatch.setattr(servidor, "_spotify", f, raising=False)
    return f


@pytest.fixture()
def bridge(falso):
    return TestClient(ponte.montar(TOKEN, "chave-gw", musica=falso)), falso


@pytest.fixture()
def admin(falso):
    return TestClient(servidor.app, base_url="http://127.0.0.1:8125"), falso


# ─── a ponte (painel do robô) ────────────────────────────────────────────────
def test_tudo_na_ponte_exige_token(bridge) -> None:
    cliente, f = bridge
    for metodo, caminho in (("GET", "/api/musica/estado"), ("POST", "/api/musica/configurar"),
                            ("DELETE", "/api/musica"), ("GET", "/api/musica/dispositivos"),
                            ("GET", "/api/musica/reproducao"), ("POST", "/api/musica/acao")):
        assert cliente.request(metodo, caminho, json={}).status_code == 401
    assert f.chamadas == []


def test_configurar_estado_reproducao_acao_e_desconectar(bridge) -> None:
    cliente, f = bridge
    assert cliente.post("/api/musica/configurar", json={"client_id": CLIENT_ID}, headers=AUT).status_code == 200
    e = cliente.get("/api/musica/estado", headers=AUT).json()
    assert e["configured"] is True and e["connected"] is False
    assert e["connect_url"] == "http://127.0.0.1:8125/spotify/conectar"
    f.conectado = True
    assert cliente.get("/api/musica/dispositivos", headers=AUT).json()["devices"][0]["name"] == "Celular"
    assert cliente.get("/api/musica/reproducao", headers=AUT).json()["plays_on"] == "Celular"
    r = cliente.post("/api/musica/acao", json={"action": "volume", "percent": 55}, headers=AUT)
    assert r.status_code == 200 and r.json()["requested"] == "volume"
    assert ("acionar", "volume", {"percent": 55}) in f.chamadas
    r = cliente.post("/api/musica/acao", json={"action": "transfer", "device_id": "dev-1"}, headers=AUT)
    assert ("acionar", "transfer", {"device_id": "dev-1"}) in f.chamadas
    assert cliente.delete("/api/musica", headers=AUT).json()["connected"] is False


@pytest.mark.parametrize("corpo", [{}, {"action": "explode"}, {"action": "volume"}, {"action": "volume", "percent": "x"},
                                   {"action": "transfer"}, "texto"])
def test_acao_mal_formada_e_400(bridge, corpo) -> None:
    cliente, f = bridge
    assert cliente.post("/api/musica/acao", json=corpo, headers=AUT).status_code == 400
    assert not any(c[0] == "acionar" for c in f.chamadas)


@pytest.mark.parametrize("codigo,status", [
    ("not_configured", 409), ("not_connected", 409), ("auth_expired", 401), ("premium_required", 402),
    ("no_active_device", 404), ("rate_limited", 429), ("unreachable", 502), ("timeout", 504),
    ("spotify_error", 502), ("restricted_device", 409),
])
def test_erros_viram_codigo_e_status(bridge, codigo, status) -> None:
    cliente, f = bridge
    f.erro = codigo
    r = cliente.get("/api/musica/dispositivos", headers=AUT)
    assert r.status_code == status and r.json()["erro"] == codigo
    if codigo == "rate_limited":
        assert r.headers.get("Retry-After") == "7"


def test_a_ponte_nao_conecta_a_conta(bridge) -> None:
    """Conectar é no navegador do PC, por loopback: a ponte não tem essa rota."""
    cliente, _ = bridge
    assert cliente.get("/spotify/conectar", headers=AUT).status_code == 404
    assert cliente.get("/api/musica/conectar", headers=AUT).status_code == 404


# ─── o servidor do PC (loopback) ─────────────────────────────────────────────
def test_conectar_redireciona_para_a_spotify(admin) -> None:
    cliente, f = admin
    f.configurado = True
    r = cliente.get("/spotify/conectar", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.spotify.com/authorize")


def test_conectar_sem_client_id_explica(admin) -> None:
    cliente, _ = admin
    r = cliente.get("/spotify/conectar", follow_redirects=False)
    assert r.status_code == 409 and "Client ID" in r.text


def test_callback_conclui_e_mostra_uma_pagina_sem_o_codigo(admin) -> None:
    cliente, f = admin
    f.configurado = True
    r = cliente.get("/spotify/callback", params={"code": "codigo-secreto", "state": "abc"})
    assert r.status_code == 200 and "conectad" in r.text.lower()
    assert "codigo-secreto" not in r.text
    assert ("concluir", "codigo-secreto", "abc") in f.chamadas


def test_callback_com_state_errado_ou_erro_da_spotify(admin) -> None:
    cliente, f = admin
    f.configurado = True
    r = cliente.get("/spotify/callback", params={"code": "x", "state": "zzz"})
    assert r.status_code == 400 and "state" in r.text.lower()
    r = cliente.get("/spotify/callback", params={"error": "access_denied", "state": "abc"})
    assert r.status_code == 400 and "access_denied" in r.text
    assert not any(c[0] == "concluir" and c[1] == "x" and c[2] == "abc" for c in f.chamadas)


def test_o_codigo_do_callback_nao_vai_ao_log(admin, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cliente, f = admin
    f.configurado = True
    cliente.get("/spotify/callback", params={"code": "codigo-secreto-xyz", "state": "abc"})
    # Só o registro do COMPANION: o cliente HTTP do teste anota a URL que
    # chamou. Em produção o uvicorn roda em `warning`, sem log de acesso.
    nosso = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("companion"))
    assert "codigo-secreto-xyz" not in nosso
