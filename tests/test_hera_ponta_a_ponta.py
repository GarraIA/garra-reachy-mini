"""As duas pontas do canal ao vivo, conversando de verdade por um soquete.

O cliente do robô (`garra_reachy_mini.ao_vivo.Canal`, websockets síncrono) contra
a rota do computador (`companion.hera.canal.registrar`, FastAPI + uvicorn), num
servidor local. A OpenAI e o Hermes são dublês; o que se prova é que as duas
implementações — escritas separadas, porque o app do robô não importa o
companion — falam o mesmo protocolo: aperto de mão, quadros, estado, fim.
E que conversar vinte vezes não deixa thread nem soquete para trás. E que o robô
que perde a conexão volta, por outra, à mesma conversa.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

pytest.importorskip("aiortc")

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from companion.hera import canal, credencial, livro, registro  # noqa: E402
from garra_reachy_mini import ao_vivo  # noqa: E402
from test_hera_canal import ParFalso, PonteFalsa  # noqa: E402


class ParQueComeca(ParFalso):
    async def abrir(self):
        await super().abrir()
        self.ao_evento({"type": "session.started", "session": {"id": "s"}})


ULTIMA: dict = {}          # a ponte falsa do servidor da vez


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def servidor(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_CONF", str(tmp_path / "conf"))
    monkeypatch.setenv("GARRA_REACHY_DADOS", str(tmp_path / "dados"))
    monkeypatch.delenv("GARRA_HERA", raising=False)
    registro.Registro().mudar({"enabled": True, "limits": {"opens_per_hour": 60}}, local=True)
    segredo = credencial.segredo()
    porta = _porta_livre()
    host = f"127.0.0.1:{porta}"
    app = FastAPI()
    ponte = ULTIMA["ponte"] = PonteFalsa()
    canal.registrar(app, ponte=ponte, abrir_par=ParQueComeca, host_e_meu=lambda h: h == host,
                    limites_de_teste={"session_max_s": 30.0, "idle_close_s": 20.0, "no_speech_s": 20.0})
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=porta, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    fim = time.monotonic() + 10
    while not srv.started and time.monotonic() < fim:
        time.sleep(0.02)
    assert srv.started
    yield host, segredo
    srv.should_exit = True
    t.join(5)


def _conversar(host: str, segredo: str) -> tuple[str, list]:
    c = ao_vivo.Canal(f"ws://{host}{ao_vivo.CAMINHO}", host, segredo, "garra-sala")
    c.abrir("alone", "")
    vistos: list = []
    try:
        def abriu() -> bool:
            return any(tipo == "json" and item.get("t") == "aberta" for tipo, item in vistos)

        fim = time.monotonic() + 5
        while time.monotonic() < fim and not abriu():
            vistos += c.receber()
            time.sleep(0.01)
        assert abriu(), vistos
        c.enviar_som(b"\x07\x00" * 1024)
        c.enviar({"t": "parar"})
        fim = time.monotonic() + 5
        while time.monotonic() < fim:
            for tipo, item in c.receber():
                vistos.append((tipo, item))
                if tipo == "json" and item.get("t") == "fim":
                    return item["motivo"], vistos
            time.sleep(0.01)
        raise AssertionError(f"sem fim: {vistos}")
    finally:
        c.fechar()


def test_o_robo_e_o_computador_falam_o_mesmo_protocolo(servidor):
    host, segredo = servidor
    motivo, vistos = _conversar(host, segredo)
    assert motivo == "robot_stop"
    assert ParFalso.ultimo.som == [b"\x07\x00" * 1024]
    (linha,) = livro.Livro().sessoes()
    assert linha["motivo"] == "robot_stop"


def test_segredo_errado_nao_passa_do_aperto(servidor):
    host, _ = servidor
    with pytest.raises(ao_vivo.Recusado) as erro:
        ao_vivo.Canal(f"ws://{host}{ao_vivo.CAMINHO}", host, "segredo-errado-" + "x" * 30, "garra-sala").abrir("alone", "")
    assert erro.value.motivo == "bad_pc_proof"


def test_endereco_discado_errado_e_recusado(servidor):
    host, segredo = servidor
    with pytest.raises(ao_vivo.Recusado) as erro:
        ao_vivo.Canal(f"ws://{host}{ao_vivo.CAMINHO}", "10.0.0.99:8126", segredo, "garra-sala").abrir("alone", "")
    assert erro.value.motivo == "wrong_host"


def _threads_e_fds() -> tuple[int, int]:
    return threading.active_count(), len(os.listdir("/proc/self/fd"))


def test_vinte_conversas_nao_deixam_nada_para_tras(servidor):
    host, segredo = servidor
    _conversar(host, segredo)                       # aquece (import, primeira conexão)
    time.sleep(0.3)
    threads, fds = _threads_e_fds()
    for _ in range(20):
        assert _conversar(host, segredo)[0] == "robot_stop"
    time.sleep(0.5)
    threads_depois, fds_depois = _threads_e_fds()
    assert threads_depois <= threads + 1, (threads, threads_depois)
    assert fds_depois <= fds + 3, (fds, fds_depois)


def _ate(c: ao_vivo.Canal, condicao, segundos: float = 5.0) -> dict:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        for tipo, item in c.receber():
            if tipo == "json" and condicao(item):
                return item
        time.sleep(0.01)
    raise AssertionError("não chegou")


def test_o_robo_cai_volta_por_outra_conexao_e_segue_na_mesma_sessao(servidor):
    host, segredo = servidor
    primeira = ao_vivo.Canal(f"ws://{host}{ao_vivo.CAMINHO}", host, segredo, "garra-sala")
    primeira.abrir("alone", "")
    senha = _ate(primeira, lambda m: m["t"] == "aberta")["retomar"]
    primeira.fechar()                                  # o Wi-Fi caiu
    volta = primeira.outro()
    try:
        volta.abrir("alone", "", retomar=senha)        # prova de novo, com a senha
        assert _ate(volta, lambda m: m["t"] == "estado")["fase"] == "ouvindo"
        volta.enviar_som(b"\x07\x00" * 1024)
        volta.enviar({"t": "parar"})
        fim = _ate(volta, lambda m: m["t"] == "fim")
    finally:
        volta.fechar()
    assert fim["motivo"] == "robot_stop"
    assert len(ULTIMA["ponte"].sessoes) == 1            # a mesma sessão da OpenAI
    assert ParFalso.ultimo.som == [b"\x07\x00" * 1024]
    (linha,) = livro.Livro().sessoes()
    assert linha["motivo"] == "robot_stop"


def test_senha_de_outra_conversa_nao_volta_a_nada(servidor):
    host, segredo = servidor
    c = ao_vivo.Canal(f"ws://{host}{ao_vivo.CAMINHO}", host, segredo, "garra-sala")
    with pytest.raises(ao_vivo.Recusado) as erro:
        c.abrir("alone", "", retomar="senha-de-uma-conversa-que-acabou")
    assert erro.value.motivo == "not_resumable"
