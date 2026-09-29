"""A escuta de contexto e o caderno DENTRO do app: laço de voz, chat e painel.

Os módulos puros têm os seus testes. Aqui se prova a costura, que é onde a
promessa de privacidade quebraria sem ninguém ver: a fala recusada indo parar
num log, a mensagem montada aparecendo no chat, a diretiva saindo pelo
alto-falante.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini import envelope
from garra_reachy_mini.acompanhamento import Acompanhamento
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub
from garra_reachy_mini.web.seguranca import resolver_politica

MAIN = (Path(__file__).resolve().parent.parent
        / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")


def _funcao(nome: str) -> str:
    linhas = MAIN.splitlines()
    i = next(n for n, l in enumerate(linhas) if f"def {nome}(" in l)
    nivel = len(linhas[i]) - len(linhas[i].lstrip())
    for n in range(i + 1, len(linhas)):
        if linhas[n].strip() and (len(linhas[n]) - len(linhas[n].lstrip())) <= nivel:
            return "\n".join(linhas[i:n])
    return "\n".join(linhas[i:])


def _sem_comentarios(codigo: str) -> str:
    return "\n".join(l for l in codigo.splitlines() if not l.lstrip().startswith("#"))


def _ramo_da_recusa() -> str:
    """O código do ramo, sem os comentários: o que importa é o que EXECUTA."""
    corpo = _sem_comentarios(_funcao("processar"))
    inicio = corpo.index("if not veredicto.aceito:")
    return corpo[inicio:corpo.index("return", inicio)]


# ─── o laço de voz ───────────────────────────────────────────────────────────
def test_a_fala_recusada_vai_para_a_memoria() -> None:
    ramo = _ramo_da_recusa()
    assert "self.acompanhamento.ouviu(texto, capturado_em" in ramo


def test_so_a_recusa_por_falta_da_frase_e_guardada() -> None:
    """`echo_suppressed` é o próprio robô dizendo a frase: não é conversa da sala."""
    ramo = _ramo_da_recusa()
    guarda = ramo.index('veredicto.motivo == "wake_phrase_required"')
    assert guarda < ramo.index("self.acompanhamento.ouviu(")


def test_a_fala_recusada_continua_fora_do_log_e_do_evento() -> None:
    ramo = _ramo_da_recusa()
    assert "log." not in ramo, "o texto recusado não entra no log do app"
    for proibido in ("content=", "transcript", "texto=", "text="):
        assert proibido not in ramo, f"o ramo da recusa carrega {proibido}"


def test_a_mensagem_e_montada_e_e_ela_que_vai_ao_cerebro() -> None:
    corpo = _funcao("processar")
    montagem = corpo.index("mensagem = self.acompanhamento.montar(texto")
    envio = corpo.index("executor.submit(cerebro.perguntar, mensagem + complemento)")
    assert montagem < envio
    assert "cerebro.perguntar, texto" not in corpo, "o pedido cru ainda vai ao cérebro"


def test_a_ativacao_sozinha_nao_gasta_o_contexto() -> None:
    """ "Fala Garra" e mais nada: o contexto espera o pedido que vem a seguir."""
    corpo = _funcao("processar")
    so_ativacao = corpo.index("if veredicto.so_ativacao:")
    assert corpo.index("return", so_ativacao) < corpo.index("self.acompanhamento.montar(")


def test_o_atalho_local_que_nao_consulta_o_modelo_nao_gasta_o_contexto() -> None:
    """ "Pare" responde na hora e retorna; colher antes disso jogaria fora o contexto."""
    corpo = _funcao("processar")
    assert corpo.index("intencao.resposta_imediata") < corpo.index(
        "self.acompanhamento.montar(")


def test_a_mensagem_montada_nao_vai_para_log_nem_para_evento() -> None:
    """O chat e o log mostram o que a pessoa DISSE; o resto é entre o app e o modelo."""
    corpo = _sem_comentarios(_funcao("processar"))
    for linha in corpo.splitlines():
        if "mensagem" in linha and "self.acompanhamento.montar(" not in linha:
            assert "log." not in linha and "publicar(" not in linha and "content=" not in linha, linha
    # E nenhuma chamada de log/evento que se estenda por várias linhas a recebe.
    for trecho in corpo.split("controlador.eventos.publicar(")[1:]:
        assert "mensagem" not in trecho[:trecho.index(")")]


def test_a_resposta_passa_pelo_caderno_antes_de_ser_limpa_para_a_voz() -> None:
    corpo = _funcao("processar")
    recebe = corpo.index("recebida = self.acompanhamento.receber(")
    assert recebe < corpo.index("fala = limpar_para_voz(recebida)")
    assert "limpar_para_voz(resposta.texto)" not in corpo, (
        "a resposta crua ainda vai para a voz, com a diretiva dentro")


def test_a_notificacao_tambem_nao_fala_diretiva() -> None:
    """Mensagem que chega pelo histórico pode trazer a linha de caderno."""
    assert "limpar_para_voz(evento.texto)" not in MAIN
    assert "caderno_mod.sem_diretivas(evento.texto)" in MAIN


def test_um_acompanhamento_por_app_e_nao_por_laco_ou_por_turno() -> None:
    """A memória do ambiente tem de sobreviver entre turnos, e o painel precisa
    alcançar a MESMA que o laço de voz usa."""
    for nome in ("processar", "_laco_voz"):
        assert "Acompanhamento(" not in _funcao(nome)
    assert "acompanhamento=self.acompanhamento" in _funcao("_montar_web")


# ─── o painel ────────────────────────────────────────────────────────────────
class ChatFalso:
    """Guarda o que recebeu e responde o que o teste mandar."""

    base, agent_id, sessao = "http://gateway.falso", "reachy_voice", "sessao-1"

    def __init__(self) -> None:
        self.recebidas: list[str] = []
        self.resposta = "Ok."
        self.historia: list[dict] = []

    async def disponivel(self) -> bool:
        return True

    async def enviar(self, conteudo: str) -> dict:
        self.recebidas.append(conteudo)
        self.historia += [{"role": "user", "content": conteudo},
                          {"role": "assistant", "content": self.resposta}]
        return {"content": self.resposta, "session_id": self.sessao}

    async def historico(self) -> list[dict]:
        return list(self.historia)


@pytest.fixture()
def painel(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    chat = ChatFalso()
    ac = Acompanhamento(ctrl.eventos)
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos, chat=chat, acompanhamento=ac,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        c.chat, c.ac, c.eventos = chat, ac, ctrl.eventos
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def ligar(painel, **chaves) -> None:
    assert painel.put("/api/robot/conversation", json=chaves).status_code == 200


def test_o_painel_liga_e_le_os_interruptores_novos(painel) -> None:
    ligar(painel, wake_phrase_enabled=True, ambient_context_enabled=True,
          ambient_context_window_s=90, notebook_enabled=True)
    c = painel.get("/api/robot/conversation").json()["conversation"]
    assert (c["ambient_context_enabled"], c["ambient_context_window_s"],
            c["notebook_enabled"]) == (True, 90, True)


def test_com_tudo_desligado_o_chat_manda_o_que_foi_digitado(painel) -> None:
    painel.post("/api/chat/enviar", json={"content": "que horas são?"})
    assert painel.chat.recebidas == ["que horas são?"]


def test_com_o_caderno_ligado_o_chat_leva_o_caderno_e_anota(painel) -> None:
    ligar(painel, notebook_enabled=True)
    painel.ac.caderno.adicionar("compras", "leite")
    painel.chat.resposta = "Anotado, manteiga na lista.\nCADERNO: +compras: manteiga"
    r = painel.post("/api/chat/enviar", json={"content": "põe manteiga na lista"})
    assert r.status_code == 200
    assert r.json()["content"] == "Anotado, manteiga na lista."
    [mensagem] = painel.chat.recebidas
    assert "> compras: leite" in mensagem.split("\n")
    assert envelope.pedido_de(mensagem) == "põe manteiga na lista"
    assert painel.ac.caderno.ler() == {"compras": ["leite", "manteiga"]}


def test_o_chat_nao_leva_nem_gasta_o_que_o_microfone_ouviu(painel) -> None:
    ligar(painel, wake_phrase_enabled=True, ambient_context_enabled=True)
    conf = painel.get("/api/robot/conversation").json()["conversation"]
    painel.ac.ouviu("conversa da sala", 100.0, conf)
    painel.post("/api/chat/enviar", json={"content": "oi"})
    assert painel.chat.recebidas == ["oi"]
    assert len(painel.ac.memoria) == 1


def test_o_evento_do_chat_mostra_o_pedido_e_a_resposta_limpa(painel) -> None:
    ligar(painel, notebook_enabled=True)
    painel.chat.resposta = "Feito.\nCADERNO: +compras: pão"
    painel.post("/api/chat/enviar", json={"content": "põe pão"})
    mensagens = [e for e in painel.eventos.historico(50) if e["type"] == "chat.message"]
    assert [(m["role"], m["content"]) for m in mensagens] == [
        ("user", "põe pão"), ("assistant", "Feito.")]


def test_o_historico_mostra_so_o_pedido_e_a_resposta_sem_diretiva(painel) -> None:
    ligar(painel, notebook_enabled=True)
    painel.chat.resposta = "Feito.\nCADERNO: +compras: pão"
    painel.post("/api/chat/enviar", json={"content": "põe pão"})
    mensagens = painel.get("/api/chat/historico").json()["messages"]
    assert [(m["role"], m["content"]) for m in mensagens] == [
        ("user", "põe pão"), ("assistant", "Feito.")]


def test_o_painel_le_o_caderno(painel) -> None:
    painel.ac.caderno.adicionar("compras", "manteiga")
    painel.ac.caderno.adicionar("lista de tarefas", "levar o lixo")
    d = painel.get("/api/robot/notebook").json()
    assert d["lists"] == {"compras": ["manteiga"], "tarefas": ["levar o lixo"]}
    assert d["limits"] == {"lists": 20, "items": 100, "item_chars": 80}


def test_o_painel_apaga_um_item_e_uma_lista(painel) -> None:
    for item in ("manteiga", "leite"):
        painel.ac.caderno.adicionar("compras", item)
    painel.ac.caderno.adicionar("tarefas", "lixo")
    r = painel.delete("/api/robot/notebook/compras/0")
    assert r.status_code == 200 and r.json()["lists"]["compras"] == ["leite"]
    assert painel.delete("/api/robot/notebook/compras/7").status_code == 404
    assert painel.delete("/api/robot/notebook/tarefas").json()["lists"] == {
        "compras": ["leite"]}
    assert painel.delete("/api/robot/notebook/tarefas").status_code == 404


def test_apagar_pelo_painel_vira_evento_sem_o_item(painel) -> None:
    painel.ac.caderno.adicionar("compras", "remédio de pressão")
    painel.delete("/api/robot/notebook/compras/0")
    [ev] = [e for e in painel.eventos.historico(50) if e["type"] == "notebook.changed"]
    assert (ev["list"], ev["op"], ev["applied"], ev["items"]) == ("compras", "remove", True, 0)
    assert "remédio" not in json.dumps(ev, ensure_ascii=False)


def test_o_painel_manda_esquecer_o_que_foi_ouvido(painel) -> None:
    ligar(painel, wake_phrase_enabled=True, ambient_context_enabled=True)
    conf = painel.get("/api/robot/conversation").json()["conversation"]
    painel.ac.ouviu("uma", 100.0, conf)
    painel.ac.ouviu("duas", 101.0, conf)
    r = painel.delete("/api/robot/ambient-context")
    assert r.status_code == 200 and r.json() == {"ok": True, "forgotten": 2}
    assert len(painel.ac.memoria) == 0


def test_o_estado_da_escuta_diz_quanto_e_nao_o_que(painel) -> None:
    ligar(painel, wake_phrase_enabled=True, ambient_context_enabled=True)
    conf = painel.get("/api/robot/conversation").json()["conversation"]
    painel.ac.ouviu("segredo de família", 100.0, conf)
    d = painel.get("/api/robot/ambient-context").json()
    assert d == {"enabled": True, "window_s": 120, "utterances": 1,
                 "chars": len("segredo de família")}
