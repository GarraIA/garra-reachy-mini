"""Rede e autenticação da API do robô.

O daemon do robô não tem autenticação nenhuma, e esta API manda no robô: dança,
liga app, desliga motor. Deixar isso em `0.0.0.0` sem porteiro entrega o robô
para qualquer coisa que alcance a máquina na rede.

A postura acompanha a da plataforma, em vez de contrariá-la:

  • **fora do robô** (desktop, Lite) o padrão é loopback (`127.0.0.1:8042`).
    Ali o daemon também é loopback, então abrir a nossa API na rede criaria
    exposição que não existia — por isso rede exige opt-in explícito
    (`GARRA_REACHY_ALLOW_REMOTE=1`) e token, que se gera sozinho;
  • **dentro do robô wireless** escutamos na rede sem exigir token. Não é
    relaxamento: lá o daemon da Pollen já está em `0.0.0.0` **sem autenticação
    nenhuma**, e quem alcança a LAN já move o robô e já vê a câmera por
    `:8000`. Um token nosso não protegeria nada — e, medido, tornaria o painel
    inutilizável: não existe endpoint no daemon que mostre o log do app
    (`GET /logs` devolve a página de descontinuação), então o usuário não teria
    de onde ler o token. `GARRA_REACHY_TOKEN` continua disponível para quem
    quiser exigir um;
  • rotas que mudam algo exigem `Origin` conhecido (anti-CSRF) e passam por um
    limitador por IP, sempre;
  • `POST /api/robot/stop` nunca pede token: um botão de pânico que responde
    401 é pior do que o acesso que ele barraria.
"""

from __future__ import annotations

import logging
import os
import secrets
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


def _endereco_e_nosso(ip: str) -> bool:
    """O IP pertence a uma interface desta máquina?

    `bind()` num endereço que não é local falha com EADDRNOTAVAIL — é o teste
    mais direto que existe, e não depende de resolver o próprio hostname (que
    no Raspberry Pi costuma devolver só 127.0.1.1).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((ip, 0))
        return True
    except OSError:
        return False


def dentro_do_robo(timeout: float = 1.0) -> bool:
    """Este processo está rodando DENTRO de um Reachy Mini wireless?

    Só nesse caso faz sentido escutar na rede: lá o daemon da Pollen já está em
    `0.0.0.0` sem autenticação nenhuma (`daemon/app/main.py:109-119`), então
    quem alcança a LAN já move o robô por `POST :8000/api/move/goto` — recusar o
    bind não protegeria nada e deixaria o painel inalcançável.

    Perguntar só `wireless_version` ao daemon local não serve: no desktop o
    `reachy-mini-control` da Pollen ocupa a 127.0.0.1:8000 fazendo proxy para o
    robô, e a resposta vem idêntica. O que desempata é o `wlan_ip`: dentro do
    robô ele é um endereço nosso, do desktop não é.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8000/api/daemon/status", timeout=timeout
        ) as r:
            dados = json.loads(r.read())
    except Exception:
        return False
    if not dados.get("wireless_version"):
        return False
    ip = (dados.get("wlan_ip") or "").strip()
    return bool(ip) and _endereco_e_nosso(ip)


def token_persistente() -> str:
    """Token estável do app, criado na primeira execução com modo 0600.

    Estável de propósito: a URL do painel carrega o token na query, e um token
    novo a cada arranque quebraria o favorito de quem já o guardou.
    """
    from .. import armazenamento

    pasta = armazenamento.diretorio()
    arquivo = pasta / "token"
    try:
        atual = arquivo.read_text(encoding="utf-8").strip()
        if atual:
            return atual
    except OSError:
        pass
    novo = secrets.token_urlsafe(24)
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(novo + "\n", encoding="utf-8")
        os.chmod(arquivo, 0o600)
    except OSError:
        log.warning("não consegui gravar o token em %s; ele muda a cada arranque",
                    arquivo)
    return novo


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
    no_robo: bool | None = None,
) -> Politica:
    env = ambiente if ambiente is not None else os.environ
    fixada = env.get("GARRA_REACHY_PORTA")
    bind = (env.get("GARRA_REACHY_BIND") or "").strip()
    quer_remoto = _verdadeiro(env.get("GARRA_REACHY_ALLOW_REMOTE"))
    token = (env.get("GARRA_REACHY_TOKEN") or "").strip() or None
    # Instalado da loja, o app roda dentro do robô e o painel precisa abrir do
    # laptop de quem o instalou. Aí escutar na rede não é opção, é requisito.
    no_robo = dentro_do_robo() if no_robo is None else no_robo
    if not bind and not quer_remoto:
        quer_remoto = no_robo
    if bind:
        quer_remoto = bind not in HOSTS_LOCAIS

    # Token automático SÓ onde ele acrescenta proteção de verdade.
    #
    # Dentro do robô wireless não acrescenta: o daemon da Pollen já está em
    # `0.0.0.0` sem autenticação nenhuma, então quem alcança a LAN já move o
    # robô e já vê a câmera por `:8000`. Exigir token ali não protegeria nada —
    # e, medido, deixaria o painel inalcançável: o daemon não tem endpoint que
    # mostre o log do app, e `GET /logs` devolve só a página de descontinuação.
    # O usuário não teria de onde ler o token que nós mesmos imprimimos.
    #
    # Fora do robô é o contrário: lá o daemon é loopback, então abrir a nossa
    # API na rede CRIA exposição que não existia. Aí o token é obrigatório e se
    # gera sozinho.
    if quer_remoto and not token and not no_robo:
        token = token_persistente()
    if fixada:
        # Porta explícita é ordem: se estiver ocupada, é melhor falhar alto do
        # que servir num lugar que ninguém está olhando.
        porta = int(fixada)
    elif escolher_porta:
        porta = porta_livre("0.0.0.0" if quer_remoto else "127.0.0.1", porta)
    origens = list(origens_locais(porta))
    extra = (env.get("GARRA_REACHY_ORIGENS") or "").strip()
    if extra:
        origens += [o.strip() for o in extra.split(",") if o.strip()]

    if not quer_remoto:
        return Politica(host=bind or "127.0.0.1", porta=porta, remoto=False,
                        token=token, origens=tuple(dict.fromkeys(origens)))

    if not token and not no_robo:  # token_persistente() não conseguiu gravar
        return Politica(
            host="127.0.0.1", porta=porta, remoto=False, token=None,
            origens=tuple(dict.fromkeys(origens)),
            aviso=(
                "Sem token utilizável: mantendo a API em 127.0.0.1. Defina "
                "GARRA_REACHY_TOKEN para liberar o acesso pela rede."
            ),
        )
    return Politica(host=bind or "0.0.0.0", porta=porta, remoto=True, token=token,
                    origens=tuple(dict.fromkeys(origens)))


def origem_permitida(origem: str | None, politica: Politica,
                     host_pedido: str | None = None) -> bool:
    """Anti-CSRF: aceita ausência de Origin (curl) e a allowlist.

    Um navegador SEMPRE manda `Origin` em requisição cross-origin que muda
    estado, então ausência não é o caso perigoso — o caso perigoso é uma origem
    presente e desconhecida.

    Na rede, `host_pedido` (o cabeçalho Host) permite aceitar a mesma origem que
    serviu a página: o painel aberto em `http://reachy-mini.local:8042` não tem
    como estar na allowlist, que é montada no arranque sem saber por qual nome o
    robô seria chamado. Mesma origem não é CSRF por definição.
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
    if host_pedido:
        alvo = host_pedido if ":" in host_pedido else f"{host_pedido}:{politica.porta}"
        return f"{url.hostname}:{url.port or 80}" == alvo
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
