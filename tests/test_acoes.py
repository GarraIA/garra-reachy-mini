"""Controlador: preempção, parada de emergência, honestidade e concorrência."""

from __future__ import annotations

import threading
import time

from garra_reachy_mini.robo.acoes import (
    PRIO_AMBIENTE,
    PRIO_EXPLICITA,
    ControladorRobo,
    EstadoControlador,
)


# ─── honestidade ─────────────────────────────────────────────────────────────
def test_modo_real_diz_que_executou(controlador):
    r = controlador.submeter("turn_head", {"direction": "right"})
    assert r.ok and r.accepted and r.executed
    assert r.mode == "real" and r.state == "completed"
    assert "direita" in r.message


def test_modo_simulado_nunca_diz_que_executou(controlador_simulado):
    r = controlador_simulado.submeter("turn_head", {"direction": "right"})
    assert r.accepted is True
    assert r.executed is False, "simulado não pode alegar movimento físico"
    assert r.mode == "simulated"
    assert "simulada" in r.message.lower()


def test_valores_fora_do_limite_sao_relatados(controlador):
    r = controlador.submeter("move_antennas", {"left": 9.0, "right": -9.0})
    assert r.executed
    assert any("antenna_left" in a for a in r.adjustments)
    assert any("antenna_right" in a for a in r.adjustments)


def test_parametro_invalido_nem_chega_ao_robo(controlador, backend):
    r = controlador.submeter("turn_head", {"direction": "para_o_teto"})
    assert not r.ok and not r.accepted and r.state == "rejected"
    assert backend.ops("goto") == []


# ─── parada de emergência ────────────────────────────────────────────────────
def test_estop_cancela_o_que_esta_rodando(controlador, backend):
    backend.duracao_move = 3.0
    resultado: list = []
    t = threading.Thread(target=lambda: resultado.append(controlador.submeter("dance")))
    t.start()
    _esperar(lambda: controlador.fila()["current"] is not None, 2.0)

    r = controlador.parar_tudo()
    assert r.executed and r.state == "completed"
    t.join(timeout=3.0)
    assert resultado and resultado[0].state == "cancelled"
    assert resultado[0].executed is False
    assert controlador.estado is EstadoControlador.ESTOPPED


def test_estop_nao_volta_ao_neutro(controlador, backend):
    """Voltar ao neutro é começar um movimento — o oposto de uma parada."""
    controlador.submeter("turn_head", {"direction": "left"})
    antes = len(backend.ops("goto"))
    controlador.parar_tudo()
    time.sleep(0.3)
    assert len(backend.ops("goto")) == antes, "o e-stop mandou um movimento novo"


def test_estop_desliga_tracking_e_wobbling(controlador, backend):
    controlador.tracking_pedir(True, 1.0, "usuario")
    controlador.wobbling_pedir(True)
    controlador.parar_tudo()
    assert backend.ops("tracking")[-1][0] is False
    assert backend.ops("wobbling")[-1] is False


def test_estop_nao_desliga_motores_sozinho(controlador, backend):
    """Cortar torque derruba a cabeça: é escalada explícita, não padrão."""
    controlador.parar_tudo()
    assert "disabled" not in backend.ops("motores")


def test_estop_bloqueia_acoes_de_movimento(controlador):
    controlador.parar_tudo()
    r = controlador.submeter("dance")
    assert not r.ok and r.state == "rejected"
    assert "clear_estop" in r.message


def test_estop_permite_o_minimo(controlador):
    controlador.parar_tudo()
    assert controlador.submeter("status").ok
    assert controlador.submeter("capture_image").ok
    r = controlador.submeter("disable_motors")
    assert r.ok


def test_duplo_estop_e_idempotente(controlador):
    a = controlador.parar_tudo()
    b = controlador.parar_tudo()
    assert a.ok and b.ok
    assert controlador.estado is EstadoControlador.ESTOPPED


def test_clear_estop_libera_sem_mover(controlador, backend):
    controlador.parar_tudo()
    antes = len(backend.ops("goto"))
    r = controlador.limpar_estop()
    assert r.ok and controlador.estado is EstadoControlador.IDLE
    assert len(backend.ops("goto")) == antes, "clear_estop não pode mover o robô"
    assert controlador.submeter("turn_head", {"direction": "center"}).executed


def test_clear_estop_sem_estop_e_inofensivo(controlador):
    r = controlador.limpar_estop()
    assert r.ok and r.executed is False


def test_estop_e_rapido(controlador, backend):
    backend.duracao_move = 5.0
    threading.Thread(target=lambda: controlador.submeter("dance"), daemon=True).start()
    _esperar(lambda: controlador.fila()["current"] is not None, 2.0)
    t0 = time.monotonic()
    controlador.parar_tudo()
    ms = (time.monotonic() - t0) * 1000
    assert ms < 100, f"e-stop levou {ms:.0f} ms"


# ─── preempção ───────────────────────────────────────────────────────────────
def test_ambiente_e_descartado_durante_comando_explicito(controlador, backend):
    backend.duracao_move = 1.5
    threading.Thread(target=lambda: controlador.submeter("dance"), daemon=True).start()
    _esperar(lambda: controlador.fila()["current"] is not None, 2.0)

    r = controlador.submeter("nod", prioridade=PRIO_AMBIENTE, esperar=False)
    assert r.accepted is False and r.state == "rejected"
    assert "ambiente" in r.message.lower()


def test_explicito_cancela_ambiente_em_curso(controlador, backend):
    backend.duracao_move = 2.0
    ambiente: list = []
    threading.Thread(
        target=lambda: ambiente.append(
            controlador.submeter("dance", prioridade=PRIO_AMBIENTE)
        ),
        daemon=True,
    ).start()
    _esperar(lambda: controlador.fila()["current"] is not None, 2.0)

    r = controlador.submeter("turn_head", {"direction": "left"}, prioridade=PRIO_EXPLICITA)
    assert r.executed
    _esperar(lambda: bool(ambiente), 2.0)
    assert ambiente[0].state == "cancelled"


def test_explicito_preempta_explicito(controlador, backend):
    backend.duracao_move = 2.0
    primeiro: list = []
    threading.Thread(
        target=lambda: primeiro.append(controlador.submeter("dance")), daemon=True
    ).start()
    _esperar(lambda: controlador.fila()["current"] is not None, 2.0)

    segundo = controlador.submeter("turn_head", {"direction": "right"})
    assert segundo.executed
    _esperar(lambda: bool(primeiro), 2.0)
    assert primeiro[0].state == "cancelled"


def test_so_a_ambiente_mais_recente_sobrevive(controlador, backend):
    backend.duracao_move = 1.0
    threading.Thread(target=lambda: controlador.submeter("dance"), daemon=True).start()
    _esperar(lambda: controlador.fila()["current"] is not None, 2.0)
    controlador.parar_tudo()
    controlador.limpar_estop()

    # Com o executor ocupado numa ambiente longa, três ambientes na fila viram uma.
    backend.duracao_move = 1.0
    threading.Thread(
        target=lambda: controlador.submeter("dance", prioridade=PRIO_AMBIENTE), daemon=True
    ).start()
    _esperar(lambda: controlador.fila()["current"] is not None, 2.0)
    for _ in range(3):
        controlador.submeter("nod", prioridade=PRIO_AMBIENTE, esperar=False)
    assert len(controlador.fila()["queued"]) <= 1


def test_dois_clientes_ao_mesmo_tempo_nao_quebram_o_estado(controlador, backend):
    backend.duracao_move = 0.2
    resultados: list = []
    trava = threading.Lock()

    def cliente(n: int) -> None:
        for _ in range(4):
            r = controlador.submeter("turn_head", {"direction": "left" if n else "right"})
            with trava:
                resultados.append(r)

    ts = [threading.Thread(target=cliente, args=(n,)) for n in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
    assert len(resultados) == 8
    assert all(r.accepted for r in resultados)
    assert all(r.state in ("completed", "cancelled") for r in resultados)
    assert controlador.fila()["current"] is None


# ─── tracking e wobbling ─────────────────────────────────────────────────────
def test_movimento_suspende_e_restaura_tracking(controlador, backend):
    controlador.tracking_pedir(True, 1.0, "usuario")
    assert backend.ops("tracking")[-1][0] is True

    controlador.submeter("turn_head", {"direction": "left"})
    ligados = [t[0] for t in backend.ops("tracking")]
    # ligou → suspendeu (desligou) → restaurou (ligou)
    assert ligados[-3:] == [True, False, True]


def test_tracking_do_ambiente_nao_apaga_o_do_usuario(controlador, backend):
    controlador.tracking_pedir(True, 1.0, "usuario")
    controlador.tracking_pedir(False, 0.35, "ambiente")
    st = controlador.status()["tracking"]
    assert st["user"] is True and st["ambient"] is False
    assert st["active_on_robot"] is True


def test_tracking_so_desliga_quando_ninguem_quer(controlador):
    controlador.tracking_pedir(True, 1.0, "usuario")
    controlador.tracking_pedir(True, 0.35, "ambiente")
    controlador.tracking_pedir(False, 1.0, "usuario")
    assert controlador.status()["tracking"]["active_on_robot"] is True
    controlador.tracking_pedir(False, 0.35, "ambiente")
    assert controlador.status()["tracking"]["active_on_robot"] is False


def test_movimento_limpa_a_fila_do_daemon_antes(controlador, backend):
    """Sem isso o daemon descarta o movimento novo em silêncio."""
    controlador.submeter("turn_head", {"direction": "left"})
    assert backend.ops("parar_e_aguardar"), "não limpou moves antes de mover"


# ─── ações internas ──────────────────────────────────────────────────────────
def test_nome_de_app_malicioso_rejeitado(controlador, backend):
    for ruim in ("../etc", "a/b", "app nome", ".."):
        r = controlador.submeter("start_app", {"name": ruim})
        assert not r.ok, ruim
    assert backend.ops("app_iniciar") == []


def test_app_valido_passa(controlador, backend):
    r = controlador.submeter("start_app", {"name": "clawbody"})
    assert r.ok and backend.ops("app_iniciar") == ["clawbody"]


def test_capture_image_salva_e_nao_devolve_imagem(controlador, tmp_path):
    controlador.dir_capturas = tmp_path
    r = controlador.submeter("capture_image")
    assert r.ok and r.data["bytes"] > 0
    assert list(tmp_path.glob("*.jpg"))
    assert "imagem" not in r.data  # a imagem nunca vai no corpo da resposta


def test_capacidades_marcam_expressao_indisponivel(controlador):
    cap = controlador.capacidades()
    assert cap["expressions"]["happy"]["resolved_move"] == "cheerful1"
    # `loving` não está na biblioteca falsa
    assert cap["expressions"]["loving"]["available"] is False
    assert cap["expressions"]["neutral"]["available"] is True


def test_expressao_indisponivel_falha_com_motivo_claro(controlador):
    r = controlador.submeter("set_expression", {"name": "loving"})
    assert not r.ok and "não existe na biblioteca" in r.message


def test_status_tem_o_contrato_da_interface(controlador):
    s = controlador.submeter("status").data
    for chave in ("mode", "controller_state", "connected", "moving", "tracking", "latency_ms"):
        assert chave in s, chave


# ─── robustez ────────────────────────────────────────────────────────────────
def test_falha_do_backend_nao_trava_a_fila(controlador, backend):
    def explode(**_kw):
        raise RuntimeError("daemon caiu")

    backend.goto = explode  # type: ignore[assignment]
    r = controlador.submeter("turn_head", {"direction": "left"})
    assert not r.ok and r.state == "failed" and r.executed is False
    assert controlador.fila()["current"] is None

    # a fila continua viva depois do erro
    backend.goto = lambda **kw: BackendFalsoGoto(backend, kw)  # type: ignore[assignment]
    assert controlador.submeter("turn_head", {"direction": "right"}).executed
    assert controlador.erros()[-1]["action"] == "turn_head"


def BackendFalsoGoto(backend, kw):  # noqa: N802 - helper de teste
    return backend._novo_id(kw.get("duracao", 0.1))


def test_encerrar_nao_deixa_thread_orfa(backend):
    ctrl = ControladorRobo(backend, semente=7)
    ctrl.iniciar()
    ctrl.submeter("turn_head", {"direction": "left"}, esperar=False)
    ctrl.encerrar(timeout=3.0)
    assert not ctrl._worker.is_alive()


def _esperar(condicao, timeout: float) -> None:
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        if condicao():
            return
        time.sleep(0.01)
    raise AssertionError("condição não aconteceu a tempo")
