"""O status da câmera não pode continuar dizendo que espera o primeiro quadro.

`_montar_robo` marcava a câmera **uma vez**, no arranque, com janela de 5 s. O
hub costuma levar mais que isso para entregar o primeiro quadro, e quem perdia
essa corrida ficava em `no_frame` para sempre — nada remarcava.

Medido num robô saudável: `camera.available` verdadeiro, `seq` avançando
3104 → 3116, `stale` falso, e o registro ainda dizendo "waiting for the first
frame". O robô inteiro aparecia como `limited`, e a causa era um relógio, não
uma câmera.
"""

from __future__ import annotations

from garra_reachy_mini.servicos import Servicos, marcar_camera


class HubFalso:
    """Só o que `marcar_camera` toca: `instantaneo(idade_maxima_s=…)`."""

    def __init__(self, quadro=None) -> None:
        self.quadro = quadro
        self.chamadas: list[float] = []

    def instantaneo(self, idade_maxima_s: float = 5.0):
        self.chamadas.append(idade_maxima_s)
        return self.quadro


def estado(s: Servicos) -> dict:
    return {x["name"]: x for x in s.json()["services"]}["camera"]


def test_primeiro_quadro_dentro_da_janela():
    s, hub = Servicos(), HubFalso(quadro=b"jpeg")
    assert marcar_camera(s, hub) is True
    e = estado(s)
    assert e["available"] is True and e["reason_code"] == "ok"
    assert e["detail"] == "streaming"


def test_primeiro_quadro_depois_da_janela_e_recuperado():
    """O caso medido: perdeu a corrida no arranque, e o laço conserta."""
    s, hub = Servicos(), HubFalso(quadro=None)
    marcar_camera(s, hub)
    assert estado(s)["reason_code"] == "no_frame"
    assert estado(s)["available"] is False
    hub.quadro = b"jpeg"          # o hub finalmente entregou
    assert marcar_camera(s, hub) is True
    assert estado(s)["available"] is True
    assert estado(s)["detail"] == "streaming"


def test_camera_que_cai_depois_de_funcionar_volta_a_aparecer_como_fora():
    """A remarcação corta nos dois sentidos — senão trocaríamos uma mentira
    por outra."""
    s, hub = Servicos(), HubFalso(quadro=b"jpeg")
    marcar_camera(s, hub)
    hub.quadro = None
    assert marcar_camera(s, hub) is False
    assert estado(s)["available"] is False
    assert estado(s)["reason_code"] == "no_frame"


def test_alterna_quantas_vezes_precisar():
    s, hub = Servicos(), HubFalso()
    for esperado in (False, True, False, True):
        hub.quadro = b"jpeg" if esperado else None
        assert marcar_camera(s, hub) is esperado
        assert estado(s)["available"] is esperado


def test_sem_hub_nao_estoura():
    """No modo simulado o hub pode não existir; isso não é motivo para cair."""
    s = Servicos()
    assert marcar_camera(s, None) is False
    assert estado(s)["available"] is False
    # E o robô fica `limited` por um motivo verdadeiro, não por um relógio.
    assert "camera" in s.json()["missing"]


def test_a_dica_so_aparece_quando_ha_problema():
    s, hub = Servicos(), HubFalso(quadro=b"jpeg")
    marcar_camera(s, hub)
    assert not estado(s).get("hint")
    hub.quadro = None
    marcar_camera(s, hub)
    assert "few seconds" in estado(s)["hint"]


def test_a_janela_pedida_e_repassada_ao_hub():
    s, hub = Servicos(), HubFalso(quadro=b"jpeg")
    marcar_camera(s, hub, idade_maxima_s=2.0)
    assert hub.chamadas == [2.0]


# ── o laço precisa chamar de verdade ─────────────────────────────────────────
import pathlib  # noqa: E402

FONTE = (pathlib.Path(__file__).resolve().parents[1]
         / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")


def test_o_laco_periodico_remarca_a_camera():
    """Um teste de unidade não prova que alguém chama. Este prova."""
    trecho = FONTE[FONTE.index("RECONFERIR_CEREBRO_S and not em_fala"):]
    trecho = trecho[:trecho.index("if not voz.pronto(")]
    assert "marcar_camera(self.servicos, self.hub)" in trecho


def test_o_arranque_usa_a_mesma_funcao():
    """Duas implementações divergiriam com o tempo; há uma só."""
    assert FONTE.count("servicos_mod.marcar_camera(") == 2
    assert 'self.servicos.marcar(\n            "camera"' not in FONTE
