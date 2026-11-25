"""O vigia: o robô e este computador se acham sozinhos, sem ninguém abrir tela.

Duas coisas deixavam o robô mudo sem que nada estivesse quebrado, e as duas
aconteceram em novembro de 2026:

* **O computador mudou de endereço.** O app do robô guarda as URLs da voz e da
  ponte com o IP do computador; trocada a rede (ou o IP pelo roteador), ele
  passa a procurar um endereço onde não há ninguém.
* **O robô reiniciou.** O daemon oficial sobe dormindo e só lança o app num
  toque de antena — então, depois de uma queda de luz, "Fala Garra" não tem
  quem ouça.

O vigia olha de tempos em tempos e conserta o que é dele consertar:

1. robô no ar, sem app nenhum rodando, e ele acabou de (re)aparecer → inicia o
   app do Garra, uma vez. Se alguém parar o app depois, ele fica parado: quem
   parou sabia o que estava fazendo;
2. app do Garra rodando e apontando para um endereço que não é mais o deste
   computador → regrava as URLs (`reachy.configurar`, que confere pelos olhos
   do robô e desfaz se não funcionar).

Ele nunca mexe num robô que está rodando OUTRO app, nunca reinicia nada e nunca
grava configuração sem o robô confirmar que alcançou o computador.

**O vigia age sozinho, e por isso não confia em quem responde.** Qualquer
aparelho da rede pode responder na porta do daemon dizendo que é o robô:

* só fala com endereço IPv4 PRIVADO, e o endereço que vale é o que de fato
  respondeu — nunca um que a resposta diga (`wlan_ip`);
* ao reapontar, grava só as duas URLs. O token não sai daqui pelo vigia: um
  robô de verdade já o tem, e um impostor ficaria só com o endereço deste
  computador, que ele já conhecia;
* não mexe em configuração feita de propósito (nome em vez de IP, voz e ponte
  em máquinas diferentes, ou outro endereço que também é deste computador).

Desligar: `GARRA_VIGIA=0` em `~/.config/garra-reachy/companion.env`.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from . import reachy, voz
from .agentes.base import da_rede_da_casa

log = logging.getLogger("companion.vigia")

APP = "garra_reachy_mini"
INTERVALO_S = 30.0
# Depois de uma regravação que o robô não confirmou, espera antes de tentar de
# novo: o motivo costuma ser o firewall, e insistir a cada volta só enche o log.
ESPERA_APOS_FALHA_S = 600.0
PORTA_DAEMON = 8000
TIMEOUT_S = 3.0
# Quantas voltas seguidas sem resposta contam como "o robô sumiu". Uma só é um
# soluço do Wi-Fi — e tratar soluço como reinício religaria o app que o dono
# acabou de parar.
FALTAS_PARA_SUMIR = 2


def _ipv4(texto: object) -> ipaddress.IPv4Address | None:
    try:
        ip = ipaddress.ip_address(str(texto))
    except ValueError:
        return None
    return ip if ip.version == 4 else None


def _resolver(host: str) -> str | None:
    """O IPv4 de `host` (nome ou número) se for da rede da casa; senão `None`."""
    ip = _ipv4(host)
    if ip is None:
        try:
            ip = _ipv4(socket.gethostbyname(host))
        except (OSError, UnicodeError):
            return None
    # Rede local, ou este próprio computador (o simulador do robô roda aqui).
    if ip is None or not (da_rede_da_casa(ip) or ip.is_loopback):
        return None
    return str(ip)


def _e_deste_computador(ip: str) -> bool:
    """`ip` é de alguma interface daqui? (Só o kernel sabe: tenta-se o bind.)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ligado(ambiente: dict | None = None) -> bool:
    valor = ((os.environ if ambiente is None else ambiente).get("GARRA_VIGIA") or "1")
    return valor.strip().lower() not in ("0", "false", "no", "nao", "não", "off")


def _arquivo() -> Path:
    return voz.pasta_conf() / "robo.json"


class Vigia:
    def __init__(self, http: Any = requests) -> None:
        self._http = http
        # Começa como "fora": a primeira vez que o robô é visto conta como
        # reaparecimento, e é nela que um robô ligado antes do computador
        # ganha o app.
        self._faltas = FALTAS_PARA_SUMIR
        self._nao_regravar_antes_de = 0.0
        self._avisou_trava = False
        self.estado: dict[str, Any] = {"status": "starting", "checked_at": None}

    # ── onde está o robô ─────────────────────────────────────────────────────
    def _candidatos(self) -> list[str]:
        lembrado = None
        try:
            lembrado = json.loads(_arquivo().read_text(encoding="utf-8")).get("host")
        except (OSError, ValueError, AttributeError):
            pass
        fixos = [os.environ.get("GARRA_ROBO_HOST"), lembrado, "reachy-mini.local"]
        return list(dict.fromkeys(h for h in fixos if isinstance(h, str) and h))

    def _lembrar(self, host: str) -> None:
        try:
            _arquivo().parent.mkdir(parents=True, exist_ok=True)
            _arquivo().write_text(json.dumps({"host": host}) + "\n", encoding="utf-8")
            os.chmod(_arquivo(), 0o600)
        except OSError:
            pass

    def _ler(self, url: str) -> Any:
        r = self._http.get(url, timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def _achar(self) -> tuple[str, dict] | None:
        for host in self._candidatos():
            # O nome é resolvido AQUI, e a conversa é com o número: o endereço
            # que vale é o que respondeu, e só se for da rede da casa. O
            # `wlan_ip` da resposta é só o que o outro lado DIZ — não se usa.
            ip = _resolver(host)
            if ip is None:
                continue
            try:
                estado = self._ler(f"http://{ip}:{PORTA_DAEMON}/api/daemon/status")
            except (requests.RequestException, ValueError):
                continue
            if isinstance(estado, dict) and estado.get("state"):
                # Lembrado como número: continua valendo numa rede sem mDNS.
                self._lembrar(ip)
                return ip, estado
        return None

    # ── uma volta ────────────────────────────────────────────────────────────
    def uma_volta(self) -> dict[str, Any]:
        agora = time.time()
        achado = self._achar()
        if achado is None:
            self._faltas += 1
            self.estado = {"status": "robot_unreachable", "checked_at": agora}
            return self.estado
        host, daemon = achado
        base = f"http://{host}:{PORTA_DAEMON}"
        estado: dict[str, Any] = {"status": "ok", "checked_at": agora, "robot": host,
                                  "daemon_version": daemon.get("version")}
        reapareceu, self._faltas = self._faltas >= FALTAS_PARA_SUMIR, 0
        try:
            app = self._ler(f"{base}/api/apps/current-app-status")
        except (requests.RequestException, ValueError):
            app = None
            estado["status"] = "daemon_busy"
        nome = ((app or {}).get("info") or {}).get("name") if isinstance(app, dict) else None
        estado["app"] = nome

        if app is None and estado["status"] == "ok":
            if reapareceu and self._instalado(base):
                estado["status"] = self._iniciar(base)
            else:
                estado["status"] = "app_not_running"
        elif nome == APP:
            estado.update(self._conferir_enderecos(host, agora))
        elif nome:
            estado["status"] = "other_app_running"     # não é conosco
        self.estado = estado
        return estado

    def _instalado(self, base: str) -> bool:
        try:
            apps = self._ler(f"{base}/api/apps/list-available/installed")
        except (requests.RequestException, ValueError):
            return False
        return any(isinstance(a, dict) and a.get("name") == APP for a in apps or [])

    def _iniciar(self, base: str) -> str:
        try:
            r = self._http.post(f"{base}/api/apps/start-app/{APP}", timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("vigia: não consegui iniciar o app no robô (%s)", type(e).__name__)
            return "app_start_failed"
        log.info("vigia: o robô reapareceu sem app rodando — app do Garra iniciado")
        return "app_started"

    def _conferir_enderecos(self, host: str, agora: float) -> dict[str, Any]:
        painel = reachy._painel(host)
        if painel is None:
            return {"status": "panel_not_ready"}
        try:
            efetiva = (self._ler(f"{painel}/api/config") or {}).get("efetiva") or {}
        except (requests.RequestException, ValueError):
            return {"status": "panel_not_ready"}
        meu = voz.ip_lan()
        apontados = {urllib.parse.urlsplit(str(efetiva.get(c) or "")).hostname
                     for c in ("gateway_url", "voz_url")}
        if meu.startswith("127.") or apontados == {meu}:
            return {"status": "ok", "panel": painel}
        # Só se conserta o caso que o vigia entende: as duas URLs no MESMO
        # número, e esse número não é mais de ninguém daqui. Nome em vez de IP,
        # voz e ponte em máquinas diferentes, ou o endereço de outra interface
        # deste computador (cabo e Wi-Fi, VPN) é escolha de quem configurou.
        unico = next(iter(apontados)) if len(apontados) == 1 else None
        if unico is None or _ipv4(unico) is None or _e_deste_computador(unico):
            return {"status": "custom_addresses", "panel": painel}
        if agora < self._nao_regravar_antes_de:
            return {"status": "address_mismatch_waiting", "panel": painel}
        log.info("vigia: o robô aponta para %s e este computador agora é %s — regravando",
                 sorted(str(a) for a in apontados), meu)
        try:
            # Só as URLs: o token não sai deste computador pelo vigia.
            relatorio = reachy.configurar(painel, so_enderecos=True)
        except reachy.ErroReachy as e:
            self._nao_regravar_antes_de = agora + ESPERA_APOS_FALHA_S
            if e.codigo == "config_locked":
                # Pareado com OUTRA credencial: tentar de novo não muda nada, e
                # repetir o aviso a cada volta só enche o registro.
                if not self._avisou_trava:
                    log.warning(
                        "vigia: o robô está travado por outra credencial e recusou o endereço "
                        "novo. Com o computador antigo: python -m companion.reachy destravar. "
                        "Sem ele: SSH no robô (ver DOCS_REACHY.md, \"A trava do PC\").")
                    self._avisou_trava = True
                return {"status": "address_locked", "panel": painel}
            log.warning("vigia: regravação falhou (%s)", type(e).__name__)
            return {"status": "address_update_failed", "panel": painel}
        except requests.RequestException as e:
            self._nao_regravar_antes_de = agora + ESPERA_APOS_FALHA_S
            log.warning("vigia: regravação falhou (%s)", type(e).__name__)
            return {"status": "address_update_failed", "panel": painel}
        self._avisou_trava = False
        if relatorio.get("ok"):
            log.info("vigia: robô reapontado para %s", meu)
            return {"status": "address_updated", "panel": painel}
        self._nao_regravar_antes_de = agora + ESPERA_APOS_FALHA_S
        log.warning("vigia: o robô não alcançou este computador em %s (firewall?) — "
                    "configuração anterior mantida", meu)
        return {"status": "address_update_rolled_back", "panel": painel}

    def rodar(self, parar: threading.Event) -> None:
        # Um instante antes da primeira volta: a ponte e a voz ainda estão subindo.
        parar.wait(8.0)
        while not parar.is_set():
            try:
                self.uma_volta()
            except Exception:  # o vigia não pode cair por causa de uma volta ruim
                log.exception("vigia: volta com defeito")
            parar.wait(INTERVALO_S)
