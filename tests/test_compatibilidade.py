"""Detecção de capacidade, e a diferença entre "robô fora" e "versão antiga".

A Rodada 1 no hardware mostrou o painel dizendo "robô indisponível" para um robô
conectado e respondendo: o app instalado era antigo demais para a rota, o
companion transformava o 405 em 502 genérico, e o painel só via "não deu".
"""

from __future__ import annotations

import requests

from companion import reachy, servidor
from garra_reachy_mini import build_info


# ── quem é este build ──────────────────────────────────────────────────────
def test_a_distribuicao_vem_do_pacote_e_nao_de_um_literal():
    """O build de staging renomeia tudo junto; um literal aqui quebraria nele."""
    assert build_info.DISTRIBUICAO == __import__("garra_reachy_mini").__name__
    assert "staging" not in build_info.DISTRIBUICAO


def test_versao_desconhecida_e_resposta_legitima(monkeypatch):
    """Rodando do fonte, sem instalar, `None` é a verdade — não um erro."""
    def estoura(_nome):
        raise __import__("importlib.metadata", fromlist=["x"]).PackageNotFoundError()
    monkeypatch.setattr(build_info.metadata, "version", estoura)
    assert build_info.versao() is None


def test_o_canal_sai_do_nome_da_distribuicao(monkeypatch):
    monkeypatch.delenv("GARRA_REACHY_CANAL", raising=False)
    monkeypatch.setattr(build_info, "DISTRIBUICAO", "staging_garra_reachy_mini")
    assert build_info.canal() == "staging"
    monkeypatch.setattr(build_info, "DISTRIBUICAO", "garra_reachy_mini")
    assert build_info.canal() == "stable"


def test_a_identidade_tem_o_contrato_inteiro():
    d = build_info.identidade()
    assert set(d) == {"app_id", "version", "channel", "commit", "api_version",
                      "capabilities"}
    # É por estas chaves que o painel decide, não pelo número da versão.
    assert d["capabilities"]["conversation_settings"] is True
    assert d["capabilities"]["voice_turn_events"] is True
    assert d["capabilities"]["audio_barge_in"] is True
    assert isinstance(d["api_version"], int)


def test_capacidades_sao_copia(monkeypatch):
    """Mutar o dict devolvido não pode contaminar o build inteiro."""
    d = build_info.identidade()
    d["capabilities"]["conversation_settings"] = False
    assert build_info.CAPACIDADES["conversation_settings"] is True


def test_o_status_do_robo_carrega_a_identidade(backend, tmp_path):
    """A fixture `cliente` vive em test_web.py; aqui monta-se o mínimo."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from garra_reachy_mini.robo.acoes import ControladorRobo
    from garra_reachy_mini.web.api import ContextoWeb, montar
    from garra_reachy_mini.web.camera import FrameHub
    from garra_reachy_mini.web.seguranca import resolver_politica

    ctrl = ControladorRobo(backend, semente=3, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)))
    with TestClient(app) as c:
        d = c.get("/api/robot/status").json()
    hub.encerrar(); ctrl.encerrar(timeout=2)
    assert d["app_id"] == build_info.DISTRIBUICAO
    assert d["capabilities"]["conversation_settings"] is True
    assert "version" in d      # pode ser None; a chave é que precisa existir


# ── o companion traduz a causa ─────────────────────────────────────────────
class RespostaFalsa:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def json(self) -> dict:
        return {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            e = requests.HTTPError(f"HTTP {self.status_code}")
            e.response = self  # type: ignore[attr-defined]
            raise e


def test_405_do_robo_vira_recurso_nao_suportado(monkeypatch):
    monkeypatch.setattr(reachy, "identidade", lambda p: {"version": "1.0.0"})
    monkeypatch.setattr(reachy.requests, "get", lambda *a, **k: RespostaFalsa(405))
    try:
        reachy.conversa_ler("http://robo:8042")
    except reachy.RecursoNaoSuportado as e:
        assert e.upstream_status == 405
        assert e.robot_version == "1.0.0"
    else:
        raise AssertionError("devia ter erguido RecursoNaoSuportado")


def test_404_tambem(monkeypatch):
    monkeypatch.setattr(reachy, "identidade", lambda p: {})
    monkeypatch.setattr(reachy.requests, "get", lambda *a, **k: RespostaFalsa(404))
    try:
        reachy.conversa_ler("http://robo:8042")
    except reachy.RecursoNaoSuportado as e:
        assert e.robot_version is None   # app velho não reporta versão
    else:
        raise AssertionError("devia ter erguido RecursoNaoSuportado")


def test_identidade_vazia_quando_o_app_e_antigo(monkeypatch):
    """Sem `build_info` do outro lado, `{}` — e não uma exceção."""
    class SemIdentidade(RespostaFalsa):
        def json(self):
            return {"controller_state": "idle", "connected": True}
    monkeypatch.setattr(reachy.requests, "get", lambda *a, **k: SemIdentidade(200))
    assert reachy.identidade("http://robo:8042") == {}


# ── os códigos estáveis que o painel consome ───────────────────────────────
def _codigo(resposta) -> tuple[int, str, object]:
    import json
    corpo = json.loads(bytes(resposta.body))
    return resposta.status_code, corpo["error"]["code"], corpo.get("supported")


def test_nao_suportado_da_501_e_diz_que_o_robo_esta_de_pe(monkeypatch):
    monkeypatch.setattr(reachy, "identidade", lambda p: {"version": "1.0.0"})
    status, codigo, suportado = _codigo(
        servidor._falha(reachy.RecursoNaoSuportado(405, "1.0.0"), "http://robo:8042"))
    assert (status, codigo, suportado) == (501, "robot_feature_unsupported", False)


def test_robo_fora_do_ar_da_502_unreachable():
    status, codigo, suportado = _codigo(
        servidor._falha(requests.ConnectionError("recusada"), "http://robo:8042"))
    assert (status, codigo, suportado) == (502, "robot_unreachable", None)


def test_credencial_errada_nao_e_robo_offline():
    """401 do robô com `reachable: true` — oferecer reconexão seria o conselho
    errado, e é o conselho que "offline" dá."""
    e = requests.HTTPError("401")
    e.response = RespostaFalsa(401)  # type: ignore[attr-defined]
    status, codigo, _ = _codigo(servidor._falha(e, "http://robo:8042"))
    assert (status, codigo) == (502, "robot_auth_failed")


def test_erro_do_robo_nao_vaza_o_corpo():
    """O corpo do robô pode trazer configuração; só o tipo da exceção sai."""
    import json
    e = requests.HTTPError("500 com segredo-no-corpo dentro")
    e.response = RespostaFalsa(500)  # type: ignore[attr-defined]
    resposta = servidor._falha(e, "http://robo:8042")
    corpo = json.loads(bytes(resposta.body))
    assert corpo["error"]["code"] == "robot_error"
    assert corpo["error"]["upstream_status"] == 500
    assert "segredo-no-corpo" not in json.dumps(corpo)


# ── garantias de fonte do painel ───────────────────────────────────────────
import pathlib

WEBCHAT = pathlib.Path(
    "/home/michel/Documents/Projetos/GarraIA/crates/garraia-gateway/src/webchat.html")


def test_o_painel_decide_pelo_codigo_estavel():
    if not WEBCHAT.exists():
        return   # o repositório do gateway não acompanha este pacote
    fonte = WEBCHAT.read_text()
    assert "robot_feature_unsupported" in fonte
    assert "Conversation settings require a newer Garra Reachy Mini app" in fonte


def test_o_painel_nao_grava_quando_nao_ha_suporte():
    if not WEBCHAT.exists():
        return
    fonte = WEBCHAT.read_text()
    corpo = fonte[fonte.index("async function salvarConversa"):]
    corpo = corpo[:corpo.index("\n}")]
    # A trava tem de vir ANTES de qualquer fetch.
    assert corpo.index("conversaSituacao !== CONVERSA_OK") < corpo.index("fetch(")
