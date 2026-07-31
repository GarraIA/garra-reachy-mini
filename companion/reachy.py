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
    pass


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
    return dados


class ConflitoConversa(RuntimeError):
    def __init__(self, atual: dict) -> None:
        super().__init__("revisão desatualizada")
        self.atual = atual


def _config_atual(painel: str) -> dict:
    r = requests.get(f"{painel}/api/config", timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _gravar(painel: str, valores: dict) -> dict:
    r = requests.post(f"{painel}/api/config", json=valores, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


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


def configurar(painel: str, *, agente: str = "reachy_voice") -> dict:
    """Grava as URLs de LAN no robô, confere pelos olhos dele e desfaz se falhar."""
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
