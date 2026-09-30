"""A visão na ponte: a rota por onde o quadro do robô chega ao PC.

Mesmo token da ponte, mesma fronteira. O que muda: é a única rota que recebe
uma IMAGEM da casa — e ela não pode parar em log, em disco, nem sair para uma
URL que não seja a do provedor combinado.
"""

from __future__ import annotations

import base64
import logging

import pytest
from fastapi.testclient import TestClient

from companion import ponte, visao

TOKEN = "token-de-teste-nao-e-segredo"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
AUT = {"Authorization": f"Bearer {TOKEN}"}


class LocalFalso:
    def __init__(self, texto="Uma sala com um sofá.", erro: str | None = None) -> None:
        self.texto, self.erro = texto, erro
        self.recebidos: list[tuple[bytes, str, visao.Preferencias]] = []

    def estado(self, p):
        return {"reachable": self.erro != "provider_offline", "model_present": True,
                "vision": self.erro != "model_without_vision", "endpoint": p.local_endpoint,
                "model": p.local_modelo}

    def descrever(self, jpeg, pergunta, p, prazo_s=None):
        self.recebidos.append((jpeg, pergunta, p))
        if self.erro:
            raise visao.ErroDeVisao(self.erro, "de mentira")
        return visao.Observacao(self.texto, "local", p.local_modelo, 12)


class NuvemFalsa:
    def __init__(self, chave="k") -> None:
        self.chave = chave
        self.recebidos: list = []

    def estado(self, p):
        return {"enabled": p.nuvem_permitida, "key_present": bool(self.chave), "model": p.nuvem_modelo}

    def descrever(self, jpeg, pergunta, p, prazo_s=None):
        if not p.nuvem_permitida:
            raise visao.ErroDeVisao("cloud_disabled")
        self.recebidos.append((jpeg, pergunta, p))
        return visao.Observacao("Nuvem viu algo.", "openrouter", p.nuvem_modelo, 30)


@pytest.fixture()
def montar():
    def _montar(local=None, nuvem=None):
        local = local or LocalFalso()
        nuvem = nuvem or NuvemFalsa()
        app = ponte.montar(TOKEN, "chave-gw", visao_local=local, visao_nuvem=nuvem)
        return TestClient(app), local, nuvem
    return _montar


def pedido(imagem=JPEG, pergunta="o que você está vendo?", **prefs):
    corpo = {"imagem": base64.b64encode(imagem).decode(), "pergunta": pergunta}
    if prefs:
        corpo["preferencias"] = prefs
    return corpo


# ─── a fronteira ─────────────────────────────────────────────────────────────
def test_sem_token_nao_ha_visao(montar) -> None:
    cliente, local, _ = montar()
    assert cliente.post("/api/olhar", json=pedido()).status_code == 401
    assert cliente.get("/api/olhar/estado").status_code == 401
    assert local.recebidos == []


def test_token_errado_nao_ha_visao(montar) -> None:
    cliente, local, _ = montar()
    r = cliente.post("/api/olhar", json=pedido(), headers={"Authorization": "Bearer outro"})
    assert r.status_code == 401 and local.recebidos == []


def test_com_token_o_quadro_e_descrito(montar) -> None:
    cliente, local, _ = montar()
    r = cliente.post("/api/olhar", json=pedido(), headers=AUT)
    assert r.status_code == 200
    assert r.json() == {"texto": "Uma sala com um sofá.", "provedor": "local",
                        "modelo": "qwen2.5vl:7b", "ms": 12}
    [(jpeg, pergunta, p)] = local.recebidos
    assert jpeg == JPEG and pergunta == "o que você está vendo?"
    assert p.provedor == "auto" and p.nuvem_permitida is False


def test_as_preferencias_do_robo_chegam_ao_provedor(montar) -> None:
    cliente, local, nuvem = montar()
    r = cliente.post("/api/olhar", json=pedido(
        provedor="openrouter", nuvem_permitida=True, nuvem_modelo="google/gemini-3.8-flash",
        local_endpoint="http://192.168.1.20:11434", local_modelo="gemma3:12b"), headers=AUT)
    assert r.status_code == 200 and r.json()["provedor"] == "openrouter"
    [(_, _, p)] = nuvem.recebidos
    assert (p.local_endpoint, p.local_modelo) == ("http://192.168.1.20:11434", "gemma3:12b")


@pytest.mark.parametrize("prefs,status", [
    ({"provedor": "google"}, 400),
    ({"local_endpoint": "http://example.com:11434"}, 400),        # fora da casa
    ({"local_endpoint": "ftp://127.0.0.1"}, 400),
    ({"local_endpoint": "http://8.8.8.8:11434"}, 400),
    ({"local_modelo": "x" * 200}, 400),
    ({"local_modelo": "qwen 2"}, 400),
    ({"nuvem_modelo": "../x"}, 400),
    ({"local_endpoint": "http://127.0.0.1:11434"}, 200),
    ({"local_endpoint": "http://10.0.0.24:11434"}, 200),
    ({"local_endpoint": "http://ollama.local:11434"}, 200),
])
def test_preferencias_fora_do_combinado_sao_recusadas(montar, prefs, status) -> None:
    """O endpoint local é para onde o companion MANDA o quadro. Quem tiver o
    token da ponte não pode apontá-lo para fora da casa."""
    cliente, _, _ = montar()
    assert cliente.post("/api/olhar", json=pedido(**prefs), headers=AUT).status_code == status


# ─── os erros, cada um com o seu código ──────────────────────────────────────
@pytest.mark.parametrize("codigo,status", [
    ("provider_offline", 503), ("model_missing", 503), ("model_without_vision", 503),
    ("timeout", 504), ("provider_error", 502), ("auth_failed", 502),
])
def test_erro_do_provedor_vira_codigo_e_status(montar, codigo, status) -> None:
    cliente, _, _ = montar(LocalFalso(erro=codigo))
    r = cliente.post("/api/olhar", json=pedido(provedor="local"), headers=AUT)
    assert r.status_code == status
    assert r.json()["erro"] == codigo


def test_nuvem_sem_permissao_e_403(montar) -> None:
    cliente, _, nuvem = montar()
    r = cliente.post("/api/olhar", json=pedido(provedor="openrouter"), headers=AUT)
    assert r.status_code == 403 and r.json()["erro"] == "cloud_disabled"
    assert nuvem.recebidos == []


@pytest.mark.parametrize("corpo", [
    {"pergunta": "oi"},                                   # sem imagem
    {"imagem": "isso não é base64!!", "pergunta": "oi"},
    {"imagem": base64.b64encode(b"nao e jpeg").decode(), "pergunta": "oi"},
    {"imagem": base64.b64encode(JPEG).decode(), "pergunta": ""},
    {"imagem": base64.b64encode(JPEG).decode(), "pergunta": "x" * 600},
    "não é um objeto",
])
def test_pedido_mal_formado_e_400(montar, corpo) -> None:
    cliente, local, _ = montar()
    r = cliente.post("/api/olhar", json=corpo, headers=AUT)
    assert r.status_code == 400 and local.recebidos == []


def test_imagem_grande_demais_e_413(montar) -> None:
    cliente, local, _ = montar()
    grande = b"\xff\xd8" + b"\x00" * (5 * 1024 * 1024) + b"\xff\xd9"
    r = cliente.post("/api/olhar", json=pedido(imagem=grande), headers=AUT)
    assert r.status_code == 413 and local.recebidos == []


# ─── nada da imagem fica ─────────────────────────────────────────────────────
def test_a_imagem_nao_vai_para_o_log(montar, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    cliente, _, _ = montar(LocalFalso(erro="provider_error"))
    marca = b"\xff\xd8\xff\xe0" + b"MARCA-UNICA-DA-IMAGEM" + b"\x00" * 40 + b"\xff\xd9"
    cliente.post("/api/olhar", json=pedido(imagem=marca), headers=AUT)
    cliente.post("/api/olhar", json=pedido(imagem=marca), headers=AUT)
    assert "MARCA-UNICA" not in caplog.text
    assert base64.b64encode(marca).decode()[:24] not in caplog.text


def test_a_resposta_de_erro_nao_traz_a_imagem_nem_o_detalhe_bruto(montar) -> None:
    cliente, _, _ = montar(LocalFalso(erro="provider_offline"))
    r = cliente.post("/api/olhar", json=pedido(), headers=AUT)
    assert set(r.json()) == {"erro", "detalhe"}
    assert "imagem" not in r.text and base64.b64encode(JPEG).decode()[:16] not in r.text


# ─── o estado ────────────────────────────────────────────────────────────────
def test_o_estado_responde_com_as_preferencias_da_query(montar) -> None:
    cliente, _, _ = montar()
    r = cliente.get("/api/olhar/estado", params={"provedor": "auto", "nuvem_permitida": "true"},
                    headers=AUT)
    assert r.status_code == 200
    e = r.json()
    assert e["local"]["vision"] is True and e["cloud"]["enabled"] is True
    assert e["effective"] == "local" and e["preferred"] == "auto"


def test_o_estado_recusa_preferencia_invalida(montar) -> None:
    cliente, _, _ = montar()
    assert cliente.get("/api/olhar/estado", params={"provedor": "x"}, headers=AUT).status_code == 400
