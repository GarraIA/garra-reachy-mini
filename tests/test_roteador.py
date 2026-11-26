"""O roteador de ativação: qual conversa um enunciado chama.

"Fala Garra" chama o agente escolhido; "Hera" chama a conversa ao vivo. O
roteador decide o segundo caso ANTES do portão do primeiro, por texto.

O que torna isto difícil é o nome: "Hera" e "era" são a mesma palavra dita em
voz alta. Medido em 25/11/2026 com o Whisper do servidor de voz: "Hera, …" saiu
escrito "Era, …" em 5 de 5; e, dando "Hera" como dica, "Era uma vez um rei" virou
"Hera era uma vez um rei". Por isso o roteador nunca pede dica, aceita a grafia
"era" como o nome, e no rigor padrão só atende o nome dito SOZINHO ou com
saudação — um chamado falso aqui abre conversa paga e manda o som da sala para
fora de casa.
"""

from __future__ import annotations

import pytest

from garra_reachy_mini import conversa, roteador
from garra_reachy_mini.ativacao import MARGEM_ECO_S


def rota(**mudancas) -> roteador.Rota:
    return roteador.Rota.de({"live_voice_enabled": True, **mudancas})


# ─── rigor estrito (o padrão) ────────────────────────────────────────────────
@pytest.mark.parametrize("texto,como,resto", [
    ("Hera", "alone", ""),
    ("Hera.", "alone", ""),
    ("Era.", "alone", ""),               # como o reconhecimento escreve o nome
    ("era", "alone", ""),
    ("Oi Hera", "alone", ""),
    ("Oi, era.", "alone", ""),
    ("Alô, Hera!", "alone", ""),
    ("Oi Hera, que horas são?", "leading", "que horas são?"),
    ("Oi era, que horas são?", "leading", "que horas são?"),
    ("Olá Hera, tudo bem?", "leading", "tudo bem?"),
    ("Ei Hera me diz uma coisa", "leading", "me diz uma coisa"),
])
def test_estrito_atende_o_nome_sozinho_ou_com_saudacao(texto, como, resto):
    chamado = roteador.chamado(texto, rota())
    assert chamado is not None, texto
    assert (chamado.destino, chamado.como, chamado.resto) == ("hera", como, resto)


@pytest.mark.parametrize("texto", [
    "Era uma vez um rei.",               # o caso que a dica transformava em chamado
    "Era isso que eu queria.",
    "Era, tudo bem com você?",           # nome + pedido de uma vez: só no rigor normal
    "Hera, que horas são?",
    "Era a que horas são.",
    "Ela era muito bonita.",
    "A Hera é uma deusa grega.",
    "Vera chegou agora.",
    "Oi, tudo bem?",
    "Fala Garra",
    "Fala Garra, que horas são?",
    "", "   ", "...",
])
def test_estrito_nao_atende_conversa_da_sala(texto):
    assert roteador.chamado(texto, rota()) is None, texto


def test_o_preco_conhecido_do_estrito():
    """Saudação + "era" + conversa tem a mesma forma de um chamado com pedido.
    Fica registrado, não escondido: quem quiser fechar isto tira "Oi Hera" das
    formas e fica só com o nome sozinho."""
    assert roteador.chamado("Oi, era só isso mesmo.", rota()) is not None
    so_o_nome = rota(live_wake_phrase_alternatives=[])
    assert roteador.chamado("Oi, era só isso mesmo.", so_o_nome) is None
    assert roteador.chamado("Hera", so_o_nome) is not None


def test_desligada_nunca_atende():
    desligada = roteador.Rota.de({})
    assert desligada.habilitada is False
    for texto in ("Hera", "Oi Hera, que horas são?"):
        assert roteador.chamado(texto, desligada) is None


def test_nunca_pede_dica_ao_reconhecimento():
    """A dica é o que inventava o chamado. A rota nem tem onde guardar uma."""
    assert not hasattr(rota(), "dica") and not hasattr(rota(), "dicas")


# ─── os outros rigores ───────────────────────────────────────────────────────
def test_so_com_saudacao_o_nome_sozinho_nao_basta():
    r = rota(live_wake_strictness="greeting")
    assert roteador.chamado("Hera", r) is None
    assert roteador.chamado("Era.", r) is None
    assert roteador.chamado("Oi Hera", r).como == "alone"
    assert roteador.chamado("Oi Hera, que horas são?", r).resto == "que horas são?"


@pytest.mark.parametrize("texto,resto", [
    ("Hera, que horas são?", "que horas são?"),
    ("Era, tudo bem com você?", "tudo bem com você?"),
    ("Hera. Me conta uma coisa", "Me conta uma coisa"),
])
def test_normal_aceita_o_nome_seguido_de_pausa_escrita(texto, resto):
    chamado = roteador.chamado(texto, rota(live_wake_strictness="normal"))
    assert chamado is not None and (chamado.como, chamado.resto) == ("leading", resto)


@pytest.mark.parametrize("texto", ["Era uma vez um rei.", "Era a que horas são.", "Era isso que eu queria."])
def test_normal_continua_recusando_o_nome_colado_na_frase(texto):
    assert roteador.chamado(texto, rota(live_wake_strictness="normal")) is None


# ─── eco e parada ────────────────────────────────────────────────────────────
def test_logo_depois_de_o_robo_falar_nao_e_chamado():
    """A própria voz do robô dizendo "era" não pode abrir a conversa ao vivo."""
    r = rota()
    assert roteador.chamado("Era.", r, capturado_em=100.0, falou_ate=100.0 - MARGEM_ECO_S / 2) is None
    assert roteador.chamado("Era.", r, capturado_em=100.0, falou_ate=100.0 - MARGEM_ECO_S - 0.1) is not None


@pytest.mark.parametrize("texto,parada", [
    ("Hera, pare", "pare"),
    ("Era, para!", "para!"),
    ("Oi Hera, pare por favor", "pare por favor"),
    ("Hera pare", "pare"),
])
def test_o_nome_na_frente_nao_esconde_o_pare(texto, parada):
    assert roteador.parada(texto, rota()) == parada
    # …e parada nunca é chamado, em rigor nenhum.
    for rigor in roteador.RIGORES:
        r = rota(live_wake_strictness=rigor)
        assert roteador.parada(texto, r) == parada


@pytest.mark.parametrize("texto", ["Hera, que horas são?", "Oi Hera", "Hera", "pare", "Era uma vez", ""])
def test_sem_parada_depois_do_nome_nada_muda(texto):
    assert roteador.parada(texto, rota()) is None


def test_com_a_conversa_ao_vivo_desligada_o_nome_nao_e_tirado():
    assert roteador.parada("Hera, pare", roteador.Rota.de({})) is None


# ─── quase chamado: o nome na frente, sem a saudação ─────────────────────────
# Medido em 26/11/2026: "Hera, que horas são?" não chamou no rigor estrito, e
# nada no painel dizia por quê. `quase` responde "teria chamado no rigor normal,
# mas não no de agora" — só a forma, nunca o que foi dito.
@pytest.mark.parametrize("texto", [
    "Hera, que horas são?",
    "Era, tudo bem com você?",
    "Hera. Me conta uma coisa",
    "Era, sim.",
])
def test_estrito_o_nome_com_pedido_sem_saudacao_e_quase(texto):
    assert roteador.chamado(texto, rota()) is None
    assert roteador.quase(texto, rota()) == roteador.Quase("hera", "leading", "strict")


@pytest.mark.parametrize("texto", [
    "Hera", "Era.", "Oi Hera", "Oi Hera, que horas são?",    # chamam: não é quase
    "Era uma vez um rei.", "Era isso que eu queria.",        # o nome colado na frase
    "A Hera é uma deusa grega.", "Vera, vem cá.", "Oi, tudo bem?",
    "Fala Garra, que horas são?", "", "   ", "...",
])
def test_o_que_chama_ou_nao_comeca_pelo_nome_com_pausa_nao_e_quase(texto):
    assert roteador.quase(texto, rota()) is None, texto


def test_so_com_saudacao_o_nome_sozinho_tambem_e_quase():
    r = rota(live_wake_strictness="greeting")
    assert roteador.quase("Hera.", r) == roteador.Quase("hera", "alone", "greeting")
    assert roteador.quase("Hera, que horas são?", r).como == "leading"
    assert roteador.quase("Oi Hera", r) is None


def test_no_rigor_normal_e_com_a_hera_desligada_nunca_ha_quase():
    assert roteador.quase("Hera, que horas são?", rota(live_wake_strictness="normal")) is None
    assert roteador.quase("Hera, que horas são?", roteador.Rota.de({})) is None


def test_hera_pare_e_parada_e_nao_quase():
    assert roteador.quase("Hera, pare", rota()) is None
    assert roteador.quase("Era, para!", rota()) is None


def test_a_propria_voz_logo_depois_de_falar_nao_e_quase():
    r = rota()
    assert roteador.quase("Era, sim.", r, capturado_em=100.0, falou_ate=100.0 - MARGEM_ECO_S / 2) is None
    assert roteador.quase("Era, sim.", r, capturado_em=100.0, falou_ate=100.0 - MARGEM_ECO_S - 0.1).como == "leading"


def test_o_quase_nao_carrega_o_que_foi_dito():
    q = roteador.quase("Hera, qual é a senha do cofre?", rota())
    assert "cofre" not in repr(q) and set(vars(q)) == {"destino", "como", "rigor"}


# ─── frases escolhidas pelo dono ─────────────────────────────────────────────
def test_frase_e_grafias_personalizadas():
    r = rota(live_wake_phrase_text="Atena", live_wake_spellings=["Atenas", "A tena"],
             live_wake_phrase_alternatives=["Bom dia Atena"])
    assert roteador.chamado("Atenas.", r).como == "alone"
    assert roteador.chamado("Bom dia, Atenas! Como vai?", r).resto == "Como vai?"
    assert roteador.chamado("Hera", r) is None
    assert roteador.chamado("A tena", r) is None        # grafia tem de ser UMA palavra


def test_a_forma_mais_longa_ganha():
    r = rota(live_wake_phrase_alternatives=["Oi Hera", "Oi Hera querida"])
    assert roteador.chamado("Oi Hera querida, que dia é hoje?", r).resto == "que dia é hoje?"


# ─── não pisar nas frases do Garra ───────────────────────────────────────────
def test_forma_que_comeca_como_a_frase_do_garra_fica_de_fora():
    """ "Fala Hera" e "Fala Garra" diferem num som: o erro de um viraria o outro."""
    r = rota(wake_phrase_text="Fala Garra",
             live_wake_phrase_alternatives=["Fala Hera", "Oi Hera"])
    assert roteador.chamado("Fala Hera, como vai?", r) is None
    assert roteador.chamado("Fala era", r) is None
    assert roteador.chamado("Oi Hera", r) is not None
    assert r.ignoradas == ("Fala Hera",)


def test_alternativa_do_garra_tambem_conta():
    r = rota(wake_phrase_alternatives=["Oi Garra"])
    assert roteador.chamado("Oi Hera", r) is None and "Oi Hera" in r.ignoradas
    assert roteador.chamado("Olá Hera", r) is not None


def test_se_o_garra_atende_por_hera_a_rota_fica_sem_o_nome():
    r = rota(wake_phrase_text="Hera")
    assert roteador.chamado("Hera", r) is None
    assert "Hera" in r.ignoradas


# ─── a configuração ──────────────────────────────────────────────────────────
def test_padroes():
    c = conversa.normalizar({})
    assert c["live_voice_enabled"] is False
    assert c["live_wake_phrase_text"] == "Hera"
    assert c["live_wake_phrase_alternatives"] == ["Oi Hera", "Olá Hera", "Ei Hera", "Alô Hera"]
    assert c["live_wake_spellings"] == ["Era"]
    assert c["live_wake_strictness"] == "strict"
    assert c["live_idle_close_s"] == 30
    # Listas do padrão não podem ser compartilhadas entre leituras.
    c["live_wake_spellings"].append("x")
    assert conversa.normalizar({})["live_wake_spellings"] == ["Era"]


@pytest.mark.parametrize("mudanca,chave,esperado", [
    ({"live_wake_phrase_text": "   "}, "live_wake_phrase_text", "Hera"),
    ({"live_wake_phrase_text": "!!!"}, "live_wake_phrase_text", "Hera"),
    ({"live_wake_phrase_text": "Atena"}, "live_wake_phrase_text", "Atena"),
    ({"live_wake_phrase_alternatives": "Oi Hera"}, "live_wake_phrase_alternatives",
     ["Oi Hera", "Olá Hera", "Ei Hera", "Alô Hera"]),
    ({"live_wake_phrase_alternatives": ["Oi Hera", "oi hera", "", "Salve Hera"]},
     "live_wake_phrase_alternatives", ["Oi Hera", "Salve Hera"]),
    ({"live_wake_phrase_alternatives": list("abcdefgh")}, "live_wake_phrase_alternatives", list("abcd")),
    ({"live_wake_spellings": ["Era", "Éra", "duas palavras", "", "Hera"]}, "live_wake_spellings", ["Era", "Hera"]),
    ({"live_wake_spellings": "Era"}, "live_wake_spellings", ["Era"]),
    ({"live_wake_strictness": "normal"}, "live_wake_strictness", "normal"),
    ({"live_wake_strictness": "qualquer"}, "live_wake_strictness", "strict"),
    ({"live_idle_close_s": 1}, "live_idle_close_s", 10),
    ({"live_idle_close_s": 999}, "live_idle_close_s", 120),
    ({"live_voice_enabled": 1}, "live_voice_enabled", True),
])
def test_a_escrita_e_a_leitura_validam_igual(mudanca, chave, esperado):
    base = conversa.normalizar({})
    assert conversa.perfil_atualizado(base, mudanca)[chave] == esperado
    assert conversa.normalizar({**base, **mudanca})[chave] == esperado
