"""As peças do modo ao vivo no robô, sem o laço de voz.

  credenciais  de onde sai o endereço do canal (o da ponte, que o robô já tem) e
               o segredo do dispositivo (gravado pelo computador, com assinatura)
  Canal        o cliente do WebSocket: aperto de mão com prova nos dois sentidos —
               o robô só manda som a quem provou ser o computador pareado
  Saida        a fila de reprodução: espera uma folga antes de tocar, toca o resto
               assim que chega, e corta na hora
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from garra_reachy_mini import ao_vivo
from hera_pc_falso import DISPOSITIVO, FALA_DA_HERA, HOST, SEGREDO, PcFalso


# ─── credenciais ─────────────────────────────────────────────────────────────
def test_credenciais_saem_da_ponte_e_do_pareamento():
    c = ao_vivo.credenciais({"hera_token": SEGREDO, "hera_device": DISPOSITIVO}, "http://10.0.0.24:8126")
    assert c == {"url": "ws://10.0.0.24:8126/api/hera/ao-vivo", "host": "10.0.0.24:8126",
                 "segredo": SEGREDO, "dispositivo": DISPOSITIVO}
    https = ao_vivo.credenciais({"hera_token": SEGREDO, "hera_device": DISPOSITIVO}, "https://pc.casa:8126/")
    assert https["url"] == "wss://pc.casa:8126/api/hera/ao-vivo"


@pytest.mark.parametrize("salva,ponte", [
    ({}, "http://10.0.0.24:8126"),
    ({"hera_token": SEGREDO}, "http://10.0.0.24:8126"),
    ({"hera_token": "", "hera_device": DISPOSITIVO}, "http://10.0.0.24:8126"),
    ({"hera_token": SEGREDO, "hera_device": DISPOSITIVO}, "http://127.0.0.1:3888"),   # sem pareamento
    ({"hera_token": SEGREDO, "hera_device": DISPOSITIVO}, "http://localhost:8126"),
    ({"hera_token": SEGREDO, "hera_device": DISPOSITIVO}, "ftp://10.0.0.24:8126"),
    ({"hera_token": SEGREDO, "hera_device": DISPOSITIVO}, ""),
])
def test_sem_pareamento_nao_ha_credencial(salva, ponte):
    assert ao_vivo.credenciais(salva, ponte) is None


# ─── o canal ─────────────────────────────────────────────────────────────────
def canal(pc: PcFalso, segredo: str = SEGREDO) -> ao_vivo.Canal:
    return ao_vivo.Canal(f"ws://{HOST}/api/hera/ao-vivo", HOST, segredo, DISPOSITIVO, conectar=pc)


def test_aperto_de_mao_e_o_que_o_robo_manda():
    pc = PcFalso()
    c = canal(pc)
    c.abrir("leading", "que horas são?")
    ola, prova = pc.recebidos[:2]
    assert ola["t"] == "ola" and ola["v"] == 1 and ola["host"] == HOST and ola["dispositivo"] == DISPOSITIVO
    assert (ola["como"], ola["pedido"]) == ("leading", "que horas são?")
    assert 16 <= len(ola["nonce"]) <= 128
    assert prova["t"] == "prova" and len(prova["prova"]) == 64
    assert SEGREDO not in str(pc.recebidos)
    c.fechar()
    assert pc.fechado


def test_prova_do_pc_errada_o_robo_desiste_sem_provar_nada():
    pc = PcFalso(prova_errada=True)
    with pytest.raises(ao_vivo.Recusado) as erro:
        canal(pc).abrir("alone", "")
    assert erro.value.motivo == "bad_pc_proof"
    assert pc.tipos() == ["ola"] and pc.fechado


@pytest.mark.parametrize("motivo", ["disabled", "not_paired", "daily_limit", "busy", "wrong_host"])
def test_pc_recusa_com_motivo(motivo):
    pc = PcFalso(recusa=motivo)
    with pytest.raises(ao_vivo.Recusado) as erro:
        canal(pc).abrir("alone", "")
    assert erro.value.motivo == motivo and pc.fechado


def test_pc_recusa_a_prova_do_robo():
    with pytest.raises(ao_vivo.Recusado) as erro:
        canal(PcFalso(), segredo="segredo-errado-" + "x" * 30).abrir("alone", "")
    assert erro.value.motivo in ("bad_pc_proof", "bad_proof")


def test_pc_que_nao_responde():
    class Mudo(PcFalso):
        def send(self, mensagem):
            pass

    with pytest.raises(ao_vivo.Recusado) as erro:
        ao_vivo.Canal(f"ws://{HOST}/x", HOST, SEGREDO, DISPOSITIVO, conectar=Mudo(), prazo_s=0.3).abrir("alone", "")
    assert erro.value.motivo == "unreachable"


def test_conexao_recusada_vira_unreachable():
    def recusa(url, **kw):
        raise ConnectionRefusedError

    with pytest.raises(ao_vivo.Recusado) as erro:
        ao_vivo.Canal(f"ws://{HOST}/x", HOST, SEGREDO, DISPOSITIVO, conectar=recusa).abrir("alone", "")
    assert erro.value.motivo == "unreachable"


def test_receber_separa_som_de_mensagem_e_nao_bloqueia():
    pc = PcFalso(pings=False)
    c = canal(pc)
    c.abrir("alone", "")
    inicio = time.monotonic()
    itens = c.receber()
    assert time.monotonic() - inicio < 0.2
    tipos = [t for t, _ in itens]
    assert any(t == "json" and i["t"] == "aberta" for t, i in itens) and tipos.count("som") == 10
    assert b"".join(i for t, i in itens if t == "som") == FALA_DA_HERA
    assert c.receber() == []


def test_receber_depois_de_fechada_avisa_a_queda():
    pc = PcFalso()
    c = canal(pc)
    c.abrir("alone", "")
    pc.close()
    c.receber()
    with pytest.raises(ao_vivo.CanalCaiu):
        c.receber()


def test_som_sai_em_quadros_com_sequencia():
    pc = PcFalso()
    c = canal(pc)
    c.abrir("alone", "")
    c.enviar_som(b"\x01\x00" * 1024)
    c.enviar_som(b"\x02\x00" * 1024)
    assert pc.som == [b"\x01\x00" * 1024, b"\x02\x00" * 1024]


# ─── conversões de som ───────────────────────────────────────────────────────
def test_pcm_de_ida_e_volta():
    onda = np.array([0.0, 0.5, -0.5, 1.2, -1.2], dtype=np.float32)
    pcm = ao_vivo.para_pcm16(onda, 16000)
    assert np.frombuffer(pcm, "<i2").tolist() == [0, 16383, -16383, 32767, -32767]
    de_volta = ao_vivo.de_pcm16(pcm, 16000)
    assert de_volta.dtype == np.float32 and np.allclose(de_volta[:3], [0.0, 0.5, -0.5], atol=1e-3)


def test_outra_taxa_e_reamostrada():
    onda = np.zeros(4800, dtype=np.float32)                    # 0,1 s a 48 kHz
    assert len(ao_vivo.para_pcm16(onda, 48000)) == 1600 * 2   # 0,1 s a 16 kHz
    assert ao_vivo.de_pcm16(b"\x00\x00" * 1600, 48000).size == 4800


# ─── a fila de reprodução ────────────────────────────────────────────────────
class Relogio:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def saida():
    tocado: list[np.ndarray] = []
    rel = Relogio()
    s = ao_vivo.Saida(lambda onda: tocado.append(onda) or True, 16000, folga_s=0.12, relogio=rel)
    return s, tocado, rel


def pcm(segundos: float) -> bytes:
    return b"\x10\x00" * int(16000 * segundos)


def test_espera_a_folga_antes_de_comecar():
    s, tocado, rel = saida()
    s.por(pcm(0.06))
    s.avancar()
    assert tocado == []                                   # 60 ms < 120 ms de folga
    s.por(pcm(0.08))
    s.avancar()
    assert sum(o.size for o in tocado) == int(16000 * 0.14) and s.tocando


def test_pouco_som_que_para_de_chegar_toca_assim_mesmo():
    s, tocado, rel = saida()
    s.por(pcm(0.04))
    rel.t += 0.1                                          # nada mais chegou
    s.avancar()
    assert sum(o.size for o in tocado) == 640


def test_tocando_o_resto_vai_direto():
    s, tocado, rel = saida()
    s.por(pcm(0.2))
    s.avancar()
    s.por(pcm(0.02))
    s.avancar()
    assert len(tocado) == 2 and tocado[1].size == 320


def test_quando_esvazia_volta_a_esperar_a_folga():
    s, tocado, rel = saida()
    s.por(pcm(0.2))
    s.avancar()
    rel.t += 0.5                                          # acabou de tocar
    assert not s.tocando
    s.por(pcm(0.02))
    s.avancar()
    assert len(tocado) == 1


def test_cortar_descarta_o_que_esperava():
    s, tocado, rel = saida()
    s.por(pcm(0.06))
    s.cortar()
    rel.t += 0.5
    s.avancar()
    assert tocado == [] and not s.tocando


def test_sinais_de_entrada_e_saida_sao_diferentes_e_curtos():
    entrada, saida_ = ao_vivo.sinal(16000, subindo=True), ao_vivo.sinal(16000, subindo=False)
    assert entrada.dtype == np.float32 and 0.2 < entrada.size / 16000 < 0.6
    assert not np.allclose(entrada, saida_)
    assert float(np.max(np.abs(entrada))) <= 0.3


# ─── o corpo parado, o corte do player e a trava ─────────────────────────────
def test_comportamento_ao_vivo_nao_mexe_e_para_os_micro_movimentos():
    from garra_reachy_mini.robo import comportamento as c

    class Ctrl:
        def __init__(self):
            self.pedidos, self.balanco = [], []
            self._aleatorio = __import__("random").Random(1)
            self.eventos = type("E", (), {"publicar": lambda *a, **k: None})()

        def submeter(self, *a, **k):
            self.pedidos.append((a, k))

        def wobbling_pedir(self, ligado):
            self.balanco.append(ligado)

    ctrl = Ctrl()
    g = c.Comportamento.__new__(c.Comportamento)
    import threading as _t
    g.ctrl, g._lock, g._estado, g.wobbling, g.ativo = ctrl, _t.Lock(), c.OUVINDO, True, True
    g._acordar = _t.Event()
    g.ao_vivo()
    assert g.estado == c.AO_VIVO and ctrl.pedidos == [] and ctrl.balanco == [False]
    for fase in ("live_connecting", "live_listening", "live_thinking", "live_speaking"):
        assert fase in c.FASES


def _comportamento_de_mentira(wobbling=True, ativo=True):
    from garra_reachy_mini.robo import comportamento as c

    class Ctrl:
        def __init__(self):
            self.pedidos, self.balanco = [], []
            self._aleatorio = __import__("random").Random(1)
            self.eventos = type("E", (), {"publicar": lambda *a, **k: None})()

        def submeter(self, nome, params, **k):
            self.pedidos.append((nome, dict(params)))

        def wobbling_pedir(self, ligado):
            self.balanco.append(ligado)

    import threading as _t
    ctrl = Ctrl()
    g = c.Comportamento(ctrl, ativo=ativo, wobbling=wobbling)
    g.ao_vivo()
    ctrl.balanco.clear()
    return g, ctrl


def test_corpo_ao_vivo_balanca_ao_falar_e_aquieta():
    g, ctrl = _comportamento_de_mentira()
    g.ao_vivo_corpo("speaking")
    g.ao_vivo_corpo("quiet")
    assert ctrl.balanco == [True, False] and ctrl.pedidos == []


def test_corpo_ao_vivo_antenas_para_tras_ao_pensar_e_de_volta_quando_ela_fala():
    g, ctrl = _comportamento_de_mentira()
    g.ao_vivo_corpo("thinking")
    g.ao_vivo_corpo("speaking")      # a volta das antenas fica abafada pela fala dela
    g.ao_vivo_corpo("speaking")      # já no lugar: não mexe de novo
    assert [n for n, _ in ctrl.pedidos] == ["move_antennas", "move_antennas"]
    assert ctrl.pedidos[0][1]["left"] < 0 and ctrl.pedidos[1][1]["preset"] == "neutral"


def test_corpo_ao_vivo_obedece_os_interruptores_do_dono():
    g, ctrl = _comportamento_de_mentira(wobbling=False, ativo=False)
    for acao in ("thinking", "speaking", "quiet", "outra coisa"):
        g.ao_vivo_corpo(acao)
    assert ctrl.pedidos == [] and True not in ctrl.balanco


def test_nivel_de_movimento_da_conversa_ao_vivo():
    from garra_reachy_mini import conversa
    assert conversa.normalizar({})["live_motion"] == "still"
    assert conversa.normalizar({"live_motion": "speaking"})["live_motion"] == "speaking"
    assert conversa.normalizar({"live_motion": "dancando"})["live_motion"] == "still"
    conf = conversa.perfil_atualizado({}, {"live_motion": "expressive"})
    assert conf["live_motion"] == "expressive"
    assert conversa.perfil_atualizado(conf, {"live_motion": 3})["live_motion"] == "expressive"
    assert ao_vivo.NIVEIS_DE_MOVIMENTO == conversa.MOVIMENTOS_AO_VIVO


def test_coordenador_corta_sem_turno():
    import logging
    from garra_reachy_mini.conversa import CoordenadorAudio

    class Media:
        def __init__(self):
            self.limpou = 0
            self.audio = self

        def clear_player(self):
            self.limpou += 1

        def push_audio_sample(self, onda):
            pass

    m = Media()
    assert CoordenadorAudio(m, 16000, logging.getLogger("t")).cortar_tudo() is True and m.limpou == 1


def test_o_segredo_da_hera_so_muda_com_a_assinatura_do_pc():
    from garra_reachy_mini.web import trava
    salva = {"trava_pc": True, "voz_token": "x", "hera_token": "s", "hera_device": "garra-sala"}
    assert trava.recusa(salva, {"hera_token": "do-vizinho"}, None, trava.Desafios(), "10.0.0.50", "***") == ["hera_token"]
    assert trava.recusa(salva, {"hera_device": "outro"}, None, trava.Desafios(), "10.0.0.50", "***") == ["hera_device"]
    assert trava.recusa(salva, {"hera_token": "***"}, None, trava.Desafios(), "10.0.0.50", "***") == []
