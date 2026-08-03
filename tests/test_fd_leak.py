"""Regressão do incidente SIGTRAP / `Process exited with code -5`.

O app do robô morria depois de uma indisponibilidade prolongada do gateway ou
do servidor de voz. A cadeia, medida em 2026-08-03:

    gateway/voz fora → laço de retry → FDs esgotados
    → GLib não cria o pipe do GWakeup
    → G_BREAKPOINT() → SIGTRAP → o daemon reporta -5

O recurso exato: o laço de voz criava um `ThreadPoolExecutor` por entrada e o
descartava com `shutdown(wait=False)`. Isso cancela só o que está na fila — uma
consulta JÁ em execução mantém a thread e o socket dela. Contra um gateway que
aceita a conexão e nunca responde, cada volta do supervisor deixava mais uma
thread e mais um socket para trás: crescimento linear, sem teto.

Estes testes medem FDs de verdade em vez de inspecionar texto: a guarda textual
vive em `test_conversa_api.py`, e sozinha ela não teria pego o vazamento — a
versão vazada passava naquela guarda.
"""

from __future__ import annotations

import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


def _contar_fds() -> dict[str, int]:
    """FDs deste processo por tipo. Só faz sentido em Linux (/proc)."""
    base = f"/proc/{os.getpid()}/fd"
    tipos = {"socket": 0, "pipe": 0, "outro": 0}
    for nome in os.listdir(base):
        try:
            alvo = os.readlink(f"{base}/{nome}")
        except OSError:
            continue  # o fd sumiu entre listdir e readlink
        if alvo.startswith("socket:"):
            tipos["socket"] += 1
        elif alvo.startswith("pipe:"):
            tipos["pipe"] += 1
        else:
            tipos["outro"] += 1
    tipos["total"] = tipos["socket"] + tipos["pipe"] + tipos["outro"]
    return tipos


@pytest.fixture()
def buraco_negro():
    """Endereço que ACEITA a conexão e nunca responde.

    Um `connection refused` fecharia o socket na hora e esconderia o
    vazamento; o gateway morto de verdade era este caso — a ponte aceitava e
    a resposta não vinha.
    """
    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    servidor.bind(("127.0.0.1", 0))
    servidor.listen(128)
    try:
        yield servidor.getsockname()
    finally:
        servidor.close()


def _consulta_pendurada(destino, timeout: float = 30.0) -> None:
    """O que uma consulta ao cérebro faz contra um gateway que não responde."""
    s = socket.create_connection(destino, timeout=timeout)
    try:
        s.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\n\r\n")
        s.settimeout(timeout)
        s.recv(1)
    except OSError:
        pass
    finally:
        s.close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="precisa de /proc")
def test_retries_do_not_leak_file_descriptors(buraco_negro):
    """100 ciclos de retry não podem fazer os FDs crescerem sem parar.

    Reproduz o padrão do supervisor: o laço de voz volta (voz sumiu, cérebro
    sumiu, config mudou) com uma consulta ainda pendurada, e o supervisor
    reentra. Com o pool único, o custo é constante: os 2 workers, e nada mais.
    """
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cerebro")
    try:
        # Aquecimento: o pool só cria as threads no primeiro submit, e a
        # baseline tem de ser medida depois disso ou o crescimento das duas
        # primeiras threads seria lido como vazamento.
        for _ in range(4):
            executor.submit(_consulta_pendurada, buraco_negro)
        base = _contar_fds()
        threads_base = threading.active_count()

        for _ in range(100):
            executor.submit(_consulta_pendurada, buraco_negro)

        depois = _contar_fds()
        assert depois["socket"] - base["socket"] <= 2, (
            f"sockets cresceram {base['socket']}→{depois['socket']} em 100 "
            "retries: é o vazamento do incidente -5")
        assert threading.active_count() - threads_base <= 2, (
            "threads acumulando: o pool está sendo recriado por ciclo")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="precisa de /proc")
def test_um_pool_por_ciclo_vaza_e_por_isso_foi_removido(buraco_negro):
    """Prova o defeito antigo, para que ninguém o reintroduza achando que dá no mesmo.

    Este teste FALHARIA de propósito se o padrão antigo não vazasse — ele
    existe para documentar a medição, não só a conclusão.
    """
    base = _contar_fds()
    for _ in range(25):
        antigo = ThreadPoolExecutor(max_workers=2, thread_name_prefix="antigo")
        antigo.submit(_consulta_pendurada, buraco_negro)
        antigo.shutdown(wait=False, cancel_futures=True)  # o padrão removido
    depois = _contar_fds()
    assert depois["socket"] - base["socket"] >= 20, (
        "o padrão antigo deveria vazar ~1 socket por ciclo; se não vaza mais, "
        "o motivo mudou e este teste precisa ser revisto")


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="precisa de /proc")
def test_o_pool_do_app_e_reaproveitado_entre_ciclos():
    """`_executor_cerebro` devolve o MESMO pool, e recria um já encerrado."""
    from garra_reachy_mini.main import GarraReachyMini

    # sem tocar hardware: só o gerenciamento do pool
    app = GarraReachyMini.__new__(GarraReachyMini)
    try:
        primeiro = app._executor_cerebro()
        assert app._executor_cerebro() is primeiro, "um pool por ciclo é o vazamento"

        primeiro.shutdown(wait=False)
        segundo = app._executor_cerebro()
        assert segundo is not primeiro, (
            "um pool encerrado não pode ser reutilizado — o robô ficaria mudo "
            "sem erro nenhum")
    finally:
        app._encerrar_executor(timeout=1.0)


def test_encerrar_executor_e_idempotente():
    """Encerrar duas vezes não pode explodir nem pendurar o shutdown."""
    from garra_reachy_mini.main import GarraReachyMini

    app = GarraReachyMini.__new__(GarraReachyMini)
    app._executor_cerebro()
    app._encerrar_executor(timeout=1.0)
    app._encerrar_executor(timeout=1.0)  # nada a fazer, e não levanta
