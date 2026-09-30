"""As unidades de usuário do companion: o limite de reinícios tem de estar em
[Unit]. Em [Service] o systemd o ignora (`Unknown key name 'StartLimitIntervalSec'
in section 'Service', ignoring`, visto no journal em 30/09) e um serviço que cai
em laço reiniciaria para sempre."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

PASTA = Path(__file__).resolve().parent.parent / "companion" / "systemd"
UNIDADES = sorted(PASTA.glob("*.service"))


def ler(caminho: Path) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser(strict=False)
    cp.optionxform = str
    cp.read(caminho, encoding="utf-8")
    return cp


def test_ha_unidades() -> None:
    assert {u.name for u in UNIDADES} >= {"garra-reachy-voice.service", "garra-reachy-companion.service",
                                          "garra-reachy-faces.service"}


@pytest.mark.parametrize("unidade", UNIDADES, ids=lambda u: u.name)
def test_limite_de_reinicios_fica_em_unit(unidade) -> None:
    cp = ler(unidade)
    for chave in ("StartLimitIntervalSec", "StartLimitBurst"):
        assert not cp.has_option("Service", chave), f"{chave} em [Service] é ignorado"
    assert cp.has_option("Unit", "StartLimitIntervalSec"), "StartLimitIntervalSec faltando em [Unit]"
    if cp.get("Unit", "StartLimitIntervalSec") != "0":      # 0 = sem limite; aí não há rajada
        assert cp.has_option("Unit", "StartLimitBurst"), "StartLimitBurst faltando em [Unit]"
    assert cp.get("Service", "Restart") in ("always", "on-failure")



def test_o_companion_nao_desiste_de_subir() -> None:
    """O companion é o único caminho do robô ao cérebro: cinco quedas
    rápidas (uma porta ocupada por um teste manual) não podem deixá-lo
    `failed` até alguém rodar reset-failed."""
    cp = ler(PASTA / "garra-reachy-companion.service")
    assert cp.get("Unit", "StartLimitIntervalSec") == "0"
