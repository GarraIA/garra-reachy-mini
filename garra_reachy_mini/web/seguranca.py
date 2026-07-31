"""Rede e autenticação da API do robô.

O daemon do robô não tem autenticação nenhuma, e esta API manda no robô: dança,
liga app, desliga motor. Deixar isso em `0.0.0.0` sem porteiro entrega o robô
para qualquer coisa que alcance a máquina na rede.

Postura adotada:

  • **padrão é loopback** (`127.0.0.1:8042`). Ninguém de fora entra, ponto;
  • rede só com opt-in explícito (`GARRA_REACHY_ALLOW_REMOTE=1`) **e** token
    próprio (`GARRA_REACHY_TOKEN`), diferente do token do gateway;
  • pediu rede sem token → **cai para loopback** com aviso alto. Falhar fechado
    é a única opção defensável quando a alternativa é expor o robô;
  • rotas que mudam algo exigem `Origin` conhecido (anti-CSRF) e passam por um
    limitador por IP.

`GET` de leitura fica livre no loopback: é o que faz o painel e o `curl` de
diagnóstico funcionarem sem cerimônia.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger("garra_reachy_mini.web.seguranca")

PORTA_PADRAO = 8042

# 8042 é a porta que o dashboard do robô espera de um app (`custom_app_url`), e
# no robô ela está livre. No desktop, porém, o `reachy-mini-control` da Pollen
# já a ocupa como proxy — e como o uvicorn da página roda numa thread, a falha
# de bind morria em silêncio. Tentamos 8042 primeiro e caímos para a próxima
# livre, avisando qual ficou.
PORTAS_ALTERNATIVAS = (8043, 8044, 8045, 8046)

# Origens que podem falar com a API sem token no modo local: o próprio painel e
# o console do Garra (que embute o painel na página #/reachy).
PORTAS_CONFIAVEIS = (3888, PORTA_PADRAO, *PORTAS_ALTERNATIVAS, 5173)
HOSTS_LOCAIS = ("localhost", "127.0.0.1", "[::1]", "::1")


def porta_livre(host: str, preferida: int, alternativas=PORTAS_ALTERNATIVAS) -> int:
    """Primeira porta que aceita bind. Devolve a preferida se nenhuma servir."""
    for porta in (preferida, *alternativas):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, porta))
            except OSError:
                continue
        if porta != preferida:
            log.warning(
                "porta %d ocupada (provavelmente pelo reachy-mini-control da Pollen); "
                "usando %d", preferida, porta,
            )
        return porta
    return preferida


def _verdadeiro(valor: str | None) -> bool:
    return (valor or "").strip().lower() in ("1", "true", "yes", "sim", "on")


@dataclass
class Politica:
    """Decisão de rede tomada uma vez, no arranque."""

    host: str = "127.0.0.1"
    porta: int = PORTA_PADRAO
    remoto: bool = False
    token: str | None = None
    origens: tuple[str, ...] = ()
    aviso: str | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.porta}"

    @property
    def url_visivel(self) -> str:
        visivel = "localhost" if self.host in ("127.0.0.1", "0.0.0.0") else self.host
        return f"http://{visivel}:{self.porta}"

    def exige_token(self) -> bool:
        return self.remoto and bool(self.token)


def origens_locais(porta: int) -> tuple[str, ...]:
    portas = {porta, *PORTAS_CONFIAVEIS}
    return tuple(
        f"http://{h}:{p}" for h in ("localhost", "127.0.0.1") for p in sorted(portas)
    )


def resolver_politica(
    porta: int = PORTA_PADRAO,
    ambiente: dict[str, str] | None = None,
    *,
    escolher_porta: bool = True,
) -> Politica:
    env = ambiente if ambiente is not None else os.environ
    fixada = env.get("GARRA_REACHY_PORTA")
    quer_remoto = _verdadeiro(env.get("GARRA_REACHY_ALLOW_REMOTE"))
    token = (env.get("GARRA_REACHY_TOKEN") or "").strip() or None
    if fixada:
        # Porta explícita é ordem: se estiver ocupada, é melhor falhar alto do
        # que servir num lugar que ninguém está olhando.
        porta = int(fixada)
    elif escolher_porta:
        porta = porta_livre("0.0.0.0" if quer_remoto and token else "127.0.0.1", porta)
    origens = list(origens_locais(porta))
    extra = (env.get("GARRA_REACHY_ORIGENS") or "").strip()
    if extra:
        origens += [o.strip() for o in extra.split(",") if o.strip()]

    if not quer_remoto:
        return Politica(host="127.0.0.1", porta=porta, remoto=False, token=token,
                        origens=tuple(dict.fromkeys(origens)))

    if not token:
        return Politica(
            host="127.0.0.1", porta=porta, remoto=False, token=None,
            origens=tuple(dict.fromkeys(origens)),
            aviso=(
                "GARRA_REACHY_ALLOW_REMOTE=1 sem GARRA_REACHY_TOKEN: mantendo a API "
                "em 127.0.0.1. Defina um token para liberar o acesso pela rede."
            ),
        )
    return Politica(host="0.0.0.0", porta=porta, remoto=True, token=token,
                    origens=tuple(dict.fromkeys(origens)))


def origem_permitida(origem: str | None, politica: Politica) -> bool:
    """Anti-CSRF: aceita ausência de Origin (curl) e a allowlist.

    Um navegador SEMPRE manda `Origin` em requisição cross-origin que muda
    estado, então ausência não é o caso perigoso — o caso perigoso é uma origem
    presente e desconhecida.
    """
    if not origem:
        return True
    if origem in politica.origens:
        return True
    try:
        url = urlparse(origem)
    except ValueError:
        return False
    if not politica.remoto:
        return url.hostname in HOSTS_LOCAIS
    return False


@dataclass
class Limitador:
    """Balde de fichas por IP para as rotas que mudam algo."""

    capacidade: int = 40
    por_segundo: float = 8.0
    _baldes: dict[str, tuple[float, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def permitir(self, chave: str) -> bool:
        agora = time.monotonic()
        with self._lock:
            fichas, quando = self._baldes.get(chave, (float(self.capacidade), agora))
            fichas = min(self.capacidade, fichas + (agora - quando) * self.por_segundo)
            if fichas < 1.0:
                self._baldes[chave] = (fichas, agora)
                return False
            self._baldes[chave] = (fichas - 1.0, agora)
            return True

    def limpar(self, idade_s: float = 300.0) -> None:
        agora = time.monotonic()
        with self._lock:
            for chave in [k for k, (_, q) in self._baldes.items() if agora - q > idade_s]:
                self._baldes.pop(chave, None)


def token_valido(cabecalho: str | None, query: str | None, politica: Politica) -> bool:
    if not politica.exige_token():
        return True
    esperado = politica.token or ""
    fornecido = query or ""
    if cabecalho and cabecalho.lower().startswith("bearer "):
        fornecido = cabecalho[7:].strip()
    if len(fornecido) != len(esperado):
        return False
    # Comparação em tempo constante: o token vale por controle do robô.
    diferenca = 0
    for a, b in zip(fornecido, esperado):
        diferenca |= ord(a) ^ ord(b)
    return diferenca == 0
