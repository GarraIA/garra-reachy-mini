"""A ferramenta de música pela ponte: o que o robô (voz e chat) e o painel usam.

Com o spotatui instalado, as rotas de sempre (`/api/musica/estado`,
`/reproducao`, `/dispositivos`, `/acao`) passam a falar com ele, no MESMO
formato de antes — o painel não precisa de dois dialetos. As novas são as que
só a ferramenta tem: `/comando` (pedido tipado com id), `/abafar`,
`/restaurar`, `/preferencias` e `/playlists`.
"""

from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from companion import musica_comandos as mc
from companion import ponte, spotatui
from spotatui_falso import Relogio, SpotatuiFalso

TOKEN = "token-de-teste-nao-e-segredo"
AUT = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def cena(tmp_path):
    falso = SpotatuiFalso(Relogio())
    sp = spotatui.Spotatui(executavel="/x/spotatui", executar=falso, dir_temp=tmp_path / "tmp")
    sp.fila = lambda: [{"uri": f.uri, "name": f.titulo, "artists": [f.artista]} for f in falso.fila]
    cmd = mc.Comandos(sp, mc.Preferencias(tmp_path / "musica.json"), agora=falso.relogio.agora,
                      dormir=falso.relogio.dormir, sorteio=random.Random(1))
    cliente = TestClient(ponte.montar(TOKEN, "chave-gw", comandos=cmd))
    return cliente, falso, cmd


def test_rotas_novas_exigem_token(cena) -> None:
    cliente, falso, _ = cena
    for metodo, caminho in (("POST", "/api/musica/comando"), ("POST", "/api/musica/abafar"),
                            ("POST", "/api/musica/restaurar"), ("GET", "/api/musica/preferencias"),
                            ("POST", "/api/musica/preferencias"), ("GET", "/api/musica/playlists"),
                            ("GET", "/api/musica/estado"), ("POST", "/api/musica/acao")):
        assert cliente.request(metodo, caminho, json={}).status_code == 401, caminho
    assert falso.chamadas == []


def test_comando_tipado_toca_e_devolve_o_resultado_conferido(cena) -> None:
    cliente, falso, _ = cena
    r = cliente.post("/api/musica/comando", headers=AUT, json={
        "request_id": "voz_1", "operation": "play_track", "query": "Hotel California", "source": "voz"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    d = r.json()
    assert d["status"] == "playing" and d["verified"] is True and d["track"]["artists"] == ["Eagles"]
    # reentrega do mesmo pedido: mesmo resultado, nenhum segundo play
    d2 = cliente.post("/api/musica/comando", headers=AUT, json={
        "request_id": "voz_1", "operation": "play_track", "query": "Hotel California"}).json()
    assert d2["duplicate"] is True and sum(1 for a in falso.chamadas if a[1] == "play") == 1


@pytest.mark.parametrize("corpo", [{}, "texto", [], {"request_id": "voz_1", "operation": "explode"},
                                   {"request_id": "voz 1", "operation": "pause"},
                                   {"request_id": "voz_1", "operation": "play_track", "query": ""}])
def test_comando_mal_formado_e_400(cena, corpo) -> None:
    cliente, falso, _ = cena
    r = cliente.post("/api/musica/comando", headers=AUT, json=corpo)
    assert r.status_code == 400 and r.json() == {"erro": "bad_request"}
    assert falso.chamadas == []


def test_indisponivel_e_200_com_o_estado_dito(cena) -> None:
    cliente, falso, _ = cena
    del falso.aparelhos["spotatui"]
    d = cliente.post("/api/musica/comando", headers=AUT, json={
        "request_id": "voz_2", "operation": "play_generic"}).json()
    assert (d["status"], d["reason"]) == ("unavailable", "player_off")


def test_estado_reproducao_e_dispositivos_no_formato_de_sempre(cena) -> None:
    cliente, falso, _ = cena
    e = cliente.get("/api/musica/estado", headers=AUT).json()
    assert e["provider"] == "spotatui" and e["state"] == "ready" and e["target_device"] == "spotatui"
    assert cliente.get("/api/musica/reproducao", headers=AUT).json() == {}
    falso.por_para_tocar(falso.catalogo[3], "spotatui", progresso_s=42)
    rep = cliente.get("/api/musica/reproducao", headers=AUT).json()
    assert rep["is_playing"] is True and rep["plays_on"] == "spotatui" and rep["plays_on_robot"] is False
    assert rep["item"]["name"] == "Bohemian Rhapsody" and rep["item"]["artists"] == ["Queen"]
    assert rep["progress_ms"] == 42000 and rep["item"]["duration_ms"] == 354000
    devs = cliente.get("/api/musica/dispositivos", headers=AUT).json()["devices"]
    alvo = next(d for d in devs if d["name"] == "spotatui")
    assert alvo["is_active"] is True and alvo["is_target"] is True and alvo["volume_percent"] == 60


def test_acao_do_painel_vira_comando_conferido(cena) -> None:
    cliente, falso, _ = cena
    falso.por_para_tocar(falso.catalogo[0], "spotatui")
    d = cliente.post("/api/musica/acao", headers=AUT, json={"action": "pause"}).json()
    assert d["requested"] == "pause" and d["status"] == "paused" and d["verified"] is True
    d = cliente.post("/api/musica/acao", headers=AUT, json={"action": "play"}).json()
    assert d["status"] == "playing"
    d = cliente.post("/api/musica/acao", headers=AUT, json={"action": "volume", "percent": 35}).json()
    assert d["status"] == "volume_set" and falso.aparelhos["spotatui"] == 35
    r = cliente.post("/api/musica/acao", headers=AUT, json={"action": "transfer", "device_id": "x"})
    assert r.status_code == 400, "no spotatui o aparelho é o alvo configurado, não uma transferência"


def test_abafar_e_restaurar(cena) -> None:
    cliente, falso, _ = cena
    falso.por_para_tocar(falso.catalogo[0], "spotatui")
    assert cliente.post("/api/musica/abafar", headers=AUT, json={"request_id": "voz_3"}).json()["ducked"] is True
    assert falso.aparelhos["spotatui"] == 18
    assert cliente.post("/api/musica/restaurar", headers=AUT, json={"request_id": "voz_3"}).json()["restored"]
    assert falso.aparelhos["spotatui"] == 60


def test_preferencias_e_playlists(cena) -> None:
    cliente, falso, _ = cena
    assert cliente.get("/api/musica/preferencias", headers=AUT).json()["target_device"] == "spotatui"
    r = cliente.post("/api/musica/preferencias", headers=AUT,
                     json={"target_device": "michel-PC", "default_playlist": "spotify:playlist:rock01"})
    assert r.status_code == 200 and r.json()["target_device"] == "michel-PC"
    assert cliente.post("/api/musica/preferencias", headers=AUT, json={"target_device": "--x"}).status_code == 400
    listas = cliente.get("/api/musica/playlists", headers=AUT).json()["playlists"]
    assert {"name": "Rock Nacional", "uri": "spotify:playlist:rock01"} in listas


def test_sem_spotatui_as_rotas_de_sempre_seguem_no_provedor_web(tmp_path) -> None:
    from test_ponte_musica import SpotifyFalso
    web = SpotifyFalso()
    cliente = TestClient(ponte.montar(TOKEN, "chave-gw", musica=web))
    e = cliente.get("/api/musica/estado", headers=AUT).json()
    assert e["provider"] == "spotify_web" and "connect_url" in e
    d = cliente.post("/api/musica/comando", headers=AUT, json={"request_id": "voz_1", "operation": "pause"})
    assert d.status_code == 200 and d.json()["status"] == "unavailable" and d.json()["reason"] == "not_installed"
