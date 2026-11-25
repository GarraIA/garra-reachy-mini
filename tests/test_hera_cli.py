"""`python -m companion.hera`: por onde o dono libera e ajusta a conversa ao vivo.

É a porta "só no computador": liberar, subir de nível e aumentar teto não existem
em nenhuma rota de rede. O que este comando nunca faz é mostrar o segredo.
"""

from __future__ import annotations

import json

import pytest

from companion.hera import __main__ as cli
from companion.hera import credencial, perfil, registro


@pytest.fixture(autouse=True)
def pasta(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_CONF", str(tmp_path / "conf"))
    monkeypatch.setenv("GARRA_REACHY_DADOS", str(tmp_path / "dados"))
    monkeypatch.delenv("GARRA_HERA", raising=False)
    reinicios: list[str] = []
    monkeypatch.setattr(cli, "_reiniciar_ponte", lambda: reinicios.append("ponte"))
    monkeypatch.setattr(cli, "_saude_da_ponte", lambda: None)
    return reinicios


def test_status_de_uma_instalacao_nova(capsys):
    assert cli.main(["status"]) == 0
    saida = capsys.readouterr().out
    assert "desligada" in saida and "completo" in saida
    assert "credencial: não criada" in saida


def test_liberar_e_desligar():
    assert cli.main(["liberar"]) == 0
    assert registro.Registro().liberada() is True
    assert cli.main(["desligar"]) == 0
    assert registro.Registro().liberada() is False


def test_trocar_de_nivel_regrava_o_perfil_e_reinicia_a_ponte(pasta):
    assert cli.main(["nivel", "web"]) == 0
    assert registro.Registro().ler()["level"] == "web"
    gravado = json.loads(perfil.caminho_padrao().read_text(encoding="utf-8"))
    assert gravado["create_params"]["toolsets"] == ["web", "todo"]
    assert pasta == ["ponte"]


def test_limite_valido_e_invalido(capsys):
    assert cli.main(["limite", "daily_s", "3600"]) == 0
    assert registro.Registro().ler()["limits"]["daily_s"] == 3600
    assert cli.main(["limite", "daily_s", "-5"]) == 2
    assert cli.main(["limite", "inexistente", "5"]) == 2
    assert registro.Registro().ler()["limits"]["daily_s"] == 3600
    assert "daily_s" in capsys.readouterr().err


def test_nivel_desconhecido_nao_muda_nada(pasta):
    assert cli.main(["nivel", "deus"]) == 2
    assert registro.Registro().ler()["level"] == "completo" and pasta == []


def test_o_segredo_nunca_aparece(capsys):
    assert cli.main(["token"]) == 0
    criado = credencial.segredo()
    assert cli.main(["token", "--girar"]) == 0
    assert credencial.segredo() != criado
    assert cli.main(["status"]) == 0
    tudo = "".join(capsys.readouterr())
    assert criado not in tudo and credencial.segredo() not in tudo
    assert "credencial: criada" in tudo


def test_revogar_apaga_a_credencial(capsys):
    cli.main(["token"])
    assert cli.main(["token", "--revogar"]) == 0
    assert credencial.existe() is False


def test_perfil_mostra_o_caminho_e_nao_o_conteudo(capsys):
    assert cli.main(["perfil"]) == 0
    saida = capsys.readouterr().out
    assert str(perfil.caminho_padrao()) in saida
    assert "Canal desta sessão" not in saida
