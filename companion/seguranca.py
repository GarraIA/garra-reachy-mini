"""Quem pode falar com o companion.

Este processo liga e desliga serviço do usuário e escreve configuração no robô.
Ele escuta só em `127.0.0.1`, mas isso sozinho **não** basta: um site qualquer
aberto no navegador do operador pode fazer requisição para `127.0.0.1:8125`, e um
domínio hostil pode apontar seu DNS para 127.0.0.1 (*DNS rebinding*) para que o
navegador trate a nossa API como se fosse dele.

A defesa é a mesma que o gateway adota no `ws.rs`: olhar o `Host` e a `Origin`
e exigir que ambos sejam nomes de loopback. Um `evil.com` religado a 127.0.0.1
chega com `Host: evil.com:8125` — não é loopback, cai fora.

A diferença é que lá a regra é *same-origin* estrita, e aqui não pode ser: a
página é servida pelo gateway em `:3888` e chama o companion em `:8125`. Então
exigimos loopback nos dois lados e a porta da origem numa allowlist curta.
"""

from __future__ import annotations

# Portas que podem nos chamar: o console do Garra e nós mesmos.
PORTAS_ORIGEM = frozenset({3888, 8125})
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def hostname_de(autoridade: str) -> str:
    """Host sem porta, lidando com literal IPv6 (`[::1]:3888` → `::1`)."""
    if autoridade.startswith("["):
        return autoridade[1:].split("]", 1)[0]
    return autoridade.split(":", 1)[0]


def porta_de(autoridade: str) -> int | None:
    resto = autoridade.split("]", 1)[1] if autoridade.startswith("[") else autoridade
    _, _, porta = resto.rpartition(":")
    return int(porta) if porta.isdigit() else None


def e_loopback(hostname: str) -> bool:
    return hostname in LOOPBACK


def origem_confiavel(host: str | None, origem: str | None) -> bool:
    """`Host` e `Origin` precisam ser loopback; a origem, de uma porta conhecida.

    Sem `Origin` é chamada de fora do navegador (curl na própria máquina) — e o
    bind em 127.0.0.1 já limita quem consegue fazê-la.
    """
    if not host or not e_loopback(hostname_de(host)):
        return False
    if origem is None:
        return True
    esquema, _, autoridade = origem.partition("://")
    if esquema not in ("http", "https") or not autoridade:
        return False
    return e_loopback(hostname_de(autoridade)) and porta_de(autoridade) in PORTAS_ORIGEM
