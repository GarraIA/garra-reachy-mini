"""A casa, do lado do PC: ligar o Home Assistant ao gateway do Garra.

O gateway é quem conhece dispositivos: `hardware.home_assistant` no
`config.yml`, o `DeviceRegistry`, os gates R0–R5, as ferramentas
`device_list`/`device_read`/`device_execute`. Nada disso é refeito aqui. O que
este módulo faz é o que o gateway não oferece por API (medido no 0.4.7, só
leitura do código):

- pôr a URL no `config.yml` e o token no arquivo de ambiente do gateway — o
  adapter só lê os dois no boot, então reinicia o `garraia.service`;
- dizer em que pé a coisa está: **não configurado** (nada gravado),
  **pendente** (gravado, gateway ainda sem dispositivos visíveis), **ok**
  (`device_read`/`device_execute` visíveis no diagnóstico) ou **quebrado**
  (gravado, e o Home Assistant não responde ou recusa o token);
- listar as entidades que o gateway vai registrar, para a pessoa conferir —
  só mostrar: o gateway registra todas as dos domínios que conhece, e não há
  lista de exposição do lado dele (registrado em `docs/plans`);
- a única decisão que é do robô: se a **voz** pode mexer na casa. Quem fala
  com o robô é qualquer pessoa na sala, então nasce desligado; ligar põe as
  três ferramentas da casa na lista branca do modo restrito. Trancas (R3+)
  continuam nas mãos do gateway, que as nega por padrão.

O token nunca volta numa resposta nem vai a log.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import yaml

from . import agente, modo

log = logging.getLogger("companion.casa")

ENV_TOKEN = "GARRA_HA_TOKEN"
ARQUIVO_CONFIG = Path(os.environ.get("GARRAIA_CONFIG", "~/.config/garraia/config.yml")).expanduser()
ARQUIVO_ENV = Path(os.environ.get("GARRAIA_ENV", "~/.config/garraia/garraia.env")).expanduser()
ARQUIVO_ESTADO = Path(os.environ.get("GARRA_REACHY_CONF", "~/.config/garra-reachy")).expanduser() / "casa.json"

# O que o adapter do gateway registra (adapter_homeassistant.rs, `capabilities_de`).
DOMINIOS = {
    "light": ("R1", ["power", "brightness"]),
    "switch": ("R1", ["power"]),
    "sensor": ("R0", ["state"]),
    "binary_sensor": ("R0", ["state"]),
    "climate": ("R1", ["power", "temperature"]),
    "cover": ("R2", ["open", "close", "set_position"]),
    "lock": ("R3", ["lock", "unlock"]),
}
FERRAMENTAS_DA_CASA = ("device_list", "device_read", "device_execute")
_TOKEN = re.compile(r"[A-Za-z0-9._\-]{20,2048}")
CODIGOS = ("invalid_url", "invalid_token", "unreachable", "unauthorized", "not_configured",
           "config_error", "gateway_restart_failed")


class ErroCasa(Exception):
    def __init__(self, codigo: str, detalhe: str = "") -> None:
        assert codigo in CODIGOS, codigo
        super().__init__(f"{codigo}: {detalhe}" if detalhe else codigo)
        self.codigo = codigo
        self.detalhe = detalhe


def url_da_casa(url: str) -> bool:
    """http(s) para um endereço da casa, sem caminho. É para onde o gateway vai
    mandar o token."""
    if not isinstance(url, str) or len(url) > 200:
        return False
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname or p.path not in ("", "/") or p.query:
        return False
    nome = p.hostname
    if nome == "localhost" or nome.endswith(".local") or nome.endswith(".lan") or nome.endswith(".home"):
        return True
    try:
        ip = ipaddress.ip_address(nome)
    except ValueError:
        return "." not in nome            # um nome sem domínio é da rede local
    return ip.is_loopback or ip.is_private


def voz_liberada() -> bool:
    try:
        return json.loads(ARQUIVO_ESTADO.read_text(encoding="utf-8")).get("voz") is True
    except (OSError, ValueError):
        return False


def politica_com_a_casa(politica: dict, liberada: bool) -> dict:
    """A lista branca do modo restrito com — ou sem — as ferramentas da casa."""
    permitidas = [t for t in politica.get("allowed", []) if t not in FERRAMENTAS_DA_CASA]
    negadas = [t for t in politica.get("denied", []) if t not in FERRAMENTAS_DA_CASA]
    if liberada:
        permitidas += list(FERRAMENTAS_DA_CASA)
    else:
        negadas.append("device_execute")
    return dict(politica, allowed=permitidas, denied=negadas)


def _reiniciar_gateway() -> None:
    agente._reiniciar_gateway()


def _gateway_saudavel() -> bool:
    return agente._gateway_saudavel()


def _ler_env(caminho: Path) -> list[str]:
    try:
        return caminho.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _gravar_env(caminho: Path, token: str | None) -> None:
    """Troca (ou tira) a linha do token, preservando as outras. 0600 sempre."""
    linhas = [l for l in _ler_env(caminho) if not l.startswith(ENV_TOKEN + "=")]
    if token is not None:
        linhas.append(f"{ENV_TOKEN}={token}")
    agente._gravar_atomico(caminho, "\n".join(linhas) + "\n", 0o600)


def _token_presente(caminho: Path) -> bool:
    return any(l.startswith(ENV_TOKEN + "=") and len(l) > len(ENV_TOKEN) + 1 for l in _ler_env(caminho))


def _token_do_env(caminho: Path) -> str | None:
    for l in _ler_env(caminho):
        if l.startswith(ENV_TOKEN + "="):
            return l[len(ENV_TOKEN) + 1:].strip() or None
    return None


class Casa:
    def __init__(self, http_casa: httpx.Client | None = None,
                 http_gateway: httpx.Client | None = None, chave_gateway: str | None = None) -> None:
        self.http = http_casa or httpx.Client(timeout=10.0)
        cabecalhos = {"Authorization": f"Bearer {chave_gateway}"} if chave_gateway else {}
        self.gateway = http_gateway or httpx.Client(base_url="http://127.0.0.1:3888", timeout=10.0,
                                                    headers=cabecalhos)

    # ── config ───────────────────────────────────────────────────────────────
    def _config(self) -> dict:
        try:
            dados = yaml.safe_load(ARQUIVO_CONFIG.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as e:
            raise ErroCasa("config_error", "config.yml não encontrado") from e
        except yaml.YAMLError as e:
            raise ErroCasa("config_error", "config.yml ilegível") from e
        if not isinstance(dados, dict):
            raise ErroCasa("config_error", "config.yml não é um mapa")
        return dados

    def _url_configurada(self, dados: dict | None = None) -> str | None:
        dados = dados or self._config()
        hw = dados.get("hardware")
        ha = hw.get("home_assistant") if isinstance(hw, dict) else None
        return str(ha.get("url")) if isinstance(ha, dict) and ha.get("url") else None

    def _gravar_config(self, dados: dict) -> None:
        agente._backup(ARQUIVO_CONFIG)
        texto = yaml.safe_dump(dados, allow_unicode=True, sort_keys=False, default_flow_style=False)
        agente._gravar_atomico(ARQUIVO_CONFIG, texto, 0o600)

    # ── o Home Assistant ─────────────────────────────────────────────────────
    def _sondar(self, url: str, token: str) -> dict[str, Any]:
        saida: dict[str, Any] = {"reachable": False, "authorized": None, "version": None}
        try:
            r = self.http.get(f"{url}/api/", headers={"Authorization": f"Bearer {token}"}, timeout=8.0)
        except httpx.HTTPError:
            return saida
        saida["reachable"] = True
        saida["authorized"] = r.status_code == 200
        if r.status_code == 200:
            try:
                c = self.http.get(f"{url}/api/config", headers={"Authorization": f"Bearer {token}"},
                                  timeout=8.0)
                if c.status_code == 200:
                    saida["version"] = c.json().get("version")
            except (httpx.HTTPError, ValueError):
                pass
        return saida

    # ── o gateway ────────────────────────────────────────────────────────────
    def _gateway(self) -> dict[str, Any]:
        """`devices_visible` vem do diagnóstico: o gateway não tem rota de
        dispositivos, e o check `tools.capabilities` é onde ele diz se
        `device_*` está visível. É texto; é o que existe."""
        try:
            r = self.gateway.get("/api/diagnostics", timeout=8.0)
            r.raise_for_status()
            checks = r.json().get("checks") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            return {"devices_visible": None, "reachable": False}
        detalhe = None
        for c in checks:
            if isinstance(c, dict) and c.get("id") == "tools.capabilities":
                detalhe = str(c.get("detail") or "")
        if detalhe is None:
            return {"devices_visible": None, "reachable": True}
        sem = re.search(r"device_(?:read|execute) \(no_devices\)", detalhe) is not None
        return {"devices_visible": not sem, "reachable": True}

    # ── o que o painel vê ────────────────────────────────────────────────────
    def estado(self) -> dict[str, Any]:
        dados = self._config()
        url = self._url_configurada(dados)
        token = _token_do_env(ARQUIVO_ENV)
        configurada = url is not None
        ha = self._sondar(url, token or "") if configurada else None
        gw = self._gateway()
        if not configurada:
            estado = "not_configured"
        elif ha is not None and (not ha["reachable"] or ha["authorized"] is False or not token):
            estado = "broken"
        elif gw["devices_visible"] is True:
            estado = "ok"
        else:
            estado = "pending"
        return {"configured": configurada, "url": url, "token_present": bool(token),
                "home_assistant": ha, "gateway": gw, "voice_control": voz_liberada(),
                "state": estado}

    def configurar(self, url: str, token: str) -> dict[str, Any]:
        url = str(url or "").strip().rstrip("/")
        token = str(token or "").strip()
        if not url_da_casa(url):
            raise ErroCasa("invalid_url")
        if not _TOKEN.fullmatch(token):
            raise ErroCasa("invalid_token")
        sonda = self._sondar(url, token)
        if not sonda["reachable"]:
            raise ErroCasa("unreachable", url)
        if not sonda["authorized"]:
            raise ErroCasa("unauthorized")
        dados = self._config()
        hw = dados.get("hardware")
        if not isinstance(hw, dict):
            hw = dados["hardware"] = {}
        hw["home_assistant"] = {"url": url, "token_env": ENV_TOKEN}
        _gravar_env(ARQUIVO_ENV, token)
        self._gravar_config(dados)
        self._reiniciar()
        log.info("casa: Home Assistant configurado em %s (versão %s)", url, sonda.get("version"))
        return self.estado()

    def remover(self) -> dict[str, Any]:
        dados = self._config()
        hw = dados.get("hardware")
        if isinstance(hw, dict):
            hw["home_assistant"] = None
        _gravar_env(ARQUIVO_ENV, None)
        self._gravar_config(dados)
        try:
            self.voz(False)
        except ErroCasa:
            pass
        self._reiniciar()
        log.info("casa: Home Assistant removido")
        return self.estado()

    def _reiniciar(self) -> None:
        _reiniciar_gateway()
        if not _gateway_saudavel():
            raise ErroCasa("gateway_restart_failed")

    def entidades(self) -> list[dict[str, Any]]:
        """As entidades que o gateway registra — para conferir, não para agir."""
        url = self._url_configurada()
        token = _token_do_env(ARQUIVO_ENV)
        if not url or not token:
            raise ErroCasa("not_configured")
        try:
            r = self.http.get(f"{url}/api/states", headers={"Authorization": f"Bearer {token}"},
                              timeout=15.0)
        except httpx.HTTPError as e:
            raise ErroCasa("unreachable", type(e).__name__) from e
        if r.status_code == 401:
            raise ErroCasa("unauthorized")
        saida = []
        for e in r.json() if r.status_code == 200 else []:
            entity_id = str(e.get("entity_id") or "")
            dominio, _, objeto = entity_id.partition(".")
            if dominio not in DOMINIOS or not re.fullmatch(r"[a-z0-9_]+", objeto):
                continue
            risco, capacidades = DOMINIOS[dominio]
            atributos = e.get("attributes") if isinstance(e.get("attributes"), dict) else {}
            saida.append({"id": f"ha:{entity_id}", "entity_id": entity_id, "domain": dominio,
                          "name": str(atributos.get("friendly_name") or objeto),
                          "state": str(e.get("state")), "risk": risco,
                          "capabilities": list(capacidades)})
        return saida

    # ── a voz ────────────────────────────────────────────────────────────────
    def voz(self, liberar: bool) -> dict[str, Any]:
        """Liga ou desliga o controle da casa por voz: muda a lista branca do
        modo restrito no gateway e guarda a escolha, para `modo.garantir` não a
        desfazer no próximo arranque."""
        if liberar and self._url_configurada() is None:
            raise ErroCasa("not_configured")
        r = self.gateway.get("/api/modes/custom", timeout=10.0)
        if r.status_code != 200:
            raise ErroCasa("gateway_restart_failed", f"modes HTTP {r.status_code}")
        atual = next((m for m in (r.json().get("modes") or []) if isinstance(m, dict)
                      and str(m.get("name", "")).lower() == modo.NOME.lower()), None)
        if atual is None:
            raise ErroCasa("gateway_restart_failed", "modo da voz não existe")
        politica = politica_com_a_casa(atual.get("tool_policy_overrides") or modo.POLITICA, liberar)
        r = self.gateway.patch(f"/api/modes/custom/{atual.get('id')}",
                               json={"tool_policy_overrides": politica}, timeout=10.0)
        if r.status_code != 200:
            raise ErroCasa("gateway_restart_failed", f"patch HTTP {r.status_code}")
        ARQUIVO_ESTADO.parent.mkdir(parents=True, exist_ok=True)
        agente._gravar_atomico(ARQUIVO_ESTADO, json.dumps({"voz": bool(liberar)}), 0o600)
        log.info("casa: controle por voz %s", "liberado" if liberar else "desligado")
        return self.estado()


__all__ = ["Casa", "CODIGOS", "DOMINIOS", "ENV_TOKEN", "ErroCasa", "FERRAMENTAS_DA_CASA",
           "politica_com_a_casa", "url_da_casa", "voz_liberada"]
