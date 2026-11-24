"""O nome do robô não depende do Garra.

Na máquina em que o 1.5.0 foi medido, o `config.yml` do gateway não tinha mais
o bloco do agente `reachy_voice` — e o painel não conseguia ler nem mudar o
nome do robô. Com vários agentes, o nome é do robô, não de um deles.
"""

from __future__ import annotations

import json
import stat

import pytest
import yaml

from companion import agente, identidade


@pytest.fixture()
def sem_garra(tmp_path, monkeypatch):
    """Um `config.yml` do Garra sem o agente da voz, e a pasta do companion."""
    config = tmp_path / "config.yml"
    config.write_text(yaml.safe_dump({"agents": {}, "gateway": {"api_key": "x"}}))
    monkeypatch.setenv("GARRAIA_CONFIG", str(config))
    monkeypatch.setenv("GARRA_REACHY_CONF", str(tmp_path / "companion"))
    return tmp_path


def test_sem_o_agente_no_garra_a_identidade_tem_padroes_e_nao_erra(sem_garra) -> None:
    d = identidade.ler()
    assert d["assistant_name"] == "Garra" and d["operator_name"] == "" and d["revision"] == 0
    assert d["storage"] == "companion" and d["core_prompt_present"] is True
    assert set(d["limits"]) == {"assistant_name", "operator_name", "persona_prompt"}
    assert not (sem_garra / "companion" / "identidade.json").exists(), "ler não grava"


def test_o_nome_e_gravado_no_companion_com_revisao_e_arquivo_privado(sem_garra) -> None:
    d = identidade.gravar({"assistant_name": "Zeca", "persona_prompt": "Fale devagar."}, "painel-robo")
    assert d["assistant_name"] == "Zeca" and d["revision"] == 1 and d["updated_by"] == "painel-robo"
    arquivo = sem_garra / "companion" / "identidade.json"
    assert stat.S_IMODE(arquivo.stat().st_mode) == 0o600
    assert json.loads(arquivo.read_text())["persona_prompt"] == "Fale devagar."
    assert identidade.ler()["assistant_name"] == "Zeca"
    # O config do Garra não foi tocado: não há agente lá para receber.
    assert yaml.safe_load((sem_garra / "config.yml").read_text())["agents"] == {}


def test_revisao_desatualizada_e_conflito_e_nada_muda(sem_garra) -> None:
    identidade.gravar({"assistant_name": "Zeca"})
    with pytest.raises(agente.ConflitoAgente) as e:
        identidade.gravar({"assistant_name": "Outro", "revision": 0})
    assert e.value.atual["assistant_name"] == "Zeca"
    assert identidade.ler()["assistant_name"] == "Zeca"


@pytest.mark.parametrize("mudanca", [{"assistant_name": "   "}, {"assistant_name": "x" * 99},
                                     {"assistant_name": 7}, {"persona_prompt": "p" * 5000}, {}])
def test_os_mesmos_validadores_do_garra(sem_garra, mudanca) -> None:
    with pytest.raises(agente.ErroAgente):
        identidade.gravar(mudanca)
    assert identidade.ler()["revision"] == 0


def test_restaurar_volta_aos_padroes(sem_garra) -> None:
    identidade.gravar({"assistant_name": "Zeca", "operator_name": "Ana", "persona_prompt": "x"})
    d = identidade.restaurar()
    assert (d["assistant_name"], d["operator_name"], d["persona_prompt"]) == ("Garra", "", "")
    assert d["revision"] == 2


def test_com_o_agente_no_garra_a_verdade_continua_la(sem_garra) -> None:
    (sem_garra / "config.yml").write_text(yaml.safe_dump(
        {"agents": {"reachy_voice": {"assistant_name": "Do Garra", "system_prompt": "núcleo"}}}))
    identidade._gravar_local({"assistant_name": "Do companion", "revision": 3})
    d = identidade.ler()
    assert d["assistant_name"] == "Do Garra" and d["storage"] == "garra"


def test_o_papel_dos_agentes_usa_o_nome_gravado(sem_garra) -> None:
    from companion.agentes import Registro
    from test_agentes_ia import Falso
    identidade.gravar({"assistant_name": "Zeca", "operator_name": "Ana"})
    r = Registro(sem_garra / "agentes.json", descobrir=lambda: [Falso("hermes")],
                 identidade=identidade.ler)
    papel = r.instrucoes(r.obter("hermes"))
    assert "Você é Zeca" in papel and "a serviço de Ana" in papel
