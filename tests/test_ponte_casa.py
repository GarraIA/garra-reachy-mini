"""A casa na ponte: as rotas por onde o painel do robô configura e consulta.

O token do Home Assistant atravessa a ponte UMA vez, ao configurar; depois
nunca mais aparece — nem no estado, nem em erro, nem em log.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from companion import casa, ponte

TOKEN = "token-de-teste-nao-e-segredo"
AUT = {"Authorization": f"Bearer {TOKEN}"}
HA_TOKEN = "eyJ-token-longo-do-home-assistant-000000000000000000"


class CasaFalsa:
    def __init__(self) -> None:
        self.chamadas: list[tuple] = []
        self.erro: str | None = None
        self.configurada = False

    def _estado(self):
        return {"configured": self.configurada, "url": "http://ha.local:8123" if self.configurada else None,
                "token_present": self.configurada, "home_assistant": None,
                "gateway": {"devices_visible": False, "reachable": True},
                "voice_control": False, "state": "pending" if self.configurada else "not_configured"}

    def estado(self, rapido=False):
        self.chamadas.append(("estado", rapido))
        return self._estado()

    def configurar(self, url, token):
        self.chamadas.append(("configurar", url, token))
        if self.erro:
            raise casa.ErroCasa(self.erro, "de mentira")
        self.configurada = True
        return self._estado()

    def remover(self):
        self.chamadas.append(("remover",))
        self.configurada = False
        return self._estado()

    def entidades(self):
        self.chamadas.append(("entidades",))
        if not self.configurada:
            raise casa.ErroCasa("not_configured")
        return [{"id": "ha:light.sala", "entity_id": "light.sala", "domain": "light", "name": "Luz",
                 "state": "on", "risk": "R1", "capabilities": ["power", "brightness"]}]

    def voz(self, liberar):
        self.chamadas.append(("voz", liberar))
        if not self.configurada:
            raise casa.ErroCasa("not_configured")
        return dict(self._estado(), voice_control=liberar)


@pytest.fixture()
def montar():
    def _montar():
        c = CasaFalsa()
        app = ponte.montar(TOKEN, "chave-gw", casa=c)
        return TestClient(app), c
    return _montar


def test_tudo_exige_token(montar) -> None:
    cliente, c = montar()
    assert cliente.get("/api/casa/estado").status_code == 401
    assert cliente.post("/api/casa/configurar", json={"url": "http://ha.local:8123", "token": HA_TOKEN}).status_code == 401
    assert cliente.delete("/api/casa").status_code == 401
    assert cliente.get("/api/casa/entidades").status_code == 401
    assert cliente.post("/api/casa/voz", json={"enabled": True}).status_code == 401
    assert c.chamadas == []


def test_estado_configurar_entidades_voz_e_remover(montar) -> None:
    cliente, c = montar()
    assert cliente.get("/api/casa/estado", headers=AUT).json()["state"] == "not_configured"
    r = cliente.post("/api/casa/configurar", json={"url": "http://ha.local:8123", "token": HA_TOKEN},
                     headers=AUT)
    assert r.status_code == 200 and r.json()["configured"] is True
    assert ("configurar", "http://ha.local:8123", HA_TOKEN) in c.chamadas
    ents = cliente.get("/api/casa/entidades", headers=AUT).json()
    assert ents["entities"][0]["id"] == "ha:light.sala"
    assert cliente.post("/api/casa/voz", json={"enabled": True}, headers=AUT).json()["voice_control"] is True
    assert cliente.delete("/api/casa", headers=AUT).json()["configured"] is False


@pytest.mark.parametrize("codigo,status", [
    ("invalid_url", 400), ("invalid_token", 400), ("unreachable", 502), ("unauthorized", 401),
    ("config_error", 500), ("gateway_restart_failed", 502),
])
def test_erros_viram_codigo_e_status(montar, codigo, status) -> None:
    cliente, c = montar()
    c.erro = codigo
    r = cliente.post("/api/casa/configurar", json={"url": "http://ha.local:8123", "token": HA_TOKEN},
                     headers=AUT)
    assert r.status_code == status and r.json()["erro"] == codigo


def test_entidades_sem_configuracao_e_409(montar) -> None:
    cliente, _ = montar()
    r = cliente.get("/api/casa/entidades", headers=AUT)
    assert r.status_code == 409 and r.json()["erro"] == "not_configured"


@pytest.mark.parametrize("corpo", [{}, {"url": "http://ha.local:8123"}, {"token": HA_TOKEN},
                                   {"url": 5, "token": HA_TOKEN}, "texto"])
def test_configurar_mal_formado_e_400(montar, corpo) -> None:
    cliente, c = montar()
    assert cliente.post("/api/casa/configurar", json=corpo, headers=AUT).status_code == 400
    assert not any(ch[0] == "configurar" for ch in c.chamadas)


def test_o_token_do_home_assistant_nao_vai_ao_log(montar, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cliente, c = montar()
    c.erro = "unauthorized"
    cliente.post("/api/casa/configurar", json={"url": "http://ha.local:8123", "token": HA_TOKEN}, headers=AUT)
    assert HA_TOKEN not in caplog.text and HA_TOKEN[:12] not in caplog.text



def test_estado_rapido_passa_pela_ponte(montar) -> None:
    cliente, c = montar()
    assert cliente.get("/api/casa/estado?rapido=1", headers=AUT).status_code == 200
    assert cliente.get("/api/casa/estado", headers=AUT).status_code == 200
    assert [x for x in c.chamadas if x[0] == "estado"] == [("estado", True), ("estado", False)]
