"""Barramento: um assinante lento não pode segurar quem move o robô."""

import threading
import time

from garra_reachy_mini.robo.barramento import Barramento


def test_evento_tem_os_campos_de_correlacao():
    b = Barramento()
    e = b.publicar(
        "robot.action.started", action_id="act_1", source="voz",
        correlation_id="msg_9", action="turn_head",
    )
    j = e.json()
    assert j["type"] == "robot.action.started"
    assert j["action_id"] == "act_1"
    assert j["source"] == "voz"
    assert j["correlation_id"] == "msg_9"
    assert j["action"] == "turn_head"
    assert j["event_id"].startswith("evt_")
    assert j["timestamp"].endswith("+00:00")


def test_campos_ausentes_nao_aparecem():
    b = Barramento()
    j = b.publicar("robot.status", connected=True).json()
    assert "action_id" not in j and "source" not in j and "correlation_id" not in j


def test_assinante_recebe():
    b = Barramento()
    with b.assinar() as a:
        b.publicar("robot.status", connected=True)
        evento = a.obter(timeout=1.0)
    assert evento is not None and evento.type == "robot.status"


def test_publicar_nao_bloqueia_com_assinante_entupido():
    """Publicar 5.000 eventos numa fila de 10 tem de ser instantâneo."""
    b = Barramento()
    with b.assinar(maxsize=10) as a:
        inicio = time.monotonic()
        for i in range(5000):
            b.publicar("robot.status", i=i)
        decorrido = time.monotonic() - inicio
        assert decorrido < 2.0, f"publicação bloqueou: {decorrido:.2f}s"
        assert a.descartados > 4000
        assert a.fila.qsize() <= 10


def test_evento_critico_nunca_e_descartado():
    b = Barramento()
    with b.assinar(maxsize=5) as a:
        for i in range(50):
            b.publicar("robot.status", i=i)  # enche e descarta
        b.publicar("robot.error", action="dance", error="boom")
        tipos = []
        while True:
            e = a.obter(timeout=0.05)
            if e is None:
                break
            tipos.append(e.type)
    assert "robot.error" in tipos


def test_historico_mantem_ordem_cronologica():
    b = Barramento()
    for i in range(5):
        b.publicar("robot.status", i=i)
    hist = b.historico(3)
    assert [h["i"] for h in hist] == [2, 3, 4]


def test_historico_filtra_por_tipo():
    b = Barramento()
    b.publicar("robot.status", i=0)
    b.publicar("robot.error", error="x")
    b.publicar("robot.status", i=1)
    hist = b.historico(10, tipos=frozenset({"robot.error"}))
    assert len(hist) == 1 and hist[0]["type"] == "robot.error"


def test_cancelar_assinatura_remove_do_barramento():
    b = Barramento()
    a = b.assinar()
    assert b.n_assinantes == 1
    a.fechar()
    assert b.n_assinantes == 0


def test_concorrencia_de_publicadores():
    b = Barramento()
    with b.assinar(maxsize=10_000) as a:
        def publicar(n):
            for i in range(200):
                b.publicar("robot.status", t=n, i=i)

        threads = [threading.Thread(target=publicar, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert a.fila.qsize() == 1600
