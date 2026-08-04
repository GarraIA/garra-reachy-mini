"""O contrato do `publicar.sh`, caso a caso.

Estes testes existem por um defeito real: a etapa `privado` **nunca** tornou
privado um Space que já era público — só a etapa `publico` mexe em visibilidade
— então rodá-la contra o Space padrão publicava para todo mundo enquanto o
banner imprimia `Visibility: private`. A lógica vivia dentro de um heredoc,
onde nada a exercitava.

O que cada teste protege é uma frase do contrato:

    etapa `privado`  → recusa Space já público
    ensaio           → exige `GARRA_SPACE` explícito
    etapa `publico`  → único caminho que muda visibilidade
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.publicacao import (ESPACO_PRODUCAO, ErroDePublicacao, decidir,
                              espaco)

RAIZ = Path(__file__).resolve().parent.parent


# ─── o alvo: produção por padrão, ensaio só se for escrito ───────────────────
def test_sem_garra_space_o_alvo_e_producao() -> None:
    assert espaco({}) == ESPACO_PRODUCAO


def test_garra_space_customizado_desvia_o_alvo() -> None:
    assert espaco({"GARRA_SPACE": "garra_reachy_mini_rc"}) == "garra_reachy_mini_rc"


def test_garra_space_vazio_ou_em_branco_cai_em_producao() -> None:
    """Variável exportada vazia é o mesmo que não ter escolhido nada."""
    assert espaco({"GARRA_SPACE": ""}) == ESPACO_PRODUCAO
    assert espaco({"GARRA_SPACE": "   "}) == ESPACO_PRODUCAO


@pytest.mark.parametrize("valor", [
    "michelbr/garra_reachy_mini",     # dono junto: viraria outro repo
    "../garra_reachy_mini",
    "garra reachy mini",
    "-comeca-com-hifen",
])
def test_garra_space_malformado_para_antes_de_qualquer_escrita(valor: str) -> None:
    with pytest.raises(ErroDePublicacao) as e:
        espaco({"GARRA_SPACE": valor})
    assert valor in str(e.value), "a mensagem tem de nomear o valor recusado"


# ─── as seis situações de visibilidade ───────────────────────────────────────
def test_space_inexistente_com_privado_cria_privado() -> None:
    p = decidir("privado", existe=False, privado=None)
    assert p.criar_privado and p.enviar and not p.tornar_publico


def test_space_inexistente_com_publico_e_recusado() -> None:
    """Publicar direto pularia o ensaio no robô, que é o ponto do fluxo."""
    with pytest.raises(ErroDePublicacao) as e:
        decidir("publico", existe=False, privado=None)
    assert "não existe" in str(e.value)


def test_space_privado_com_privado_envia_sem_mexer_em_visibilidade() -> None:
    p = decidir("privado", existe=True, privado=True)
    assert p.enviar and not p.tornar_publico and not p.criar_privado


def test_space_publico_com_privado_e_recusado() -> None:
    """O defeito que motivou o guarda: isto publicava sem dizer que publicava."""
    with pytest.raises(ErroDePublicacao) as e:
        decidir("privado", existe=True, privado=False)
    msg = str(e.value)
    assert "PÚBLICO" in msg
    assert "GARRA_SPACE" in msg, "a mensagem tem de ensinar o caminho do ensaio"


def test_space_privado_com_publico_torna_publico() -> None:
    p = decidir("publico", existe=True, privado=True)
    assert p.enviar and p.tornar_publico


def test_space_ja_publico_com_publico_envia_sem_alterar_nada() -> None:
    p = decidir("publico", existe=True, privado=False)
    assert p.enviar and not p.tornar_publico


# ─── a invariante que resume tudo ────────────────────────────────────────────
def test_nenhuma_etapa_alem_de_publico_muda_visibilidade() -> None:
    """Varre o espaço de estados: só `publico` pode devolver tornar_publico."""
    for existe in (True, False):
        for privado in (True, False, None):
            if not existe and privado is not None:
                continue          # combinação impossível
            try:
                p = decidir("privado", existe, privado)
            except ErroDePublicacao:
                continue
            assert not p.tornar_publico, (
                f"privado/existe={existe}/privado={privado} mudaria visibilidade")


def test_etapa_desconhecida_nao_faz_nada() -> None:
    for etapa in ("oficial", "", "PUBLICO", "publicar"):
        with pytest.raises(ErroDePublicacao):
            decidir(etapa, existe=True, privado=True)


# ─── o script de verdade continua ligado a este módulo ───────────────────────
def test_publicar_sh_resolve_o_espaco_por_este_modulo() -> None:
    """Se o script voltar a decidir sozinho, os testes acima param de valer."""
    fonte = (RAIZ / "publicar.sh").read_text(encoding="utf-8")
    assert "tools.publicacao espaco" in fonte
    assert "from tools.publicacao import" in fonte
    assert "plano.tornar_publico" in fonte
    assert 'if etapa == "publico":' not in fonte, (
        "a decisão de visibilidade voltou para dentro do script")


def test_cli_do_modulo_responde_as_duas_perguntas() -> None:
    """O script chama o módulo por linha de comando; ela tem de funcionar."""
    r = subprocess.run(
        [sys.executable, "-m", "tools.publicacao", "espaco"],
        cwd=RAIZ, capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(RAIZ)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ESPACO_PRODUCAO

    r = subprocess.run(
        [sys.executable, "-m", "tools.publicacao", "espaco"],
        cwd=RAIZ, capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(RAIZ),
             "GARRA_SPACE": "garra_reachy_mini_rc"})
    assert r.stdout.strip() == "garra_reachy_mini_rc"

    r = subprocess.run(
        [sys.executable, "-m", "tools.publicacao", "espaco"],
        cwd=RAIZ, capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(RAIZ),
             "GARRA_SPACE": "michelbr/garra_reachy_mini"})
    assert r.returncode == 1, "nome inválido tem de parar o script inteiro"
