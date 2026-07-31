"""Atalhos locais: pegar o que é ordem curta sem roubar o que é conversa."""

import pytest

from garra_reachy_mini.robo.intencoes import ResultadoIntencao, normalizar, reconhecer


def test_normalizar_tira_acento_e_pontuacao():
    assert normalizar("  Olhe,  para a DIREITA!  ") == "olhe para a direita"
    assert normalizar("Faça que sim?") == "faca que sim"


@pytest.mark.parametrize("frase", ["pare", "para", "PARE!", "stop", "chega",
                                   "garra, pare", "pare por favor", "para tudo"])
def test_parada_reconhecida(frase):
    r = reconhecer(frase)
    assert r.tratada and r.acao == "stop"
    assert r.encaminhar_ao_agente is False, "parar não espera o modelo"
    assert r.resposta_imediata == "Parei."


@pytest.mark.parametrize("frase", ["dance", "dança", "dance comigo", "vamos dançar",
                                   "garra, dance para mim", "let's dance"])
def test_danca_reconhecida(frase):
    r = reconhecer(frase)
    assert r.tratada and r.acao == "dance"
    assert r.encaminhar_ao_agente is True, "quem fala continua sendo o Garra"


@pytest.mark.parametrize(
    "frase,direcao",
    [
        ("vire a cabeça para a direita", "right"),
        ("olhe para a esquerda", "left"),
        ("olhe para cima", "up"),
        ("vira pra baixo", "down"),
        ("turn your head to the right", "right"),
        ("look left", "left"),
        ("olhe para a frente", "center"),
    ],
)
def test_direcoes(frase, direcao):
    r = reconhecer(frase)
    assert r.tratada and r.acao == "turn_head"
    assert r.params["direction"] == direcao


@pytest.mark.parametrize(
    "frase,emocao",
    [
        ("fique feliz", "happy"),
        ("fique triste", "sad"),
        ("fique curioso", "curious"),
        ("fique surpreso", "surprised"),
        ("be happy", "happy"),
        ("fique neutro", "neutral"),
    ],
)
def test_expressoes(frase, emocao):
    r = reconhecer(frase)
    assert r.tratada and r.acao == "set_expression"
    assert r.params["name"] == emocao


def test_sim_e_nao_com_a_cabeca():
    assert reconhecer("faça que sim").acao == "nod"
    assert reconhecer("faça que não").acao == "shake_head"
    assert reconhecer("nod").acao == "nod"


def test_olhe_para_mim_liga_o_rastreamento():
    r = reconhecer("olhe para mim")
    assert r.acao == "look_at" and r.params == {"target": "user"}


def test_volta_ao_neutro():
    for frase in ("volte para a posição inicial", "centralize", "reset", "center"):
        assert reconhecer(frase).acao == "return_to_neutral", frase


# ─── o que NÃO pode ser capturado ────────────────────────────────────────────
@pytest.mark.parametrize(
    "frase",
    [
        "pare de falar sobre dança",
        "você sabe dançar?",
        "o que você acha de dança de salão",
        "me explique como funciona a dança das abelhas",
        "eu queria parar de fumar, alguma dica?",
        "quem inventou a dança do ventre",
        "olhe, eu queria saber uma coisa sobre a direita política",
        "estou triste hoje",
        "o carro virou para a direita na esquina e capotou",
    ],
)
def test_conversa_nao_vira_comando(frase):
    r = reconhecer(frase)
    assert not r.tratada, f"o atalho sequestrou uma conversa: {frase!r}"


def test_frase_longa_vai_direto_ao_modelo():
    assert not reconhecer("dance " * 40).tratada


def test_frase_vazia():
    assert not reconhecer("").tratada
    assert not reconhecer("   ").tratada


# ─── anti-execução dupla ─────────────────────────────────────────────────────
def test_atalho_bloqueia_ferramenta_fisica_do_modelo():
    for frase in ("dance", "olhe para a direita", "fique feliz", "faça que sim"):
        r = reconhecer(frase)
        assert r.ferramentas_fisicas_liberadas is False, frase


def test_aviso_diz_ao_modelo_o_que_ja_foi_feito():
    r = reconhecer("dance")
    aviso = r.aviso_para_o_agente("act_abc", "O robô dançou (simple_nod).")
    assert "act_abc" in aviso
    assert "`dance`" in aviso
    assert "NÃO chame outra ferramenta de movimento" in aviso
    assert "não é fala do usuário" in aviso


def test_intencao_nao_tratada_e_inofensiva():
    r = ResultadoIntencao(tratada=False)
    assert r.acao is None and r.encaminhar_ao_agente is True
