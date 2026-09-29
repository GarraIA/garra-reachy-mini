"""O que acompanha o pedido: a escuta de contexto e o caderno, ligados ao app.

`ambiente`, `caderno` e `envelope` não sabem de configuração nem de evento. Quem
sabe é `Acompanhamento`, e é aqui que mora a promessa de privacidade: com o
recurso desligado, o que foi dito sem a frase de ativação não fica em lugar
nenhum.
"""

from __future__ import annotations

import json

import pytest

from garra_reachy_mini import acompanhamento as mod
from garra_reachy_mini import conversa, envelope


class Eventos:
    def __init__(self) -> None:
        self.publicados: list[tuple[str, dict]] = []

    def publicar(self, tipo: str, **campos) -> None:
        self.publicados.append((tipo, campos))

    def de(self, tipo: str) -> list[dict]:
        return [c for t, c in self.publicados if t == tipo]


def conf(**mudancas) -> dict:
    return conversa.perfil_atualizado(conversa.normalizar({}), mudancas)


TUDO = dict(wake_phrase_enabled=True, ambient_context_enabled=True, notebook_enabled=True)
SO_CONTEXTO = dict(wake_phrase_enabled=True, ambient_context_enabled=True)
SO_CADERNO = dict(notebook_enabled=True)


@pytest.fixture()
def ac(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    a = mod.Acompanhamento(Eventos())
    return a


# ─── configuração ────────────────────────────────────────────────────────────
def test_tudo_nasce_desligado() -> None:
    """Ligar muda o que sai de casa: ninguém pode descobrir isso num upgrade."""
    c = conversa.normalizar({})
    assert c["ambient_context_enabled"] is False
    assert c["notebook_enabled"] is False
    assert c["ambient_context_window_s"] == 120


def test_config_antigo_recebe_os_padroes() -> None:
    c = conversa.normalizar({"mode": "informative", "wake_phrase_enabled": True})
    assert c["ambient_context_enabled"] is False and c["notebook_enabled"] is False


def test_a_janela_tem_piso_e_teto() -> None:
    assert conf(ambient_context_window_s=1)["ambient_context_window_s"] == 30
    assert conf(ambient_context_window_s=9999)["ambient_context_window_s"] == 300
    assert conf(ambient_context_window_s="x")["ambient_context_window_s"] == 120


# ─── o que é dito sem chamar o robô ──────────────────────────────────────────
def test_desligado_nada_do_que_foi_ouvido_fica_guardado(ac) -> None:
    """A promessa de hoje, e o critério de aceite nº 3."""
    assert ac.ouviu("acabou a manteiga", 100.0, conf(wake_phrase_enabled=True)) is False
    assert len(ac.memoria) == 0
    assert ac.montar("que horas são?", 110.0, conf(wake_phrase_enabled=True)) == \
        "que horas são?"


def test_ligado_o_que_foi_ouvido_acompanha_o_pedido(ac) -> None:
    c = conf(**SO_CONTEXTO)
    assert ac.ouviu("acabou a manteiga", 100.0, c) is True
    assert ac.ouviu("tem que comprar mais", 130.0, c) is True
    m = ac.montar("insira isso na lista de compras", 195.0, c)
    assert "> há 95 s: acabou a manteiga" in m
    assert "> há 65 s: tem que comprar mais" in m
    assert envelope.pedido_de(m) == "insira isso na lista de compras"


def test_sem_frase_de_ativacao_nao_existe_ambiente(ac) -> None:
    """Sem a frase toda fala já é um turno; guardar seria mandar tudo em dobro."""
    c = conf(ambient_context_enabled=True, wake_phrase_enabled=False)
    assert ac.ouviu("acabou a manteiga", 100.0, c) is False
    assert len(ac.memoria) == 0


def test_o_contexto_vai_uma_vez_so(ac) -> None:
    c = conf(**SO_CONTEXTO)
    ac.ouviu("acabou a manteiga", 100.0, c)
    assert "manteiga" in ac.montar("anota isso", 110.0, c)
    assert ac.montar("e o que mais?", 120.0, c) == "e o que mais?"


def test_desligar_esquece_na_hora_o_que_ja_tinha_sido_ouvido(ac) -> None:
    """Quem desliga não pode ter dois minutos de conversa esperando a religada."""
    ac.ouviu("segredo de família", 100.0, conf(**SO_CONTEXTO))
    ac.ouviu("outra coisa", 101.0, conf(wake_phrase_enabled=True))
    assert len(ac.memoria) == 0
    assert ac.montar("oi", 102.0, conf(**SO_CONTEXTO)) == "oi"


def test_desligar_a_frase_de_ativacao_tambem_esquece(ac) -> None:
    ac.ouviu("segredo de família", 100.0, conf(**SO_CONTEXTO))
    ac.montar("oi", 101.0, conf(ambient_context_enabled=True, wake_phrase_enabled=False))
    assert len(ac.memoria) == 0


def test_a_janela_e_a_da_configuracao(ac) -> None:
    c = conf(**SO_CONTEXTO, ambient_context_window_s=30)
    ac.ouviu("isto é velho", 100.0, c)
    ac.ouviu("isto é recente", 150.0, c)
    m = ac.montar("resuma", 160.0, c)
    assert "recente" in m and "velho" not in m


def test_o_chat_do_painel_nao_leva_nem_gasta_o_que_foi_ouvido(ac) -> None:
    """Quem digita não estava sendo ouvido; o contexto espera o pedido por voz."""
    c = conf(**TUDO)
    ac.ouviu("acabou a manteiga", 100.0, c)
    digitado = ac.montar("o que tem na lista?", 110.0, c, com_ambiente=False)
    assert "manteiga" not in digitado and "CADERNO DO ROBÔ" in digitado
    assert "manteiga" in ac.montar("anota isso", 120.0, c)


def test_esquecer_limpa_e_diz_quantas_falas_havia(ac) -> None:
    c = conf(**SO_CONTEXTO)
    ac.ouviu("uma", 100.0, c)
    ac.ouviu("duas", 101.0, c)
    assert ac.esquecer() == 2
    assert ac.esquecer() == 0
    assert ac.montar("oi", 102.0, c) == "oi"


# ─── caderno ─────────────────────────────────────────────────────────────────
def test_com_o_caderno_ligado_o_pedido_leva_o_que_esta_anotado(ac) -> None:
    ac.caderno.adicionar("compras", "leite")
    m = ac.montar("o que tem na lista?", 100.0, conf(**SO_CADERNO))
    assert "> compras: leite" in m.split("\n")
    assert "CONTEXTO DO AMBIENTE" not in m


def test_a_resposta_anota_e_a_diretiva_nao_e_falada(ac) -> None:
    fala = ac.receber("Anotado, manteiga na lista.\nCADERNO: +compras: manteiga",
                      conf(**SO_CADERNO))
    assert fala == "Anotado, manteiga na lista."
    assert ac.caderno.ler() == {"compras": ["manteiga"]}


def test_com_o_caderno_desligado_a_diretiva_nao_anota_mas_tambem_nao_e_falada(ac) -> None:
    """A sessão do gateway lembra do formato mesmo depois de o dono desligar."""
    fala = ac.receber("Anotado.\nCADERNO: +compras: manteiga", conf())
    assert fala == "Anotado."
    assert ac.caderno.ler() == {}
    assert ac.eventos.de("notebook.changed") == []


def test_resposta_comum_passa_intacta(ac) -> None:
    for c in (conf(), conf(**TUDO)):
        assert ac.receber("São três horas.  Em ponto.", c) == "São três horas.  Em ponto."


# ─── eventos: contagem, nunca conteúdo ───────────────────────────────────────
def test_o_evento_de_contexto_conta_e_nao_conta_o_que_foi_dito(ac) -> None:
    c = conf(**SO_CONTEXTO)
    ac.ouviu("o número do cartão é 4111", 100.0, c)
    ac.ouviu("acabou a manteiga", 140.0, c)
    ac.montar("anota isso", 160.0, c, correlacao="voz_1")
    [ev] = ac.eventos.de("voice.context.attached")
    assert ev == {"correlation_id": "voz_1", "utterances": 2, "span_s": 60,
                  "chars": len("o número do cartão é 4111") + len("acabou a manteiga")}


def test_sem_contexto_nao_ha_evento(ac) -> None:
    ac.montar("oi", 100.0, conf(**TUDO))
    assert ac.eventos.de("voice.context.attached") == []


def test_o_evento_do_caderno_diz_a_lista_e_nao_o_item(ac) -> None:
    ac.receber("Ok.\nCADERNO: +compras: remédio de pressão\nCADERNO: -compras: nada disso",
               conf(**SO_CADERNO), correlacao="voz_2")
    evs = ac.eventos.de("notebook.changed")
    assert evs == [
        {"correlation_id": "voz_2", "list": "compras", "op": "add", "applied": True, "items": 1},
        {"correlation_id": "voz_2", "list": "compras", "op": "remove", "applied": False,
         "items": 1},
    ]


def test_nenhum_evento_carrega_o_que_foi_dito_ou_anotado(ac) -> None:
    c = conf(**TUDO)
    ac.ouviu("segredo de família", 100.0, c)
    ac.montar("anota o aniversário surpresa", 110.0, c, correlacao="x")
    ac.receber("Feito.\nCADERNO: +lembretes: aniversário surpresa", c, correlacao="x")
    tudo = json.dumps(ac.eventos.publicados, ensure_ascii=False)
    for palavra in ("segredo", "família", "aniversário", "surpresa"):
        assert palavra not in tudo
