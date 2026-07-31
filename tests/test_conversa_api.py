"""Os endpoints do ritmo da conversa e as garantias de fonte no `main.py`.

Separado de `test_conversa.py` (que testa a política pura, sem I/O): aqui entra
o arquivo de configuração de verdade, com `GARRA_REACHY_DIR` apontando para um
diretório temporário.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini import armazenamento, conversa
from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub
from garra_reachy_mini.web.seguranca import resolver_politica


@pytest.fixture()
def cliente_conf(backend, tmp_path, monkeypatch):
    """Painel com um `config.json` isolado — nada toca a instalação real."""
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    armazenamento.diretorio.cache_clear() if hasattr(
        armazenamento.diretorio, "cache_clear") else None
    ctrl = ControladorRobo(backend, semente=1, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=4.0)
    app = FastAPI()
    montar(app, ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos,
        politica=resolver_politica(ambiente={}, escolher_porta=False,
                                   no_robo=False)))
    with TestClient(app) as c:
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


# ── leitura ────────────────────────────────────────────────────────────────
def test_get_devolve_padrao_e_politica_efetiva(cliente_conf):
    """Sem nada salvo, o robô já responde com o modo rápido — não com vazio."""
    d = cliente_conf.get("/api/robot/conversation").json()
    assert d["conversation"]["mode"] == "fast"
    assert d["conversation"]["revision"] == 0
    # `effective` existe para o painel não ter de reimplementar os perfis.
    assert d["effective"]["ack_atraso_ms"] == 4000
    assert d["updated_at"] is None


# ── escrita e concorrência ─────────────────────────────────────────────────
def test_put_incrementa_revisao_e_registra_autor(cliente_conf):
    r = cliente_conf.put("/api/robot/conversation",
                         json={"mode": "informative", "updated_by": "garra-dashboard"})
    assert r.status_code == 200
    d = r.json()
    assert d["conversation"]["mode"] == "informative"
    assert d["conversation"]["revision"] == 1
    assert d["effective"]["ack_atraso_ms"] == 1500
    assert d["updated_by"] == "garra-dashboard"
    assert d["updated_at"] and d["updated_at"].endswith("+00:00")


def test_revisao_antiga_da_409_com_o_estado_atual(cliente_conf):
    """Dois painéis escrevendo o mesmo arquivo: o segundo não sobrescreve calado."""
    cliente_conf.put("/api/robot/conversation", json={"mode": "informative"})
    r = cliente_conf.put("/api/robot/conversation",
                         json={"mode": "fast", "revision": 0})
    assert r.status_code == 409
    # O corpo traz o que vale agora, para o painel recarregar sem outra chamada.
    assert r.json()["conversation"]["mode"] == "informative"
    assert r.json()["conversation"]["revision"] == 1


def test_sem_revisao_o_put_passa(cliente_conf):
    """Cliente simples (curl, script) não precisa participar do protocolo."""
    assert cliente_conf.put("/api/robot/conversation",
                            json={"mode": "informative"}).status_code == 200


def test_modo_desconhecido_da_400(cliente_conf):
    r = cliente_conf.put("/api/robot/conversation", json={"mode": "turbo"})
    assert r.status_code == 400
    # E não gravou nada.
    assert cliente_conf.get(
        "/api/robot/conversation").json()["conversation"]["revision"] == 0


def test_false_sobrevive_a_gravacao(cliente_conf):
    """`if not valor` apagaria a chave e o aviso de progresso voltaria sozinho."""
    cliente_conf.put("/api/robot/conversation",
                     json={"spoken_progress_updates": False})
    d = cliente_conf.get("/api/robot/conversation").json()
    assert d["conversation"]["spoken_progress_updates"] is False
    assert d["effective"]["progresso_falado"] is False


def test_persiste_no_arquivo_e_sobrevive_a_releitura(cliente_conf, tmp_path):
    cliente_conf.put("/api/robot/conversation",
                     json={"mode": "informative", "progress_update_delay_ms": 7000})
    salvo = armazenamento.carregar_config()
    assert salvo["conversation"]["mode"] == "informative"
    assert salvo["conversation"]["progress_update_delay_ms"] == 7000
    # Uma leitura nova (como faz o app a cada turno) enxerga o mesmo.
    assert conversa.Politica.de(salvo["conversation"]).progresso_atraso_ms == 7000


def test_troca_de_modo_preserva_ajuste_do_outro_perfil(cliente_conf):
    """O ajuste fino de um modo não pode sumir ao visitar o outro."""
    cliente_conf.put("/api/robot/conversation",
                     json={"mode": "fast", "acknowledgement_delay_ms": 6000})
    cliente_conf.put("/api/robot/conversation", json={"mode": "informative"})
    d = cliente_conf.put("/api/robot/conversation", json={"mode": "fast"}).json()
    assert d["conversation"]["profiles"]["fast"]["acknowledgement_delay_ms"] == 6000
    assert d["effective"]["ack_atraso_ms"] == 6000


def test_o_put_nao_apaga_o_resto_do_config(cliente_conf):
    """A conversa mora no mesmo arquivo das outras opções; não pode zerá-las."""
    salvo = armazenamento.carregar_config()
    salvo["gateway_url"] = "http://exemplo.invalido:3888"
    armazenamento.salvar_config(salvo)
    cliente_conf.put("/api/robot/conversation", json={"mode": "informative"})
    assert armazenamento.carregar_config()["gateway_url"] == \
        "http://exemplo.invalido:3888"


# ── garantias de fonte, no laço de voz ─────────────────────────────────────
FONTE = (pathlib.Path(__file__).resolve().parent.parent
         / "garra_reachy_mini" / "main.py").read_text()


def test_a_frase_de_espera_nao_e_mais_incondicional():
    """A regressão que este trabalho existe para impedir.

    O código antigo empurrava `random.choice(esperas)` direto no alto-falante
    antes de consultar o modelo — toda pergunta ganhava um "deixa eu pensar",
    inclusive as respondidas em meio segundo.
    """
    assert "push_audio_sample(random.choice(esperas))" not in FONTE


def test_so_o_coordenador_fala_no_turno():
    """`push_audio_sample` fora do coordenador é como a sobreposição voltaria."""
    diretos = FONTE.count("reachy_mini.media.push_audio_sample")
    # Um só: o caminho sem turno (saudação, notificação, fala do painel).
    assert diretos == 1, "áudio empurrado direto além do caminho sem turno"


def test_o_cerebro_e_consultado_antes_de_qualquer_espera():
    """Sintetizar o aviso primeiro custaria o tempo dele à resposta."""
    submissao = FONTE.index("executor.submit(cerebro.perguntar")
    primeiro_ack = FONTE.index("coordenador.tocar_ack(")
    assert submissao < primeiro_ack


def test_o_prazo_de_progresso_e_contado_do_inicio_do_turno():
    """Encadear as esperas faria o aviso de 10 s cair aos 14 s."""
    assert "politica.prazo_progresso(turno.inicio)" in FONTE
    assert "politica.prazo_ack(turno.inicio)" in FONTE


def test_a_resposta_tardia_confere_o_turno_antes_de_falar():
    corte = FONTE.index("decisao = coordenador.resolver_ack(turno)")
    fala = FONTE.index("falar(fala, turno=turno)")
    assert "if not turno.vivo:" in FONTE[corte:fala]


def test_politica_e_relida_a_cada_turno():
    """Mudar o modo no painel tem de valer na pergunta seguinte, sem reiniciar."""
    processar = FONTE[FONTE.index("def processar("):]
    assert "conversa.Politica.de(Config.carregar().conversa)" in processar


def test_erro_do_cerebro_nao_derruba_o_laco():
    """Com o executor, a exceção reaparece no `.result()` — e mataria o laço."""
    trecho = FONTE[FONTE.index("def esperar_cerebro("):]
    trecho = trecho[:trecho.index("resposta = None")]
    assert "except Exception:" in trecho
    assert "FALHA_GENERICA" in trecho


def test_progresso_nunca_vem_antes_do_aviso():
    """Progresso configurado mais curto que o aviso colaria as duas frases."""
    assert "max(politica.prazo_progresso(turno.inicio), prazo_ack)" in FONTE


def test_o_executor_morre_com_o_laco():
    """Um pool encerrado sobrevivendo ao laço deixaria o robô mudo sem erro."""
    assert "executor.shutdown(wait=False, cancel_futures=True)" in FONTE
    assert "self._executor" not in FONTE


def test_o_laco_de_voz_nao_gira_sem_limpar_o_sinal():
    """Guarda antiga, mantida: sem `_acordar.clear()` o laço consome uma CPU."""
    assert "self._acordar.clear()" in FONTE


def test_calar_existe_para_o_estop():
    """Parar o corpo e continuar falando seria contraditório."""
    from garra_reachy_mini.web import api as api_mod
    fonte_api = inspect.getsource(api_mod)
    assert "ctx.calar" in fonte_api
    assert "self._calar = calar" in FONTE
