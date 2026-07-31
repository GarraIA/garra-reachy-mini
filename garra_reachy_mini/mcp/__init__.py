"""Servidor MCP `reachy`: dá corpo ao Garra.

O gateway sobe este pacote como processo filho por stdio e as ferramentas
aparecem no runtime como `reachy__<nome>` (`reachy__turn_head`, `reachy__dance`,
…), porque o bridge do gateway monta `{servidor}__{ferramenta}` — ponto em nome
de ferramenta é rejeitado pelas APIs da OpenAI/Anthropic.

Ele **não abre `ReachyMini`**: fala HTTP com a API do app em `127.0.0.1:8042`,
que é o único processo dono do robô. Abrir uma segunda conexão aqui brigaria
com o loop de voz por estado, áudio e movimento.

Rode isolado para conferir:

    printf '%s\\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \\
                  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \\
      | reachy_mini_env/bin/python -m garra_reachy_mini.mcp
"""

from .servidor import FERRAMENTAS, main

__all__ = ["FERRAMENTAS", "main"]
