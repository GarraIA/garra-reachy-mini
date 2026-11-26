"""A conversa ao vivo no robô, fase 5: interrupção, corpo, troca e reconexão.

O `Atendente` roda de verdade, numa thread, contra um computador de mentira
(`hera_pc_falso.PcFalso`). As portas do laço de voz são dublês que guardam o que
o atendimento fez: o que tocou, o que cortou, as fases e os pedidos ao corpo.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from garra_reachy_mini import ao_vivo
from garra_reachy_mini.roteador import Chamado
from hera_pc_falso import HOST, SEGREDO, SENHA, PcFalso

SR = 16000
BLOCO = 1024
CRED = {"url": f"ws://{HOST}{ao_vivo.CAMINHO}", "host": HOST, "segredo": SEGREDO,
        "dispositivo": "garra-sala"}


def esperar(condicao, segundos: float = 5.0) -> bool:
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.01)
    return False


class Robo:
    """As portas do laço de voz, com um microfone que entrega 64 ms a cada 64 ms."""

    def __init__(self, nivel: str = "still", continuo: bool = False) -> None:
        self.nivel = nivel
        self.continuo = continuo     # um bloco a cada volta: o robô sempre manda antes de ler
        self.tocadas: list[np.ndarray] = []
        self.cortes = 0
        self.fases: list[str] = []
        self.corpo: list[str] = []
        self.estops = 0
        self.parar = False
        self.eventos: list[tuple[str, dict]] = []
        self.faladas: list[str] = []
        self.blocos_lidos = 0
        self._proximo = 0.0

    def ler_mono(self) -> np.ndarray:
        agora = time.monotonic()
        if agora < self._proximo and not self.continuo:
            return np.empty(0, np.float32)
        self._proximo = agora + BLOCO / SR
        self.blocos_lidos += 1
        # cada bloco diz quem é: o número dele, em amplitude
        return np.full(BLOCO, 0.001 * (self.blocos_lidos % 500 + 1), np.float32)

    def portas(self) -> ao_vivo.Portas:
        return ao_vivo.Portas(
            ler_mono=self.ler_mono, sr_in=SR, sr_out=SR, tocar=self.tocadas.append,
            cortar=self._cortar, fase=self.fases.append, sem_movimento=lambda: None,
            estop=self._estop, deve_parar=lambda: self.parar, credenciais=lambda: dict(CRED),
            saida_habilitada=lambda: True, trava_fala=threading.Lock(),
            corpo=self.corpo.append, movimento=lambda: self.nivel)

    def _cortar(self):
        self.cortes += 1

    def _estop(self):
        self.estops += 1

    def tipos(self) -> list[str]:
        return [t for t, _ in self.eventos]


class Conversa:
    def __init__(self, robo: Robo, pc: PcFalso, monkeypatch) -> None:
        monkeypatch.setattr(ao_vivo, "conectar", pc)
        self.robo, self.pc = robo, pc
        self.at = ao_vivo.Atendente(lambda tipo, **k: robo.eventos.append((tipo, k)),
                                    robo.faladas.append, portas=robo.portas())
        self.resultado: list[bool] = []
        self.thread = threading.Thread(target=lambda: self.resultado.append(
            self.at.atender(Chamado("hera", ("oi", "hera"), "alone"))), daemon=True)
        self.thread.start()
        assert esperar(lambda: "voice.live.started" in robo.tipos()), robo.tipos()

    def terminar(self, motivo: str = "idle", **extra) -> dict:
        self.pc.empurrar({"t": "fim", "motivo": motivo, "segundos": 3, **extra})
        self.thread.join(10)
        assert not self.thread.is_alive()
        return dict(next(k for t, k in self.robo.eventos if t == "voice.live.ended"))


@pytest.fixture
def conversa(monkeypatch):
    def _abrir(robo: Robo | None = None, pc: PcFalso | None = None) -> Conversa:
        return Conversa(robo or Robo(), pc or PcFalso(falar=False), monkeypatch)
    return _abrir


def _fala(segundos: float) -> bytes:
    onda = np.sin(np.arange(int(SR * segundos)) * 2 * np.pi * 330 / SR) * 0.3
    return (onda * 32767).astype("<i2").tobytes()


# ─── interrupção ─────────────────────────────────────────────────────────────
def test_cortar_joga_fora_a_voz_dela_que_ainda_nao_saiu_e_nao_mexe_no_microfone(conversa):
    c = conversa()
    tons = len(c.robo.tocadas)                       # as três notas de entrada
    c.pc.empurrar({"t": "estado", "fase": "falando"})
    # menos que a folga: ainda não foi para o alto-falante quando o corte chega
    c.pc.empurrar(ao_vivo.quadro(ao_vivo.FALA, 1, 0, _fala(0.06)))
    c.pc.empurrar({"t": "cortar"})
    time.sleep(0.4)
    assert len(c.robo.tocadas) == tons
    # sem `clear_player`: no robô ele pausaria o microfone, e a pessoa está falando
    assert c.robo.cortes == 0
    c.terminar()


def test_calada_por_um_tempo_ela_volta_a_ouvir(conversa):
    c = conversa()
    c.pc.empurrar({"t": "estado", "fase": "falando"})
    for i in range(10):
        c.pc.empurrar(ao_vivo.quadro(ao_vivo.FALA, i, 0, _fala(0.02)))
    assert esperar(lambda: ao_vivo.FASE_FALANDO in c.robo.fases)
    assert esperar(lambda: c.robo.fases[-1] == ao_vivo.FASE_OUVINDO, 3.0), c.robo.fases
    c.terminar()


# ─── o corpo, por nível ──────────────────────────────────────────────────────
def test_parado_o_corpo_nao_recebe_pedido_nenhum(conversa):
    c = conversa(Robo("still"))
    for fase in ("pensando", "falando", "ouvindo"):
        c.pc.empurrar({"t": "estado", "fase": fase})
        time.sleep(0.1)
    c.terminar()
    assert c.robo.corpo == []


def test_ao_falar_o_corpo_balanca_so_enquanto_ela_fala(conversa):
    c = conversa(Robo("speaking"))
    c.pc.empurrar({"t": "estado", "fase": "pensando"})
    time.sleep(0.1)
    c.pc.empurrar({"t": "estado", "fase": "falando"})
    time.sleep(0.1)
    c.pc.empurrar({"t": "estado", "fase": "ouvindo"})
    time.sleep(0.1)
    c.terminar()
    # nada ao pensar (fora da fala dela o servo é ouvido); e o fim aquieta
    assert c.robo.corpo[:2] == ["speaking", "quiet"]
    assert "thinking" not in c.robo.corpo


def test_expressivo_mexe_as_antenas_ao_pensar_com_o_microfone_calado(conversa):
    c = conversa(Robo("expressive"))
    time.sleep(0.3)
    antes = len(c.pc.som)
    c.pc.empurrar({"t": "estado", "fase": "pensando"})
    assert esperar(lambda: "thinking" in c.robo.corpo)
    time.sleep(ao_vivo.MUDO_GESTO_S + 0.5)
    som = [np.frombuffer(s, dtype="<i2") for s in c.pc.som[antes:]]
    mudos = [bool(np.all(s == 0)) for s in som]
    # o ruído das antenas não sobe: logo depois do gesto, só silêncio...
    assert mudos[:5] == [True] * 5, mudos
    # ...e o microfone volta depois
    assert mudos[-3:] == [False] * 3, mudos
    c.terminar()


def test_nivel_desconhecido_vale_parado(conversa):
    c = conversa(Robo("dancando"))
    c.pc.empurrar({"t": "estado", "fase": "falando"})
    time.sleep(0.1)
    c.terminar()
    assert c.robo.corpo == []


# ─── "Fala Garra": o trecho volta para o robô ────────────────────────────────
def test_fala_garra_devolve_o_trecho_que_o_robo_mandou(conversa):
    c = conversa()
    assert esperar(lambda: len(c.pc.som) >= 20)
    a, b = c.pc.carimbos[8], c.pc.carimbos[12]
    fim = c.terminar("handoff_garra", trecho_ms=[a, b])
    assert fim["reason"] == "handoff_garra"
    trecho = c.at.ao_garra["trecho"]
    esperado = [np.frombuffer(s, dtype="<i2") for s in c.pc.som[8:13]]
    guardado = (np.clip(trecho, -1, 1) * 32767).astype("<i2")
    for bloco in esperado:                           # cada bloco do intervalo está lá
        assert any(np.array_equal(bloco, guardado[i:i + bloco.size])
                   for i in range(0, guardado.size - bloco.size + 1, BLOCO))
    assert trecho.size <= (b - a + 2 * ao_vivo.MARGEM_TRECHO_MS + 200) * SR // 1000


def test_fala_garra_sem_trecho_passa_ao_garra_sem_som(conversa):
    c = conversa()
    c.terminar("handoff_garra")
    assert c.at.ao_garra == {"trecho": None}


@pytest.mark.parametrize("trecho", [[5, 1], "x", [0], [0, 60_000], [None, 3]])
def test_trecho_torto_nao_vira_som(conversa, trecho):
    c = conversa()
    c.terminar("handoff_garra", trecho_ms=trecho)
    assert c.at.ao_garra == {"trecho": None}


def test_outro_fim_nao_passa_nada_ao_garra(conversa):
    c = conversa()
    c.terminar("idle", trecho_ms=[0, 100])
    assert c.at.ao_garra is None


def test_na_troca_nao_toca_as_notas_de_saida(conversa):
    c = conversa()
    tons = len(c.robo.tocadas)
    c.terminar("handoff_garra")
    assert len(c.robo.tocadas) == tons              # quem fala agora é o bipe do Garra


# ─── a sessão reabriu no computador ──────────────────────────────────────────
def test_sessao_reaberta_no_pc_toca_de_novo_as_notas_e_volta_a_ouvir(conversa):
    c = conversa()
    tons = len(c.robo.tocadas)
    c.pc.empurrar({"t": "estado", "fase": "conectando"})
    assert esperar(lambda: c.robo.fases[-1] == ao_vivo.FASE_CONECTANDO)
    c.pc.empurrar({"t": "aberta", "retomar": SENHA})
    assert esperar(lambda: len(c.robo.tocadas) == tons + 1)
    assert esperar(lambda: c.robo.fases[-1] == ao_vivo.FASE_OUVINDO)
    c.terminar()
    assert c.robo.tipos().count("voice.live.started") == 1


# ─── o fim que chega junto com o fechamento ──────────────────────────────────
@pytest.mark.parametrize("motivo", ["idle", "farewell", "max"])
def test_fim_seguido_do_fechamento_vale_o_motivo_do_pc_e_nao_e_queda(conversa, motivo):
    """Medido no robô (25/11): a ponte manda o fim e fecha; o robô, que manda o
    microfone antes de ler, falhava no envio, tomava aquilo por queda e dizia
    "A conversa com a Hera caiu." depois de toda despedida."""
    c = conversa(Robo(continuo=True))
    assert esperar(lambda: len(c.pc.som) >= 3)
    c.pc.encerrar(motivo)
    c.thread.join(8)
    (fim,) = [k for t, k in c.robo.eventos if t == "voice.live.ended"]
    assert fim["reason"] == motivo
    assert c.robo.faladas == []                      # nada de aviso de queda
    assert c.pc.tipos().count("ola") == 1            # nem tentativa de volta


# ─── reconexão: o robô perdeu o computador ───────────────────────────────────
def test_queda_do_wifi_o_robo_volta_com_a_senha_e_segue(conversa):
    c = conversa()
    assert esperar(lambda: len(c.pc.som) >= 3)
    c.pc.cair()
    assert esperar(lambda: c.pc.tipos().count("ola") == 2), c.pc.tipos()
    volta = [m for m in c.pc.recebidos if m["t"] == "ola"][1]
    assert volta["retomar"] == SENHA and volta["pedido"] == ""
    assert c.pc.tipos().count("prova") == 2          # prova completa de novo
    antes = len(c.pc.som)
    assert esperar(lambda: len(c.pc.som) > antes + 3), "o microfone não voltou a subir"
    fim = c.terminar("idle")
    assert fim["reason"] == "idle"
    assert ao_vivo.FASE_CONECTANDO in c.robo.fases[1:]
    assert "voice.live.resumed" in c.robo.tipos()


def test_o_relogio_do_som_segue_depois_da_volta(conversa):
    c = conversa()
    assert esperar(lambda: len(c.pc.som) >= 3)
    ultimo = c.pc.carimbos[-1]
    c.pc.cair()
    assert esperar(lambda: c.pc.tipos().count("ola") == 2)
    antes = len(c.pc.carimbos)
    assert esperar(lambda: len(c.pc.carimbos) > antes)
    assert c.pc.carimbos[antes] > ultimo             # o "Fala Garra" depois da volta acha o trecho
    c.terminar()


def test_computador_que_nao_volta_encerra_com_aviso(conversa, monkeypatch):
    monkeypatch.setattr(ao_vivo, "RETOMAR_S", 1.0)
    c = conversa(pc=PcFalso(falar=False, aceita_volta=False))
    c.pc.cair()
    c.thread.join(8)
    assert not c.thread.is_alive()
    (fim,) = [k for t, k in c.robo.eventos if t == "voice.live.ended"]
    assert fim["reason"] == "pc_lost"
    assert c.robo.faladas == [ao_vivo.AVISO_QUEDA]


def test_senha_recusada_nao_insiste(conversa, monkeypatch):
    monkeypatch.setattr(ao_vivo, "RETOMAR_S", 5.0)
    pc = PcFalso(falar=False)
    c = conversa(pc=pc)
    pc.senha = "a-conversa-acabou-la"               # o computador já encerrou aquela conversa
    inicio = time.monotonic()
    pc.cair()
    c.thread.join(8)
    assert time.monotonic() - inicio < 3.0
    (fim,) = [k for t, k in c.robo.eventos if t == "voice.live.ended"]
    assert fim["reason"] == "pc_lost"


def test_so_uma_volta_por_conversa(conversa):
    c = conversa()
    c.pc.cair()
    assert esperar(lambda: c.pc.tipos().count("ola") == 2)
    assert esperar(lambda: "voice.live.resumed" in c.robo.tipos())
    c.pc.cair()
    c.thread.join(8)
    assert c.pc.tipos().count("ola") == 2
    (fim,) = [k for t, k in c.robo.eventos if t == "voice.live.ended"]
    assert fim["reason"] == "pc_lost"


def test_computador_antigo_sem_senha_nao_tem_volta(conversa):
    c = conversa(pc=PcFalso(falar=False, senha=None))
    c.pc.cair()
    c.thread.join(8)
    assert c.pc.tipos().count("ola") == 1
    (fim,) = [k for t, k in c.robo.eventos if t == "voice.live.ended"]
    assert fim["reason"] == "pc_lost"


def test_parar_durante_a_volta_encerra_na_hora(conversa, monkeypatch):
    monkeypatch.setattr(ao_vivo, "RETOMAR_S", 6.0)
    c = conversa(pc=PcFalso(falar=False, aceita_volta=False))
    c.pc.cair()
    assert esperar(lambda: c.robo.fases[-1] == ao_vivo.FASE_CONECTANDO)
    c.at.pedir_parada("panel")
    c.thread.join(3)
    assert not c.thread.is_alive()
