"""Ritmo da conversa: política de tempo, corte do aviso e turnos.

O núcleo é testável sem robô de propósito. A decisão de cortar ou esperar é
função pura sobre tempos, e o coordenador de áudio aceita um relógio injetado —
sem isso, provar "faltando exatamente 1,2 s" viraria um teste que dorme.
"""

from __future__ import annotations

import logging
import threading

import pytest

from garra_reachy_mini import conversa
from garra_reachy_mini.conversa import (
    AGENDADO,
    CANCELADO,
    CONCLUIDO,
    CORTADO,
    TOCANDO,
    CoordenadorAudio,
    Politica,
    Turno,
    decidir_corte,
    normalizar,
    perfil_atualizado,
)

SR = 16000
LOG = logging.getLogger("teste")


class MediaFalsa:
    """Alto-falante de mentira que registra a ordem exata dos comandos."""

    def __init__(self, clear_ok: bool = True, tem_clear: bool = True) -> None:
        self.eventos: list[tuple[str, int]] = []
        self._clear_ok = clear_ok
        if tem_clear:
            self.audio = self   # o SDK expõe clear_player em `media.audio`

    def push_audio_sample(self, onda) -> None:
        self.eventos.append(("push", len(onda)))

    def clear_player(self) -> None:
        if not self._clear_ok:
            raise RuntimeError("gstreamer recusou o flush")
        self.eventos.append(("clear", 0))


class Relogio:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def avancar(self, segundos: float) -> None:
        self.t += segundos


def onda(ms: int) -> list[int]:
    return [0] * int(SR * ms / 1000)


def montar(relogio=None, **kw) -> tuple[CoordenadorAudio, MediaFalsa, Turno]:
    media = MediaFalsa(**kw)
    rel = relogio or Relogio()
    coord = CoordenadorAudio(media, SR, LOG, relogio=rel)
    turno = Turno(id="t1", correlacao="c1", inicio=rel(), politica=Politica.de({}))
    coord.abrir(turno)
    return coord, media, turno


# ─── política de tempo ───────────────────────────────────────────────────────
def test_prazos_sao_absolutos_a_partir_do_inicio():
    """O progresso sai em t0+10s, não 10s DEPOIS do aviso.

    Encadear as esperas daria t0+4+10 = 14 s para um aviso configurado em 10.
    """
    p = Politica.de({})
    t0 = 500.0
    assert p.prazo_ack(t0) == pytest.approx(t0 + 4.0)
    assert p.prazo_progresso(t0) == pytest.approx(t0 + 10.0)
    assert p.prazo_progresso(t0) - p.prazo_ack(t0) == pytest.approx(6.0)


def test_modo_escolhe_o_perfil():
    assert Politica.de({"mode": "fast"}).ack_atraso_ms == 4000
    assert Politica.de({"mode": "informative"}).ack_atraso_ms == 1500


def test_modo_desconhecido_cai_no_padrao():
    assert Politica.de({"mode": "turbo"}).modo == "fast"
    assert normalizar({"mode": ""})["mode"] == "fast"
    assert normalizar(None)["mode"] == "fast"


def test_valores_absurdos_sao_limitados_em_vez_de_quebrar():
    c = normalizar({"progress_update_delay_ms": 10**9,
                    "acknowledgement_cut_threshold_ms": -5,
                    "max_progress_messages": "muitas"})
    assert c["progress_update_delay_ms"] == 120000
    assert c["acknowledgement_cut_threshold_ms"] == 0
    assert c["max_progress_messages"] == 1   # inválido → mantém o anterior


# ─── perfis: trocar de modo não pode apagar ajuste do usuário ────────────────
def test_trocar_de_modo_preserva_o_ajuste_de_cada_perfil():
    c = normalizar({})
    c = perfil_atualizado(c, {"acknowledgement_delay_ms": 2500})     # ajusta fast
    assert c["profiles"]["fast"]["acknowledgement_delay_ms"] == 2500

    c = perfil_atualizado(c, {"mode": "informative"})
    assert Politica.de(c).ack_atraso_ms == 1500, "informativo tem o tempo dele"
    c = perfil_atualizado(c, {"acknowledgement_delay_ms": 800})      # ajusta informative
    assert c["profiles"]["informative"]["acknowledgement_delay_ms"] == 800

    c = perfil_atualizado(c, {"mode": "fast"})
    assert Politica.de(c).ack_atraso_ms == 2500, "o ajuste do fast voltou"
    assert c["profiles"]["informative"]["acknowledgement_delay_ms"] == 800


def test_ida_e_volta_repetida_entre_modos_nao_perde_nada():
    c = perfil_atualizado(normalizar({}), {"acknowledgement_delay_ms": 3300})
    for _ in range(5):
        c = perfil_atualizado(c, {"mode": "informative"})
        c = perfil_atualizado(c, {"mode": "fast"})
    assert c["profiles"]["fast"]["acknowledgement_delay_ms"] == 3300
    assert c["profiles"]["informative"]["acknowledgement_delay_ms"] == 1500


def test_modo_invalido_no_patch_e_recusado():
    with pytest.raises(ValueError):
        perfil_atualizado(normalizar({}), {"mode": "turbo"})


# ─── os 12 casos do corte ────────────────────────────────────────────────────
def test_1_resposta_pronta_antes_do_aviso_comecar():
    coord, media, turno = montar()
    assert turno.ack_estado == AGENDADO
    assert coord.resolver_ack(turno) == CANCELADO
    assert [e for e in media.eventos if e[0] == "push"] == [], "não podia ter tocado"


def test_2_faltando_menos_de_1200ms_deixa_terminar():
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(2000))
    rel.avancar(1.2)                       # restam 800 ms
    assert coord.resolver_ack(turno) == CONCLUIDO
    assert ("clear", 0) not in media.eventos


def test_3_faltando_exatamente_1200ms_deixa_terminar():
    """O limite é inclusivo: `<=` e não `<`. Empate não corta."""
    assert decidir_corte(1200.0, 1200) == "aguardar"
    assert decidir_corte(1200.1, 1200) == "cortar"


def test_4_faltando_mais_de_1200ms_corta():
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(3000))
    rel.avancar(0.5)                       # restam 2500 ms
    assert coord.resolver_ack(turno) == CORTADO
    assert ("clear", 0) in media.eventos


def test_5_resposta_logo_apos_o_inicio_da_frase_corta():
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(2500))
    assert coord.resolver_ack(turno) == CORTADO   # decorrido zero


def test_6_barge_in_do_usuario_corta_sem_respeitar_o_limite():
    """Quando é o usuário que interrompe, esperar seria o oposto do pedido."""
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(2000))
    rel.avancar(1.5)                       # restam 500 ms → o automático esperaria
    coord.cancelar(turno, "barge-in")
    assert ("clear", 0) in media.eventos
    assert turno.ack_estado == CANCELADO


def test_7_pergunta_nova_invalida_o_turno_anterior():
    coord, media, anterior = montar()
    novo = Turno(id="t2", correlacao="c2", inicio=0.0, politica=Politica.de({}))
    anterior.substituido_por = novo.id
    coord.abrir(novo)
    assert coord.tocar_final(anterior, [onda(500)]) is False, \
        "resposta de turno substituído não pode falar"
    assert coord.tocar_final(novo, [onda(500)]) is True


def test_8_estop_durante_a_reproducao_corta_na_hora():
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(4000))
    coord.cancelar(turno, "e-stop")
    assert ("clear", 0) in media.eventos
    assert not turno.vivo


def test_9_clear_player_falhando_espera_em_vez_de_sobrepor():
    rel = Relogio()
    coord, media, turno = montar(rel, clear_ok=False)
    coord.tocar_ack(turno, onda(1500))
    # Sem avançar o relógio restam 1500 ms → decidiria cortar, mas o flush falha.
    assert coord.resolver_ack(turno) == CONCLUIDO
    assert turno.corte_falhou is True


def test_9b_sdk_sem_clear_player_tambem_espera():
    coord, media, turno = montar(tem_clear=False)
    coord.tocar_ack(turno, onda(1500))
    assert coord.resolver_ack(turno) == CONCLUIDO
    assert turno.corte_falhou is True


def test_10_nunca_ha_dois_audios_sobrepostos():
    """A resposta só é empurrada depois do aviso resolver."""
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(3000))
    coord.resolver_ack(turno)              # corta
    coord.tocar_final(turno, [onda(1000)])
    tipos = [e[0] for e in media.eventos]
    assert tipos == ["push", "clear", "push"], f"ordem errada: {tipos}"


def test_11_aviso_de_turno_velho_nunca_toca():
    coord, media, velho = montar()
    novo = Turno(id="t2", correlacao="c2", inicio=0.0, politica=Politica.de({}))
    coord.abrir(novo)                      # o turno corrente mudou
    assert coord.tocar_ack(velho, onda(1000)) is False
    assert velho.ack_estado == CANCELADO
    assert media.eventos == []


def test_12_resposta_comeca_logo_apos_o_flush():
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(5000))
    assert coord.resolver_ack(turno) == CORTADO
    assert coord.tocar_final(turno, [onda(800)]) is True
    assert media.eventos[-1] == ("push", len(onda(800)))


# ─── extras pedidos na revisão ───────────────────────────────────────────────
def test_callback_atrasado_do_aviso_nao_mexe_no_estado_da_resposta():
    """Um `tocar_ack` que chega tarde, depois do flush, não pode ressuscitar."""
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(3000))
    coord.resolver_ack(turno)              # → flushed
    coord.tocar_final(turno, [onda(500)])
    antes = list(media.eventos)

    turno.cancelado = True                 # o turno já se foi
    assert coord.tocar_ack(turno, onda(3000)) is False
    assert media.eventos == antes, "callback atrasado empurrou áudio"
    assert turno.ack_estado == CANCELADO


def test_duracao_desconhecida_espera_o_teto_em_vez_de_sobrepor():
    coord, media, turno = montar()
    turno.ack_estado = TOCANDO             # tocando, mas sem duração registrada
    assert turno.restante_ack_ms() is None
    assert decidir_corte(None, 1200) == "aguardar"


def test_metricas_registram_a_decisao_sem_conteudo():
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(3000))
    rel.avancar(0.4)
    coord.resolver_ack(turno)
    assert turno.ack_decisao == CORTADO
    assert turno.metricas["ack_restante_ms"] == pytest.approx(2600, abs=1)
    texto = " ".join(map(str, turno.metricas.values()))
    assert "pensar" not in texto and "instante" not in texto


def test_coordenador_nao_segura_o_lock_durante_a_reproducao():
    """Se o lock ficasse preso pelo áudio, cancelar travaria — deadlock."""
    rel = Relogio()
    coord, media, turno = montar(rel)
    coord.tocar_ack(turno, onda(3000))

    pronto = threading.Event()

    def cancelar():
        coord.cancelar(turno, "barge-in")
        pronto.set()

    threading.Thread(target=cancelar, daemon=True).start()
    assert pronto.wait(2.0), "cancelar ficou preso no lock do áudio"


# ─── endpoint no robô: revisão, 409 e gravação ───────────────────────────────
@pytest.fixture()
def api_conversa(tmp_path, monkeypatch):
    """Só as rotas do robô, com o armazenamento num diretório temporário."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from garra_reachy_mini.web.api import ContextoWeb, montar
    from garra_reachy_mini.web.camera import FrameHub
    from garra_reachy_mini.web.seguranca import resolver_politica
    from garra_reachy_mini.robo.acoes import ControladorRobo
    from garra_reachy_mini.robo.backends import BackendSimulado

    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    backend = BackendSimulado()
    ctrl = ControladorRobo(backend, semente=7)
    ctrl.iniciar()
    app = FastAPI()
    hub = FrameHub(backend, fps_ativo=4.0)
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def test_get_devolve_padrao_e_politica_efetiva(api_conversa):
    d = api_conversa.get("/api/robot/conversation").json()
    assert d["conversation"]["mode"] == "fast"
    assert d["conversation"]["revision"] == 0
    assert d["effective"]["ack_atraso_ms"] == 4000


def test_put_grava_incrementa_revisao_e_devolve_o_confirmado(api_conversa):
    d = api_conversa.put("/api/robot/conversation",
                         json={"mode": "informative", "revision": 0,
                               "updated_by": "garra-dashboard"}).json()
    assert d["conversation"]["mode"] == "informative"
    assert d["conversation"]["revision"] == 1
    assert d["effective"]["ack_atraso_ms"] == 1500
    assert d["updated_by"] == "garra-dashboard"
    # E persistiu de verdade.
    assert api_conversa.get("/api/robot/conversation").json()["conversation"]["mode"] \
        == "informative"


def test_revisao_antiga_da_409_com_o_estado_atual(api_conversa):
    api_conversa.put("/api/robot/conversation", json={"mode": "informative", "revision": 0})
    r = api_conversa.put("/api/robot/conversation", json={"mode": "fast", "revision": 0})
    assert r.status_code == 409, "escrita cega sobrescreveria o outro painel"
    assert r.json()["conversation"]["mode"] == "informative", "o corpo traz o atual"
    assert r.json()["conversation"]["revision"] == 1


def test_sem_revisao_grava_sem_conflito(api_conversa):
    """Cliente que não acompanha revisão (curl de diagnóstico) ainda funciona."""
    r = api_conversa.put("/api/robot/conversation", json={"mode": "informative"})
    assert r.status_code == 200


def test_modo_invalido_vira_400(api_conversa):
    assert api_conversa.put("/api/robot/conversation",
                            json={"mode": "turbo"}).status_code == 400


def test_false_sobrevive_a_gravacao(api_conversa):
    """`if not valor` apagaria a chave e o padrão `True` voltaria em silêncio."""
    d = api_conversa.put("/api/robot/conversation",
                         json={"spoken_progress_updates": False}).json()
    assert d["conversation"]["spoken_progress_updates"] is False
    assert api_conversa.get("/api/robot/conversation").json()[
        "conversation"]["spoken_progress_updates"] is False
