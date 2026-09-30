"""O cliente do serviço de rostos, no companion: fala com :8127 e traduz.

O serviço pode estar fora (unidade parada, venv quebrado): o cliente diz isso
com um código, sem estourar — e nunca com a imagem no erro.
"""

from __future__ import annotations

import json

import httpx
import pytest

from companion import familia

JPEG = b"\xff\xd8" + b"\x01" * 64 + b"\xff\xd9"
CONSENTE = {"accepted": True, "by": "Maria", "minor": False}


class ServicoFalso:
    def __init__(self) -> None:
        self.pedidos: list[tuple[str, str, bytes]] = []
        self.fora = False
        self.lento = False
        self.perfis = [{"id": "abc", "name": "Maria", "active": True, "samples": 2, "samples_readable": True,
                        "consent": dict(CONSENTE, guardian=None, at="2026-09-30T05:00:00+00:00"),
                        "created_at": "2026-09-30T05:00:00+00:00"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.pedidos.append((request.method, request.url.path, request.content))
        if self.fora:
            raise httpx.ConnectError("recusou")
        if self.lento:
            raise httpx.ReadTimeout("demorou")
        caminho = request.url.path
        if caminho == "/saude":
            return httpx.Response(200, json={"ok": True, "engine": "yunet+sface", "profiles": 1,
                                             "unreadable_profiles": 0, "threshold": 0.45, "margin": 0.1,
                                             "max_samples": 5})
        if caminho == "/perfis" and request.method == "GET":
            return httpx.Response(200, json={"profiles": self.perfis})
        if caminho == "/perfis" and request.method == "POST":
            corpo = json.loads(request.content)
            if corpo.get("consent", {}).get("accepted") is not True:
                return httpx.Response(400, json={"ok": False, "error": {"code": "consent_required", "message": "x"}})
            return httpx.Response(201, json={"ok": True, "profile": dict(self.perfis[0], name=corpo["name"], id="novo")})
        if caminho == "/perfis/abc" and request.method == "PATCH":
            return httpx.Response(200, json={"ok": True, "profile": dict(self.perfis[0], active=json.loads(request.content)["active"])})
        if caminho == "/perfis/abc" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True})
        if caminho == "/perfis/abc/amostras" and request.method == "POST":
            assert request.headers["content-type"] == "image/jpeg"
            return httpx.Response(200, json={"ok": True, "samples": 3, "quality": {"size_px": 160, "sharpness": 90.0, "brightness": 110.0}})
        if caminho == "/perfis/abc/amostras" and request.method == "DELETE":
            return httpx.Response(200, json={"ok": True, "removed": 2})
        if caminho == "/reconhecer":
            assert request.headers["content-type"] == "image/jpeg"
            return httpx.Response(200, json={"faces": [{"box": [1, 2, 100, 100], "known": True, "profile_id": "abc",
                                                        "name": "Maria", "score": 0.81, "reason": None}], "ms": 12.0})
        return httpx.Response(404, json={"ok": False, "error": {"code": "not_found", "message": "perfil não existe"}})


@pytest.fixture()
def fam():
    s = ServicoFalso()
    f = familia.Familia(http=httpx.Client(base_url="http://127.0.0.1:8127", transport=httpx.MockTransport(s.handler)))
    return f, s


def test_estado_quando_o_servico_responde(fam) -> None:
    f, s = fam
    assert f.estado() == {"state": "ok", "engine": "yunet+sface", "profiles": 1, "unreadable_profiles": 0,
                          "threshold": 0.45, "margin": 0.1, "max_samples": 5}


def test_estado_quando_o_servico_esta_fora(fam) -> None:
    f, s = fam
    s.fora = True
    assert f.estado() == {"state": "offline", "engine": None, "profiles": None, "unreadable_profiles": None,
                          "threshold": None, "margin": None, "max_samples": None}


def test_perfis_e_criar(fam) -> None:
    f, s = fam
    assert [p["name"] for p in f.perfis()] == ["Maria"]
    p = f.criar("João", CONSENTE)
    assert p["id"] == "novo" and p["name"] == "João"
    assert json.loads(s.pedidos[-1][2]) == {"name": "João", "consent": CONSENTE}


def test_erro_do_servico_vira_erro_com_codigo(fam) -> None:
    f, s = fam
    with pytest.raises(familia.ErroFamilia) as e:
        f.criar("João", {"accepted": False})
    assert e.value.codigo == "consent_required"
    with pytest.raises(familia.ErroFamilia) as e:
        f.apagar("zzz")
    assert e.value.codigo == "not_found"


def test_ativar_apagar_amostras(fam) -> None:
    f, s = fam
    assert f.ativo("abc", False)["active"] is False
    assert f.apagar("abc") == {"ok": True}
    assert f.limpar_amostras("abc") == {"ok": True, "removed": 2}
    r = f.amostra("abc", JPEG)
    assert r["ok"] is True and r["samples"] == 3
    assert s.pedidos[-1] == ("POST", "/perfis/abc/amostras", JPEG)


def test_reconhecer_devolve_os_rostos(fam) -> None:
    f, s = fam
    r = f.reconhecer(JPEG)
    assert r["faces"][0]["name"] == "Maria" and r["ms"] == 12.0
    assert s.pedidos[-1] == ("POST", "/reconhecer", JPEG)


@pytest.mark.parametrize("falha,codigo", [("fora", "service_offline"), ("lento", "timeout")])
def test_servico_fora_ou_lento_e_codigo_sem_imagem(fam, falha, codigo) -> None:
    f, s = fam
    setattr(s, falha, True)
    with pytest.raises(familia.ErroFamilia) as e:
        f.reconhecer(JPEG)
    assert e.value.codigo == codigo
    assert "\\x01" not in str(e.value) and JPEG not in str(e.value).encode("latin-1", "ignore")


def test_resposta_que_nao_e_json_e_service_error() -> None:
    def handler(request):
        return httpx.Response(200, content=b"<html>")
    f = familia.Familia(http=httpx.Client(base_url="http://127.0.0.1:8127", transport=httpx.MockTransport(handler)))
    with pytest.raises(familia.ErroFamilia) as e:
        f.perfis()
    assert e.value.codigo == "service_error"
    assert f.estado()["state"] == "offline"


def test_o_servico_padrao_e_o_loopback() -> None:
    assert familia.SERVICO == "http://127.0.0.1:8127"
