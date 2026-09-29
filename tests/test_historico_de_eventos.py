"""O histórico de eventos não pode ser engolido pelo enfeite.

Medido no robô em 29/09/2026: dos 400 eventos guardados, 358 eram do movimento
de ambiente (antenas e micro-movimentos, três eventos cada, a cada poucos
segundos) e 40 eram mudanças de estado da voz. Sobravam dois de conversa. Tudo o
que interessa — quem chamou o robô, o que ele respondeu, o que anotou — saía do
histórico em sete minutos.
"""

from __future__ import annotations

from garra_reachy_mini.robo.barramento import Barramento


def rotina(b: Barramento, vezes: int) -> None:
    for i in range(vezes):
        b.publicar("robot.action.queued", source="ambiente", action=f"a{i}")
        b.publicar("robot.action.started", source="ambiente", action=f"a{i}")
        b.publicar("robot.action.completed", source="ambiente", action=f"a{i}")
        b.publicar("voice.state", state="listening")


def test_o_enfeite_nao_expulsa_a_conversa() -> None:
    b = Barramento(tamanho_historico=50)
    b.publicar("voice.wake.activated", match="exact")
    b.publicar("chat.message", role="user", content="que horas são?", source="voz")
    b.publicar("notebook.changed", list="compras", op="add")
    rotina(b, 200)

    tipos = [e["type"] for e in b.historico(50)]
    assert "voice.wake.activated" in tipos
    assert "chat.message" in tipos
    assert "notebook.changed" in tipos


def test_o_que_sobra_do_limite_e_preenchido_com_a_rotina_mais_recente() -> None:
    b = Barramento(tamanho_historico=50)
    b.publicar("chat.message", role="user", content="oi", source="voz")
    rotina(b, 200)

    eventos = b.historico(20)
    assert len(eventos) == 20
    assert eventos[0]["type"] == "chat.message", "na ordem em que aconteceram"
    assert [e["action"] for e in eventos if "action" in e][-1] == "a199"


def test_a_ordem_e_a_dos_acontecimentos() -> None:
    b = Barramento(tamanho_historico=50)
    b.publicar("chat.message", role="user", content="um", source="voz")
    rotina(b, 2)
    b.publicar("chat.message", role="user", content="dois", source="voz")
    rotina(b, 2)

    tipos = [e.get("content") or e["type"] for e in b.historico(50)]
    assert tipos.index("um") < tipos.index("robot.action.queued") < tipos.index("dois")
    numeros = [int(e["event_id"].split("_")[1]) for e in b.historico(50)]
    assert numeros == sorted(numeros)


def test_acao_pedida_por_alguem_nao_e_rotina() -> None:
    """Só o movimento de AMBIENTE é enfeite. O que o painel, a voz ou o modelo
    pediram é o que se quer encontrar depois."""
    b = Barramento(tamanho_historico=50)
    b.publicar("robot.action.completed", source="painel", action="olhar")
    b.publicar("robot.action.failed", source="ambiente", action="falhou")
    rotina(b, 200)

    acoes = [e.get("action") for e in b.historico(50)]
    assert "olhar" in acoes
    assert "falhou" in acoes, "falha nunca é rotina, venha de onde vier"


def test_o_historico_duravel_tambem_tem_teto() -> None:
    b = Barramento(tamanho_historico=50)
    for i in range(500):
        b.publicar("chat.message", role="user", content=str(i), source="voz")
    eventos = b.historico(1000)
    assert len(eventos) == 50
    assert eventos[-1]["content"] == "499" and eventos[0]["content"] == "450"


def test_o_filtro_por_tipo_continua_valendo() -> None:
    b = Barramento(tamanho_historico=50)
    b.publicar("chat.message", role="user", content="oi", source="voz")
    rotina(b, 100)
    so_chat = b.historico(10, frozenset({"chat.message"}))
    assert [e["content"] for e in so_chat] == ["oi"]
    so_estado = b.historico(5, frozenset({"voice.state"}))
    assert len(so_estado) == 5


def test_nenhum_evento_aparece_duas_vezes() -> None:
    b = Barramento(tamanho_historico=50)
    for i in range(10):
        b.publicar("chat.message", role="user", content=str(i), source="voz")
    rotina(b, 5)
    ids = [e["event_id"] for e in b.historico(100)]
    assert len(ids) == len(set(ids)) == 30
