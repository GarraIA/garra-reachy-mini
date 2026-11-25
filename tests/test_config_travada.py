"""A trava do PC: depois de pareado, o robô não muda de endereço sem assinatura.

O painel do robô não pede senha na rede de casa. Até o 1.5.0 qualquer aparelho
do Wi-Fi podia gravar `voz_url` em `POST /api/config`, e o robô passava a mandar
o token e cada trecho de fala para o endereço novo. Com a trava, o que decide
PARA ONDE o robô manda coisas (endereços, credenciais, binário) só muda com a
assinatura de quem conhece a credencial — o companion do PC.

Quem usa o app sem companion nunca liga a trava, e o formulário segue valendo.
"""

from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini import armazenamento
from garra_reachy_mini.web import trava

SEGREDO = "segredo-do-pc-0123456789abcdef"
A = "http://10.0.0.24:8123"
B = "http://10.0.0.99:8123"
DA_REDE = ("10.0.0.50", 40000)
PAREADO = {"gateway_url": "http://10.0.0.24:8126", "voz_url": A,
           "voz_token": SEGREDO, "gateway_key": SEGREDO, "trava_pc": True}


@pytest.fixture
def painel(tmp_path, monkeypatch):
    """O painel de configuração do app, de pé, sem robô. Devolve um fabricante
    de clientes: `painel()` fala da rede, `painel(("127.0.0.1", 1))` do robô."""
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    for variavel in ("GARRA_VOZ_TOKEN", "GARRA_GATEWAY_KEY", "GARRA_VOZ_URL", "GARRA_GATEWAY_URL"):
        monkeypatch.delenv(variavel, raising=False)
    from garra_reachy_mini.main import GarraReachyMini

    app = GarraReachyMini.__new__(GarraReachyMini)   # sem tocar no robô
    app.settings_app = FastAPI()
    app._acordar = threading.Event()
    app._rotas_configuracao()
    return lambda cliente=DA_REDE: TestClient(app.settings_app, client=cliente)


def _assinado(cliente: TestClient, valores: dict, segredo: str = SEGREDO,
              assinar: dict | None = None):
    nonce = cliente.get("/api/config/desafio").json()["nonce"]
    marca = trava.assinatura(segredo, nonce, valores if assinar is None else assinar)
    return cliente.post("/api/config", json=valores,
                        headers={trava.CABECALHO: f"v1 {nonce} {marca}"}), nonce, marca


# ─── sem trava, nada muda ────────────────────────────────────────────────────
def test_sem_trava_o_formulario_continua_valendo(painel):
    r = painel().post("/api/config", json={"voz_url": B})
    assert r.status_code == 200 and armazenamento.carregar_config()["voz_url"] == B


def test_o_primeiro_pareamento_entra_sem_assinatura_e_liga_a_trava(painel):
    c = painel()
    assert c.post("/api/config", json=PAREADO).status_code == 200
    assert c.get("/api/config").json()["travada"] is True
    assert c.post("/api/config", json={"voz_url": B}).status_code == 403


# ─── com trava ───────────────────────────────────────────────────────────────
def test_com_a_trava_endereco_sem_assinatura_e_recusado(painel):
    armazenamento.salvar_config(dict(PAREADO))
    r = painel().post("/api/config", json={"voz_url": B})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "config_locked"
    assert r.json()["error"]["fields"] == ["voz_url"]
    assert armazenamento.carregar_config()["voz_url"] == A


@pytest.mark.parametrize("pedido", [
    {"gateway_url": "http://10.0.0.99:8126"},
    {"voz_token": "outro"}, {"gateway_key": "outro"},
    {"voz_token": ""}, {"gateway_key": None},
    {"garra_bin": "/bin/sh"},
    {"trava_pc": False}, {"trava_pc": None},
])
def test_tudo_o_que_decide_para_onde_o_robo_fala_fica_travado(painel, pedido):
    armazenamento.salvar_config(dict(PAREADO))
    assert painel().post("/api/config", json=pedido).status_code == 403
    assert armazenamento.carregar_config() == PAREADO


def test_com_a_trava_o_que_nao_e_protegido_continua_livre(painel):
    armazenamento.salvar_config(dict(PAREADO))
    assert painel().post("/api/config", json={"janela_turnos": 4}).status_code == 200
    assert armazenamento.carregar_config()["janela_turnos"] == 4


def test_repetir_o_valor_que_ja_esta_la_nao_pede_assinatura(painel):
    """O formulário reenvia campos; a máscara do segredo também não é mudança."""
    armazenamento.salvar_config(dict(PAREADO))
    r = painel().post("/api/config", json={"voz_url": A, "gateway_key": "***", "janela_turnos": 3})
    assert r.status_code == 200 and armazenamento.carregar_config()["gateway_key"] == SEGREDO


def test_um_pedido_recusado_nao_grava_nem_a_parte_livre(painel):
    armazenamento.salvar_config(dict(PAREADO))
    assert painel().post("/api/config", json={"voz_url": B, "janela_turnos": 2}).status_code == 403
    assert "janela_turnos" not in armazenamento.carregar_config()


def test_do_proprio_robo_nao_precisa_de_assinatura(painel):
    """A saída de emergência do dono: SSH no robô e um curl em 127.0.0.1."""
    armazenamento.salvar_config(dict(PAREADO))
    r = painel(("127.0.0.1", 5000)).post("/api/config", json={"trava_pc": None, "voz_url": B})
    assert r.status_code == 200
    salvo = armazenamento.carregar_config()
    assert salvo["voz_url"] == B and "trava_pc" not in salvo


# ─── a assinatura do PC ──────────────────────────────────────────────────────
def test_assinatura_do_pc_destrava_a_mudanca(painel):
    armazenamento.salvar_config(dict(PAREADO))
    r, _, _ = _assinado(painel(), {"voz_url": B, "gateway_url": "http://10.0.0.99:8126"})
    assert r.status_code == 200 and armazenamento.carregar_config()["voz_url"] == B


def test_o_desafio_vale_uma_vez_so(painel):
    armazenamento.salvar_config(dict(PAREADO))
    c = painel()
    r, nonce, marca = _assinado(c, {"voz_url": B})
    assert r.status_code == 200
    de_novo = c.post("/api/config", json={"voz_url": A},
                     headers={trava.CABECALHO: f"v1 {nonce} {trava.assinatura(SEGREDO, nonce, {'voz_url': A})}"})
    assert de_novo.status_code == 403


def test_assinatura_com_outro_segredo_e_recusada(painel):
    armazenamento.salvar_config(dict(PAREADO))
    r, _, _ = _assinado(painel(), {"voz_url": B}, segredo="segredo-de-outro-computador")
    assert r.status_code == 403 and armazenamento.carregar_config()["voz_url"] == A


def test_assinatura_de_outro_pedido_e_recusada(painel):
    """A assinatura cobre o conteúdo: não serve de carimbo para outro endereço."""
    armazenamento.salvar_config(dict(PAREADO))
    r, _, _ = _assinado(painel(), {"voz_url": B}, assinar={"voz_url": A})
    assert r.status_code == 403


@pytest.mark.parametrize("cabecalho", ["", "v1", "v1 so-o-nonce", "v2 a b", "v1 a b c", "lixo"])
def test_cabecalho_torto_e_recusado_sem_estourar(painel, cabecalho):
    armazenamento.salvar_config(dict(PAREADO))
    r = painel().post("/api/config", json={"voz_url": B}, headers={trava.CABECALHO: cabecalho})
    assert r.status_code == 403


def test_trocar_a_credencial_pede_a_assinatura_da_antiga(painel):
    armazenamento.salvar_config(dict(PAREADO))
    novo = {"voz_token": "credencial-nova", "gateway_key": "credencial-nova"}
    r, _, _ = _assinado(painel(), novo)
    assert r.status_code == 200 and armazenamento.carregar_config()["voz_token"] == "credencial-nova"


def test_a_leitura_diz_que_esta_travada_sem_mostrar_o_segredo(painel):
    armazenamento.salvar_config(dict(PAREADO))
    r = painel().get("/api/config")
    assert r.json()["travada"] is True and SEGREDO not in r.text


def test_sem_trava_a_leitura_diz_que_nao_esta(painel):
    assert painel().get("/api/config").json()["travada"] is False


# ─── as peças ────────────────────────────────────────────────────────────────
def test_desafio_expira_e_o_estoque_tem_teto():
    agora = [100.0]
    d = trava.Desafios(validade_s=60.0, maximo=3, relogio=lambda: agora[0])
    velho = d.emitir()
    agora[0] += 61
    assert d.consumir(velho) is False
    emitidos = [d.emitir() for _ in range(5)]
    assert d.consumir(emitidos[0]) is False          # caiu pelo teto
    assert d.consumir(emitidos[-1]) is True
    assert d.consumir(emitidos[-1]) is False         # uso único
    assert d.consumir("nunca-emitido") is False


def test_a_assinatura_nao_depende_da_ordem_das_chaves():
    um = trava.assinatura(SEGREDO, "n", {"voz_url": A, "gateway_url": B})
    outro = trava.assinatura(SEGREDO, "n", {"gateway_url": B, "voz_url": A})
    assert um == outro and len(um) == 64
    assert trava.assinatura(SEGREDO, "outro-nonce", {"voz_url": A, "gateway_url": B}) != um


@pytest.mark.parametrize("host,esperado", [
    ("127.0.0.1", True), ("::1", True), ("127.8.9.1", True),
    ("10.0.0.50", False), ("", False), (None, False), ("localhost.evil", False),
])
def test_loopback(host, esperado):
    assert trava.loopback(host) is esperado


# ─── o lado do PC: o companion assina o que grava ────────────────────────────
class _Resposta:
    def __init__(self, status: int = 200, corpo: dict | None = None) -> None:
        self.status_code, self._corpo = status, corpo or {}
        self.ok = 200 <= status < 300

    def json(self) -> dict:
        return self._corpo

    def raise_for_status(self) -> None:
        if not self.ok:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def robo_falso(monkeypatch):
    """Um robô de mentira para o `companion/reachy.py`: guarda o que recebeu."""
    from companion import reachy, voz

    class Robo:
        tem_desafio = True
        resposta_do_post = _Resposta(200, {"ok": True})
        posts: list[tuple[dict, dict]] = []

    robo = Robo()
    robo.posts = []

    def get(url, **kw):
        if url.endswith("/api/config/desafio"):
            return _Resposta(200, {"nonce": "desafio-do-teste"}) if robo.tem_desafio else _Resposta(404)
        raise AssertionError(f"GET inesperado: {url}")

    def post(url, json=None, headers=None, **kw):
        robo.posts.append((dict(json or {}), dict(headers or {})))
        return robo.resposta_do_post

    monkeypatch.setattr(reachy.requests, "get", get)
    monkeypatch.setattr(reachy.requests, "post", post)
    monkeypatch.setattr(voz, "token", lambda: SEGREDO)
    return robo


def test_o_companion_assina_com_a_mesma_conta_do_robo(robo_falso):
    """As duas pontas calculam o mesmo HMAC — é o que as mantém compatíveis."""
    from companion import reachy

    valores = {"gateway_url": "http://10.0.0.24:8126", "voz_url": A}
    reachy._gravar("http://10.0.0.142:8042", valores)
    (corpo, cabecalhos), = robo_falso.posts
    assert corpo == valores
    assert cabecalhos[trava.CABECALHO] == \
        f"v1 desafio-do-teste {trava.assinatura(SEGREDO, 'desafio-do-teste', valores)}"
    assert SEGREDO not in str(cabecalhos)


def test_a_assinatura_do_companion_abre_a_trava_de_verdade(painel, monkeypatch):
    """Ponta a ponta: o `_gravar` do PC contra a rota do robô, sem dublê no meio."""
    from companion import reachy, voz

    armazenamento.salvar_config(dict(PAREADO))
    cliente = painel()
    monkeypatch.setattr(voz, "token", lambda: SEGREDO)
    monkeypatch.setattr(reachy.requests, "get", lambda url, **kw: cliente.get(url.replace("http://robo", "")))
    monkeypatch.setattr(reachy.requests, "post", lambda url, json=None, headers=None, **kw:
                        cliente.post(url.replace("http://robo", ""), json=json, headers=headers))
    reachy._gravar("http://robo", {"voz_url": B})
    assert armazenamento.carregar_config()["voz_url"] == B


def test_app_antigo_sem_desafio_recebe_o_pedido_sem_assinatura(robo_falso):
    from companion import reachy

    robo_falso.tem_desafio = False
    reachy._gravar("http://10.0.0.142:8042", {"voz_url": A})
    (_, cabecalhos), = robo_falso.posts
    assert trava.CABECALHO not in cabecalhos


def test_robo_pareado_com_outra_credencial_vira_erro_que_diz_o_que_fazer(robo_falso):
    from companion import reachy

    robo_falso.resposta_do_post = _Resposta(403, {"ok": False, "error": {
        "code": "config_locked", "fields": ["voz_url"]}})
    with pytest.raises(reachy.ErroReachy) as erro:
        reachy._gravar("http://10.0.0.142:8042", {"voz_url": B})
    assert "pareado" in str(erro.value) and "trava_pc" in str(erro.value)
    assert SEGREDO not in str(erro.value)


def test_parear_pela_tela_liga_a_trava_e_o_desfazer_devolve_a_de_antes(monkeypatch):
    from companion import reachy, voz

    monkeypatch.setattr(voz, "ip_lan", lambda: "10.0.0.24")
    monkeypatch.setattr(voz, "token", lambda: SEGREDO)
    gravados: list[dict] = []
    monkeypatch.setattr(reachy, "_config_atual", lambda painel: {"efetiva": {}, "salva": {}})
    monkeypatch.setattr(reachy, "_gravar", lambda painel, valores: gravados.append(dict(valores)))
    monkeypatch.setattr(reachy, "_esperar_alcancar", lambda *a, **k: (False, {}))
    relatorio = reachy.configurar("http://10.0.0.142:8042")
    assert gravados[0]["trava_pc"] is True
    assert gravados[1]["trava_pc"] is None           # não estava travado: volta a não estar
    assert relatorio["rolled_back"] is True


def test_reapontar_sozinho_nao_mexe_na_trava(monkeypatch):
    from companion import reachy, voz

    monkeypatch.setattr(voz, "ip_lan", lambda: "10.0.0.24")
    monkeypatch.setattr(voz, "token", lambda: SEGREDO)
    gravados: list[dict] = []
    monkeypatch.setattr(reachy, "_config_atual", lambda painel: {"efetiva": {}, "salva": dict(PAREADO)})
    monkeypatch.setattr(reachy, "_gravar", lambda painel, valores: gravados.append(dict(valores)))
    monkeypatch.setattr(reachy, "_esperar_alcancar", lambda *a, **k: (True, {}))
    reachy.configurar("http://10.0.0.142:8042", so_enderecos=True)
    assert [set(g) for g in gravados] == [{"gateway_url", "voz_url"}]
