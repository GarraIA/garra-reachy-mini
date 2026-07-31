"""Catálogo: allowlist, schemas e resolução de expressões contra o robô real."""

import pytest

from garra_reachy_mini.robo import catalogo
from garra_reachy_mini.robo.catalogo import AcaoFalhou


def test_toda_acao_tem_schema_fechado():
    for acao in catalogo.ACOES:
        assert acao.schema["additionalProperties"] is False, acao.nome
        assert acao.descricao.strip(), acao.nome


def test_toda_acao_de_movimento_tem_handler():
    for acao in catalogo.ACOES:
        if not acao.interna:
            assert acao.handler is not None, acao.nome


def test_acao_desconhecida():
    with pytest.raises(AcaoFalhou, match="desconhecida"):
        catalogo.validar("virar_tudo", {})


def test_parametro_desconhecido_rejeitado():
    with pytest.raises(AcaoFalhou, match="não aceitos"):
        catalogo.validar("turn_head", {"direction": "left", "velocidade": 9})


def test_parametro_obrigatorio():
    with pytest.raises(AcaoFalhou, match="exige"):
        catalogo.validar("turn_head", {})


def test_enum_invalido():
    with pytest.raises(AcaoFalhou, match="não é válido"):
        catalogo.validar("turn_head", {"direction": "diagonal_maluca"})


def test_tipo_errado():
    with pytest.raises(AcaoFalhou, match="numérico"):
        catalogo.validar("turn_head", {"direction": "left", "intensity": "muito"})
    with pytest.raises(AcaoFalhou, match="booleano"):
        catalogo.validar("face_tracking", {"enabled": "sim"})


def test_validacao_normaliza_numeros():
    limpos = catalogo.validar("turn_head", {"direction": "left", "intensity": 1})
    assert isinstance(limpos["intensity"], float)


def test_resolver_expressoes_usa_a_primeira_disponivel():
    resolvido = catalogo.resolver_expressoes(["enthusiastic1", "sad1"])
    # `happy` prefere cheerful1, que não existe nessa lista → cai para enthusiastic1
    assert resolvido["happy"] == "enthusiastic1"
    assert resolvido["sad"] == "sad1"
    assert resolvido["curious"] is None
    assert resolvido["neutral"] is None  # neutral não usa move gravado


def test_expressoes_principais_existem_no_mapa():
    for alias in catalogo.EXPRESSOES_PRINCIPAIS:
        assert alias in catalogo.EXPRESSOES


BIBLIOTECA_REAL = [
    "understanding2", "scared1", "displeased1", "sad2", "curious1", "dance3", "waiting",
    "yes_sad1", "shy1", "resigned1", "relief1", "dying1", "go_away1", "attentive1",
    "exhausted1", "reprimand2", "come1", "surprised1", "attentive2", "indifferent1",
    "thoughtful1", "laughing1", "inquiring1", "fear1", "impatient1", "contempt1",
    "helpful2", "success1", "reprimand1", "uncomfortable1", "lost1", "dance1", "relief2",
    "frustrated1", "boredom1", "grateful1", "proud1", "inquiring3", "confused1",
    "irritated1", "welcoming2", "impatient2", "no_sad1", "no1", "cheerful1", "proud2",
    "inquiring2", "amazed1", "surprised2", "mini-deep-sleep", "disgusted1", "uncertain1",
    "welcoming1", "anxiety1", "sad1", "reprimand3", "irritated2", "oops2", "sleep1",
    "serenity1", "calming1", "electric1", "yes1", "loving1", "incomprehensible2",
    "understanding1", "enthusiastic1", "rage1", "success2", "tired1", "oops1",
    "toc-toc-toc", "wake-mini-up", "lonely1", "furious1", "thoughtful2", "proud3",
    "helpful1", "boredom2", "downcast1", "laughing2", "displeased2", "no_excited1",
    "dance2", "enthusiastic2",
]


def test_todos_os_aliases_resolvem_na_biblioteca_do_robo():
    """Guarda contra alias inventado.

    A lista acima foi lida do daemon em 2026-07-30. Se a biblioteca mudar, este
    teste falha e o mapa canônico é corrigido — em vez de o robô só não fazer
    nada em produção.
    """
    resolvido = catalogo.resolver_expressoes(BIBLIOTECA_REAL)
    faltando = [a for a, real in resolvido.items() if real is None and a != "neutral"]
    assert faltando == []


def test_permitidas_em_estop_sao_todas_sem_movimento():
    for nome in catalogo.PERMITIDAS_EM_ESTOP:
        assert catalogo.CATALOGO[nome].movimento is False, nome


def test_estop_permite_o_minimo_exigido():
    for obrigatoria in ("status", "clear_estop", "disable_motors"):
        assert obrigatoria in catalogo.PERMITIDAS_EM_ESTOP
