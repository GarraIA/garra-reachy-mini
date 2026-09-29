"""A faixa de "recursos limitados" e a sessão que o gateway recusa.

Três defeitos do 1.3.1, vistos em uso em 2026-09-29:

  * a faixa mostrava `servico.gateway` cru (faltava a tradução do nome) e, para
    o gateway fora do ar, a dica do **servidor de voz** — a dica era escolhida
    só pelo motivo, e "não respondeu" é o mesmo motivo para os dois;
  * a rota de "nova conversa" agia sobre o cérebro antigo depois de o laço de
    voz reconstruir o cérebro;
  * o verificador de notificações consultava para sempre uma sessão que o
    gateway (ou a ponte) já não aceitava.
"""

from __future__ import annotations

import inspect
import logging
import pathlib
import threading

import pytest

from garra_reachy_mini import cerebro as mod_cerebro
from garra_reachy_mini import main as mod_main
from garra_reachy_mini import servicos as mod_servicos

PAINEL = pathlib.Path(mod_cerebro.__file__).parent / "static"


def _idiomas() -> dict[str, str]:
    i18n = (PAINEL / "i18n.js").read_text(encoding="utf-8")
    return {"en": i18n[i18n.index("  en: {"):i18n.index("  pt: {")],
            "pt": i18n[i18n.index("  pt: {"):]}


# ── a faixa ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("nome", mod_servicos.Servicos.NOMES)
def test_todo_servico_tem_nome_nos_dois_idiomas(nome):
    """Sem a chave, `t()` devolve a própria chave e a tela mostra `servico.x`."""
    for idioma, texto in _idiomas().items():
        assert f"'servico.{nome}'" in texto, f"{nome} sem nome em {idioma}"


def test_gateway_fora_do_ar_nao_recebe_a_dica_da_voz():
    for idioma, texto in _idiomas().items():
        assert "'dica.gateway.unreachable'" in texto, idioma
        assert "'dica.voice.unreachable'" in texto, idioma
        # A genérica por motivo era a origem do conselho trocado.
        assert "'dica.unreachable'" not in texto, idioma
        linha = next(l for l in texto.splitlines() if "'dica.gateway.unreachable'" in l)
        assert "GARRA_VOZ_URL" not in linha, idioma
        assert "companion" in linha.lower(), idioma


def test_o_painel_procura_a_dica_do_servico_antes_da_do_motivo():
    js = (PAINEL / "reachy.js").read_text(encoding="utf-8")
    corpo = js[js.index("function pintarServicos"):js.index("function pintarStatus")]
    do_servico = corpo.index("dica.${s.name}.${s.reason_code}")
    do_motivo = corpo.index("dica.${s.reason_code}")
    assert do_servico < do_motivo


def test_a_dica_do_servidor_aponta_para_o_que_existe():
    """A página `#/reachy` do console não existe no gateway público."""
    fonte = inspect.getsource(mod_main)
    trecho = fonte[fonte.index("def _marcar_cerebro"):]
    trecho = trecho[:trecho.index("def _sondar_cerebro")]
    assert "Reachy page" not in trecho
    assert "companion" in trecho


# ── a sessão que o gateway recusa ───────────────────────────────────────────
class _Resposta:
    def __init__(self, status, corpo=None):
        self.status_code = status
        self._corpo = corpo or {}
        self.text = str(corpo)

    def json(self):
        return self._corpo


class _Http:
    def __init__(self, historico: int) -> None:
        self.historico = historico
        self.criadas = 0
        self.lidas: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url, timeout=None):
        self.lidas.append(url)
        if "nova-" in url:
            return _Resposta(200, {"messages": []})
        return _Resposta(self.historico, {"messages": []})

    def post(self, url, json=None, timeout=None):
        self.criadas += 1
        return _Resposta(201, {"session_id": f"nova-{self.criadas}"})


@pytest.fixture
def cerebro_do_gateway(monkeypatch):
    def montar(historico: int):
        gravado: dict = {}
        monkeypatch.setattr(mod_cerebro.armazenamento, "salvar_estado",
                            lambda d: gravado.update(d))
        monkeypatch.setattr(mod_cerebro.armazenamento, "carregar_estado",
                            lambda: dict(gravado))
        c = mod_cerebro.GatewayBrain.__new__(mod_cerebro.GatewayBrain)
        c.log = logging.getLogger("teste")
        c._trava = threading.RLock()
        c.session_id = "recusada"
        c.cursor = c.cursor_falado = 3
        c._ultima_resposta = None
        c.http = _Http(historico)
        c.cfg = type("Cfg", (), {"agent_id": "reachy_voice", "gateway_url": ""})()
        c._url = lambda caminho: caminho
        return c, gravado
    return montar


def test_sessao_recusada_no_poll_vira_sessao_nova(cerebro_do_gateway):
    """404 é "essa sessão não vale mais": insistir nela é consultar para sempre."""
    c, gravado = cerebro_do_gateway(404)
    assert c.novas_mensagens() == []
    assert c.session_id == "nova-1"
    assert c.cursor == c.cursor_falado == 0
    assert gravado["session_id"] == "nova-1"
    # E a próxima consulta já é na sessão nova, não na recusada.
    c.novas_mensagens()
    assert c.http.lidas[-1].endswith("/api/sessions/nova-1/history")
    assert c.http.criadas == 1


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_gateway_tropecando_nao_troca_de_sessao(cerebro_do_gateway, status):
    """Erro passageiro não é recusa: trocar aqui jogaria a conversa fora."""
    c, _ = cerebro_do_gateway(status)
    assert c.novas_mensagens() == []
    assert c.session_id == "recusada"
    assert c.http.criadas == 0


# ── "nova conversa" depois de o cérebro ser reconstruído ────────────────────
class _Cerebro:
    def __init__(self, nome):
        self.nome = nome
        self.trocas = 0

    def nova_sessao(self):
        self.trocas += 1
        return f"sessao-de-{self.nome}"


def test_a_troca_de_sessao_segue_o_cerebro_em_uso():
    """O laço reconstrói o cérebro quando o gateway volta; a rota tem de ir junto."""
    antigo, novo = _Cerebro("antigo"), _Cerebro("novo")
    em_uso = [antigo]
    trocar = mod_main.troca_de_sessao(em_uso)
    assert trocar() == "sessao-de-antigo"
    em_uso[0] = novo
    assert trocar() == "sessao-de-novo"
    assert (antigo.trocas, novo.trocas) == (1, 1)
