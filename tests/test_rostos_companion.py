"""Família: cadastro consentido e reconhecimento, tudo neste PC.

Não é detecção de rosto (o robô já faz, para olhar para quem fala). É
comparar o rosto de agora com os PERFIS cadastrados — e só isso. O modelo de
visão nunca é convidado a adivinhar nome.

O que estes testes seguram:

- perfil só nasce com consentimento registrado; menor só com responsável;
- amostra só entra com UM rosto, nítido, grande e bem iluminado, e só o
  vetor fica — a imagem, nunca;
- "desconhecido" quando ninguém passa do limiar, ou quando os dois melhores
  estão perto demais um do outro;
- perfil desativado não é reconhecido; perfil apagado leva os vetores junto;
- os vetores ficam cifrados em disco, com a chave fora do repo e 0600.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from companion import rostos

CONSENTE = {"accepted": True, "by": "Maria", "minor": False, "guardian": None}


def vetor(semente: int, ruido: float = 0.0) -> np.ndarray:
    g = np.random.default_rng(semente)
    v = g.standard_normal(128).astype(np.float32)
    if ruido:
        v = v + np.random.default_rng(semente + 1000).standard_normal(128).astype(np.float32) * ruido
    return v / np.linalg.norm(v)


def rosto(emb: np.ndarray | None, tamanho: int = 160, nitidez: float = 120.0, brilho: float = 120.0,
          caixa=(100, 80)) -> rostos.Rosto:
    return rostos.Rosto(caixa=(caixa[0], caixa[1], tamanho, tamanho), pontuacao=0.95, embedding=emb,
                        nitidez=nitidez, brilho=brilho)


class MotorFalso:
    """Devolve os rostos que o teste mandar, por imagem."""

    def __init__(self) -> None:
        self.por_imagem: dict[bytes, list[rostos.Rosto]] = {}
        self.chamadas: list[bytes] = []

    def detectar(self, jpeg: bytes) -> list[rostos.Rosto]:
        self.chamadas.append(jpeg)
        return self.por_imagem.get(jpeg, [])


@pytest.fixture()
def fam(tmp_path):
    motor = MotorFalso()
    r = rostos.Reconhecedor(rostos.Base(tmp_path / "rostos"), motor)
    r._motor_teste, r._pasta_teste = motor, tmp_path / "rostos"
    return r


def imagem(n: int) -> bytes:
    return b"\xff\xd8" + bytes([n]) * 32 + b"\xff\xd9"


def cadastrar(fam, nome: str, semente: int, amostras: int = 3, consentimento=CONSENTE) -> dict:
    perfil = fam.base.criar(nome, consentimento)
    for i in range(amostras):
        img = imagem(semente * 10 + i)
        fam._motor_teste.por_imagem[img] = [rosto(vetor(semente, ruido=0.05 * i))]
        assert fam.cadastrar(perfil["id"], img)["ok"] is True
    return perfil


# ─── consentimento ───────────────────────────────────────────────────────────
def test_perfil_nasce_com_consentimento_registrado(fam) -> None:
    p = fam.base.criar("Maria", CONSENTE)
    assert p["name"] == "Maria" and p["active"] is True and p["samples"] == 0
    assert p["consent"] == {"accepted": True, "by": "Maria", "minor": False, "guardian": None,
                            "at": p["consent"]["at"]}
    assert p["consent"]["at"]


@pytest.mark.parametrize("consentimento,codigo", [
    ({"accepted": False, "by": "Maria", "minor": False}, "consent_required"),
    ({"by": "Maria", "minor": False}, "consent_required"),
    ({"accepted": "sim", "by": "Maria", "minor": False}, "consent_required"),
    ({"accepted": 1, "by": "Maria", "minor": False}, "consent_required"),
    ({"accepted": True, "by": "", "minor": False}, "consent_required"),
    ({"accepted": True, "by": "Pai", "minor": True, "guardian": ""}, "guardian_required"),
    ({"accepted": True, "by": "Pai", "minor": True}, "guardian_required"),
    ("sim", "consent_required"),
])
def test_sem_consentimento_nao_ha_perfil(fam, consentimento, codigo) -> None:
    with pytest.raises(rostos.ErroRostos) as e:
        fam.base.criar("Maria", consentimento)
    assert e.value.codigo == codigo
    assert fam.base.perfis() == []


def test_menor_com_responsavel_e_registrado(fam) -> None:
    p = fam.base.criar("Joãozinho", {"accepted": True, "by": "Michel", "minor": True, "guardian": "Michel"})
    assert p["consent"]["minor"] is True and p["consent"]["guardian"] == "Michel"


@pytest.mark.parametrize("nome", ["", " ", "x" * 41, "<script>", "Maria\nSilva"])
def test_nome_torto_e_recusado(fam, nome) -> None:
    with pytest.raises(rostos.ErroRostos) as e:
        fam.base.criar(nome, CONSENTE)
    assert e.value.codigo == "invalid_name"


# ─── amostras ────────────────────────────────────────────────────────────────
def test_amostra_boa_guarda_so_o_vetor(fam) -> None:
    p = fam.base.criar("Maria", CONSENTE)
    img = imagem(1)
    fam._motor_teste.por_imagem[img] = [rosto(vetor(1))]
    r = fam.cadastrar(p["id"], img)
    assert r == {"ok": True, "samples": 1, "quality": {"size_px": 160, "sharpness": 120.0, "brightness": 120.0}}
    assert fam.base.perfis()[0]["samples"] == 1
    arquivos = list(fam._pasta_teste.iterdir())
    assert not any(a.suffix in (".jpg", ".jpeg", ".png") for a in arquivos), "imagem nunca fica"
    assert not any(img in a.read_bytes() for a in arquivos if a.is_file())


@pytest.mark.parametrize("rostos_na_imagem,motivo", [
    ([], "no_face"),
    ([rosto(vetor(1)), rosto(vetor(2), caixa=(400, 80))], "many_faces"),
    ([rosto(vetor(1), tamanho=50)], "too_small"),
    ([rosto(vetor(1), nitidez=5.0)], "blurry"),
    ([rosto(vetor(1), brilho=20.0)], "too_dark"),
    ([rosto(vetor(1), brilho=245.0)], "too_bright"),
])
def test_amostra_ruim_e_recusada_com_o_motivo(fam, rostos_na_imagem, motivo) -> None:
    p = fam.base.criar("Maria", CONSENTE)
    img = imagem(7)
    fam._motor_teste.por_imagem[img] = rostos_na_imagem
    r = fam.cadastrar(p["id"], img)
    assert r["ok"] is False and r["reason"] == motivo
    assert fam.base.perfis()[0]["samples"] == 0


def test_ha_um_teto_de_amostras(fam) -> None:
    p = cadastrar(fam, "Maria", 1, amostras=rostos.AMOSTRAS_MAX)
    img = imagem(99)
    fam._motor_teste.por_imagem[img] = [rosto(vetor(1))]
    r = fam.cadastrar(p["id"], img)
    assert r["ok"] is False and r["reason"] == "enough_samples"


def test_recadastrar_limpa_as_amostras(fam) -> None:
    p = cadastrar(fam, "Maria", 1)
    assert fam.base.limpar_amostras(p["id"]) == 3
    assert fam.base.perfis()[0]["samples"] == 0


# ─── reconhecer ──────────────────────────────────────────────────────────────
def test_pessoa_cadastrada_e_reconhecida_e_estranha_e_desconhecida(fam) -> None:
    maria = cadastrar(fam, "Maria", 1)
    cadastrar(fam, "João", 2)
    cena = imagem(50)
    fam._motor_teste.por_imagem[cena] = [rosto(vetor(1, ruido=0.08)), rosto(vetor(77), caixa=(400, 80))]
    vistos = fam.reconhecer(cena)
    assert len(vistos) == 2
    assert vistos[0]["profile_id"] == maria["id"] and vistos[0]["name"] == "Maria"
    assert vistos[0]["score"] > rostos.LIMIAR_SEMELHANCA
    assert vistos[1]["profile_id"] is None and vistos[1]["name"] is None and vistos[1]["known"] is False
    assert vistos[0]["box"] == [100, 80, 160, 160]


def test_estranho_com_um_so_perfil_fica_abaixo_do_limiar(fam) -> None:
    """Com um perfil só não há segundo colocado: o limiar tem de segurar sozinho."""
    cadastrar(fam, "Maria", 1)
    cena = imagem(56)
    fam._motor_teste.por_imagem[cena] = [rosto(vetor(88))]
    [v] = fam.reconhecer(cena)
    assert v["known"] is False and v["reason"] == "below_threshold"
    assert v["score"] is not None and v["score"] < rostos.LIMIAR_SEMELHANCA


def test_sem_perfis_todo_mundo_e_desconhecido(fam) -> None:
    cena = imagem(50)
    fam._motor_teste.por_imagem[cena] = [rosto(vetor(1))]
    [v] = fam.reconhecer(cena)
    assert v["known"] is False and v["score"] is None


def test_dois_perfis_parecidos_demais_dao_desconhecido(fam) -> None:
    """A margem entre o melhor e o segundo tem de existir; senão é chute."""
    cadastrar(fam, "Maria", 1)
    p2 = fam.base.criar("Gêmea", CONSENTE)
    img = imagem(200)
    fam._motor_teste.por_imagem[img] = [rosto(vetor(1, ruido=0.02))]
    assert fam.cadastrar(p2["id"], img)["ok"]
    cena = imagem(51)
    fam._motor_teste.por_imagem[cena] = [rosto(vetor(1, ruido=0.03))]
    [v] = fam.reconhecer(cena)
    assert v["known"] is False and v["reason"] == "ambiguous"


def test_perfil_desativado_nao_e_reconhecido(fam) -> None:
    maria = cadastrar(fam, "Maria", 1)
    fam.base.desativar(maria["id"])
    cena = imagem(52)
    fam._motor_teste.por_imagem[cena] = [rosto(vetor(1))]
    [v] = fam.reconhecer(cena)
    assert v["known"] is False
    fam.base.ativar(maria["id"])
    [v] = fam.reconhecer(cena)
    assert v["known"] is True


def test_rosto_sem_vetor_e_desconhecido_e_nao_estoura(fam) -> None:
    cadastrar(fam, "Maria", 1)
    cena = imagem(53)
    fam._motor_teste.por_imagem[cena] = [rosto(None)]
    [v] = fam.reconhecer(cena)
    assert v["known"] is False and v["reason"] == "no_embedding"


def test_reconhecer_nao_guarda_a_imagem_nem_o_vetor(fam) -> None:
    cadastrar(fam, "Maria", 1)
    antes = {a.name: a.stat().st_size for a in fam._pasta_teste.iterdir()}
    cena = imagem(54)
    fam._motor_teste.por_imagem[cena] = [rosto(vetor(3))]
    fam.reconhecer(cena)
    depois = {a.name: a.stat().st_size for a in fam._pasta_teste.iterdir()}
    assert antes == depois


# ─── apagar e proteger ───────────────────────────────────────────────────────
def test_apagar_leva_os_vetores_junto(fam) -> None:
    maria = cadastrar(fam, "Maria", 1)
    assert (fam._pasta_teste / f"{maria['id']}.emb").exists()
    fam.base.apagar(maria["id"])
    assert fam.base.perfis() == []
    assert not (fam._pasta_teste / f"{maria['id']}.emb").exists()


def test_os_vetores_ficam_cifrados_e_a_pasta_e_so_do_dono(fam) -> None:
    maria = cadastrar(fam, "Maria", 1)
    bruto = (fam._pasta_teste / f"{maria['id']}.emb").read_bytes()
    assert vetor(1).tobytes()[:16] not in bruto, "o vetor está em claro no disco"
    assert oct(fam._pasta_teste.stat().st_mode & 0o777) == "0o700"
    for a in fam._pasta_teste.iterdir():
        assert oct(a.stat().st_mode & 0o777) == "0o600", a.name
    meta = json.loads((fam._pasta_teste / "perfis.json").read_text())
    assert "embedding" not in json.dumps(meta) and maria["id"] in json.dumps(meta)


def test_chave_perdida_nao_deixa_ler_mas_nao_derruba(fam, tmp_path) -> None:
    maria = cadastrar(fam, "Maria", 1)
    (fam._pasta_teste / "chave").unlink()
    outro = rostos.Reconhecedor(rostos.Base(fam._pasta_teste), fam._motor_teste)
    cena = imagem(55)
    fam._motor_teste.por_imagem[cena] = [rosto(vetor(1))]
    [v] = outro.reconhecer(cena)
    assert v["known"] is False
    assert outro.base.perfis()[0]["samples_readable"] is False


def test_perfil_desconhecido_e_erro(fam) -> None:
    with pytest.raises(rostos.ErroRostos) as e:
        fam.cadastrar("nao-existe", imagem(1))
    assert e.value.codigo == "not_found"
