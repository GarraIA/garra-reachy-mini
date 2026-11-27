"""O cliente da ponte da Hera: HTTP por soquete Unix, com erros que têm nome.

A ponte é o processo que guarda a chave da OpenAI e fala com o Hermes. O
companion só lhe entrega a oferta SDP e o texto de um turno delegado — nunca
instrução, voz, modelo ou ferramenta, que são do perfil do canal.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer

import pytest

from companion.hera import ponte_hera, registro


class _Servidor(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def ponte_falsa(tmp_path):
    """Uma ponte de mentira num soquete de verdade; o teste dita as respostas."""
    estado = {"respostas": {}, "pedidos": []}

    class Tratador(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _responder(self):
            status, corpo = estado["respostas"].get(self.path, (404, {"message": "rota não encontrada"}))
            dados = corpo if isinstance(corpo, bytes) else json.dumps(corpo).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)

        def do_GET(self):
            estado["pedidos"].append(("GET", self.path, None))
            self._responder()

        def do_POST(self):
            corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            estado["pedidos"].append(("POST", self.path, corpo))
            self._responder()

    caminho = str(tmp_path / "p.sock")
    servidor = _Servidor(caminho, Tratador)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    estado["cliente"] = ponte_hera.PonteHera(caminho)
    yield estado
    servidor.shutdown()
    servidor.server_close()


def test_saude(ponte_falsa):
    ponte_falsa["respostas"]["/health"] = (200, {"available": True, "channel": "garra", "model": "gpt-live-1"})
    assert ponte_falsa["cliente"].saude()["channel"] == "garra"


def test_ponte_fora_do_ar_nao_e_excecao_na_saude(tmp_path):
    assert ponte_hera.PonteHera(str(tmp_path / "nao-existe.sock")).saude() is None


def test_sessao_entrega_so_a_oferta_e_devolve_a_resposta(ponte_falsa):
    ponte_falsa["respostas"]["/session"] = (200, {
        "session": {"id": "sess_1"}, "transport": {"type": "webrtc", "sdp": "RESPOSTA"}})
    sessao = ponte_falsa["cliente"].sessao("OFERTA")
    assert (sessao.id, sessao.sdp) == ("sess_1", "RESPOSTA")
    (_, caminho, corpo), = ponte_falsa["pedidos"]
    assert caminho == "/session" and corpo == {"sdp": "OFERTA"}


def test_sessao_com_historico_montado_pelo_computador(ponte_falsa):
    ponte_falsa["respostas"]["/session"] = (200, {
        "session": {"id": "s"}, "transport": {"type": "webrtc", "sdp": "R"}})
    historico = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "oi"}]}]
    ponte_falsa["cliente"].sessao("O", historico=historico)
    assert ponte_falsa["pedidos"][0][2] == {"sdp": "O", "history": historico}


def test_turno(ponte_falsa):
    ponte_falsa["respostas"]["/turn"] = (200, {"text": "São dez horas.", "latency_ms": 4200, "conversation_id": "c1"})
    turno = ponte_falsa["cliente"].turno("c1", "que horas são?")
    assert (turno.texto, turno.latencia_ms) == ("São dez horas.", 4200)
    assert ponte_falsa["pedidos"][0][2] == {"conversation_id": "c1", "text": "que horas são?"}


@pytest.mark.parametrize("status,corpo,codigo", [
    (503, {"message": "GPT-Live recusou a sessão (429)"}, "unavailable"),
    (400, {"message": "oferta SDP ausente"}, "refused"),
    (500, {"message": "erro interno da ponte: KeyError"}, "bridge_error"),
    (200, b"isto nao e json", "bad_response"),
    (200, {"session": {}, "transport": {}}, "bad_response"),
])
def test_erros_tem_nome_e_nao_levam_o_corpo_para_fora(ponte_falsa, status, corpo, codigo):
    ponte_falsa["respostas"]["/session"] = (status, corpo)
    with pytest.raises(ponte_hera.ErroDaPonte) as erro:
        ponte_falsa["cliente"].sessao("OFERTA")
    assert erro.value.codigo == codigo
    assert "KeyError" not in str(erro.value) and "429" not in str(erro.value)


def test_ponte_fora_do_ar_na_sessao_e_erro_com_nome(tmp_path):
    with pytest.raises(ponte_hera.ErroDaPonte) as erro:
        ponte_hera.PonteHera(str(tmp_path / "nao-existe.sock")).sessao("OFERTA")
    assert erro.value.codigo == "unreachable"


@pytest.fixture
def ponte_muda(tmp_path):
    """Uma ponte que aceita o pedido e não responde: o Hermes ainda pesquisando."""
    caminho = str(tmp_path / "muda.sock")
    servidor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    servidor.bind(caminho)
    servidor.listen(4)
    servidor.settimeout(0.1)
    presos, parar = [], threading.Event()

    def aceitar():
        while not parar.is_set():
            try:
                presos.append(servidor.accept()[0])
            except OSError:
                pass

    fio = threading.Thread(target=aceitar, daemon=True)
    fio.start()
    yield caminho
    parar.set()
    fio.join(1)
    for conexao in presos:
        conexao.close()
    servidor.close()


def test_turno_que_passa_do_prazo_e_tempo_esgotado_e_nao_ponte_fora_do_ar(ponte_muda, monkeypatch):
    # 26/11/2026: uma pesquisa de 206 s passou do prazo, e o registro dizia
    # "unreachable", como se a ponte estivesse fora do ar.
    monkeypatch.setattr(ponte_hera, "PRAZO_TURNO_S", 0.3)
    with pytest.raises(ponte_hera.ErroDaPonte) as erro:
        ponte_hera.PonteHera(ponte_muda).turno("c1", "pesquisa isso")
    assert erro.value.codigo == "timeout"


def test_o_prazo_do_turno_nao_corta_uma_pesquisa_dentro_da_conversa():
    # Quem limita a espera é a conversa: o fim dela cancela o turno. O prazo
    # daqui é só a rede de segurança e cobre a conversa mais longa permitida.
    assert ponte_hera.PRAZO_TURNO_S >= registro.LIMITES_FAIXA["session_max_s"][1]


def test_turno_cancelado_de_outra_thread_acaba_na_hora(ponte_muda):
    cancelamento = ponte_hera.Cancelamento()
    resultado = {}

    def rodar():
        try:
            ponte_hera.PonteHera(ponte_muda).turno("c1", "pesquisa isso", cancelamento=cancelamento)
        except ponte_hera.ErroDaPonte as erro:
            resultado["codigo"] = erro.codigo

    fio = threading.Thread(target=rodar, daemon=True)
    fio.start()
    time.sleep(0.2)
    assert fio.is_alive(), "o turno devia estar esperando o Hermes"
    inicio = time.monotonic()
    cancelamento.cancelar()
    fio.join(2)
    assert not fio.is_alive() and time.monotonic() - inicio < 1.0
    assert resultado == {"codigo": "cancelled"}


def test_turno_ja_cancelado_nem_chega_a_ponte(ponte_falsa):
    cancelamento = ponte_hera.Cancelamento()
    cancelamento.cancelar()
    with pytest.raises(ponte_hera.ErroDaPonte) as erro:
        ponte_falsa["cliente"].turno("c1", "oi", cancelamento=cancelamento)
    assert erro.value.codigo == "cancelled"
    assert ponte_falsa["pedidos"] == []


def test_o_soquete_padrao_fica_na_pasta_de_execucao_do_usuario(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    assert str(ponte_hera.soquete_padrao()) == "/run/user/4242/garra-reachy/hera-bridge.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert str(ponte_hera.soquete_padrao()) == f"/run/user/{os.getuid()}/garra-reachy/hera-bridge.sock"
