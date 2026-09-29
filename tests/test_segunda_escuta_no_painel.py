"""A segunda escuta, o diagnóstico da frase e o detector de fala, no painel.

Em 29/09/2026 o robô não respondia e a tela não dizia por quê. Havia três
motivos e nenhum aparecia: o alto-falante desligado (uma caixa desmarcada entre
quinze), a frase saindo errada do STT (o evento de recusa não diz o que foi
ouvido, de propósito) e o detector de fala preso no ruído.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini.acompanhamento import Acompanhamento
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub
from garra_reachy_mini.web.seguranca import resolver_politica

PAINEL = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini" / "static"
HTML = (PAINEL / "reachy.html").read_text(encoding="utf-8")
JS = (PAINEL / "reachy.js").read_text(encoding="utf-8")
I18N = (PAINEL / "i18n.js").read_text(encoding="utf-8")
EN = I18N[I18N.index("  en: {"):I18N.index("  pt: {")]
PT = I18N[I18N.index("  pt: {"):]

CHAVES = ("conversa.wake_conferir", "conversa.wake_conferir_dica",
          "conversa.wake_diagnostico", "conversa.wake_diagnostico_dica",
          "conversa.mudo_aviso", "conversa.perto_titulo", "conversa.perto_vazio",
          "conversa.perto_confirmed", "conversa.perto_rejected", "conversa.perto_failed",
          "conversa.perto_not_tried", "lista.microfone", "lista.microfone_val",
          "lista.microfone_fixo", "lista.microfone_parado")


def valor(idioma: str, chave: str) -> str:
    m = re.search(rf"'{re.escape(chave)}':\s*(['\"])(.*?)(?<!\\)\1,?\s*\n", idioma)
    assert m, f"{chave} não tem tradução"
    return m.group(2)


def funcao(nome: str) -> str:
    inicio = JS.index(f"function {nome}(")
    return JS[inicio:JS.index("\n}\n", inicio)]


CARTAO = HTML[HTML.index('id="conversa-bloco"'):HTML.index('id="btn-conversa-salvar"')]


# ─── o robô mudo tem de gritar ───────────────────────────────────────────────
def test_o_aviso_de_robo_sem_voz_abre_o_cartao() -> None:
    assert 'id="conversa-mudo-aviso"' in CARTAO
    assert CARTAO.index('id="conversa-mudo-aviso"') < CARTAO.index('id="conversa-saida"')
    aviso = CARTAO[CARTAO.index('id="conversa-mudo-aviso"') - 80:][:260]
    assert 'class="aviso-caixa"' in aviso and "hidden" in aviso
    assert 'data-i18n="conversa.mudo_aviso"' in aviso


def test_o_aviso_aparece_quando_a_saida_esta_desligada() -> None:
    assert ("$('conversa-mudo-aviso').hidden = c.speech_output_enabled !== false"
            in funcao("pintarConversa"))


def test_o_aviso_diz_o_que_fazer() -> None:
    pt, en = valor(PT, "conversa.mudo_aviso"), valor(EN, "conversa.mudo_aviso")
    assert valor(PT, "conversa.saida") in pt, "tem de citar o nome da caixa a marcar"
    assert valor(EN, "conversa.saida") in en
    assert "chat" in pt and "chat" in en


# ─── os dois interruptores ───────────────────────────────────────────────────
@pytest.mark.parametrize("ident", ["conversa-wake-conferir", "conversa-wake-diagnostico"])
def test_os_interruptores_ficam_sob_a_frase_de_ativacao(ident) -> None:
    assert f'id="{ident}"' in CARTAO
    assert CARTAO.index('id="conversa-wake"') < CARTAO.index(f'id="{ident}"')
    linha = CARTAO[:CARTAO.index(f'id="{ident}"')].rsplit("<label", 1)[1]
    assert 'data-conversa-wake-sub="1"' in linha


def test_o_painel_grava_os_dois() -> None:
    corpo = JS[JS.index("$('btn-conversa-salvar').addEventListener"):]
    corpo = corpo[:corpo.index("}));")]
    assert "wake_second_listen_enabled: $('conversa-wake-conferir').checked" in corpo
    assert "wake_diagnostics_enabled: $('conversa-wake-diagnostico').checked" in corpo


def test_cada_um_aparece_com_o_padrao_do_servidor() -> None:
    """A segunda escuta nasce LIGADA (`!== false`); o diagnóstico, desligado
    (`=== true`). Config antiga, sem as chaves, tem de aparecer assim."""
    pinta = funcao("pintarConversa")
    assert "$('conversa-wake-conferir').checked = c.wake_second_listen_enabled !== false" in pinta
    assert "$('conversa-wake-diagnostico').checked = c.wake_diagnostics_enabled === true" in pinta


def test_a_dica_do_diagnostico_diz_o_que_aparece_e_o_que_nao() -> None:
    pt = valor(PT, "conversa.wake_diagnostico_dica").lower()
    en = valor(EN, "conversa.wake_diagnostico_dica").lower()
    assert "trecho" in pt and "resto" in pt
    assert "snippet" in en and "rest" in en


def test_a_dica_da_segunda_escuta_diz_para_onde_o_audio_vai() -> None:
    pt = valor(PT, "conversa.wake_conferir_dica").lower()
    en = valor(EN, "conversa.wake_conferir_dica").lower()
    assert "servidor de voz" in pt and "voice server" in en


# ─── o que foi ouvido no lugar da frase ──────────────────────────────────────
def test_a_lista_do_que_foi_ouvido_fica_no_cartao() -> None:
    assert 'id="conversa-wake-perto"' in CARTAO


def test_o_trecho_ouvido_e_escapado_antes_de_virar_html() -> None:
    """Veio de uma transcrição: é texto de fora."""
    pinta = funcao("pintarPerto")
    # A chave de tradução também é montada com `${}`, mas não vira HTML: o que
    # `t()` devolve é que vira, e passa por `esc` como o resto.
    html = re.sub(r"t\(`[^`]*`\)", "t()", pinta)
    internas = re.findall(r"\$\{([^{}`]*)\}", html)
    assert len(internas) >= 4
    for interpolacao in internas:
        assert interpolacao.strip().startswith("esc("), (
            f"`{interpolacao}` entra no HTML sem escapar")
    assert "heard" in pinta


def test_o_evento_de_quase_frase_entra_na_lista() -> None:
    trata = funcao("tratarEvento")
    caso = trata[trata.index("case 'voice.wake.near_miss'"):]
    assert "anotarPerto(e)" in caso[:caso.index("break")]


def test_a_lista_e_curta_e_so_vive_na_pagina() -> None:
    anota = funcao("anotarPerto")
    assert "PERTO_MAX" in anota
    assert re.search(r"const PERTO_MAX = [1-9];", JS)
    for proibido in ("localStorage", "sessionStorage", "indexedDB"):
        assert proibido not in anota and proibido not in funcao("pintarPerto")


def test_cada_resultado_da_conferencia_tem_nome_na_tela() -> None:
    assert "t(`conversa.perto_${" in funcao("pintarPerto")


# ─── o detector de fala ──────────────────────────────────────────────────────
def test_o_status_mostra_o_que_o_microfone_esta_medindo() -> None:
    assert "microfone(s.voice?.detector)" in funcao("pintarStatus")
    linha = funcao("microfone")
    assert "t('lista.microfone')" in linha
    assert "d.floor" in linha and "d.open_threshold" in linha
    # Sem laço de voz não há o que medir, e isso tem de aparecer como problema.
    assert "if (!d) return [t('lista.microfone'), t('lista.microfone_parado'), false]" in linha


def test_tudo_tem_traducao_nos_dois_idiomas() -> None:
    for chave in CHAVES:
        assert valor(EN, chave) and valor(PT, chave), chave


def test_toda_chave_que_o_script_pede_existe_nos_dois_idiomas() -> None:
    usadas = set(re.findall(r"\bt\('([a-z_]+\.[a-z0-9_.]+)'", JS))
    faltam = sorted(k for k in usadas if f"'{k}'" not in EN or f"'{k}'" not in PT)
    assert faltam == []


# ─── a API ───────────────────────────────────────────────────────────────────
MEDIDA = {"floor": 0.007, "open_threshold": 0.028, "close_threshold": 0.0175,
          "fixed_threshold": False, "in_speech": False, "segments": 3,
          "dropped_short": 1, "cut_by_max": 0, "block_ms": 20.0}


@pytest.fixture()
def montar_painel(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    abertos = []

    def _montar(**extra):
        ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
        ctrl.iniciar()
        hub = FrameHub(backend, fps_ativo=4.0)
        app = FastAPI()
        montar(app, ContextoWeb(
            controlador=ctrl, hub=hub, eventos=ctrl.eventos,
            acompanhamento=Acompanhamento(ctrl.eventos),
            politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False),
            **extra))
        cliente = TestClient(app)
        cliente.__enter__()
        abertos.append((cliente, hub, ctrl))
        return cliente

    yield _montar
    for cliente, hub, ctrl in abertos:
        cliente.__exit__(None, None, None)
        hub.encerrar()
        ctrl.encerrar(timeout=2)


def test_sem_laco_de_voz_o_status_diz_que_nao_ha_detector(montar_painel) -> None:
    voz = montar_painel().get("/api/robot/status").json()["voice"]
    assert "detector" in voz and voz["detector"] is None


def test_com_laco_de_voz_o_status_traz_a_medida(montar_painel) -> None:
    voz = montar_painel(detector=lambda: dict(MEDIDA)).get("/api/robot/status").json()["voice"]
    assert voz["detector"] == MEDIDA


def test_detector_que_falha_nao_derruba_o_status(montar_painel) -> None:
    def quebrado():
        raise RuntimeError("laço de voz reiniciando")
    r = montar_painel(detector=quebrado).get("/api/robot/status")
    assert r.status_code == 200 and r.json()["voice"]["detector"] is None


def test_as_duas_chaves_novas_tem_padrao_e_sao_gravaveis(montar_painel) -> None:
    painel = montar_painel()
    c = painel.get("/api/robot/conversation").json()["conversation"]
    assert c["wake_second_listen_enabled"] is True
    assert c["wake_diagnostics_enabled"] is False
    r = painel.put("/api/robot/conversation", json={
        "wake_second_listen_enabled": False, "wake_diagnostics_enabled": True})
    assert r.status_code == 200
    c = painel.get("/api/robot/conversation").json()["conversation"]
    assert (c["wake_second_listen_enabled"], c["wake_diagnostics_enabled"]) == (False, True)


def test_o_app_entrega_o_detector_ao_painel() -> None:
    main = (pathlib.Path(__file__).resolve().parents[1]
            / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")
    assert "detector=self.medida_do_detector," in main
