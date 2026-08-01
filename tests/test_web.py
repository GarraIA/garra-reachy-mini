"""API, segurança, câmera e chat — com robô falso e gateway falso."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from garra_reachy_mini.robo.acoes import ControladorRobo
from garra_reachy_mini.web.api import ContextoWeb, montar
from garra_reachy_mini.web.camera import FrameHub
from garra_reachy_mini.web.seguranca import (
    Limitador,
    Politica,
    origem_permitida,
    resolver_politica,
    token_valido,
)

# JPEG com estrutura de marcadores válida: SOI + APP0 (para exercitar o salto de
# segmento) + SOF0 declarando 320x240 + EOI. Não é decodificável, e nem precisa
# ser: o que se testa é a leitura do cabeçalho.
JPEG_MINIMO = bytes.fromhex(
    "ffd8"                                    # SOI
    "ffe0" "0010" "4a46494600" "0101" "00" "0001" "0001" "0000"   # APP0/JFIF
    "ffc0" "0011" "08" "00f0" "0140" "03" "011100" "021101" "031101"  # SOF0 240x320
    "ffd9"                                    # EOI
)
LARGURA_ESPERADA, ALTURA_ESPERADA = 320, 240


# ─── segurança ───────────────────────────────────────────────────────────────
def test_padrao_e_loopback():
    p = resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)
    assert p.host == "127.0.0.1" and p.remoto is False


def test_remoto_sem_token_gera_um(tmp_path, monkeypatch):
    """Expor o robô sem porteiro não é opção — mas exigir token manual tornava
    o modo rede inútil. Agora ele se gera sozinho, com modo 0600."""
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    p = resolver_politica(ambiente={"GARRA_REACHY_ALLOW_REMOTE": "1"},
                          escolher_porta=False, no_robo=False)
    assert p.host == "0.0.0.0" and p.remoto and p.exige_token()
    assert p.token and len(p.token) >= 20
    arquivo = tmp_path / "token"
    assert arquivo.read_text().strip() == p.token
    assert oct(arquivo.stat().st_mode)[-3:] == "600"


def test_token_persiste_entre_arranques(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    amb = {"GARRA_REACHY_ALLOW_REMOTE": "1"}
    a = resolver_politica(ambiente=amb, escolher_porta=False, no_robo=False)
    b = resolver_politica(ambiente=amb, escolher_porta=False, no_robo=False)
    assert a.token == b.token


def test_dentro_do_robo_abre_na_rede_sem_exigir_token(tmp_path, monkeypatch):
    """No robô wireless o daemon da Pollen já está em 0.0.0.0 sem autenticação
    nenhuma, então um token nosso não protegeria nada — e não haveria de onde
    lê-lo: o daemon não expõe o log do app. Medido no hardware."""
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    p = resolver_politica(ambiente={}, escolher_porta=False, no_robo=True)
    assert p.host == "0.0.0.0" and p.remoto
    assert not p.exige_token()
    assert not (tmp_path / "token").exists()


def test_no_robo_o_token_continua_disponivel_por_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    p = resolver_politica(ambiente={"GARRA_REACHY_TOKEN": "meutoken"},
                          escolher_porta=False, no_robo=True)
    assert p.host == "0.0.0.0" and p.exige_token() and p.token == "meutoken"


def test_fora_do_robo_a_rede_exige_token(tmp_path, monkeypatch):
    """Fora do robô o daemon é loopback: abrir a nossa API CRIA exposição."""
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    p = resolver_politica(ambiente={"GARRA_REACHY_ALLOW_REMOTE": "1"},
                          escolher_porta=False, no_robo=False)
    assert p.host == "0.0.0.0" and p.exige_token()


def test_bind_explicito_vence_a_deteccao(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_DIR", str(tmp_path))
    p = resolver_politica(ambiente={"GARRA_REACHY_BIND": "127.0.0.1"},
                          escolher_porta=False, no_robo=True)
    assert p.host == "127.0.0.1" and p.remoto is False


def test_mesma_origem_passa_na_rede():
    """O painel aberto em http://reachy-mini.local:8042 não tem como estar na
    allowlist montada no arranque; mesma origem não é CSRF."""
    p = Politica(host="0.0.0.0", porta=8042, remoto=True, token="t")
    assert origem_permitida("http://reachy-mini.local:8042", p, "reachy-mini.local:8042")
    assert not origem_permitida("http://evil.example", p, "reachy-mini.local:8042")


def test_remoto_com_token_abre():
    p = resolver_politica(
        ambiente={"GARRA_REACHY_ALLOW_REMOTE": "1", "GARRA_REACHY_TOKEN": "abc123"},
        escolher_porta=False, no_robo=False,
    )
    assert p.host == "0.0.0.0" and p.remoto and p.exige_token()


def test_origens_incluem_o_console_do_garra():
    p = resolver_politica(ambiente={}, escolher_porta=False, no_robo=False)
    assert "http://localhost:3888" in p.origens
    assert "http://127.0.0.1:3888" in p.origens


def test_origem_desconhecida_barrada_em_modo_remoto():
    p = resolver_politica(
        ambiente={"GARRA_REACHY_ALLOW_REMOTE": "1", "GARRA_REACHY_TOKEN": "t"},
        escolher_porta=False, no_robo=False,
    )
    assert not origem_permitida("http://evil.example", p)
    assert origem_permitida(None, p)  # curl não manda Origin


def test_token_comparado_em_tempo_constante():
    p = Politica(remoto=True, token="segredo")
    assert token_valido("Bearer segredo", None, p)
    assert token_valido(None, "segredo", p)
    assert not token_valido("Bearer errado0", None, p)
    assert not token_valido(None, "seg", p)


def test_porta_ocupada_cai_para_a_proxima():
    """No desktop o reachy-mini-control da Pollen já ocupa a 8042."""
    import socket

    from garra_reachy_mini.web.seguranca import porta_livre

    with socket.socket() as ocupada, socket.socket() as livre:
        for s in (ocupada, livre):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ocupada.bind(("127.0.0.1", 0))
        ocupada.listen(1)
        preferida = ocupada.getsockname()[1]
        livre.bind(("127.0.0.1", 0))
        alternativa = livre.getsockname()[1]
        livre.close()
        assert porta_livre("127.0.0.1", preferida, (alternativa,)) == alternativa


def test_sem_alternativa_livre_devolve_a_preferida():
    from garra_reachy_mini.web.seguranca import porta_livre

    assert porta_livre("127.0.0.1", 65000, ()) in (65000,)


def test_porta_explicita_e_respeitada():
    p = resolver_politica(ambiente={"GARRA_REACHY_PORTA": "9999"}, no_robo=False)
    assert p.porta == 9999


def test_limitador_de_taxa():
    lim = Limitador(capacidade=3, por_segundo=0.0)
    assert [lim.permitir("1.2.3.4") for _ in range(4)] == [True, True, True, False]
    assert lim.permitir("5.6.7.8") is True  # balde por IP


# ─── FrameHub ────────────────────────────────────────────────────────────────
class CameraFalsa:
    def __init__(self, jpeg=JPEG_MINIMO):
        self.jpeg = jpeg
        self.leituras = 0
        self._lock = threading.Lock()

    def frame_jpeg(self):
        with self._lock:
            self.leituras += 1
        return self.jpeg


def test_hub_le_uma_vez_e_distribui_para_todos():
    cam = CameraFalsa()
    hub = FrameHub(cam, fps_ativo=50, fps_ocioso=50)
    hub.iniciar()
    try:
        time.sleep(0.25)
        a, b, c = hub.ultimo(), hub.ultimo(), hub.ultimo()
        assert a is b is c, "consumidores têm de ver o MESMO quadro"
        # a leitura é do produtor: 20 consumidores não viraram 20 leituras
        leituras_antes = cam.leituras
        for _ in range(20):
            hub.ultimo()
        assert cam.leituras == leituras_antes
    finally:
        hub.encerrar()


def test_hub_le_dimensoes_do_jpeg():
    hub = FrameHub(CameraFalsa(), fps_ativo=50, fps_ocioso=50)
    q = hub._capturar()
    assert q is not None
    assert (q.largura, q.altura) == (LARGURA_ESPERADA, ALTURA_ESPERADA)


def test_leitor_de_jpeg_rejeita_lixo():
    from garra_reachy_mini.robo.imagem import dimensoes_jpeg

    assert dimensoes_jpeg(b"") is None
    assert dimensoes_jpeg(b"nao sou jpeg") is None
    assert dimensoes_jpeg(b"\xff\xd8\xff\xe0") is None  # truncado
    assert dimensoes_jpeg(JPEG_MINIMO) == (LARGURA_ESPERADA, ALTURA_ESPERADA)


def test_hub_marca_quadro_obsoleto():
    from garra_reachy_mini.web import camera as mod

    hub = FrameHub(CameraFalsa())
    q = hub._capturar()
    assert q is not None and not q.obsoleto
    antigo = mod.Quadro(jpeg=b"x", seq=1, ts=time.monotonic() - 99, largura=1, altura=1)
    assert antigo.obsoleto


def test_hub_sem_camera_nao_estoura():
    class Morta:
        def frame_jpeg(self):
            return None

    hub = FrameHub(Morta())
    assert hub._capturar() is None
    assert hub.status()["available"] is False


def test_hub_limita_espectadores():
    hub = FrameHub(CameraFalsa(), max_clientes=2)
    assert hub.entrar() and hub.entrar()
    assert hub.entrar() is False
    hub.sair()
    assert hub.entrar() is True


def test_hub_acelera_com_espectador():
    hub = FrameHub(CameraFalsa(), fps_ativo=20, fps_ocioso=1)
    assert hub.status()["fps"] == 1
    hub.entrar()
    assert hub.status()["fps"] == 20


# ─── API ─────────────────────────────────────────────────────────────────────
@pytest.fixture
def cliente(backend, tmp_path):
    ctrl = ControladorRobo(backend, semente=99, dir_capturas=tmp_path)
    ctrl.iniciar()
    hub = FrameHub(backend, fps_ativo=30, fps_ocioso=5)
    hub.iniciar()
    app = FastAPI()
    ctx = ContextoWeb(
        controlador=ctrl, hub=hub, eventos=ctrl.eventos,
        politica=resolver_politica(ambiente={}, escolher_porta=False, no_robo=False),
    )
    montar(app, ctx)
    with TestClient(app) as c:
        c.ctx = ctx  # type: ignore[attr-defined]
        c.ctrl = ctrl  # type: ignore[attr-defined]
        yield c
    hub.encerrar()
    ctrl.encerrar(timeout=2)


def test_status_traz_o_contrato_do_painel(cliente):
    d = cliente.get("/api/robot/status").json()
    for chave in ("mode", "controller_state", "connected", "moving", "camera",
                  "tracking", "network", "chat", "latency_ms",
                  "services", "limited", "missing"):
        assert chave in d, chave
    assert d["network"]["remote"] is False


def test_status_descreve_cada_subsistema(cliente):
    """O painel precisa distinguir 'você não configurou' de 'quebrou'."""
    d = cliente.get("/api/robot/status").json()
    nomes = {s["name"] for s in d["services"]}
    assert {"robot", "movement", "camera", "voice", "gateway", "brain"} == nomes
    for s in d["services"]:
        assert isinstance(s["available"], bool)
        assert s["reason_code"]


@pytest.fixture()
def cliente_na_rede(controlador):
    """Painel exposto na LAN, como fica instalado dentro do robô wireless."""
    ctrl = controlador
    app = FastAPI()
    hub = FrameHub(ctrl.backend, fps_ativo=4.0)
    pol = Politica(host="0.0.0.0", porta=8042, remoto=True, token="segredo",
                   origens=("http://localhost:8042",))
    ctx = ContextoWeb(controlador=ctrl, hub=hub, eventos=ctrl.eventos, politica=pol)
    montar(app, ctx)
    with TestClient(app) as c:
        yield c
    hub.encerrar()


def test_na_rede_a_api_exige_token(cliente_na_rede):
    assert cliente_na_rede.get("/api/robot/status").status_code == 401
    ok = cliente_na_rede.get("/api/robot/status", headers={"Authorization": "Bearer segredo"})
    assert ok.status_code == 200


def test_parada_de_emergencia_nunca_pede_token(cliente_na_rede):
    """Um botão de pânico que responde 401 é pior do que a API que ele
    protegeria: quem alcança o robô fisicamente precisa conseguir pará-lo."""
    r = cliente_na_rede.post("/api/robot/stop")
    assert r.status_code == 200
    assert r.json()["ok"]


def test_configuracao_nao_fica_aberta_na_rede(cliente_na_rede):
    """`/api/config` guarda a chave do gateway."""
    assert cliente_na_rede.post("/api/config", json={"gateway_url": "http://x"}
                                ).status_code == 401


def test_capabilities_lista_acoes_e_expressoes(cliente):
    d = cliente.get("/api/robot/capabilities").json()
    nomes = {a["name"] for a in d["actions"]}
    assert {"turn_head", "dance", "stop", "look_at", "set_expression"} <= nomes
    assert d["expressions"]["happy"]["resolved_move"] == "cheerful1"
    assert len(d["primary_expressions"]) == 9


def test_acao_executa_e_responde_honestamente(cliente):
    r = cliente.post("/api/robot/action", json={"action": "turn_head", "direction": "right"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["executed"] and d["mode"] == "real"
    assert d["action_id"].startswith("act_")


def test_acao_invalida_vira_400(cliente):
    r = cliente.post("/api/robot/action", json={"action": "turn_head", "direction": "xyz"})
    assert r.status_code == 400
    assert r.json()["accepted"] is False


def test_acao_sem_nome(cliente):
    assert cliente.post("/api/robot/action", json={}).status_code == 400


def test_estop_e_o_ciclo_de_recuperacao(cliente):
    assert cliente.post("/api/robot/stop").json()["executed"] is True
    st = cliente.get("/api/robot/status").json()
    assert st["estopped"] is True and st["controller_state"] == "estopped"

    barrada = cliente.post("/api/robot/action", json={"action": "dance"})
    assert barrada.status_code == 400

    assert cliente.post("/api/robot/clear-estop").json()["ok"] is True
    assert cliente.get("/api/robot/status").json()["estopped"] is False
    assert cliente.post("/api/robot/action", json={"action": "dance"}).json()["executed"]


def test_neutro_e_tracking(cliente):
    assert cliente.post("/api/robot/neutral").json()["executed"]
    d = cliente.post("/api/robot/tracking", json={"enabled": True, "weight": 0.5}).json()
    assert d["ok"] and "Rastreamento" in d["message"]


def test_instantaneo_devolve_jpeg(cliente):
    r = cliente.get("/api/robot/camera/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")


def test_apps_lista(cliente):
    assert "apps" in cliente.get("/api/robot/apps").json()


@pytest.mark.parametrize("ruim", ["nome com espaco", "app;rm", "-traco", "a" * 90])
def test_nome_de_app_malicioso_nao_inicia_nada(cliente, backend, ruim):
    """O caminho pode até casar com a rota; o controlador é quem barra."""
    r = cliente.post(f"/api/robot/apps/{ruim}/start")
    assert r.status_code == 400 or r.json().get("accepted") is False
    assert backend.ops("app_iniciar") == []


def test_travessia_de_caminho_nao_chega_na_rota(cliente, backend):
    r = cliente.post("/api/robot/apps/..%2Fetc%2Fpasswd/start")
    assert r.status_code >= 400
    assert backend.ops("app_iniciar") == []


def test_eventos_ficam_no_historico(cliente):
    cliente.post("/api/robot/action", json={"action": "turn_head", "direction": "left"})
    tipos = [e["type"] for e in cliente.get("/api/robot/events").json()["events"]]
    assert "robot.action.started" in tipos
    assert "robot.action.completed" in tipos


def test_logs_trazem_erros(cliente):
    cliente.post("/api/robot/action", json={"action": "set_expression", "name": "loving"})
    erros = cliente.get("/api/robot/logs").json()["errors"]
    assert any(e["action"] == "set_expression" for e in erros)


def test_origem_desconhecida_barrada_em_post(cliente):
    r = cliente.post(
        "/api/robot/stop", headers={"Origin": "http://atacante.example"}
    )
    assert r.status_code == 403


def test_origem_do_console_do_garra_liberada(cliente):
    r = cliente.get("/api/robot/status", headers={"Origin": "http://localhost:3888"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3888"


def test_get_nao_precisa_de_origem(cliente):
    assert cliente.get("/api/robot/status").status_code == 200


def test_websocket_manda_status_e_eventos(cliente):
    with cliente.websocket_connect("/ws/eventos") as ws:
        primeiro = ws.receive_json()
        assert primeiro["type"] == "robot.status"
        cliente.post("/api/robot/action", json={"action": "turn_head", "direction": "up"})
        tipos = set()
        fim = time.time() + 5
        while time.time() < fim and "robot.action.completed" not in tipos:
            tipos.add(ws.receive_json()["type"])
        assert "robot.action.completed" in tipos


def test_chat_desligado_responde_503(cliente):
    assert cliente.get("/api/chat/status").json()["available"] is False
    assert cliente.post("/api/chat/enviar", json={"content": "oi"}).status_code == 503


def test_falar_sem_voz_responde_503(cliente):
    assert cliente.post("/api/chat/falar", json={"text": "oi"}).status_code == 503


# ─── despertar do supervisor ─────────────────────────────────────────────────
def test_config_acorda_o_supervisor_em_vez_de_esperar_o_backoff():
    """Configuração salva pelo console não pode esperar até 60 s de backoff.

    O `_esperar` acorda em dois sinais: o stop_event do daemon (que tem 20 s
    antes do SIGKILL) e o `_acordar` que o POST /api/config levanta.
    """
    import threading
    import time

    from garra_reachy_mini.main import GarraReachyMini

    app = GarraReachyMini.__new__(GarraReachyMini)   # sem tocar no robô
    app._acordar = threading.Event()
    parar = threading.Event()

    t0 = time.monotonic()
    threading.Timer(0.3, app._acordar.set).start()
    app._esperar(parar, 60.0)
    assert time.monotonic() - t0 < 1.5, "não acordou com a configuração nova"
    assert not app._acordar.is_set(), "o sinal tem de ser consumido"

    t0 = time.monotonic()
    threading.Timer(0.3, parar.set).start()
    app._esperar(parar, 60.0)
    assert time.monotonic() - t0 < 1.5, "não acordou no stop_event"


def test_esperar_respeita_o_prazo_quando_ninguem_acorda():
    import threading
    import time

    from garra_reachy_mini.main import GarraReachyMini

    app = GarraReachyMini.__new__(GarraReachyMini)
    app._acordar = threading.Event()
    t0 = time.monotonic()
    app._esperar(threading.Event(), 0.6)
    assert 0.5 <= time.monotonic() - t0 < 1.2


def test_ponte_chat_repontada_sem_reiniciar():
    """Trocar a URL do gateway tem de valer sem reiniciar o app.

    O cliente httpx nasce com `base_url` fixa; sem `reconfigurar` o chat do
    painel continuaria batendo no endereço antigo até alguém reiniciar.
    """
    from garra_reachy_mini.web.chat import PonteChat

    ponte = PonteChat("http://127.0.0.1:3888", None, "reachy_voice")
    ponte._sessao = "sessao-do-gateway-antigo"
    ponte._cliente = object()  # type: ignore[assignment]

    assert ponte.reconfigurar("http://127.0.0.1:3888", None, "reachy_voice") is False
    assert ponte._sessao == "sessao-do-gateway-antigo", "sem mudança, não descarta"

    assert ponte.reconfigurar("http://192.0.2.10:3888/", None, "reachy_voice") is True
    assert ponte.base == "http://192.0.2.10:3888"
    assert ponte._sessao is None, "sessão de um gateway não vale em outro"
    assert ponte._cliente is None


def test_sinal_de_configuracao_e_consumido_uma_vez_so():
    """Quem trata o `_acordar` tem de consumi-lo.

    O supervisor reentra no laço de voz logo depois de ele voltar; um sinal
    ainda levantado faria o laço sair de novo na hora, girando entre as duas
    funções sem nunca ouvir o microfone.
    """
    import pathlib
    import re

    fonte = pathlib.Path(__file__).parent.parent / "garra_reachy_mini" / "main.py"
    texto = fonte.read_text(encoding="utf-8")
    # Todo `if self._acordar.is_set():` precisa de um `.clear()` logo abaixo.
    for m in re.finditer(r"if self\._acordar\.is_set\(\):", texto):
        trecho = texto[m.end():m.end() + 700]
        assert "self._acordar.clear()" in trecho, (
            "sinal de configuração tratado sem ser consumido em "
            f"main.py, offset {m.start()}")


def test_sonda_do_cerebro_nao_cria_sessao():
    """O health check periódico não pode passar por `conectar()`.

    `Cerebro.iniciar()` retoma ou CRIA sessão no gateway — até 12 s de chamadas
    bloqueantes. Usado a cada 20 s, isso segurava o SIGINT e o daemon matava o
    app no limite de 20 s (medido: 2 s antes, 17 s depois).
    """
    import ast
    import inspect

    from garra_reachy_mini import cerebro as mod

    arvore = ast.parse(inspect.getsource(mod.sondar))
    # Olha as CHAMADAS, não o texto: a docstring cita `conectar()` de propósito.
    chamadas = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(arvore) if isinstance(n, ast.Call)
    }
    assert "Cerebro" not in chamadas, "a sonda voltou a construir o cérebro"
    assert "conectar" not in chamadas and "iniciar" not in chamadas
    assert "get" in chamadas, "a sonda precisa fazer o /ping"


def test_supervisor_usa_a_sonda_barata_e_nao_o_cerebro_inteiro():
    import pathlib
    import re

    texto = (pathlib.Path(__file__).parent.parent
             / "garra_reachy_mini" / "main.py").read_text(encoding="utf-8")
    sup = texto[texto.index("def _supervisionar"):texto.index("def _laco_voz")]
    assert "_sondar_cerebro" in sup
    assert "_avaliar_cerebro" not in sup, (
        "o supervisor voltou a construir Cerebro a cada rodada")
    # E o laço de voz só reconstrói quando o cérebro REALMENTE volta.
    laco = texto[texto.index("def _laco_voz"):]
    periodico = laco[laco.index("RECONFERIR_CEREBRO_S"):]
    assert "_sondar_cerebro" in periodico[:700]
