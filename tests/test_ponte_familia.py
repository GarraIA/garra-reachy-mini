"""A família na ponte: o painel do robô cadastra e o robô reconhece por aqui.

A imagem chega em base64 (como em `/api/olhar`), vira bytes, vai ao serviço e
morre. Nada dela em log, nem em erro.
"""

from __future__ import annotations

import base64
import logging

import pytest
from fastapi.testclient import TestClient

from companion import familia, ponte

TOKEN = "token-de-teste-nao-e-segredo"
AUT = {"Authorization": f"Bearer {TOKEN}"}
JPEG = b"\xff\xd8" + b"\x07" * 64 + b"\xff\xd9"
B64 = base64.b64encode(JPEG).decode()
CONSENTE = {"accepted": True, "by": "Maria", "minor": False}
PERFIL = {"id": "a1b2c3d4e5f6", "name": "Maria", "active": True, "samples": 1, "samples_readable": True,
          "consent": dict(CONSENTE, guardian=None, at="2026-09-30T05:00:00+00:00"),
          "created_at": "2026-09-30T05:00:00+00:00"}


class FamiliaFalsa:
    def __init__(self) -> None:
        self.chamadas: list[tuple] = []
        self.erro: str | None = None

    def _talvez(self):
        if self.erro:
            raise familia.ErroFamilia(self.erro, "de mentira")

    def estado(self):
        self.chamadas.append(("estado",))
        return {"state": "ok", "engine": "falso", "profiles": 1, "unreadable_profiles": 0,
                "threshold": 0.45, "margin": 0.1, "max_samples": 5}

    def perfis(self):
        self.chamadas.append(("perfis",)); self._talvez()
        return [PERFIL]

    def criar(self, nome, consent):
        self.chamadas.append(("criar", nome, consent)); self._talvez()
        return dict(PERFIL, name=nome, samples=0)

    def ativo(self, perfil_id, ativo):
        self.chamadas.append(("ativo", perfil_id, ativo)); self._talvez()
        return dict(PERFIL, active=ativo)

    def apagar(self, perfil_id):
        self.chamadas.append(("apagar", perfil_id)); self._talvez()
        return {"ok": True}

    def limpar_amostras(self, perfil_id):
        self.chamadas.append(("limpar", perfil_id)); self._talvez()
        return {"ok": True, "removed": 1}

    def amostra(self, perfil_id, jpeg):
        self.chamadas.append(("amostra", perfil_id, jpeg)); self._talvez()
        return {"ok": True, "samples": 2, "quality": {"size_px": 150, "sharpness": 80.0, "brightness": 100.0}}

    def reconhecer(self, jpeg):
        self.chamadas.append(("reconhecer", jpeg)); self._talvez()
        return {"faces": [{"box": [0, 0, 100, 100], "known": True, "profile_id": "a1b2c3d4e5f6", "name": "Maria",
                           "score": 0.8, "reason": None}], "ms": 9.0}


@pytest.fixture()
def montar():
    def _montar():
        f = FamiliaFalsa()
        app = ponte.montar(TOKEN, "chave-gw", familia=f)
        return TestClient(app), f
    return _montar


def test_tudo_exige_token(montar) -> None:
    cliente, f = montar()
    assert cliente.get("/api/rostos/estado").status_code == 401
    assert cliente.get("/api/rostos/perfis").status_code == 401
    assert cliente.post("/api/rostos/perfis", json={"name": "Maria", "consent": CONSENTE}).status_code == 401
    assert cliente.patch("/api/rostos/perfis/a1b2c3d4e5f6", json={"active": False}).status_code == 401
    assert cliente.delete("/api/rostos/perfis/a1b2c3d4e5f6").status_code == 401
    assert cliente.post("/api/rostos/perfis/a1b2c3d4e5f6/amostras", json={"imagem": B64}).status_code == 401
    assert cliente.delete("/api/rostos/perfis/a1b2c3d4e5f6/amostras").status_code == 401
    assert cliente.post("/api/rostos/reconhecer", json={"imagem": B64}).status_code == 401
    assert f.chamadas == []


def test_estado_e_perfis(montar) -> None:
    cliente, f = montar()
    r = cliente.get("/api/rostos/estado", headers=AUT)
    assert r.status_code == 200 and r.json()["state"] == "ok"
    assert r.headers["cache-control"] == "no-store"
    r = cliente.get("/api/rostos/perfis", headers=AUT)
    assert r.status_code == 200 and r.json() == {"profiles": [PERFIL]}


def test_criar_ativar_apagar_limpar(montar) -> None:
    cliente, f = montar()
    r = cliente.post("/api/rostos/perfis", json={"name": "João", "consent": CONSENTE}, headers=AUT)
    assert r.status_code == 201 and r.json()["profile"]["name"] == "João"
    assert f.chamadas[-1] == ("criar", "João", CONSENTE)
    r = cliente.patch("/api/rostos/perfis/a1b2c3d4e5f6", json={"active": False}, headers=AUT)
    assert r.status_code == 200 and r.json()["profile"]["active"] is False
    assert cliente.delete("/api/rostos/perfis/a1b2c3d4e5f6", headers=AUT).json() == {"ok": True}
    assert cliente.delete("/api/rostos/perfis/a1b2c3d4e5f6/amostras", headers=AUT).json() == {"ok": True, "removed": 1}


@pytest.mark.parametrize("caminho,corpo", [
    ("/api/rostos/perfis", {"consent": CONSENTE}),
    ("/api/rostos/perfis", {"name": 5, "consent": CONSENTE}),
    ("/api/rostos/perfis", {"name": "Maria", "consent": "sim"}),
    ("/api/rostos/perfis", "nao-e-objeto"),
])
def test_corpo_torto_e_400_sem_chamar_o_servico(montar, caminho, corpo) -> None:
    cliente, f = montar()
    r = cliente.post(caminho, json=corpo, headers=AUT)
    assert r.status_code == 400 and r.json()["erro"] == "bad_request"
    assert f.chamadas == []


def test_ativo_precisa_de_booleano(montar) -> None:
    cliente, f = montar()
    r = cliente.patch("/api/rostos/perfis/a1b2c3d4e5f6", json={"active": "sim"}, headers=AUT)
    assert r.status_code == 400 and f.chamadas == []


def test_perfil_id_torto_nao_vira_caminho(montar) -> None:
    cliente, f = montar()
    r = cliente.delete("/api/rostos/perfis/..%2Fsaude", headers=AUT)
    assert r.status_code in (400, 404) and f.chamadas == []


def test_amostra_e_reconhecer_levam_os_bytes(montar) -> None:
    cliente, f = montar()
    r = cliente.post("/api/rostos/perfis/a1b2c3d4e5f6/amostras", json={"imagem": B64}, headers=AUT)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["samples"] == 2
    assert f.chamadas[-1] == ("amostra", "a1b2c3d4e5f6", JPEG)
    r = cliente.post("/api/rostos/reconhecer", json={"imagem": B64}, headers=AUT)
    assert r.status_code == 200 and r.json()["faces"][0]["name"] == "Maria"
    assert f.chamadas[-1] == ("reconhecer", JPEG)


@pytest.mark.parametrize("corpo", [{"imagem": "@@@"}, {"imagem": ""}, {}, {"imagem": 5}])
def test_imagem_torta_e_400_bad_image(montar, corpo) -> None:
    cliente, f = montar()
    r = cliente.post("/api/rostos/reconhecer", json=corpo, headers=AUT)
    assert r.status_code == 400 and r.json()["erro"] == "bad_image"
    assert f.chamadas == []


def test_imagem_grande_demais_e_413(montar) -> None:
    cliente, f = montar()
    grande = base64.b64encode(b"\xff\xd8" + b"x" * (ponte.IMAGEM_B64_MAX + 10) + b"\xff\xd9").decode()
    r = cliente.post("/api/rostos/reconhecer", json={"imagem": grande}, headers=AUT)
    assert r.status_code == 413 and f.chamadas == []


@pytest.mark.parametrize("codigo,status", [
    ("not_found", 404), ("consent_required", 400), ("guardian_required", 400), ("invalid_name", 400),
    ("bad_image", 400), ("service_offline", 503), ("timeout", 504), ("service_error", 502),
])
def test_erro_do_servico_vira_status(montar, codigo, status) -> None:
    cliente, f = montar()
    f.erro = codigo
    r = cliente.post("/api/rostos/reconhecer", json={"imagem": B64}, headers=AUT)
    assert r.status_code == status
    assert r.json() == {"erro": codigo, "detalhe": "de mentira"}


def test_nada_da_imagem_vai_ao_log(montar, caplog) -> None:
    cliente, f = montar()
    f.erro = "service_offline"
    with caplog.at_level(logging.DEBUG, logger="companion.ponte"):
        cliente.post("/api/rostos/reconhecer", json={"imagem": B64}, headers=AUT)
        f.erro = None
        cliente.post("/api/rostos/perfis/a1b2c3d4e5f6/amostras", json={"imagem": B64}, headers=AUT)
    texto = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("companion"))
    assert B64[:24] not in texto and "Maria" not in texto
