"""O laço de voz de verdade, com microfone, transcrição e cérebro de mentira.

Os outros testes da escuta olham módulos soltos ou a ORDEM das linhas no fonte.
Este roda `_laco_voz` inteiro e confere o que de fato saiu por cada porta: o que
o modelo recebeu, o que o alto-falante disse, o que o chat mostrou, o que o log
guardou e o que ficou no caderno.

A cena é a que define o recurso: alguém comenta que a manteiga acabou, e depois
pede ao robô para anotar "isso".
"""

from __future__ import annotations

import json
import logging
import threading
import time

import numpy as np
import pytest

from garra_reachy_mini import armazenamento, envelope, familia, visao
from garra_reachy_mini.acompanhamento import Acompanhamento
from garra_reachy_mini.cerebro import RespostaCerebro
from garra_reachy_mini.config import Config
from garra_reachy_mini.main import GarraReachyMini
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.servicos import Servicos

SR = 16000
CONVERSA_DA_SALA = "Acabou a manteiga, hein."


class Microfone:
    """Silêncio, até o teste mandar alguém falar."""

    def __init__(self) -> None:
        self._pendentes = 0
        self._trava = threading.Lock()
        self.tocadas: list[np.ndarray] = []
        self.lendo = threading.Event()

    # o que o laço chama
    def get_input_audio_samplerate(self) -> int: return SR
    def get_output_audio_samplerate(self) -> int: return SR
    def start_recording(self) -> None: pass
    def start_playing(self) -> None: pass
    def stop_recording(self) -> None: pass
    def stop_playing(self) -> None: pass
    def clear_player(self) -> None: pass

    def push_audio_sample(self, onda) -> None:
        self.tocadas.append(onda)

    def get_audio_sample(self):
        self.lendo.set()
        with self._trava:
            if self._pendentes:
                self._pendentes -= 1
                return np.full(SR // 10, 0.2, dtype=np.float32)
        return None

    # o que o teste chama
    def dizer(self) -> None:
        """Seis blocos de 100 ms acima do limiar: uma fala de 0,6 s."""
        with self._trava:
            self._pendentes = 6


class Voz:
    """Transcreve o que o teste roteirizou e 'sintetiza' 50 ms por frase."""

    def __init__(self, roteiro: list[str]) -> None:
        self.roteiro = list(roteiro)
        self.transcritas = 0
        self.faladas: list[str] = []

    def pronto(self, timeout: float = 5.0) -> bool: return True

    def transcrever(self, audio, sr) -> str:
        texto = self.roteiro[self.transcritas]
        self.transcritas += 1
        return texto

    def falar(self, frase: str, sr: int) -> np.ndarray:
        self.faladas.append(frase)
        return np.zeros(sr // 20, dtype=np.float32)


class Cerebro:
    disponivel = True

    def __init__(self, respostas: list[str]) -> None:
        self.respostas = list(respostas)
        self.recebidas: list[str] = []

    def perguntar(self, texto: str) -> RespostaCerebro:
        self.recebidas.append(texto)
        return RespostaCerebro(True, self.respostas[len(self.recebidas) - 1], "gateway",
                               marca=len(self.recebidas))

    def novas_mensagens(self) -> list: return []
    def confirmar_falado(self, marca) -> None: pass
    def fechar(self) -> None: pass
    def nova_sessao(self): return None


class Gestos:
    """O `Comportamento` sem robô: guarda o que o laço pediu, para o teste ler."""

    def __init__(self) -> None:
        self.fases: list[tuple[str, str | None]] = []
        self.erros: list[str] = []
        self.ativacoes = 0
        self.pensou = 0           # quantas vezes o corpo foi a "pensando"
        self.fase_ao_ouvir = None

    def ouvindo(self) -> None: self.atualizar_fase_de_escuta()
    def pensando(self) -> None: self.pensou += 1
    def falando(self) -> None: self.fase("speaking")
    def ativado(self) -> None: self.ativacoes += 1
    def erro(self, codigo: str, detalhe: str = "") -> None: self.erros.append(codigo)

    def fase(self, nova: str, motivo: str | None = None) -> None:
        if not self.fases or self.fases[-1] != (nova, motivo):
            self.fases.append((nova, motivo))

    def atualizar_fase_de_escuta(self) -> None:
        if self.fase_ao_ouvir is not None:
            self.fase(*self.fase_ao_ouvir())


class Cena:
    """O app montado à mão, com o laço de voz rodando numa thread."""

    def __init__(self, backend, pasta, conversa: dict, roteiro: list[str],
                 respostas: list[str], mic=None, voz=None, olho=None) -> None:
        armazenamento.salvar_config({"conversation": conversa})
        self.mic, self.voz = mic or Microfone(), voz or Voz(roteiro)
        self.cerebro = Cerebro(respostas)
        self.ctrl = ControladorRobo(backend, semente=1, dir_capturas=pasta)
        self.ctrl.iniciar()
        app = GarraReachyMini.__new__(GarraReachyMini)
        app.logger = logging.getLogger("garra_reachy_mini.teste_do_laco")
        app.servicos = Servicos()
        app.comportamento = Gestos()
        app.hub = None
        app.acompanhamento = Acompanhamento(self.ctrl.eventos)
        # Sem câmera, a não ser que o teste dê uma: o laço pergunta ao olho só
        # quando o pedido pede para olhar.
        app.olho = olho or visao.Olho(None, None, self.ctrl.eventos)
        app.familia = familia.Familia(None, None, self.ctrl.eventos)
        app._midia_ligada = False
        app._acordar = threading.Event()
        app._falhas_voz = 0
        app._avisou_sem_cerebro = 0.0
        app._turno = None
        app._aleatorio = __import__("random").Random(1)
        app._lock_fala = threading.Lock()
        app._falar_texto = app._calar = app._trocar_sessao = None
        app._avaliar_cerebro = lambda cfg: self.cerebro
        self.app = app
        self.parar = threading.Event()
        robo = type("Robo", (), {"media": self.mic})()
        self.thread = threading.Thread(
            target=app._laco_voz,
            args=(robo, self.parar, Config.carregar(), self.ctrl, self.voz), daemon=True)
        self.thread.start()
        assert self.mic.lendo.wait(10), "o laço de voz não chegou a ler o microfone"

    def alguem_fala(self, espera_s: float = 20.0) -> None:
        """Uma fala, e espera o laço terminar de tratá-la (inclusive responder)."""
        antes = self.voz.transcritas
        turnos = len(self.eventos("voice.turn.completed"))
        self.mic.dizer()
        fim = time.monotonic() + espera_s
        while time.monotonic() < fim:
            if self.voz.transcritas > antes:
                # Recusada, ou aceita e já respondida: nos dois casos o laço
                # voltou a escutar. Um turno só acaba no `voice.turn.completed`.
                time.sleep(0.3)
                respondendo = len(self.cerebro.recebidas) > turnos
                if not respondendo or len(self.eventos("voice.turn.completed")) > turnos:
                    return
            time.sleep(0.05)
        raise AssertionError("o laço de voz não terminou de tratar a fala")

    def eventos(self, tipo: str | None = None) -> list[dict]:
        todos = self.ctrl.eventos.historico(400)
        return [e for e in todos if tipo is None or e["type"] == tipo]

    def encerrar(self) -> None:
        self.parar.set()
        self.thread.join(timeout=10)
        self.app._encerrar_executor()
        self.ctrl.encerrar(timeout=2)


@pytest.fixture()
def montar_cena(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    monkeypatch.setenv("GARRA_LIMIAR", "0.05")          # sem calibrar o ambiente
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "0")
    monkeypatch.setenv("GARRA_NOTIFICACOES_S", "3600")
    cenas: list[Cena] = []

    def _montar(conversa: dict, roteiro: list[str], respostas: list[str]) -> Cena:
        cenas.append(Cena(backend, tmp_path, conversa, roteiro, respostas))
        return cenas[-1]

    yield _montar
    for c in cenas:
        c.encerrar()


LIGADO = {"wake_phrase_enabled": True, "ambient_context_enabled": True,
          "notebook_enabled": True}
ROTEIRO = [CONVERSA_DA_SALA,
           "Fala Garra, insira isso na lista de compras",
           "o que tem na lista de compras?"]
RESPOSTAS = ["Anotado, manteiga na lista de compras.\nCADERNO: +compras: manteiga",
             "Na lista de compras tem manteiga."]


def test_a_cena_da_manteiga(montar_cena, caplog, tmp_path) -> None:
    caplog.set_level(logging.DEBUG)
    cena = montar_cena(LIGADO, ROTEIRO, RESPOSTAS)

    cena.alguem_fala()          # conversa da sala, sem chamar o robô
    assert cena.cerebro.recebidas == [], "fala sem a frase não consulta o modelo"
    assert cena.voz.faladas == [], "e o robô não diz nada"

    cena.alguem_fala()          # "Fala Garra, insira isso na lista de compras"
    cena.alguem_fala()          # sessão aberta: "o que tem na lista de compras?"

    primeira, segunda = cena.cerebro.recebidas
    # 1. o modelo recebeu o pedido COM o que se falava na sala
    linhas = primeira.split("\n")
    assert any(l.startswith("> há ") and l.endswith(f": {CONVERSA_DA_SALA}") for l in linhas)
    assert envelope.pedido_de(primeira) == "insira isso na lista de compras"
    assert "> (caderno vazio)" in linhas
    # 2. o que foi ouvido vai uma vez só, e o caderno já aparece anotado
    assert "CONTEXTO DO AMBIENTE" not in segunda
    assert "> compras: manteiga" in segunda.split("\n")
    assert envelope.pedido_de(segunda) == "o que tem na lista de compras?"
    # 3. o caderno está em disco
    assert json.loads((tmp_path / "caderno.json").read_text())["lists"] == {
        "compras": ["manteiga"]}
    # 4. o alto-falante disse a resposta, e não a diretiva
    assert cena.voz.faladas == ["Anotado, manteiga na lista de compras.",
                                "Na lista de compras tem manteiga."]
    # 5. o chat mostra o que a pessoa disse e o que o robô respondeu
    chat = [(e["role"], e["content"]) for e in cena.eventos("chat.message")]
    assert chat == [
        ("user", "insira isso na lista de compras"),
        ("assistant", "Anotado, manteiga na lista de compras."),
        ("user", "o que tem na lista de compras?"),
        ("assistant", "Na lista de compras tem manteiga."),
    ]
    # 6. a conversa da sala não está em evento nenhum, nem no log
    tudo = json.dumps(cena.eventos(), ensure_ascii=False)
    assert "Acabou a manteiga" not in tudo and "CONTEXTO DO AMBIENTE" not in tudo
    assert "Acabou a manteiga" not in caplog.text
    assert "CONTEXTO DO AMBIENTE" not in caplog.text and "CADERNO:" not in caplog.text
    # 7. e os eventos dizem quanto, não o quê
    [contexto] = cena.eventos("voice.context.attached")
    assert (contexto["utterances"], contexto["chars"]) == (1, len(CONVERSA_DA_SALA))
    [anotou] = cena.eventos("notebook.changed")
    assert (anotou["list"], anotou["op"], anotou["applied"]) == ("compras", "add", True)


def test_com_a_escuta_desligada_a_conversa_da_sala_nao_sai_do_robo(montar_cena, caplog) -> None:
    """O comportamento de hoje, que não pode mudar para quem não ligou nada."""
    caplog.set_level(logging.DEBUG)
    cena = montar_cena({"wake_phrase_enabled": True}, ROTEIRO[:2], ["Anotar o quê?"])

    cena.alguem_fala()
    cena.alguem_fala()

    assert cena.cerebro.recebidas == ["insira isso na lista de compras"]
    assert len(cena.app.acompanhamento.memoria) == 0
    assert cena.voz.faladas == ["Anotar o quê?"]
    assert cena.eventos("voice.context.attached") == []
    assert "Acabou a manteiga" not in json.dumps(cena.eventos(), ensure_ascii=False)
    assert "Acabou a manteiga" not in caplog.text


def test_chamar_o_robo_sem_pedir_nada_guarda_o_contexto_para_o_pedido(montar_cena) -> None:
    """ "Fala Garra." — pausa — "anota isso": o contexto é do segundo, não do primeiro."""
    cena = montar_cena(
        LIGADO, [CONVERSA_DA_SALA, "Fala Garra.", "anota isso na lista de compras"],
        ["Anotado.\nCADERNO: +compras: manteiga"])

    cena.alguem_fala()
    cena.alguem_fala()
    assert cena.cerebro.recebidas == [], "ativação sozinha não é pedido"
    cena.alguem_fala()

    [mensagem] = cena.cerebro.recebidas
    assert f": {CONVERSA_DA_SALA}" in mensagem
    assert envelope.pedido_de(mensagem) == "anota isso na lista de compras"


def test_com_o_teste_de_escuta_armado_o_pare_continua_sendo_pare(montar_cena, monkeypatch) -> None:
    """Achado da revisão: a sonda do painel engolia TODA fala, inclusive a
    parada de emergência. "Pare" não é assunto de teste."""
    monkeypatch.setenv("GARRA_ATALHOS_LOCAIS", "1")
    cena = montar_cena({"wake_phrase_enabled": True}, ["pare", "bom dia a todos"], ["Parei."])
    app = cena.app
    app._sonda_escuta, app._trava_sonda = None, threading.Lock()
    ouvido: list = []
    fio = threading.Thread(target=lambda: ouvido.append(app._sondar_escuta(20.0)), daemon=True)
    fio.start()
    fim = time.monotonic() + 5
    while app._sonda_escuta is None and time.monotonic() < fim:
        time.sleep(0.01)
    assert app._sonda_escuta is not None

    def esperar(condicao, limite_s: float = 15.0) -> bool:
        fim = time.monotonic() + limite_s
        while time.monotonic() < fim:
            if condicao():
                return True
            time.sleep(0.05)
        return False

    cena.mic.dizer()                              # "pare", com o teste armado
    assert esperar(lambda: cena.ctrl.em_estop), "o pare não parou o robô"
    assert cena.eventos("robot.estop")
    assert ouvido == [], "o teste não pode ficar com a parada de emergência"
    # O turno do "pare" termina (falado ou cancelado) antes da próxima fala.
    esperar(lambda: cena.eventos("voice.turn.completed") or cena.eventos("voice.turn.cancelled"), 8.0)
    time.sleep(0.5)

    cena.mic.dizer()                              # a fala seguinte é do teste
    fio.join(15)
    assert ouvido and ouvido[0]["text"] == "bom dia a todos"
    assert "bom dia" not in json.dumps(cena.cerebro.recebidas, ensure_ascii=False), \
        "fala de teste não vai a agente nenhum"
