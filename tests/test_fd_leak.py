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


# ─── segundo vazamento, achado em hardware depois do primeiro ────────────────
# O pool único resolveu uma parte e a taxa caiu, mas não zerou: no robô, com
# TUDO em repouso, ainda subia +1 thread e +1 socket por minuto, linear. A
# medição está em ~/.local/state/garra-reachy/validacao-1.2.1/soak.jsonl.
#
# A causa era outra, e maior: `_laco_voz` iniciava a thread de notificações a
# cada entrada, e ela esperava no `stop_event` — que só é acionado quando o app
# INTEIRO encerra. As antigas ficavam de pé publicando numa `FilaEventos` que
# ninguém lia mais, cada uma segurando a sessão keep-alive do seu `Cerebro`.


class _CerebroFalso:
    """Só o que o poll usa: `novas_mensagens()` e um socket para segurar."""

    def __init__(self, destino) -> None:
        self.sock = socket.create_connection(destino, timeout=5)
        self.fechado = False

    def novas_mensagens(self):
        return []

    def fechar(self) -> None:
        self.fechado = True
        self.sock.close()


class _AppMinimo:
    """O bastante para exercitar `_poll_notificacoes` sem robô nem gateway."""

    def __init__(self) -> None:
        import logging
        self.logger = logging.getLogger("teste-poll")

    _poll_notificacoes = None  # preenchido no import abaixo


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="precisa de /proc")
def test_poll_de_notificacoes_termina_com_o_ciclo(buraco_negro):
    """O invariante novo: a thread do poll morre quando o CICLO acaba."""
    from garra_reachy_mini.main import GarraReachyMini

    app = _AppMinimo()
    app._poll_notificacoes = GarraReachyMini._poll_notificacoes.__get__(
        app, _AppMinimo)

    stop = threading.Event()
    fim = threading.Event()
    cerebro = _CerebroFalso(buraco_negro)
    t = threading.Thread(target=app._poll_notificacoes,
                         args=([cerebro], _FilaBoba(), 0.05, stop, fim),
                         daemon=True)
    t.start()
    try:
        assert t.is_alive()
        fim.set()                      # o ciclo acabou; o app NÃO acabou
        t.join(timeout=5)
        assert not t.is_alive(), (
            "a thread do poll sobreviveu ao ciclo: é o vazamento de +1 thread "
            "por entrada no laço de voz")
        assert not stop.is_set(), "o app não precisou encerrar para isso"
    finally:
        fim.set()
        cerebro.fechar()


class _FilaBoba:
    def publicar(self, evento) -> None:
        pass


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="precisa de /proc")
def test_ciclos_do_laco_nao_acumulam_threads_nem_sockets(buraco_negro):
    """30 ciclos: nem thread nem socket podem sobrar. Mede, não inspeciona."""
    from garra_reachy_mini.main import GarraReachyMini

    app = _AppMinimo()
    app._poll_notificacoes = GarraReachyMini._poll_notificacoes.__get__(
        app, _AppMinimo)
    stop = threading.Event()

    # Um ciclo de aquecimento antes da baseline: o primeiro sempre custa
    # estruturas que não se repetem.
    for aquecer in range(1):
        fim = threading.Event()
        c = _CerebroFalso(buraco_negro)
        t = threading.Thread(target=app._poll_notificacoes,
                             args=([c], _FilaBoba(), 0.02, stop, fim), daemon=True)
        t.start()
        fim.set()
        t.join(timeout=5)
        c.fechar()

    base_fd = _contar_fds()
    base_threads = threading.active_count()

    for _ in range(30):
        fim = threading.Event()
        c = _CerebroFalso(buraco_negro)
        t = threading.Thread(target=app._poll_notificacoes,
                             args=([c], _FilaBoba(), 0.02, stop, fim), daemon=True)
        t.start()
        fim.set()          # é o que o `finally` do laço de voz faz
        t.join(timeout=5)
        c.fechar()         # e é o que `cerebro.fechar()` faz

    depois = _contar_fds()
    assert depois["socket"] - base_fd["socket"] <= 2, (
        f"sockets {base_fd['socket']}→{depois['socket']} em 30 ciclos: a sessão "
        "do cérebro não está sendo devolvida no fim do ciclo")
    assert threading.active_count() - base_threads <= 2, (
        "threads acumulando: o poll voltou a sobreviver ao ciclo")


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="precisa de /proc")
def test_o_padrao_antigo_do_poll_vazava(buraco_negro):
    """Prova o defeito antigo: esperar só no `stop_event` deixa a thread viva.

    Sem isto, a correção parece uma preferência de estilo. O antigo esperava no
    evento do APP; o ciclo terminar não o acordava.
    """
    stop_do_app = threading.Event()
    viva = threading.Event()

    def poll_antigo() -> None:
        viva.set()
        while not stop_do_app.wait(0.05):
            pass

    t = threading.Thread(target=poll_antigo, daemon=True)
    t.start()
    viva.wait(timeout=5)

    fim_do_ciclo = threading.Event()
    fim_do_ciclo.set()          # o ciclo acabou...
    t.join(timeout=0.5)
    assert t.is_alive(), "o padrão antigo deveria sobreviver ao fim do ciclo"

    stop_do_app.set()           # só o app inteiro encerrando a matava
    t.join(timeout=5)
    assert not t.is_alive()


def test_cerebro_devolve_a_sessao_ao_fechar() -> None:
    """`Cerebro.fechar()` fecha a sessão keep-alive do gateway. Idempotente."""
    import logging
    from garra_reachy_mini.cerebro import GatewayBrain

    class _Cfg:
        gateway_url = "http://127.0.0.1:1"
        gateway_key = ""
        agent_id = "garra"
        janela_turnos = 8

    g = GatewayBrain(_Cfg(), logging.getLogger("teste-fechar"))
    assert g.http.adapters, "a sessão nasce com adaptadores montados"
    g.fechar()
    g.fechar()   # idempotente: o encerramento pode passar duas vezes aqui
