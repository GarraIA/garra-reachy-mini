"""O serviço de rostos no PC (127.0.0.1:8127): a porta HTTP do núcleo.

Sem OpenCV aqui — o motor é falso. O que se prova é o contrato: caminhos,
códigos de estado, corpos, e que nenhuma imagem sai gravada nem logada.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from companion import rostos

ARQUIVO = Path(__file__).resolve().parent.parent / "tools" / "servidor_rostos.py"
CONSENTE = {"accepted": True, "by": "Maria", "minor": False}


def carregar():
    spec = importlib.util.spec_from_file_location("servidor_rostos", ARQUIVO)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["servidor_rostos"] = mod
    spec.loader.exec_module(mod)
    return mod


def vetor(semente: int) -> np.ndarray:
    v = np.random.default_rng(semente).standard_normal(128).astype(np.float32)
    return v / np.linalg.norm(v)


def rosto(emb, tamanho=160, nitidez=120.0, brilho=120.0, caixa=(100, 80)):
    return rostos.Rosto(caixa=(caixa[0], caixa[1], tamanho, tamanho), pontuacao=0.95, embedding=emb,
                        nitidez=nitidez, brilho=brilho)


def jpeg(n: int) -> bytes:
    return b"\xff\xd8" + bytes([n]) * 64 + b"\xff\xd9"


class MotorFalso:
    def __init__(self) -> None:
        self.por_imagem: dict[bytes, list] = {}
        self.pronto = True

    def detectar(self, dados: bytes):
        return self.por_imagem.get(dados, [])


@pytest.fixture()
def servico(tmp_path):
    mod = carregar()
    motor = MotorFalso()
    rec = rostos.Reconhecedor(rostos.Base(tmp_path / "rostos"), motor)
    app = mod.montar(rec, motor_nome="falso")
    cliente = TestClient(app)
    cliente.motor, cliente.pasta, cliente.mod = motor, tmp_path / "rostos", mod
    return cliente


def criar(servico, nome="Maria", consent=CONSENTE) -> dict:
    r = servico.post("/perfis", json={"name": nome, "consent": consent})
    assert r.status_code == 201, r.text
    return r.json()["profile"]


def test_saude_diz_o_que_ha(servico) -> None:
    r = servico.get("/saude")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["ok"] is True and corpo["engine"] == "falso" and corpo["profiles"] == 0
    assert corpo["threshold"] == rostos.LIMIAR_SEMELHANCA and corpo["margin"] == rostos.MARGEM_MINIMA


def test_perfil_nasce_e_aparece_na_lista(servico) -> None:
    p = criar(servico)
    assert p["name"] == "Maria" and p["samples"] == 0 and p["consent"]["by"] == "Maria"
    r = servico.get("/perfis")
    assert r.status_code == 200 and [x["id"] for x in r.json()["profiles"]] == [p["id"]]


@pytest.mark.parametrize("corpo,codigo", [
    ({"name": "Maria", "consent": {"accepted": False, "by": "Maria"}}, "consent_required"),
    ({"name": "Maria"}, "consent_required"),
    ({"name": "Bebê", "consent": {"accepted": True, "by": "Pai", "minor": True}}, "guardian_required"),
    ({"name": "<b>", "consent": CONSENTE}, "invalid_name"),
])
def test_perfil_torto_e_400_com_codigo(servico, corpo, codigo) -> None:
    r = servico.post("/perfis", json=corpo)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == codigo
    assert servico.get("/perfis").json()["profiles"] == []


def test_corpo_que_nao_e_json_e_400(servico) -> None:
    r = servico.post("/perfis", content=b"nao e json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


def test_amostra_boa_entra_e_ruim_volta_com_motivo(servico) -> None:
    p = criar(servico)
    boa, ruim = jpeg(1), jpeg(2)
    servico.motor.por_imagem[boa] = [rosto(vetor(1))]
    servico.motor.por_imagem[ruim] = [rosto(vetor(1)), rosto(vetor(2), caixa=(400, 80))]
    r = servico.post(f"/perfis/{p['id']}/amostras", content=boa, headers={"content-type": "image/jpeg"})
    assert r.status_code == 200 and r.json() == {"ok": True, "samples": 1, "quality": {"size_px": 160, "sharpness": 120.0, "brightness": 120.0}}
    r = servico.post(f"/perfis/{p['id']}/amostras", content=ruim, headers={"content-type": "image/jpeg"})
    assert r.status_code == 200 and r.json() == {"ok": False, "reason": "many_faces", "samples": 1}


def test_amostra_para_perfil_inexistente_e_404(servico) -> None:
    r = servico.post("/perfis/nada/amostras", content=jpeg(1), headers={"content-type": "image/jpeg"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("dados", [b"", b"\xff\xd8curto", b"GIF89a" + b"x" * 40, b"\xff\xd8" + b"x" * (4 * 1024 * 1024 + 1) + b"\xff\xd9"])
def test_imagem_torta_e_400_bad_image(servico, dados) -> None:
    p = criar(servico)
    r = servico.post(f"/perfis/{p['id']}/amostras", content=dados, headers={"content-type": "image/jpeg"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_image"
    r = servico.post("/reconhecer", content=dados, headers={"content-type": "image/jpeg"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_image"


def test_reconhecer_devolve_um_item_por_rosto(servico) -> None:
    p = criar(servico)
    amostra = jpeg(1)
    servico.motor.por_imagem[amostra] = [rosto(vetor(1))]
    assert servico.post(f"/perfis/{p['id']}/amostras", content=amostra, headers={"content-type": "image/jpeg"}).json()["ok"]
    cena = jpeg(9)
    servico.motor.por_imagem[cena] = [rosto(vetor(1)), rosto(vetor(50), caixa=(400, 80))]
    r = servico.post("/reconhecer", content=cena, headers={"content-type": "image/jpeg"})
    assert r.status_code == 200
    corpo = r.json()
    assert isinstance(corpo["ms"], (int, float))
    assert [f["known"] for f in corpo["faces"]] == [True, False]
    assert corpo["faces"][0]["name"] == "Maria" and corpo["faces"][0]["profile_id"] == p["id"]
    assert corpo["faces"][1]["reason"] == "below_threshold"


def test_ativar_desativar_apagar_e_limpar(servico) -> None:
    p = criar(servico)
    amostra = jpeg(1)
    servico.motor.por_imagem[amostra] = [rosto(vetor(1))]
    servico.post(f"/perfis/{p['id']}/amostras", content=amostra, headers={"content-type": "image/jpeg"})
    r = servico.patch(f"/perfis/{p['id']}", json={"active": False})
    assert r.status_code == 200 and r.json()["profile"]["active"] is False
    r = servico.patch(f"/perfis/{p['id']}", json={"active": "sim"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    r = servico.delete(f"/perfis/{p['id']}/amostras")
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": 1}
    assert servico.get("/perfis").json()["profiles"][0]["samples"] == 0
    r = servico.delete(f"/perfis/{p['id']}")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert servico.get("/perfis").json()["profiles"] == []
    assert servico.delete(f"/perfis/{p['id']}").status_code == 404
    assert servico.patch(f"/perfis/{p['id']}", json={"active": True}).status_code == 404


def test_nenhuma_imagem_fica_em_disco_nem_no_log(servico, caplog) -> None:
    p = criar(servico)
    amostra, cena = jpeg(3), jpeg(4)
    servico.motor.por_imagem[amostra] = [rosto(vetor(3))]
    servico.motor.por_imagem[cena] = [rosto(vetor(3))]
    with caplog.at_level(logging.DEBUG, logger="servidor_rostos"):
        servico.post(f"/perfis/{p['id']}/amostras", content=amostra, headers={"content-type": "image/jpeg"})
        servico.post("/reconhecer", content=cena, headers={"content-type": "image/jpeg"})
    for a in servico.pasta.rglob("*"):
        if a.is_file():
            assert amostra not in a.read_bytes() and cena not in a.read_bytes(), a
    assert all(a.suffix in (".json", ".emb", "") for a in servico.pasta.iterdir())
    registros = " ".join(r.getMessage() for r in caplog.records if r.name == "servidor_rostos")
    assert "Maria" not in registros and "\\xff" not in registros


def test_o_servico_so_escuta_no_loopback() -> None:
    fonte = ARQUIVO.read_text(encoding="utf-8")
    assert 'host="127.0.0.1"' in fonte
    assert "0.0.0.0" not in fonte


def test_motor_de_verdade_so_carrega_ao_pedir() -> None:
    """`import cv2` fica dentro do motor: o módulo importa em qualquer venv."""
    fonte = ARQUIVO.read_text(encoding="utf-8")
    topo = fonte.split("class MotorOpenCV")[0]
    assert "import cv2" not in topo
    assert "import cv2" in fonte.split("class MotorOpenCV")[1]
