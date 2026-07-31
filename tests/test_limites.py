"""Limites: nada sai daqui fora do envelope, e o que foi limitado é relatado."""

import numpy as np
import pytest

from garra_reachy_mini.robo import limites


def test_clamp_registra_ajuste():
    lim = limites.Limitador()
    assert lim.valor("yaw_deg", 300.0, -45.0, 45.0) == 45.0
    assert len(lim.ajustes) == 1
    assert lim.ajustes[0].pedido == 300.0
    assert lim.ajustes[0].aplicado == 45.0


def test_clamp_dentro_do_limite_nao_registra():
    lim = limites.Limitador()
    assert lim.valor("yaw_deg", 10.0, -45.0, 45.0) == 10.0
    assert lim.ajustes == []


@pytest.mark.parametrize(
    "pedido,esperado",
    [(None, limites.DURACAO_PADRAO_S), (0.0, limites.DURACAO_MIN_S), (999.0, limites.DURACAO_MAX_S)],
)
def test_duracao(pedido, esperado):
    assert limites.Limitador().duracao(pedido) == esperado


def test_intensidade_limitada():
    lim = limites.Limitador()
    assert lim.intensidade(5.0) == 1.0
    assert lim.intensidade(-3.0) == 0.0
    assert lim.intensidade(None) == 1.0


def test_antenas_invertem_para_a_ordem_do_sdk():
    """A API fala esquerda/direita; o SDK espera [direita, esquerda]."""
    lim = limites.Limitador()
    assert lim.antenas(0.3, -0.4) == [-0.4, 0.3]


def test_antenas_limitadas():
    lim = limites.Limitador()
    assert lim.antenas(9.0, -9.0) == [-limites.MAX_ANTENA_RAD, limites.MAX_ANTENA_RAD]
    assert len(lim.ajustes) == 2


def test_pose_cabeca_e_matriz_homogenea_valida():
    pose = limites.pose_cabeca(limites.Limitador(), yaw_deg=20, pitch_deg=10, z_mm=5)
    assert pose.shape == (4, 4)
    assert np.allclose(pose[3], [0, 0, 0, 1])
    # rotação ortonormal
    r = pose[:3, :3]
    assert np.allclose(r @ r.T, np.eye(3), atol=1e-9)
    # translação em metros: 5 mm = 0,005 m
    assert pose[2, 3] == pytest.approx(0.005)


def test_pose_direcao_respeita_intensidade():
    lim = limites.Limitador()
    forte = limites.pose_direcao(lim, "left", 1.0)
    fraca = limites.pose_direcao(lim, "left", 0.25)
    # yaw extraído da matriz: o forte tem de girar mais
    assert abs(np.arctan2(forte[1, 0], forte[0, 0])) > abs(np.arctan2(fraca[1, 0], fraca[0, 0]))
    assert lim.ajustes == []


def test_pose_direcao_nunca_passa_do_envelope():
    lim = limites.Limitador()
    for direcao in limites.DIRECOES:
        pose = limites.pose_direcao(lim, direcao, 1.0)
        yaw = np.degrees(np.arctan2(pose[1, 0], pose[0, 0]))
        assert abs(yaw) <= limites.MAX_YAW_DEG + 1e-6


@pytest.mark.parametrize("nome", ["clawbody", "reachy_mini_conversation_app", "app-1.2"])
def test_nomes_de_app_validos(nome):
    assert limites.nome_app_valido(nome)


@pytest.mark.parametrize(
    "nome",
    ["", "../etc/passwd", "a/b", "app nome", "-comeca-com-traco", "..", "x" * 80, "app;rm"],
)
def test_nomes_de_app_maliciosos_rejeitados(nome):
    assert not limites.nome_app_valido(nome)


def test_limitar_pose_traz_uma_pose_extrema_de_volta():
    """`look_at` do SDK devolve pose pronta; ela também tem de caber no envelope."""
    from reachy_mini.vision.look_at import look_at_world_pose

    lim = limites.Limitador()
    # Ponto bem à esquerda e alto: exige giro além do que permitimos.
    extrema = look_at_world_pose(0.3, 1.5, 1.2)
    yaw_bruto = abs(np.degrees(np.arctan2(extrema[1, 0], extrema[0, 0])))
    assert yaw_bruto > limites.MAX_YAW_DEG, "o teste precisa de uma pose fora do limite"

    limitada = limites.limitar_pose(lim, extrema)
    yaw = abs(np.degrees(np.arctan2(limitada[1, 0], limitada[0, 0])))
    assert yaw <= limites.MAX_YAW_DEG + 1e-6
    assert lim.ajustes, "o ajuste tem de ser relatado, não silencioso"


def test_limitar_pose_nao_mexe_no_que_ja_cabe():
    lim = limites.Limitador()
    dentro = limites.pose_cabeca(limites.Limitador(), yaw_deg=15, pitch_deg=8)
    assert np.allclose(limites.limitar_pose(lim, dentro), dentro, atol=1e-9)
    assert lim.ajustes == []


def test_limitar_pose_rejeita_forma_errada():
    with pytest.raises(ValueError, match="4x4"):
        limites.limitar_pose(limites.Limitador(), np.eye(3))
