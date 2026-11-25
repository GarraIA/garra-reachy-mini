"""Descobrir o robô e gravar nele as URLs do desktop — com rollback.

O app instalado no Reachy nasce com `GARRA_GATEWAY_URL` e `GARRA_VOZ_URL`
apontando para `127.0.0.1`, que **dentro do robô é o próprio robô**. É a causa
raiz do `Voice: not reachable` / `Conversation: not configured`: nunca houve
nada errado com os serviços, eles só estavam sendo procurados no lugar errado.

Gravar não basta: só o robô pode dizer se alcançou o desktop. Por isso o fluxo
é transacional — guarda a configuração anterior, grava a nova, espera o robô
reavaliar, confere pelos olhos dele e **desfaz** se não funcionar. Sem isso um
IP errado deixaria o robô pior do que estava, sem ninguém perceber.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import requests

from . import voz

PORTAS_PAINEL = (8042, 8043, 8044, 8045, 8046)
# Mesmos nomes que `main.py` trata como segredo, pela mesma razão: nada disto
# pode voltar num corpo de resposta que o navegador vai ler.
SEGREDOS = ("gateway_key", "voz_token")
MASCARA = "***"
TIMEOUT_S = 8.0
# Quanto esperamos o robô reavaliar depois de gravar. O supervisor é acordado
# pelo próprio POST, então isto é folga, não expectativa.
ESPERA_APLICAR_S = 12.0


class ErroReachy(RuntimeError):
    """`codigo` diz o tipo quando ele muda o que fazer (ex.: `config_locked`)."""

    def __init__(self, mensagem: str = "", codigo: str | None = None) -> None:
        super().__init__(mensagem)
        self.codigo = codigo


def _painel(endereco: str) -> str | None:
    """Qual porta desse host serve o NOSSO painel (e não o proxy da Pollen)."""
    for porta in PORTAS_PAINEL:
        base = f"http://{endereco}:{porta}"
        try:
            r = requests.get(f"{base}/api/robot/status", timeout=1.5)
            if r.ok and "controller_state" in r.json():
                return base
        except (requests.RequestException, ValueError):
            continue
    return None


def procurar(timeout: float = 5.0) -> list[dict]:
    """Robôs na rede, pelo mDNS do próprio SDK, mais o nome mDNS padrão.

    O SDK já publica `_reachy-mini._tcp.local.` de dentro do daemon do robô e
    traz um `find_robots()` pronto — não há motivo para escrever outro browser.
    """
    achados: dict[str, dict] = {}
    try:
        from reachy_mini.utils.discovery import find_robots

        for robo in find_robots(timeout=timeout):
            endereco = getattr(robo, "address", None) or getattr(robo, "host", None)
            if not endereco:
                continue
            achados[str(endereco)] = {
                "name": getattr(robo, "name", None),
                "address": str(endereco),
                "unit_id": getattr(robo, "unit_id", None),
                "source": "mdns",
            }
    except Exception:  # zeroconf indisponível, rede sem multicast, etc.
        pass

    if not achados:
        # Rede que bloqueia multicast ainda costuma resolver o nome .local.
        achados["reachy-mini.local"] = {
            "name": "reachy-mini", "address": "reachy-mini.local",
            "unit_id": None, "source": "hostname",
        }

    saida = []
    for dados in achados.values():
        base = _painel(dados["address"])
        dados["panel"] = base
        dados["app_running"] = base is not None
        if base:
            try:
                dados["status"] = requests.get(f"{base}/api/robot/status", timeout=3).json()
                dados["config"] = requests.get(f"{base}/api/config", timeout=3).json()
            except (requests.RequestException, ValueError):
                pass
        saida.append(dados)
    return saida


def conversa_ler(painel: str) -> dict:
    """Lê o ritmo da conversa NO ROBÔ. Sem cópia aqui, de propósito.

    Guardar uma segunda cópia no desktop criaria duas verdades e um laço de
    sincronização; e o comportamento roda no robô, então o arquivo dele é a
    fonte. Com o robô fora do ar o painel mostra indisponível — não uma fila de
    mudanças pendentes que ninguém sabe quando (nem se) serão aplicadas.
    """
    r = requests.get(f"{painel}/api/robot/conversation", timeout=TIMEOUT_S)
    _erguer_se_nao_suportado(r, painel)
    r.raise_for_status()
    return r.json()


def conversa_gravar(painel: str, mudancas: dict) -> dict:
    """Grava e **relê**: só o robô pode dizer o que ficou salvo.

    Um 200 diz que o HTTP chegou, não que o valor sobreviveu à normalização do
    robô. O 409 (revisão desatualizada) sobe intacto para o painel decidir.
    """
    r = requests.put(f"{painel}/api/robot/conversation", json=mudancas,
                     timeout=TIMEOUT_S)
    if r.status_code == 409:
        raise ConflitoConversa(r.json())
    _erguer_se_nao_suportado(r, painel)
    r.raise_for_status()
    return conversa_ler(painel)


def conversa_estado(painel: str) -> dict:
    """Ritmo em vigor mais as últimas medições de turno, do barramento do robô."""
    dados = conversa_ler(painel)
    try:
        r = requests.get(f"{painel}/api/robot/events",
                         params={"limite": 60, "tipos": "voice.turn.completed"},
                         timeout=TIMEOUT_S)
        eventos = r.json().get("events", []) if r.ok else []
    except (requests.RequestException, ValueError):
        eventos = []
    dados["turns"] = eventos[-10:]
    dados["robot"] = identidade(painel)
    return dados


class ConflitoConversa(RuntimeError):
    def __init__(self, atual: dict) -> None:
        super().__init__("revisão desatualizada")
        self.atual = atual


class RecursoNaoSuportado(RuntimeError):
    """O robô respondeu, mas esta versão dele não conhece a rota.

    Separado de "robô fora do ar" porque a resposta ao usuário é oposta: aqui
    não adianta reconectar nem tentar de novo — o app instalado é mais antigo
    que o recurso, e a única saída é atualizá-lo.
    """

    def __init__(self, status: int, versao: str | None = None) -> None:
        super().__init__(f"o app do robô não expõe esta rota (HTTP {status})")
        self.upstream_status = status
        self.robot_version = versao


def identidade(painel: str) -> dict:
    """Quem é o app do robô e o que ele sabe fazer. `{}` se ele não disser.

    Um app anterior ao `build_info` não devolve nada disto — e devolver `{}` é
    a resposta certa, não um erro: o painel decide por capacidade e trata a
    ausência como "não sei", que é a verdade.
    """
    try:
        r = requests.get(f"{painel}/api/robot/status", timeout=TIMEOUT_S)
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError):
        return {}
    return {c: d.get(c) for c in ("app_id", "version", "channel", "commit",
                                  "api_version", "capabilities")
            if d.get(c) is not None}


def _erguer_se_nao_suportado(r: requests.Response, painel: str) -> None:
    """404/405 do robô é versão antiga, não robô inacessível."""
    if r.status_code in (404, 405):
        raise RecursoNaoSuportado(r.status_code,
                                  identidade(painel).get("version"))


def _config_atual(painel: str) -> dict:
    r = requests.get(f"{painel}/api/config", timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


CABECALHO_ASSINATURA = "X-Garra-Assinatura"


def _assinatura(segredo: str, nonce: str, valores: dict) -> str:
    """HMAC do desafio do robô e do pedido. Gêmea de `garra_reachy_mini/web/trava.py`
    (o companion não importa o pacote do robô); um teste compara as duas."""
    corpo = json.dumps(valores, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    mensagem = f"garra-config-v1\n{nonce}\n{corpo}".encode("utf-8")
    return hmac.new(segredo.encode("utf-8"), mensagem, hashlib.sha256).hexdigest()


def _desafio(painel: str) -> str | None:
    """Desafio de uso único do robô; `None` quando o app dele é anterior à trava."""
    r = requests.get(f"{painel}/api/config/desafio", timeout=TIMEOUT_S)
    if r.status_code in (404, 405):
        return None
    r.raise_for_status()
    nonce = r.json().get("nonce")
    return nonce if isinstance(nonce, str) and nonce else None


def _gravar(painel: str, valores: dict) -> dict:
    """Grava no robô, assinando: depois de pareado ele só troca de endereço para
    quem prova conhecer a credencial. O que sai daqui é o HMAC, nunca o token."""
    cabecalhos = {}
    nonce = _desafio(painel)
    if nonce:
        cabecalhos[CABECALHO_ASSINATURA] = f"v1 {nonce} {_assinatura(voz.token(), nonce, valores)}"
    r = requests.post(f"{painel}/api/config", json=valores, headers=cabecalhos,
                      timeout=TIMEOUT_S)
    if r.status_code == 403 and _codigo(r) == "config_locked":
        raise ErroReachy(
            "o robô está pareado com outra credencial e recusou a mudança. Para parear "
            "de novo, entre no robô por SSH e desligue a trava dali: "
            "curl -X POST http://127.0.0.1:8042/api/config -H 'Content-Type: application/json' "
            "-d '{\"trava_pc\": null}'", codigo="config_locked")
    r.raise_for_status()
    return r.json()


def destravar(painel: str) -> dict:
    """Desliga a trava do robô, com a assinatura deste computador.

    É o passo ANTES de voltar o computador para um código que não assina (o
    1.5.0), de trocar de computador ou de apagar `voz.token`: depois disso o
    robô não teria mais quem assinasse por ele, e a única saída seria o SSH.
    """
    _gravar(painel, {"trava_pc": None})
    return {"travada": bool(_config_atual(painel).get("travada"))}


def painel_lembrado() -> str | None:
    """O painel do robô que o vigia lembra (ou `GARRA_ROBO_HOST`)."""
    import json
    import os

    host = os.environ.get("GARRA_ROBO_HOST")
    if not host:
        try:
            host = json.loads((voz.pasta_conf() / "robo.json").read_text(encoding="utf-8")).get("host")
        except (OSError, ValueError, AttributeError):
            host = None
    return _painel(host) if isinstance(host, str) and host else None


def main(argv: list[str] | None = None) -> int:
    """`python -m companion.reachy estado | destravar [--painel URL]`"""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m companion.reachy",
                                     description="O robô pareado com este computador.")
    parser.add_argument("comando", choices=("estado", "destravar"))
    parser.add_argument("--painel", help="ex.: http://10.0.0.142:8042 (padrão: o que o vigia lembra)")
    args = parser.parse_args(argv)
    painel = args.painel or painel_lembrado()
    if not painel:
        print("não achei o robô; passe --painel http://<ip-do-robo>:8042")
        return 2
    try:
        if args.comando == "destravar":
            r = destravar(painel)
            print(f"{painel}: travado: {'sim' if r['travada'] else 'não'}")
            return 0 if not r["travada"] else 1
        atual = _config_atual(painel)
        efetiva = atual.get("efetiva") or {}
        print(f"{painel}: travado: {'sim' if atual.get('travada') else 'não'} · "
              f"voz: {efetiva.get('voz_url')} · ponte: {efetiva.get('gateway_url')}")
        return 0
    except (ErroReachy, requests.RequestException) as erro:
        print(f"{painel}: {erro}")
        return 1


def _codigo(r: requests.Response) -> str | None:
    try:
        erro = r.json().get("error")
    except ValueError:
        return None
    return erro.get("code") if isinstance(erro, dict) else None


def _servicos(painel: str) -> dict:
    r = requests.get(f"{painel}/api/robot/status", timeout=TIMEOUT_S)
    r.raise_for_status()
    d = r.json()
    return {s["name"]: s for s in d.get("services", [])}


def _esperar_alcancar(painel: str, esperados: tuple[str, ...],
                      limite_s: float) -> tuple[bool, dict]:
    """Espera o ROBÔ dizer que alcançou os serviços. É a prova no sentido certo."""
    fim = time.monotonic() + limite_s
    servicos: dict = {}
    while time.monotonic() < fim:
        try:
            servicos = _servicos(painel)
        except requests.RequestException:
            time.sleep(1.0)
            continue
        if all(servicos.get(n, {}).get("available") for n in esperados):
            return True, servicos
        time.sleep(1.0)
    return False, servicos


def configurar(painel: str, *, agente: str = "reachy_voice",
               so_enderecos: bool = False) -> dict:
    """Grava as URLs de LAN no robô, confere pelos olhos dele e desfaz se falhar.

    `so_enderecos`: grava SÓ as duas URLs, sem token nem agente. É como o vigia
    chama — ele age sozinho, sem ninguém olhando, sobre o que respondeu na rede
    como robô. Um robô de verdade já tem o token (quem o deu foi o dono, por
    esta mesma função, na tela); um aparelho que só se passa por robô não o
    recebe.
    """
    ip = voz.ip_lan()
    if ip.startswith("127."):
        raise ErroReachy(
            "não achei o IP de LAN deste desktop (só loopback). O robô não tem "
            "como alcançar 127.0.0.1 — verifique a rede.")

    from . import ponte

    # O robô fala com a PONTE, não com o gateway. O gateway responde 201 sem
    # autenticação e o agente padrão dele tem `bash` — abri-lo na rede seria
    # entregar um shell. A ponte expõe quatro rotas, exige token e fixa o
    # agente. Ver companion/ponte.py.
    novo = {
        "gateway_url": f"http://{ip}:{ponte.PORTA}",
        "voz_url": f"http://{ip}:{voz.PORTA_VOZ}",
        "agent_id": agente,
        "voz_token": voz.token(),
        "gateway_key": voz.token(),
        # Pareado: daqui em diante o robô só troca de endereço ou de credencial
        # com a assinatura deste computador (ver `_gravar`).
        "trava_pc": True,
    }
    anterior_bruto = _config_atual(painel)
    efetiva = anterior_bruto.get("efetiva", {})
    salva = anterior_bruto.get("salva", {})
    # Só devolvemos ao estado ANTERIOR o que estava salvo; o que vinha de padrão
    # volta a vir de padrão (string vazia limpa a chave no app).
    # Inclui os tokens: um rollback que deixasse credencial para trás apontando
    # para um desktop inalcançável seria pior do que não ter gravado nada.
    anterior = {c: salva.get(c, "")
                for c in ("gateway_url", "voz_url", "agent_id",
                          "voz_token", "gateway_key")}
    anterior["trava_pc"] = salva.get("trava_pc") or None
    if so_enderecos:
        novo = {c: novo[c] for c in ("gateway_url", "voz_url")}
        anterior = {c: anterior[c] for c in ("gateway_url", "voz_url")}

    relatorio = {
        "target": painel,
        "desktop_ip": ip,
        # Lista de segredos, não um nome só. `gateway_key` carrega EXATAMENTE o
        # mesmo valor que `voz_token` (acima), então excluir apenas um deixava a
        # credencial sair em claro no corpo da resposta e chegar ao navegador.
        # `main.py` já trata os dois como segredo; aqui faltava.
        "applied": {c: (MASCARA if c in SEGREDOS and v else v)
                    for c, v in novo.items()},
        "previous": {"gateway_url": efetiva.get("gateway_url"),
                     "voz_url": efetiva.get("voz_url"),
                     "agent_id": efetiva.get("agent_id")},
        "rolled_back": False,
    }

    _gravar(painel, novo)
    lido = _config_atual(painel).get("efetiva", {})
    relatorio["read_back"] = {c: lido.get(c) for c in ("gateway_url", "voz_url", "agent_id")}

    alcancou, servicos = _esperar_alcancar(painel, ("voice", "gateway"), ESPERA_APLICAR_S)
    relatorio["services_seen_by_robot"] = {
        n: {"available": s.get("available"), "reason_code": s.get("reason_code")}
        for n, s in servicos.items()
    }
    relatorio["ok"] = alcancou

    if not alcancou:
        _gravar(painel, anterior)
        relatorio["rolled_back"] = True
        relatorio["error"] = (
            "o robô não alcançou o desktop com as URLs novas; configuração "
            "anterior restaurada. Confira se a voz está no ar e se o firewall "
            f"deixa o robô chegar em {ip}:{voz.PORTA_VOZ} e {ip}:{ponte.PORTA}.")
    return relatorio


if __name__ == "__main__":
    raise SystemExit(main())
