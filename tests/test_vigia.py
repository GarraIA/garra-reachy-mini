"""O vigia: reaponta o robô quando o computador muda de endereço, e sobe o app
depois que o robô reinicia — sem se meter no que não é dele.

O robô é um duplo de `requests`: cada teste diz o que o daemon e o painel
respondem, e confere o que o vigia fez.
"""

from __future__ import annotations

import pytest
import requests

from companion import reachy, vigia, voz

ROBO = "10.0.0.142"
DAEMON = f"http://{ROBO}:8000"


class Resposta:
    def __init__(self, corpo, status: int = 200) -> None:
        self._corpo, self.status_code = corpo, status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._corpo


class Robo:
    """O daemon e o painel do robô, vistos por HTTP."""

    def __init__(self, *, no_ar: bool = True, app: str | None = None,
                 instalado: bool = True, aponta_para: str = "10.0.0.24") -> None:
        self.no_ar, self.app, self.instalado, self.aponta_para = no_ar, app, instalado, aponta_para
        self.iniciados: list[str] = []

    def get(self, url, timeout=None):
        if not self.no_ar or ROBO not in url:
            raise requests.ConnectionError("fora do ar")
        if url.endswith("/api/daemon/status"):
            return Resposta({"state": "running", "version": "1.11.0", "wlan_ip": ROBO})
        if url.endswith("/api/apps/current-app-status"):
            return Resposta({"info": {"name": self.app}, "state": "running"} if self.app else None)
        if url.endswith("/api/apps/list-available/installed"):
            return Resposta([{"name": vigia.APP}] if self.instalado else [])
        if url.endswith("/api/config"):
            return Resposta({"efetiva": {"gateway_url": f"http://{self.aponta_para}:8126",
                                         "voz_url": f"http://{self.aponta_para}:8123"}})
        raise AssertionError(f"rota inesperada: {url}")

    def post(self, url, timeout=None):
        self.iniciados.append(url)
        self.app = vigia.APP
        return Resposta({"state": "starting"})


@pytest.fixture()
def cena(tmp_path, monkeypatch):
    monkeypatch.setenv("GARRA_REACHY_CONF", str(tmp_path))
    monkeypatch.setenv("GARRA_ROBO_HOST", ROBO)
    monkeypatch.setattr(voz, "ip_lan", lambda: "10.0.0.24")
    monkeypatch.setattr(reachy, "_painel", lambda host: f"http://{host}:8042")
    regravados: list[str] = []

    def configurar(painel, **kw):
        regravados.append(painel)
        cena.pedidos.append(kw)
        return {"ok": cena.robo_confirma}

    monkeypatch.setattr(reachy, "configurar", configurar)

    def montar(robo: Robo) -> vigia.Vigia:
        return vigia.Vigia(http=robo)

    cena = montar
    cena.regravados = regravados
    cena.pedidos = []
    cena.robo_confirma = True
    return cena


def test_robo_que_reaparece_sem_app_ganha_o_app_uma_vez(cena) -> None:
    robo = Robo(app=None)
    v = cena(robo)
    assert v.uma_volta()["status"] == "app_started"
    assert robo.iniciados == [f"{DAEMON}/api/apps/start-app/{vigia.APP}"]
    assert v.uma_volta()["status"] == "ok"
    assert len(robo.iniciados) == 1


def test_app_parado_por_alguem_fica_parado(cena) -> None:
    """Quem parou o app sabia o que estava fazendo: o vigia não o religa."""
    robo = Robo(app=vigia.APP)
    v = cena(robo)
    v.uma_volta()
    robo.app = None
    assert v.uma_volta()["status"] == "app_not_running"
    assert v.uma_volta()["status"] == "app_not_running"
    assert robo.iniciados == []


def test_depois_de_sumir_e_voltar_o_app_e_iniciado_de_novo(cena) -> None:
    robo = Robo(app=vigia.APP)
    v = cena(robo)
    v.uma_volta()
    robo.no_ar = False
    assert v.uma_volta()["status"] == "robot_unreachable"
    assert v.uma_volta()["status"] == "robot_unreachable"
    robo.no_ar, robo.app = True, None            # voltou de um reinício
    assert v.uma_volta()["status"] == "app_started"


def test_um_soluco_da_rede_nao_religa_o_app_que_o_dono_parou(cena) -> None:
    """Uma volta sem resposta é o Wi-Fi piscando, não o robô reiniciando."""
    robo = Robo(app=vigia.APP)
    v = cena(robo)
    v.uma_volta()
    robo.app = None                               # o dono parou o app
    assert v.uma_volta()["status"] == "app_not_running"
    robo.no_ar = False
    assert v.uma_volta()["status"] == "robot_unreachable"
    robo.no_ar = True
    assert v.uma_volta()["status"] == "app_not_running"
    assert robo.iniciados == []


def test_outro_app_rodando_nao_e_tocado(cena) -> None:
    robo = Robo(app="reachy_mini_conversation_app", aponta_para="1.2.3.4")
    v = cena(robo)
    assert v.uma_volta()["status"] == "other_app_running"
    assert robo.iniciados == [] and cena.regravados == []


def test_app_que_nao_esta_instalado_nao_e_iniciado(cena) -> None:
    robo = Robo(app=None, instalado=False)
    assert cena(robo).uma_volta()["status"] == "app_not_running"
    assert robo.iniciados == []


def test_enderecos_certos_ninguem_regrava(cena) -> None:
    assert cena(Robo(app=vigia.APP)).uma_volta()["status"] == "ok"
    assert cena.regravados == []


def test_computador_mudou_de_ip_o_robo_e_reapontado(cena) -> None:
    """O defeito de novembro: o robô procurando o computador no endereço antigo."""
    v = cena(Robo(app=vigia.APP, aponta_para="192.168.1.78"))
    assert v.uma_volta()["status"] == "address_updated"
    assert cena.regravados == [f"http://{ROBO}:8042"]
    # Sozinho, o vigia só reaponta: o token não sai deste computador por ele.
    assert cena.pedidos == [{"so_enderecos": True}]


def test_regravacao_que_o_robo_nao_confirma_espera_antes_de_insistir(cena) -> None:
    """Firewall fechado: o robô não alcança o computador novo. Insistir a cada
    volta só encheria o log — e a configuração anterior já foi restaurada."""
    cena.robo_confirma = False
    v = cena(Robo(app=vigia.APP, aponta_para="192.168.1.78"))
    assert v.uma_volta()["status"] == "address_update_rolled_back"
    assert v.uma_volta()["status"] == "address_mismatch_waiting"
    assert len(cena.regravados) == 1


def test_sem_ip_de_rede_o_vigia_nao_grava_loopback_no_robo(cena, monkeypatch) -> None:
    monkeypatch.setattr(voz, "ip_lan", lambda: "127.0.0.1")
    v = cena(Robo(app=vigia.APP, aponta_para="10.0.0.24"))
    assert v.uma_volta()["status"] == "ok" and cena.regravados == []


def test_o_ultimo_endereco_do_robo_e_lembrado(cena, tmp_path, monkeypatch) -> None:
    cena(Robo(app=vigia.APP)).uma_volta()
    monkeypatch.delenv("GARRA_ROBO_HOST")
    assert vigia.Vigia()._candidatos()[0] == ROBO
    assert (tmp_path / "robo.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("valor, esperado", [("0", False), ("false", False), ("off", False),
                                             ("1", True), ("", True), (None, True)])
def test_o_vigia_se_desliga_pelo_ambiente(valor, esperado) -> None:
    assert vigia.ligado({} if valor is None else {"GARRA_VIGIA": valor}) is esperado


# ─── o vigia não confia em quem responde ─────────────────────────────────────
class Impostor(Robo):
    """Responde como daemon, e diz que o robô mora em outro lugar."""

    def __init__(self, diz_que_mora_em: str, **kw) -> None:
        super().__init__(**kw)
        self.diz, self.vistos = diz_que_mora_em, []

    def get(self, url, timeout=None):
        self.vistos.append(url)
        if url.endswith("/api/daemon/status") and ROBO in url:
            return Resposta({"state": "x", "version": "9", "wlan_ip": self.diz})
        return super().get(url, timeout)

    def post(self, url, timeout=None):
        self.vistos.append(url)
        return super().post(url, timeout)


@pytest.mark.parametrize("alvo", ["203.0.113.9", "evil.example", "127.0.0.1"])
def test_o_endereco_que_a_resposta_diz_nao_e_seguido(cena, tmp_path, alvo) -> None:
    """Achado alto da revisão: `wlan_ip` mandava o vigia (e o token) a qualquer host."""
    falso = Impostor(alvo, app=vigia.APP, aponta_para="192.168.1.78")
    v = cena(falso)
    estado = v.uma_volta()
    assert estado["robot"] == ROBO
    assert all(alvo not in url for url in falso.vistos)
    assert cena.regravados == [f"http://{ROBO}:8042"]
    assert alvo not in (tmp_path / "robo.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("host", ["203.0.113.9", "8.8.8.8", "0.0.0.0", "224.0.0.1"])
def test_so_endereco_privado_e_procurado(cena, monkeypatch, host) -> None:
    monkeypatch.setenv("GARRA_ROBO_HOST", host)
    monkeypatch.setattr(vigia.socket, "gethostbyname",
                        lambda _n: (_ for _ in ()).throw(OSError("sem mDNS")))
    robo = Impostor("10.0.0.5")
    assert cena(robo).uma_volta()["status"] == "robot_unreachable"
    assert robo.vistos == []


def test_nome_que_resolve_para_fora_da_casa_nao_e_procurado(cena, monkeypatch) -> None:
    monkeypatch.setenv("GARRA_ROBO_HOST", "reachy-mini.local")
    monkeypatch.setattr(vigia.socket, "gethostbyname", lambda _n: "203.0.113.9")
    robo = Impostor("10.0.0.5")
    assert cena(robo).uma_volta()["status"] == "robot_unreachable"
    assert robo.vistos == []


def test_o_nome_e_resolvido_aqui_e_a_conversa_e_com_o_numero(cena, monkeypatch) -> None:
    monkeypatch.setenv("GARRA_ROBO_HOST", "reachy-mini.local")
    monkeypatch.setattr(vigia.socket, "gethostbyname", lambda _n: ROBO)
    estado = cena(Robo(app=vigia.APP)).uma_volta()
    assert estado["status"] == "ok" and estado["robot"] == ROBO


@pytest.mark.parametrize("gateway, voz_", [
    ("http://meu-pc.local:8126", "http://meu-pc.local:8123"),     # nome, não IP
    ("http://10.0.0.24:8126", "http://10.0.0.99:8123"),           # voz em outra máquina
])
def test_configuracao_feita_de_proposito_nao_e_sobrescrita(cena, gateway, voz_) -> None:
    class Proposital(Robo):
        def get(self, url, timeout=None):
            if url.endswith("/api/config"):
                return Resposta({"efetiva": {"gateway_url": gateway, "voz_url": voz_}})
            return super().get(url, timeout)

    assert cena(Proposital(app=vigia.APP)).uma_volta()["status"] == "custom_addresses"
    assert cena.regravados == []


def test_outro_endereco_deste_computador_nao_e_trocado(cena, monkeypatch) -> None:
    """Cabo e Wi-Fi, ou VPN: `ip_lan` muda de ideia, e o robô estava certo."""
    monkeypatch.setattr(vigia, "_e_deste_computador", lambda ip: ip == "192.168.1.78")
    v = cena(Robo(app=vigia.APP, aponta_para="192.168.1.78"))
    assert v.uma_volta()["status"] == "custom_addresses" and cena.regravados == []


def test_reapontar_nao_reenvia_o_token(monkeypatch, tmp_path) -> None:
    """`reachy.configurar(so_enderecos=True)` grava as duas URLs e mais nada."""
    monkeypatch.setenv("GARRA_REACHY_CONF", str(tmp_path))
    monkeypatch.setattr(voz, "ip_lan", lambda: "10.0.0.24")
    monkeypatch.setattr(voz, "token", lambda: "SEGREDO-DO-TESTE")
    gravados: list[dict] = []
    monkeypatch.setattr(reachy, "_config_atual", lambda painel: {
        "efetiva": {"gateway_url": "http://192.168.1.78:8126"},
        "salva": {"gateway_url": "http://192.168.1.78:8126",
                  "voz_url": "http://192.168.1.78:8123", "voz_token": "x", "gateway_key": "x"}})
    monkeypatch.setattr(reachy, "_gravar", lambda painel, valores: gravados.append(dict(valores)))
    monkeypatch.setattr(reachy, "_esperar_alcancar", lambda *a, **k: (False, {}))
    relatorio = reachy.configurar("http://10.0.0.142:8042", so_enderecos=True)
    assert relatorio["rolled_back"] is True and len(gravados) == 2
    for valores in gravados:                      # a gravação e o desfazer
        assert set(valores) == {"gateway_url", "voz_url"}
        assert "SEGREDO-DO-TESTE" not in str(valores)
    assert "SEGREDO-DO-TESTE" not in str(relatorio)
