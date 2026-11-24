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

Desligar: `GARRA_VIGIA=0` em `~/.config/garra-reachy/companion.env`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from . import reachy, voz

log = logging.getLogger("companion.vigia")

APP = "garra_reachy_mini"
INTERVALO_S = 30.0
# Depois de uma regravação que o robô não confirmou, espera antes de tentar de
# novo: o motivo costuma ser o firewall, e insistir a cada volta só enche o log.
ESPERA_APOS_FALHA_S = 600.0
PORTA_DAEMON = 8000
TIMEOUT_S = 3.0


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
        self._fora = True
        self._nao_regravar_antes_de = 0.0
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
            try:
                estado = self._ler(f"http://{host}:{PORTA_DAEMON}/api/daemon/status")
            except (requests.RequestException, ValueError):
                continue
            if isinstance(estado, dict) and estado.get("state"):
                # O IP que o próprio robô diz ter vale mais que o nome: é o que
                # continua funcionando numa rede que derruba o mDNS.
                ip = estado.get("wlan_ip")
                achado = ip if isinstance(ip, str) and ip else host
                self._lembrar(achado)
                return achado, estado
        return None

    # ── uma volta ────────────────────────────────────────────────────────────
    def uma_volta(self) -> dict[str, Any]:
        agora = time.time()
        achado = self._achar()
        if achado is None:
            self._fora = True
            self.estado = {"status": "robot_unreachable", "checked_at": agora}
            return self.estado
        host, daemon = achado
        base = f"http://{host}:{PORTA_DAEMON}"
        estado: dict[str, Any] = {"status": "ok", "checked_at": agora, "robot": host,
                                  "daemon_version": daemon.get("version")}
        reapareceu, self._fora = self._fora, False
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
        if agora < self._nao_regravar_antes_de:
            return {"status": "address_mismatch_waiting", "panel": painel}
        log.info("vigia: o robô aponta para %s e este computador agora é %s — regravando",
                 sorted(str(a) for a in apontados), meu)
        try:
            relatorio = reachy.configurar(painel)
        except (reachy.ErroReachy, requests.RequestException) as e:
            self._nao_regravar_antes_de = agora + ESPERA_APOS_FALHA_S
            log.warning("vigia: regravação falhou (%s)", type(e).__name__)
            return {"status": "address_update_failed", "panel": painel}
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
