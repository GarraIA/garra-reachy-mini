"""O canal da conversa ao vivo no computador: o aperto de mão e a condução.

O robô abre um WebSocket na ponte (`/api/hera/ao-vivo`). Antes de qualquer som:

  1. o robô diz quem é, o endereço que discou e manda um desafio;
  2. o computador prova PRIMEIRO que conhece o segredo (o robô confere);
  3. o robô prova; o computador confere, e só então olha liberação e tetos.

Daí em diante o computador conduz a sessão: abre o par WebRTC pela ponte da
Hera, passa o som nos dois sentidos, atende as delegações no Hermes e decide
quando acaba — silêncio, teto, "pare", "Fala Garra", despedida, queda.

Aqui a OpenAI, a ponte e o robô são dublês; o que se confere é a condução.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from companion.hera import canal, credencial, livro, ponte_hera, registro

SEGREDO = "segredo-do-dispositivo-de-teste-0123456789"
HOST = "10.0.0.24:8126"
LIMITES = {"session_max_s": 60, "idle_close_s": 30, "no_speech_s": 8,
           "daily_s": 1800, "monthly_s": 18000, "opens_per_hour": 12}
RAPIDO = {"session_max_s": 3.0, "idle_close_s": 0.6, "no_speech_s": 0.5}


@pytest.fixture(autouse=True)
def pasta(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_CONF", str(tmp_path / "conf"))
    monkeypatch.setenv("GARRA_REACHY_DADOS", str(tmp_path / "dados"))
    monkeypatch.delenv("GARRA_HERA", raising=False)
    return tmp_path


# ─── dublês ──────────────────────────────────────────────────────────────────
class ParFalso:
    def __init__(self, trocar_sdp, *, ao_audio, ao_evento, ao_cair=None, **_):
        self.trocar_sdp, self.ao_audio, self.ao_evento, self.ao_cair = trocar_sdp, ao_audio, ao_evento, ao_cair
        self.som: list[bytes] = []
        self.enviados: list[dict] = []
        self.fechado = False
        self.pronto = False
        ParFalso.ultimo = self

    async def abrir(self):
        self.resposta = await self.trocar_sdp("OFERTA")
        self.pronto = True

    def microfone(self, pcm):
        self.som.append(pcm)

    def enviar(self, evento):
        self.enviados.append(evento)
        if evento.get("type") == "session.close":
            self.ao_evento({"type": "session.closed", "reason": "close_requested",
                            "usage": {"seconds": 42, "active_seconds": 40, "paused_seconds": 2}})
        return self.pronto

    async def fechar(self):
        self.fechado, self.pronto = True, False

    # o que a "OpenAI" faz
    def evento(self, **e):
        self.ao_evento(e)

    def tipos(self):
        return [e["type"] for e in self.enviados]


class PonteFalsa:
    def __init__(self, resposta="São dez horas.", falha=None):
        self.resposta, self.falha = resposta, falha
        self.sessoes: list[tuple] = []
        self.turnos: list[tuple] = []
        self.contextos: list[str] = []

    def sessao(self, oferta, historico=None):
        self.sessoes.append((oferta, historico))
        return ponte_hera.Sessao("sess_teste", "RESPOSTA")

    def turno(self, conversa, texto, contexto=""):
        self.turnos.append((conversa, texto))
        self.contextos.append(contexto)
        if self.falha:
            raise self.falha
        return ponte_hera.Turno(self.resposta, 120)


class RoboFalso:
    def __init__(self):
        self.json: list[dict] = []
        self.audio: list[bytes] = []

    async def enviar_json(self, m):
        self.json.append(m)

    async def enviar_audio(self, pcm):
        self.audio.append(pcm)

    def tipos(self):
        return [m["t"] for m in self.json]


def conversa(ponte=None, como="alone", pedido="", descrever=None, **limites):
    robo = RoboFalso()
    c = canal.Conversa(robo, ponte=ponte or PonteFalsa(), livro=livro.Livro(),
                       limites={**LIMITES, **RAPIDO, **limites}, dispositivo="garra-sala",
                       nivel="completo", como=como, pedido=pedido, abrir_par=ParFalso,
                       tolerancia_despedida_s=0.2, descrever=descrever)
    return c, robo


async def esperar(condicao, segundos=3.0):
    fim = asyncio.get_running_loop().time() + segundos
    while asyncio.get_running_loop().time() < fim:
        if condicao():
            return True
        await asyncio.sleep(0.01)
    return False


async def comecar(c):
    tarefa = asyncio.ensure_future(c.conduzir())
    assert await esperar(lambda: getattr(ParFalso, "ultimo", None) is not None and ParFalso.ultimo.pronto)
    par = ParFalso.ultimo
    par.evento(type="session.started", session={"id": "sess_teste"})
    return tarefa, par


@pytest.fixture(autouse=True)
def _limpa_par():
    ParFalso.ultimo = None
    yield


# ─── condução ────────────────────────────────────────────────────────────────
def test_abre_avisa_o_robo_e_passa_o_som_nos_dois_sentidos():
    async def cena():
        c, robo = conversa()
        tarefa, par = await comecar(c)
        assert await esperar(lambda: "aberta" in robo.tipos())
        c.microfone(b"\x01\x00" * 160)
        assert par.som == [b"\x01\x00" * 160]
        par.ao_audio(b"\x02\x00" * 320)
        assert await esperar(lambda: robo.audio == [b"\x02\x00" * 320])
        c.parar("robot_stop")
        return await tarefa, robo

    fim, robo = asyncio.run(cena())
    assert fim["motivo"] == "robot_stop" and fim["segundos"] == 42
    assert robo.json[-1] == {"t": "fim", "motivo": "robot_stop", "segundos": 42}
    (linha,) = livro.Livro().sessoes()
    assert (linha["motivo"], linha["segundos"], linha["sessao_openai"]) == ("robot_stop", 42, "sess_teste")


def test_o_pedido_dito_junto_do_chamado_vai_no_historico_e_so_ele():
    async def cena():
        ponte = PonteFalsa()
        c, _ = conversa(ponte, como="leading", pedido="que horas são?")
        tarefa, _ = await comecar(c)
        c.parar("robot_stop")
        await tarefa
        return ponte

    ponte = asyncio.run(cena())
    ((oferta, historico),) = ponte.sessoes
    assert oferta == "OFERTA"
    assert historico == [{"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": "que horas são?"}]}]


def test_chamado_sozinho_nao_leva_historico():
    async def cena():
        ponte = PonteFalsa()
        c, _ = conversa(ponte)
        tarefa, _ = await comecar(c)
        c.parar("robot_stop")
        await tarefa
        return ponte

    assert asyncio.run(cena()).sessoes[0][1] is None


def test_delegacao_vai_ao_hermes_com_o_que_a_pessoa_disse_e_volta_falada():
    async def cena():
        ponte = PonteFalsa()
        c, robo = conversa(ponte)
        tarefa, par = await comecar(c)
        par.evento(type="session.input_transcript.delta", delta=" que horas")
        par.evento(type="session.input_transcript.delta", delta=" são?")
        par.evento(type="session.delegation.created", delegation={"id": "d1", "target": "client"})
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        c.parar("robot_stop")
        await tarefa
        return ponte, par, robo

    ponte, par, robo = asyncio.run(cena())
    assert [t for _, t in ponte.turnos] == ["que horas são?"]
    # Data e hora do computador acompanham o turno: sem ferramenta nenhuma, o
    # Hermes respondeu (medido) que não conseguia ver o relógio.
    assert ponte.contextos and ponte.contextos[0].startswith("Data e hora locais: ")
    pensando = next(e for e in par.enviados if e["type"] == "session.thinking.append")
    falado = next(e for e in par.enviados if e["type"] == "session.commentary.append")
    assert pensando["delegation_id"] == falado["delegation_id"] == "d1"
    assert falado["content"] == "São dez horas."
    assert {"t": "estado", "fase": "pensando"} in robo.json


def test_primeira_delegacao_sem_fala_usa_o_pedido_do_chamado():
    async def cena():
        ponte = PonteFalsa()
        c, _ = conversa(ponte, como="leading", pedido="que horas são?")
        tarefa, par = await comecar(c)
        par.evento(type="session.delegation.created", delegation={"id": "d1"})
        assert await esperar(lambda: ponte.turnos)
        c.parar("robot_stop")
        await tarefa
        return ponte

    assert asyncio.run(cena()).turnos[0][1] == "que horas são?"


def test_hermes_fora_do_ar_vira_frase_falada_e_a_conversa_segue():
    async def cena():
        c, _ = conversa(PonteFalsa(falha=ponte_hera.ErroDaPonte("unavailable")))
        tarefa, par = await comecar(c)
        par.evento(type="session.input_transcript.delta", delta="abre a agenda")
        par.evento(type="session.delegation.created", delegation={"id": "d1"})
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        assert not tarefa.done()
        c.parar("robot_stop")
        await tarefa
        return par

    falado = next(e for e in asyncio.run(cena()).enviados if e["type"] == "session.commentary.append")
    assert falado["content"] == canal.FALHA_DO_HERMES


def test_resposta_longa_sai_em_pedacos():
    async def cena():
        c, _ = conversa(PonteFalsa(resposta="Frase número tal. " * 200))
        tarefa, par = await comecar(c)
        par.evento(type="session.input_transcript.delta", delta="conta tudo")
        par.evento(type="session.delegation.created", delegation={"id": "d1"})
        assert await esperar(lambda: par.tipos().count("session.commentary.append") >= 2)
        c.parar("robot_stop")
        await tarefa
        return par

    pedacos = [e["content"] for e in asyncio.run(cena()).enviados if e["type"] == "session.commentary.append"]
    assert all(len(p) <= canal.PEDACO_MAX for p in pedacos)
    assert "".join(pedacos).replace(" ", "") == ("Frase número tal. " * 200).replace(" ", "")


# ─── como acaba ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("roteiro,motivo", [
    ([], "no_speech"),
    ([" oi, tudo bem?"], "idle"),
])
def test_silencio(roteiro, motivo):
    async def cena():
        c, robo = conversa()
        tarefa, par = await comecar(c)
        for delta in roteiro:
            par.evento(type="session.input_transcript.delta", delta=delta)
        return await asyncio.wait_for(tarefa, 4), par

    fim, par = asyncio.run(cena())
    assert fim["motivo"] == motivo
    assert "session.close" in par.tipos() and par.fechado


def test_o_silencio_nao_encerra_enquanto_o_hermes_trabalha():
    async def cena():
        lento = PonteFalsa()

        def turno_lento(conversa_id, texto, contexto=""):
            import time
            time.sleep(1.2)                        # mais que o silêncio de 0,6 s
            return ponte_hera.Turno("Pronto.", 1200)

        lento.turno = turno_lento
        c, _ = conversa(lento)
        tarefa, par = await comecar(c)
        par.evento(type="session.input_transcript.delta", delta="pesquisa isso")
        par.evento(type="session.delegation.created", delegation={"id": "d1"})
        await asyncio.sleep(1.0)
        assert not tarefa.done(), "fechou com o Hermes ainda trabalhando"
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        c.parar("robot_stop")
        return await tarefa

    assert asyncio.run(cena())["motivo"] == "robot_stop"


def test_teto_da_conversa_nunca_renova():
    async def cena():
        c, _ = conversa(session_max_s=1.0, idle_close_s=5.0)
        tarefa, par = await comecar(c)
        for _ in range(8):
            par.evento(type="session.input_transcript.delta", delta=" fala")
            await asyncio.sleep(0.2)
        return await asyncio.wait_for(tarefa, 3)

    assert asyncio.run(cena())["motivo"] == "max"


@pytest.mark.parametrize("dito,motivo", [
    ("pare", "stop"), ("Pare!", "stop"), ("Hera, para.", "stop"),
    ("fala garra", "handoff_garra"), ("Fala Garra, que horas são?", "handoff_garra"),
])
def test_comandos_ditos_na_conversa(dito, motivo):
    async def cena():
        c, robo = conversa()
        tarefa, par = await comecar(c)
        par.evento(type="session.input_transcript.delta", delta=dito)
        return await asyncio.wait_for(tarefa, 3), robo

    fim, robo = asyncio.run(cena())
    assert fim["motivo"] == motivo
    assert ({"t": "estop"} in robo.json) is (motivo == "stop")


def test_pare_que_e_eco_da_propria_hera_nao_para():
    async def cena():
        c, robo = conversa(idle_close_s=0.8)
        tarefa, par = await comecar(c)
        par.evento(type="session.output_transcript.delta", delta=" Se quiser que eu pare, é só dizer.")
        par.evento(type="session.input_transcript.delta", delta="pare")
        return await asyncio.wait_for(tarefa, 3), robo

    fim, robo = asyncio.run(cena())
    assert fim["motivo"] != "stop" and {"t": "estop"} not in robo.json


@pytest.mark.parametrize("dito", ["Tchau, Hera.", "pode desligar", "é só isso, obrigado", "encerrar"])
def test_despedida_encerra_depois_de_ela_responder(dito):
    async def cena():
        c, _ = conversa(idle_close_s=5.0)
        tarefa, par = await comecar(c)
        par.evento(type="session.input_transcript.delta", delta=dito)
        await asyncio.sleep(0.05)
        assert not tarefa.done(), "a despedida tem de dar tempo de ela se despedir"
        return await asyncio.wait_for(tarefa, 3)

    assert asyncio.run(cena())["motivo"] == "farewell"


@pytest.mark.parametrize("dito", ["obrigado", "era isso que eu queria", "para de brincadeira e me conta"])
def test_o_que_nao_e_comando_segue_a_conversa(dito):
    async def cena():
        c, _ = conversa(idle_close_s=0.6)
        tarefa, par = await comecar(c)
        par.evento(type="session.input_transcript.delta", delta=dito)
        return await asyncio.wait_for(tarefa, 3)

    assert asyncio.run(cena())["motivo"] == "idle"


def test_queda_da_conexao_com_a_openai():
    async def cena():
        c, robo = conversa(idle_close_s=5.0)
        tarefa, par = await comecar(c)
        par.ao_cair("connection_failed")
        return await asyncio.wait_for(tarefa, 3), robo

    fim, robo = asyncio.run(cena())
    assert fim["motivo"] == "connection_lost" and robo.json[-1]["t"] == "fim"


def test_ponte_recusa_a_sessao():
    async def cena():
        ponte = PonteFalsa()

        def recusa(oferta, historico=None):
            raise ponte_hera.ErroDaPonte("unavailable")

        ponte.sessao = recusa
        c, robo = conversa(ponte)
        return await asyncio.wait_for(c.conduzir(), 3), robo

    fim, robo = asyncio.run(cena())
    assert fim["motivo"] == "unavailable" and fim["segundos"] == 0
    assert robo.json == [{"t": "fim", "motivo": "unavailable", "segundos": 0}]
    (linha,) = livro.Livro().sessoes()
    assert linha["motivo"] == "unavailable"


def test_o_robo_nunca_recebe_o_que_foi_dito():
    """O painel do robô não pede senha: transcrição não atravessa para lá."""
    async def cena():
        c, robo = conversa()
        tarefa, par = await comecar(c)
        par.evento(type="session.input_transcript.delta", delta="minha senha do banco é 1234")
        par.evento(type="session.output_transcript.delta", delta="Anotei a senha 1234.")
        par.evento(type="session.delegation.created", delegation={"id": "d1"})
        await asyncio.sleep(0.2)
        c.parar("robot_stop")
        await tarefa
        return robo

    assert "1234" not in json.dumps(asyncio.run(cena()).json)


# ─── o aperto de mão ─────────────────────────────────────────────────────────
def _ola(**troca):
    return {"t": "ola", "v": 1, "dispositivo": "garra-sala", "host": HOST, "nonce": "n-do-robo",
            "como": "alone", "pedido": "", **troca}


def _aperto(**troca):
    return canal.Aperto(segredo=SEGREDO, dispositivo="garra-sala",
                        host_e_meu=lambda h: h == HOST, **troca)


def test_o_computador_prova_primeiro_e_confere_o_robo():
    ap = _aperto()
    desafio = ap.responder(_ola())
    assert desafio["t"] == "desafio" and desafio["nonce"]
    assert credencial.confere(desafio["prova"], "pc", segredo=SEGREDO, nonce_robo="n-do-robo",
                              nonce_pc=desafio["nonce"], host=HOST, dispositivo="garra-sala")
    boa = credencial.prova("robo", segredo=SEGREDO, nonce_robo="n-do-robo", nonce_pc=desafio["nonce"],
                           host=HOST, dispositivo="garra-sala")
    assert ap.conferir({"t": "prova", "prova": boa}) is True


@pytest.mark.parametrize("ola,motivo", [
    (_ola(host="10.0.0.99:8126"), "wrong_host"),           # discou outro endereço (intermediário)
    (_ola(dispositivo="intruso"), "unknown_device"),
    (_ola(v=2), "bad_version"),
    (_ola(nonce=""), "bad_hello"),
    (_ola(nonce="x" * 200), "bad_hello"),
    ({"t": "outra coisa"}, "bad_hello"),
    (_ola(como="talvez"), "bad_hello"),
    (_ola(pedido="x" * 2000), "bad_hello"),
])
def test_ola_torto_e_recusado(ola, motivo):
    with pytest.raises(canal.Recusa) as erro:
        _aperto().responder(ola)
    assert erro.value.motivo == motivo


@pytest.mark.parametrize("prova", ["", "a" * 64, None, 12])
def test_prova_do_robo_errada(prova):
    ap = _aperto()
    ap.responder(_ola())
    assert ap.conferir({"t": "prova", "prova": prova}) is False


def test_a_prova_do_proprio_computador_nao_serve_de_prova_do_robo():
    ap = _aperto()
    desafio = ap.responder(_ola())
    assert ap.conferir({"t": "prova", "prova": desafio["prova"]}) is False


def test_conferir_antes_do_ola_nao_vale():
    assert _aperto().conferir({"t": "prova", "prova": "a" * 64}) is False


# ─── o WebSocket de verdade ──────────────────────────────────────────────────
@pytest.fixture
def app_do_canal(monkeypatch):
    """A rota da ponte com o par e a ponte da Hera dublados."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class ParQueComeca(ParFalso):
        """A "OpenAI" anuncia a sessão de dentro do laço do servidor (sem cruzar threads)."""

        async def abrir(self):
            await super().abrir()
            self.ao_evento({"type": "session.started", "session": {"id": "s"}})

    ponte = PonteFalsa()
    app = FastAPI()
    canal.registrar(app, ponte=ponte, abrir_par=ParQueComeca, host_e_meu=lambda h: h == HOST,
                    limites_de_teste={"session_max_s": 5.0, "idle_close_s": 3.0, "no_speech_s": 3.0})
    return TestClient(app), ponte


def _liberar():
    registro.Registro().mudar({"enabled": True}, local=True)
    return credencial.segredo()


def _apertar(ws, segredo, conferir_pc=True, **troca):
    """O lado do robô. Com o segredo errado um robô de verdade pararia ao conferir a
    prova do computador; `conferir_pc=False` segue adiante para testar o outro lado."""
    ws.send_text(json.dumps(_ola(**troca)))
    desafio = json.loads(ws.receive_text())
    if desafio["t"] != "desafio":
        return desafio
    assert credencial.confere(desafio["prova"], "pc", segredo=segredo, nonce_robo="n-do-robo",
                              nonce_pc=desafio["nonce"], host=HOST, dispositivo="garra-sala") is conferir_pc
    ws.send_text(json.dumps({"t": "prova", "prova": credencial.prova(
        "robo", segredo=segredo, nonce_robo="n-do-robo", nonce_pc=desafio["nonce"],
        host=HOST, dispositivo="garra-sala")}))
    return json.loads(ws.receive_text())


def test_websocket_conversa_inteira(app_do_canal):
    cliente, ponte = app_do_canal
    segredo = _liberar()
    with cliente.websocket_connect("/api/hera/ao-vivo") as ws:
        assert _apertar(ws, segredo) == {"t": "aceito"}
        resposta = json.loads(ws.receive_text())
        while resposta["t"] != "aberta":
            assert resposta["t"] != "fim", resposta
            resposta = json.loads(ws.receive_text())
        ws.send_bytes(canal.quadro(canal.MICROFONE, 1, 0, b"\x05\x00" * 160))
        ws.send_text(json.dumps({"t": "parar"}))
        while True:
            m = ws.receive()
            if "text" in m and m["text"] and json.loads(m["text"])["t"] == "fim":
                fim = json.loads(m["text"])
                break
    assert fim["motivo"] == "robot_stop"
    assert ParFalso.ultimo.som == [b"\x05\x00" * 160]
    assert len(ponte.sessoes) == 1


@pytest.mark.parametrize("preparar,motivo", [
    (lambda: None, "disabled"),
    (lambda: registro.Registro().mudar({"enabled": True}, local=True), "not_paired"),
])
def test_websocket_sem_liberacao_ou_credencial(app_do_canal, preparar, motivo):
    cliente, ponte = app_do_canal
    preparar()
    with cliente.websocket_connect("/api/hera/ao-vivo") as ws:
        ws.send_text(json.dumps(_ola()))
        assert json.loads(ws.receive_text()) == {"t": "fim", "motivo": motivo, "segundos": 0}
    assert ponte.sessoes == []


def test_websocket_prova_errada_nao_abre_sessao(app_do_canal):
    cliente, ponte = app_do_canal
    _liberar()
    with cliente.websocket_connect("/api/hera/ao-vivo") as ws:
        resposta = _apertar(ws, "segredo-errado-" + "x" * 30, conferir_pc=False)
    assert resposta == {"t": "fim", "motivo": "bad_proof", "segundos": 0}
    assert ponte.sessoes == []


def test_websocket_teto_atingido(app_do_canal):
    cliente, ponte = app_do_canal
    segredo = _liberar()
    lv = livro.Livro()
    import time
    sid = lv.abrir(dispositivo="garra-sala", nivel="completo", como="alone", agora=time.time() - 10)
    lv.fechar(sid, segundos=1800.0, motivo="max", agora=time.time() - 5)
    with cliente.websocket_connect("/api/hera/ao-vivo") as ws:
        assert _apertar(ws, segredo) == {"t": "fim", "motivo": "daily_limit", "segundos": 0}
    assert ponte.sessoes == []


# ─── os quadros de som ───────────────────────────────────────────────────────
def test_quadro_ida_e_volta_e_gemeo_do_robo():
    from garra_reachy_mini import ao_vivo

    q = canal.quadro(canal.FALA, 7, 1234, b"\x01\x02" * 10)
    assert len(q) == 12 + 20
    assert canal.ler_quadro(q) == (canal.FALA, 7, 1234, b"\x01\x02" * 10)
    assert ao_vivo.quadro(canal.FALA, 7, 1234, b"\x01\x02" * 10) == q
    assert ao_vivo.ler_quadro(q) == canal.ler_quadro(q)
    for torto in (b"", b"\x01" * 5, b"\x09\x01" + b"\x00" * 10, canal.quadro(canal.FALA, 1, 0, b"\x01")):
        assert canal.ler_quadro(torto) is None


# ─── a Hera olha pela câmera ─────────────────────────────────────────────────
# A camada falada delega "o que você está vendo?" como qualquer pedido. O
# computador reconhece que é para olhar, pede UM quadro ao robô (sem dizer o que
# foi perguntado), descreve com o modelo de visão da casa e devolve a descrição
# para ela falar. O Hermes não entra.
import base64  # noqa: E402

from companion import visao as visao_pc  # noqa: E402
from companion.hera import olhar  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
PREFS = {"provedor": "auto", "local_endpoint": "http://127.0.0.1:11434",
         "local_modelo": "qwen2.5vl:7b", "nuvem_modelo": "google/gemini-3.8-flash",
         "nuvem_permitida": False}


class Descritor:
    """O modelo de visão do computador, de mentira."""

    def __init__(self, texto="Uma caneca azul sobre a mesa, e uma pessoa sentada ao fundo.",
                 erro=None):
        self.texto, self.erro = texto, erro
        self.chamadas: list[tuple] = []

    def __call__(self, jpeg, pergunta, prefs):
        self.chamadas.append((jpeg, pergunta, prefs))
        if self.erro:
            raise visao_pc.ErroDeVisao(self.erro)
        return visao_pc.Observacao(self.texto, "local", "qwen2.5vl:7b", 900)


def _pedido_de_quadro(robo):
    pedidos = [m for m in robo.json if m["t"] == "olhar"]
    return pedidos[-1] if pedidos else None


async def _perguntar(par, dito="Hera, o que você está vendo?", ident="d1"):
    par.evento(type="session.input_transcript.delta", delta=dito)
    par.evento(type="session.delegation.created", delegation={"id": ident})


def _falado(par):
    return " ".join(e["content"] for e in par.enviados if e["type"] == "session.commentary.append")


def test_ver_pede_um_quadro_ao_robo_descreve_e_ela_fala():
    async def cena():
        ponte, descritor = PonteFalsa(), Descritor()
        c, robo = conversa(ponte, descrever=descritor)
        tarefa, par = await comecar(c)
        await _perguntar(par)
        assert await esperar(lambda: _pedido_de_quadro(robo) is not None)
        pedido = _pedido_de_quadro(robo)
        c.imagem({"t": "imagem", "id": pedido["id"], "jpeg": base64.b64encode(JPEG).decode(),
                  "prefs": PREFS, "idade_s": 0.2})
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        c.parar("robot_stop")
        await tarefa
        return ponte, descritor, par, robo, pedido

    ponte, descritor, par, robo, pedido = asyncio.run(cena())
    assert set(pedido) == {"t", "id"}, "o robô não recebe o que foi perguntado"
    assert descritor.chamadas == [(JPEG, "Hera, o que você está vendo?", PREFS)]
    assert ponte.turnos == [], "olhar não passa pelo Hermes"
    pensando = next(e for e in par.enviados if e["type"] == "session.thinking.append")
    assert pensando["content"] == olhar.OLHANDO
    falado = _falado(par)
    assert "Uma caneca azul sobre a mesa" in falado and "câmera" in falado
    assert {"t": "estado", "fase": "pensando"} in robo.json
    # nem a pergunta nem a descrição vão ao robô (o painel dele não pede senha)
    tudo = json.dumps(robo.json, ensure_ascii=False)
    assert "caneca" not in tudo and "vendo" not in tudo


@pytest.mark.parametrize("resposta_do_robo,explicacao", [
    ({"motivo": "disabled"}, "a visão está desligada na configuração"),
    ({"motivo": "camera_unavailable"}, "a câmera não está entregando imagem"),
    ({"motivo": "qualquer-coisa"}, "a câmera não está entregando imagem"),
    ({"jpeg": "isto não é base64!"}, "a imagem da câmera veio inválida"),
    ({}, "a imagem da câmera veio inválida"),
])
def test_sem_imagem_ela_diz_por_que_e_nada_e_descrito(resposta_do_robo, explicacao):
    async def cena():
        descritor = Descritor()
        c, robo = conversa(descrever=descritor)
        tarefa, par = await comecar(c)
        await _perguntar(par)
        assert await esperar(lambda: _pedido_de_quadro(robo) is not None)
        c.imagem({"t": "imagem", "id": _pedido_de_quadro(robo)["id"], **resposta_do_robo})
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        c.parar("robot_stop")
        await tarefa
        return descritor, par

    descritor, par = asyncio.run(cena())
    assert descritor.chamadas == []
    assert _falado(par) == f"Não consegui olhar agora: {explicacao}."


def test_robo_que_nao_responde_ao_pedido_de_quadro(monkeypatch):
    """Um robô 1.6.0.dev3 não conhece `olhar`: a Hera diz que não conseguiu e segue."""
    monkeypatch.setattr(canal, "ESPERA_IMAGEM_S", 0.3)

    async def cena():
        c, robo = conversa(descrever=Descritor())
        tarefa, par = await comecar(c)
        await _perguntar(par)
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        c.parar("robot_stop")
        await tarefa
        return par

    assert _falado(asyncio.run(cena())) == "Não consegui olhar agora: a câmera do robô não respondeu."


@pytest.mark.parametrize("codigo,explicacao", [
    ("provider_offline", "o modelo de visão no computador não respondeu"),
    ("cloud_disabled", "a descrição pela nuvem está desligada e o modelo local não serviu"),
    ("no_provider", "não há modelo de visão configurado no computador"),
])
def test_modelo_de_visao_falhou(codigo, explicacao):
    async def cena():
        c, robo = conversa(descrever=Descritor(erro=codigo))
        tarefa, par = await comecar(c)
        await _perguntar(par)
        assert await esperar(lambda: _pedido_de_quadro(robo) is not None)
        c.imagem({"t": "imagem", "id": _pedido_de_quadro(robo)["id"],
                  "jpeg": base64.b64encode(JPEG).decode(), "prefs": PREFS})
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        c.parar("robot_stop")
        await tarefa
        return par

    assert _falado(asyncio.run(cena())) == f"Não consegui olhar agora: {explicacao}."


def test_imagem_de_outro_pedido_nao_vale():
    async def cena():
        descritor = Descritor()
        c, robo = conversa(descrever=descritor)
        tarefa, par = await comecar(c)
        await _perguntar(par)
        assert await esperar(lambda: _pedido_de_quadro(robo) is not None)
        c.imagem({"t": "imagem", "id": "outro", "jpeg": base64.b64encode(JPEG).decode(), "prefs": PREFS})
        await asyncio.sleep(0.2)
        assert descritor.chamadas == []
        c.parar("robot_stop")
        await tarefa

    asyncio.run(cena())


def test_pergunta_que_nao_e_para_olhar_vai_ao_hermes_sem_pedir_quadro():
    async def cena():
        ponte, descritor = PonteFalsa(), Descritor()
        c, robo = conversa(ponte, descrever=descritor)
        tarefa, par = await comecar(c)
        await _perguntar(par, dito="que horas são?")
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        c.parar("robot_stop")
        await tarefa
        return ponte, descritor, robo

    ponte, descritor, robo = asyncio.run(cena())
    assert [t for _, t in ponte.turnos] == ["que horas são?"]
    assert descritor.chamadas == [] and "olhar" not in [m["t"] for m in robo.json]


def test_sem_descritor_o_pedido_de_ver_segue_para_o_hermes():
    async def cena():
        ponte = PonteFalsa()
        c, robo = conversa(ponte)
        tarefa, par = await comecar(c)
        await _perguntar(par)
        assert await esperar(lambda: "session.commentary.append" in par.tipos())
        c.parar("robot_stop")
        await tarefa
        return ponte, robo

    ponte, robo = asyncio.run(cena())
    assert len(ponte.turnos) == 1 and "olhar" not in [m["t"] for m in robo.json]


# ─── o detector e as frases (gêmeos do robô) ─────────────────────────────────
def test_o_detector_e_os_motivos_sao_gemeos_dos_do_robo():
    from garra_reachy_mini import visao as visao_robo
    assert olhar._GATILHOS == visao_robo._GATILHOS
    for codigo, texto in visao_robo.MOTIVOS.items():
        assert olhar.MOTIVOS[codigo] == texto
    for codigo in visao_pc.CODIGOS:
        assert codigo in olhar.MOTIVOS, codigo
    for frase in ("Hera, o que você está vendo?", "O que você tá vendo agora?", "descreve a sala",
                  "olha isso aqui", "o que eu estou segurando?", "que horas são?",
                  "você viu o jogo ontem?", "Era uma vez um rei"):
        assert olhar.pede_visao(frase) is visao_robo.pede_visao(frase), frase


def test_imagem_grande_demais_e_recusada():
    grande = "A" * (olhar.IMAGEM_B64_MAX + 4)
    assert olhar.ler_imagem({"t": "imagem", "id": "x", "jpeg": grande}) == "bad_image"


# ─── pelo WebSocket de verdade ───────────────────────────────────────────────
def test_websocket_o_robo_manda_a_imagem_e_ela_e_descrita(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class ParQuePergunta(ParFalso):
        """A "OpenAI" abre e, de dentro do laço do servidor, delega um pedido de ver."""

        async def abrir(self):
            await super().abrir()
            self.ao_evento({"type": "session.started", "session": {"id": "s"}})
            asyncio.get_running_loop().call_later(0.2, lambda: (
                self.ao_evento({"type": "session.input_transcript.delta", "delta": "o que você está vendo?"}),
                self.ao_evento({"type": "session.delegation.created", "delegation": {"id": "d9"}})))

    descritor = Descritor()
    app = FastAPI()
    canal.registrar(app, ponte=PonteFalsa(), abrir_par=ParQuePergunta, host_e_meu=lambda h: h == HOST,
                    limites_de_teste={"session_max_s": 5.0, "idle_close_s": 3.0, "no_speech_s": 3.0},
                    descrever=descritor)
    segredo = _liberar()
    with TestClient(app).websocket_connect("/api/hera/ao-vivo") as ws:
        assert _apertar(ws, segredo) == {"t": "aceito"}
        while True:
            m = json.loads(ws.receive_text())
            assert m["t"] != "fim", m
            if m["t"] == "olhar":
                break
        ws.send_text(json.dumps({"t": "imagem", "id": m["id"], "jpeg": base64.b64encode(JPEG).decode(),
                                 "prefs": PREFS, "idade_s": 0.1}))
        fim = None
        prazo = 0
        while fim is None and prazo < 200:
            prazo += 1
            if any(e["type"] == "session.commentary.append" for e in ParFalso.ultimo.enviados):
                ws.send_text(json.dumps({"t": "parar"}))
            m = ws.receive()
            if m.get("text") and json.loads(m["text"])["t"] == "fim":
                fim = json.loads(m["text"])
    assert fim and fim["motivo"] == "robot_stop"
    assert descritor.chamadas == [(JPEG, "o que você está vendo?", PREFS)]
    assert "Uma caneca azul" in _falado(ParFalso.ultimo)
