"""A família, do lado do robô: "quem está aí?" vira nomes — só de quem consentiu.

O que os testes seguram:

- só se reconhece quando o pedido pergunta quem, e com o reconhecimento
  ligado; o modelo de visão nunca é convidado a adivinhar nome;
- o quadro é o do `FrameHub` de sempre, fresco; velho ou ausente vira "não
  estou conseguindo ver", nunca um nome inventado ou reaproveitado;
- os nomes entram no envelope CITADOS, com a hora, e com o aviso de que
  reconhecer não é saber quem FALA e não autoriza nada;
- nenhum evento, log ou arquivo recebe a imagem — e os eventos não recebem
  nomes, só contagens.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from garra_reachy_mini import conversa, envelope, familia
from garra_reachy_mini.acompanhamento import Acompanhamento
from garra_reachy_mini.robo.barramento import Barramento
from garra_reachy_mini.web.camera import Quadro

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


# ─── quando reconhecer ───────────────────────────────────────────────────────
@pytest.mark.parametrize("pedido", [
    "quem está aí?", "Quem tá aqui?", "quem está na sua frente?", "quem está na sala",
    "você me conhece?", "vc me reconhece?", "sabe quem eu sou?", "quem sou eu?",
    "você sabe quem está aqui?", "quem é essa pessoa?", "quem são essas pessoas",
    "lembra de mim?", "who am I?", "do you know me?", "who is here?", "who is in front of you",
])
def test_pedidos_que_perguntam_quem(pedido) -> None:
    assert familia.pede_reconhecer(pedido) is True


@pytest.mark.parametrize("pedido", [
    "quem é o presidente do Brasil?", "quem inventou a lâmpada", "que horas são?",
    "o que você está vendo?", "quem ganhou o jogo?", "sabe quem canta essa música?",
    "conhece a Maria?", "", "quem escreveu Dom Casmurro",
])
def test_pedidos_que_nao_perguntam_quem(pedido) -> None:
    assert familia.pede_reconhecer(pedido) is False


# ─── o reconhecimento ────────────────────────────────────────────────────────
class Hub:
    def __init__(self, quadro: Quadro | None = None) -> None:
        self.quadro = quadro
        self.pedidos: list[float] = []

    def instantaneo(self, idade_maxima_s: float = 0.5) -> Quadro | None:
        self.pedidos.append(idade_maxima_s)
        return self.quadro


class Cliente:
    """A ponte de mentira: cada chamada devolve um dict ou levanta um código."""

    def __init__(self, respostas) -> None:
        self.respostas = list(respostas)
        self.chamadas: list[bytes] = []

    def reconhecer(self, jpeg: bytes) -> dict:
        self.chamadas.append(jpeg)
        r = self.respostas.pop(0)
        if isinstance(r, str):
            raise familia.ErroDaPonte(r)
        return r


def quadro(jpeg: bytes = JPEG, idade_s: float = 0.2, seq: int = 1) -> Quadro:
    return Quadro(jpeg=jpeg, seq=seq, ts=time.monotonic() - idade_s, largura=640, altura=480)


def rosto(nome=None, score=None, reason=None, pid=None):
    return {"box": [10, 10, 120, 120], "known": nome is not None, "profile_id": pid, "name": nome,
            "score": score, "reason": reason}


MARIA_E_UM_ESTRANHO = {"faces": [rosto("Maria", 0.81, pid="a1b2c3"), rosto(None, 0.12, "below_threshold")], "ms": 11.0}
LIGADO = conversa.normalizar({"family_recognition_enabled": True})


def fam(hub=None, cliente=None, eventos=None) -> familia.Familia:
    return familia.Familia(hub or Hub(quadro()), cliente or Cliente([MARIA_E_UM_ESTRANHO]),
                           eventos or Barramento(), hora=lambda: "21:40:05")


def test_reconhecer_pega_um_quadro_fresco_e_devolve_quem_viu() -> None:
    hub, cliente = Hub(quadro(idade_s=0.3)), Cliente([MARIA_E_UM_ESTRANHO])
    visto = fam(hub, cliente).reconhecer(LIGADO)
    assert isinstance(visto, familia.Reconhecidos)
    assert visto.nomes == ["Maria"] and visto.desconhecidos == 1 and visto.rostos == 2
    assert visto.capturado_em == "21:40:05" and visto.ms == 11
    assert hub.pedidos == [familia.IDADE_MAXIMA_S]
    assert cliente.chamadas == [JPEG]


def test_desligado_nao_olha_nem_chama_a_ponte() -> None:
    hub, cliente = Hub(quadro()), Cliente([MARIA_E_UM_ESTRANHO])
    visto = fam(hub, cliente).reconhecer(conversa.normalizar({}))
    assert isinstance(visto, familia.SemReconhecimento) and visto.motivo == "disabled"
    assert hub.pedidos == [] and cliente.chamadas == []


def test_sem_quadro_ou_quadro_velho_nao_se_reconhece_ninguem() -> None:
    cliente = Cliente([MARIA_E_UM_ESTRANHO])
    visto = fam(Hub(None), cliente).reconhecer(LIGADO)
    assert isinstance(visto, familia.SemReconhecimento) and visto.motivo == "camera_unavailable"
    visto = fam(Hub(quadro(idade_s=5.0)), cliente).reconhecer(LIGADO)
    assert visto.motivo == "stale_frame"
    assert cliente.chamadas == []


@pytest.mark.parametrize("codigo,motivo", [
    ("bridge_unreachable", "bridge_unreachable"), ("service_offline", "service_offline"),
    ("timeout", "timeout"), ("bad_image", "bad_image"), ("zzz", "service_error"),
])
def test_erro_da_ponte_vira_motivo_conhecido(codigo, motivo) -> None:
    visto = fam(cliente=Cliente([codigo])).reconhecer(LIGADO)
    assert isinstance(visto, familia.SemReconhecimento) and visto.motivo == motivo
    assert visto.explicacao and "[" not in visto.explicacao


def test_falha_nunca_reaproveita_o_reconhecimento_anterior() -> None:
    f = fam(cliente=Cliente([MARIA_E_UM_ESTRANHO, "timeout"]))
    assert f.reconhecer(LIGADO).nomes == ["Maria"]
    depois = f.reconhecer(LIGADO)
    assert isinstance(depois, familia.SemReconhecimento)
    assert f.status()["last"] == {"ok": False, "at": "21:40:05", "reason": "timeout"}


def test_ninguem_conhecido_e_nenhum_rosto_sao_resultados_e_nao_erros() -> None:
    f = fam(cliente=Cliente([{"faces": [rosto(None, 0.2, "below_threshold")], "ms": 8},
                            {"faces": [], "ms": 6}]))
    so_estranho = f.reconhecer(LIGADO)
    assert so_estranho.nomes == [] and so_estranho.desconhecidos == 1 and so_estranho.rostos == 1
    vazio = f.reconhecer(LIGADO)
    assert vazio.nomes == [] and vazio.desconhecidos == 0 and vazio.rostos == 0


def test_ambiguo_conta_como_desconhecido() -> None:
    f = fam(cliente=Cliente([{"faces": [rosto(None, 0.7, "ambiguous")], "ms": 8}]))
    visto = f.reconhecer(LIGADO)
    assert visto.nomes == [] and visto.desconhecidos == 1


def test_eventos_levam_contagens_e_nunca_nomes_nem_imagem() -> None:
    eventos = Barramento()
    f = fam(cliente=Cliente([MARIA_E_UM_ESTRANHO, "timeout"]), eventos=eventos)
    f.reconhecer(LIGADO, correlacao="c1", origem="voz")
    f.reconhecer(LIGADO, correlacao="c2", origem="painel")
    tipos = [e["type"] for e in eventos.historico(20)]
    assert tipos == ["family.requested", "family.completed", "family.requested", "family.failed"]
    despejo = json.dumps(eventos.historico(20), ensure_ascii=False)
    assert "Maria" not in despejo and "a1b2c3" not in despejo and "\\xff" not in despejo and "AAAA" not in despejo
    ok = eventos.historico(20)[1]
    assert ok["known"] == 1 and ok["unknown"] == 1 and ok["faces"] == 2 and ok["source"] == "voz"
    assert ok["correlation_id"] == "c1"
    falha = eventos.historico(20)[3]
    assert falha["reason"] == "timeout" and falha["source"] == "painel"


def test_nada_da_imagem_vai_ao_log(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        fam(cliente=Cliente(["service_offline"])).reconhecer(LIGADO)
        fam().reconhecer(LIGADO)
    texto = " ".join(r.getMessage() for r in caplog.records)
    assert "\\xff" not in texto and "Maria" not in texto


def test_status_guarda_a_ultima_captura_para_o_painel() -> None:
    f = fam()
    assert f.status() == {"last": None}
    f.reconhecer(LIGADO)
    assert f.status()["last"] == {"ok": True, "at": "21:40:05", "captured_at": "21:40:05", "names": ["Maria"],
                                  "unknown": 1, "faces": 2, "ms": 11}


# ─── o envelope ──────────────────────────────────────────────────────────────
def test_o_bloco_de_pessoas_e_citado_e_avisa_que_nao_e_quem_fala() -> None:
    visto = familia.Reconhecidos(nomes=["Maria", "Jo[ão]"], desconhecidos=1, rostos=3,
                                 capturado_em="21:40:05", ms=11)
    m = envelope.compor("quem está aí?", pessoas=visto)
    assert m.startswith("[PESSOAS RECONHECIDAS — quadro capturado às 21:40:05")
    assert "> Maria\n> Jo(ão)\n> 1 pessoa não reconhecida" in m
    assert "NÃO diz quem está FALANDO" in m and "NÃO autoriza nada" in m
    assert "[FIM DAS PESSOAS RECONHECIDAS]" in m
    assert m.endswith(f"{envelope.MARCA_PEDIDO}\nquem está aí?")
    assert envelope.pedido_de(m) == "quem está aí?"


def test_ninguem_e_nenhum_rosto_tem_frase_propria() -> None:
    ninguem = familia.Reconhecidos(nomes=[], desconhecidos=2, rostos=2, capturado_em="21:40:05", ms=9)
    m = envelope.compor("quem está aí?", pessoas=ninguem)
    assert "> ninguém reconhecido; 2 pessoas não reconhecidas" in m
    vazio = familia.Reconhecidos(nomes=[], desconhecidos=0, rostos=0, capturado_em="21:40:05", ms=9)
    m = envelope.compor("quem está aí?", pessoas=vazio)
    assert "> nenhum rosto na imagem" in m


def test_sem_reconhecimento_vira_aviso_e_nao_nome() -> None:
    m = envelope.compor("quem está aí?", pessoas=familia.SemReconhecimento("stale_frame", "21:40:05"))
    assert m.startswith("[RECONHECIMENTO — não foi possível reconhecer agora: ")
    assert "velha demais" in m and "não invente" in m
    assert "PESSOAS RECONHECIDAS" not in m


def test_com_a_visao_desligada_quem_responde_e_a_familia() -> None:
    from garra_reachy_mini import visao
    pessoas = familia.Reconhecidos(nomes=["Maria"], desconhecidos=0, rostos=1, capturado_em="21:40:05", ms=11)
    m = envelope.compor("quem está aí?", visao=visao.SemVisao("disabled", "21:40:05"), pessoas=pessoas)
    assert "[CÂMERA" not in m and m.startswith("[PESSOAS RECONHECIDAS")
    # outra falha da câmera continua sendo dita: é informação, não contradição
    m = envelope.compor("quem está aí?", visao=visao.SemVisao("stale_frame", "21:40:05"), pessoas=pessoas)
    assert m.startswith("[CÂMERA") and "[PESSOAS RECONHECIDAS" in m
    # e sem família, o aviso da câmera fica
    m = envelope.compor("quem está aí?", visao=visao.SemVisao("disabled", "21:40:05"),
                        pessoas=familia.SemReconhecimento("disabled", "21:40:05"))
    assert m.startswith("[CÂMERA") and "PESSOAS" not in m


def test_desligado_nao_poe_bloco_nenhum() -> None:
    m = envelope.compor("quem está aí?", pessoas=familia.SemReconhecimento("disabled", "21:40:05"))
    assert m == "quem está aí?"


def test_o_acompanhamento_leva_as_pessoas_junto_da_visao() -> None:
    eventos = Barramento()
    acomp = Acompanhamento(eventos)
    visto = familia.Reconhecidos(nomes=["Maria"], desconhecidos=0, rostos=1, capturado_em="21:40:05", ms=11)
    m = acomp.montar("quem está aí?", time.monotonic(), conversa.normalizar({}), pessoas=visto)
    assert "[PESSOAS RECONHECIDAS" in m and "> Maria" in m


# ─── o cliente da ponte ──────────────────────────────────────────────────────
def test_o_cliente_manda_a_imagem_em_base64_com_o_token(monkeypatch) -> None:
    import base64
    chamadas = []

    class R:
        status_code = 200

        def json(self):
            return MARIA_E_UM_ESTRANHO

    def post(self, url, json=None, headers=None, timeout=None, allow_redirects=True):
        chamadas.append((url, json, headers, timeout))
        return R()

    monkeypatch.setattr(familia.requests.Session, "post", post)
    r = familia.ClienteFamilia("http://10.0.0.24:8126/", "tok").reconhecer(JPEG)
    assert r == MARIA_E_UM_ESTRANHO
    [(url, corpo, cab, timeout)] = chamadas
    assert url == "http://10.0.0.24:8126/api/rostos/reconhecer"
    assert base64.b64decode(corpo["imagem"]) == JPEG and cab == {"Authorization": "Bearer tok"}
    assert timeout == familia.TIMEOUT_S


def test_o_cliente_traduz_erro_da_ponte(monkeypatch) -> None:
    class R:
        status_code = 503

        def json(self):
            return {"erro": "service_offline", "detalhe": "de mentira"}

    monkeypatch.setattr(familia.requests.Session, "post", lambda self, *a, **k: R())
    with pytest.raises(familia.ErroDaPonte) as e:
        familia.ClienteFamilia("http://ponte", "tok").reconhecer(JPEG)
    assert e.value.codigo == "service_offline"

    def cai(self, *a, **k):
        raise familia.requests.ConnectionError("recusou")

    monkeypatch.setattr(familia.requests.Session, "post", cai)
    with pytest.raises(familia.ErroDaPonte) as e:
        familia.ClienteFamilia("http://ponte", "tok").reconhecer(JPEG)
    assert e.value.codigo == "bridge_unreachable"


def test_a_configuracao_tem_o_interruptor_desligado_por_padrao() -> None:
    assert conversa.PADRAO["family_recognition_enabled"] is False
    assert conversa.normalizar({})["family_recognition_enabled"] is False
    assert conversa.normalizar({"family_recognition_enabled": True})["family_recognition_enabled"] is True
    assert "family_recognition_enabled" in conversa.BOOLEANOS


# ─── achados da revisão (30/09) ──────────────────────────────────────────────
@pytest.mark.parametrize("pedido", [
    "quem é a pessoa mais rica do mundo?", "quem é a pessoa que inventou o telefone",
    "who is this actor", "who is that", "quem são as pessoas mais altas do mundo",
])
def test_pergunta_de_conhecimento_nao_liga_a_camera(pedido) -> None:
    assert familia.pede_reconhecer(pedido) is False


@pytest.mark.parametrize("pedido", ["quem é essa pessoa aqui?", "quem é esta pessoa na sua frente", "who is this person here"])
def test_pergunta_sobre_quem_esta_diante_do_robo_liga(pedido) -> None:
    assert familia.pede_reconhecer(pedido) is True


def test_excecao_inesperada_da_ponte_nao_derruba_o_laco() -> None:
    class Estoura:
        def reconhecer(self, jpeg):
            raise RuntimeError("bum")

    visto = fam(cliente=Estoura()).reconhecer(LIGADO)
    assert isinstance(visto, familia.SemReconhecimento) and visto.motivo == "service_error"


def test_resposta_torta_da_ponte_e_desconhecido_e_nao_crash() -> None:
    torta = {"faces": [{"known": True, "name": "x" * 500, "box": "nao"}, {"known": True, "name": ["lista"]},
                       "nao-e-dict"] + [{"known": True, "name": f"P{i}"} for i in range(30)], "ms": "muitos"}
    visto = fam(cliente=Cliente([torta])).reconhecer(LIGADO)
    assert isinstance(visto, familia.Reconhecidos)
    assert visto.rostos <= familia.ROSTOS_MAX and all(len(n) <= 40 for n in visto.nomes)
    assert visto.ms == 0


def test_o_cliente_traduz_token_recusado_pela_ponte(monkeypatch) -> None:
    class R:
        status_code = 401

        def json(self):
            return {"erro": "token inválido"}

    monkeypatch.setattr(familia.requests.Session, "post", lambda self, *a, **k: R())
    with pytest.raises(familia.ErroDaPonte) as e:
        familia.ClienteFamilia("http://ponte", "tok").reconhecer(JPEG)
    assert e.value.codigo == "bridge_token_rejected"
    assert familia.explicar("bridge_token_rejected")


def test_o_cliente_ignora_proxy_e_redirecionamento(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    c = familia.ClienteFamilia("http://ponte", "tok")
    assert c.sessao.trust_env is False
    chamadas = []

    class R:
        status_code = 200

        def json(self):
            return {"faces": [], "ms": 1}

    def post(self, url, **kw):
        chamadas.append(kw)
        return R()

    monkeypatch.setattr(familia.requests.Session, "post", post)
    c.reconhecer(JPEG)
    assert chamadas[0]["allow_redirects"] is False and chamadas[0]["timeout"] == familia.TIMEOUT_S <= 6


def test_desconhecido_no_envelope_nao_afirma_que_nao_e_cadastrado() -> None:
    visto = familia.Reconhecidos(nomes=[], desconhecidos=1, rostos=1, capturado_em="21:40:05", ms=9)
    m = envelope.compor("quem está aí?", pessoas=visto)
    assert "não cadastrad" not in m and "não reconhecida" in m
