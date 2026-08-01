"""O painel tem de estar dentro do pacote instalado — e dizer quando não está.

O build de staging renomeou o diretório do pacote e a chave de
`[tool.setuptools.package-data]` ficou apontando para o nome antigo. Nenhum
arquivo de `static/` entrou no wheel (35 arquivos contra os 42 da produção), o
`reachy.html` não existia no destino, `_montar_painel` desistia em silêncio, e o
painel embutido do robô mostrava `{"detail":"Not Found"}` — indistinguível de
uma URL errada. Uma rodada inteira no hardware para achar isso.

Dois testes, portanto: um que impede a chave de errar de novo, e um que garante
que, se errar mesmo assim, o app **explica** em vez de dar 404.
"""

from __future__ import annotations

import fnmatch
import pathlib
import tomllib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini import build_info
from garra_reachy_mini.web.api import _montar_painel

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = RAIZ / "pyproject.toml"


@pytest.fixture(scope="module")
def projeto() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _padroes_para(projeto: dict, pacote: str) -> list[str]:
    """Os globs que valem para `pacote`, incluindo a chave curinga."""
    dados = projeto["tool"]["setuptools"]["package-data"]
    return [*dados.get("*", []), *dados.get(pacote, [])]


def test_todo_ativo_do_painel_esta_declarado(projeto):
    """A pergunta certa não é "a chave está certa?", e sim "os arquivos entram?"."""
    pacote = build_info.DISTRIBUICAO
    padroes = _padroes_para(projeto, pacote)
    assert padroes, f"nenhum package-data vale para o pacote {pacote}"
    for nome in build_info.ATIVOS_PAINEL:
        alvo = f"static/{nome}"
        assert any(fnmatch.fnmatch(alvo, p) for p in padroes), \
            f"{alvo} não é coberto por nenhum padrão: {padroes}"


def test_os_ativos_declarados_existem_mesmo_no_disco(projeto):
    """Declarar não basta: o `package-data` não avisa quando o arquivo sumiu."""
    assert build_info.ativos_do_painel() == {"ok": True, "missing": []}


def test_a_chave_do_package_data_nao_depende_do_nome_do_pacote(projeto):
    """Foi exatamente o acoplamento que quebrou o staging.

    Com `"*"` a renomeação do pacote deixa de ser uma edição em dois lugares.
    """
    dados = projeto["tool"]["setuptools"]["package-data"]
    assert "*" in dados, ("use a chave curinga: uma chave com o nome do pacote "
                          "silencia o empacotamento quando o nome muda")


def test_o_pacote_encontrado_e_o_declarado(projeto):
    """`packages.find`, `project.name` e o entry point são uma string só."""
    nome = projeto["project"]["name"]
    entradas = projeto["project"]["entry-points"]["reachy_mini_apps"]
    assert nome == build_info.DISTRIBUICAO
    assert list(entradas) == [nome]
    assert entradas[nome].startswith(f"{nome}.main:")


# ── e se faltar mesmo assim ───────────────────────────────────────────────────
def test_painel_ausente_responde_503_explicando(tmp_path):
    """404 é o que o FastAPI diz para uma URL errada. O build quebrado merece
    uma resposta que se distinga dela."""
    from garra_reachy_mini.web.api import ContextoWeb

    app = FastAPI()
    (tmp_path / "vazio").mkdir()
    # `dir_estatico` existe, mas sem `reachy.html` — o build mal empacotado.
    _montar_painel(app, ContextoWeb(controlador=None, hub=None, eventos=None,
                                    politica=None, dir_estatico=tmp_path / "vazio"))
    with TestClient(app) as c:
        for rota in ("/", "/reachy"):
            r = c.get(rota)
            assert r.status_code == 503, rota
            assert "reachy.html" in r.text
            assert "package-data" in r.text
            assert build_info.DISTRIBUICAO in r.text


def test_sem_dir_estatico_nao_inventa_rota(tmp_path):
    """Rodar sem painel é um modo legítimo (testes, headless); não é defeito."""
    from garra_reachy_mini.web.api import ContextoWeb

    app = FastAPI()
    _montar_painel(app, ContextoWeb(controlador=None, hub=None, eventos=None,
                                    politica=None, dir_estatico=None))
    caminhos = {getattr(r, "path", None) for r in app.router.routes}
    assert "/reachy" not in caminhos
