"""Qualquer agente que converse por linha de comando.

É o adaptador de quem não tem um próprio: o dono descreve como chamar o
programa, e a ponte passa a falar com ele. O Claude Code entra por aqui, com a
receita pronta em `CLAUDE`.

Uma receita é um dicionário:

    {
      "id": "meu-agente", "name": "Meu agente", "kind": "command",
      "command":  ["meu-agente", "--pergunta", "{prompt}"],
      "continue": ["meu-agente", "--sessao", "{session}", "--pergunta", "{prompt}"],
      "output": "text",              # ou "json:campo" / "json:a.b"
      "stdin": false,                # true: o pedido vai pela entrada padrão
      "safety": "no_tools",          # o que o dono DECLARA sobre o programa
      "timeout_s": 90
    }

`{prompt}`, `{session}` (um id estável da conversa) e `{instructions}` (o papel
do agente) são trocados dentro de cada argumento; nenhum deles passa por shell.
O pedido vem de um microfone e pode começar com hífen: ponha `"--"` antes de
`{prompt}` quando ele for um argumento solto, ou use a forma `--flag={prompt}`.
Sem `continue`, o programa não tem memória: o histórico recente vai junto do
pedido.

**Só o dono, neste computador, cria uma receita** (`agentes.json` ou
`python -m companion.agentes`). Uma receita é um comando que roda nesta
máquina; aceitar uma pela rede seria aceitar um comando pela rede.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import (LIVRE, RESTRITO, SEM_FERRAMENTAS, Adaptador, Conversa,
                   ErroDoAgente, Saude, Trecho, achar_executavel, com_historico,
                   executar, pasta_de_trabalho, versao_de)

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,31}")
_SAIDA = re.compile(r"text|json:[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){0,4}")
ARGS_MAX = 40

# Claude Code sem ferramenta nenhuma e sem os servidores MCP do dono: só
# conversa. `--session-id` exige um UUID — o id da conversa na ponte já é um.
CLAUDE = {
    "id": "claude", "name": "Claude", "kind": "command",
    "command": ["claude", "-p", "--output-format", "json", "--tools", "",
                "--strict-mcp-config", "--session-id", "{session}",
                "--append-system-prompt", "{instructions}", "--", "{prompt}"],
    "continue": ["claude", "-p", "--output-format", "json", "--tools", "",
                 "--strict-mcp-config", "--resume", "{session}", "--", "{prompt}"],
    "output": "json:result", "safety": SEM_FERRAMENTAS, "timeout_s": 90,
}


def validar(receita: Any) -> dict[str, Any]:
    """Confere a forma de uma receita. Levanta `ErroDoAgente("refused")`."""
    def recusa(motivo: str) -> ErroDoAgente:
        return ErroDoAgente("refused", motivo)

    if not isinstance(receita, dict):
        raise recusa("a receita precisa ser um objeto")
    ident = str(receita.get("id") or "")
    if not _ID.fullmatch(ident):
        raise recusa("id: letras minúsculas, números, `-` e `_` (2 a 32)")
    nome = " ".join(str(receita.get("name") or ident).split())[:40] or ident
    limpa: dict[str, Any] = {"id": ident, "name": nome, "kind": "command"}
    for chave in ("command", "continue"):
        argv = receita.get(chave)
        if argv is None and chave == "continue":
            continue
        if (not isinstance(argv, list) or not argv or len(argv) > ARGS_MAX
                or not all(isinstance(a, str) and len(a) <= 4000 for a in argv)):
            raise recusa(f"{chave}: lista de até {ARGS_MAX} textos")
        limpa[chave] = list(argv)
    usa_stdin = bool(receita.get("stdin"))
    if not usa_stdin and not any("{prompt}" in a for a in limpa["command"]):
        raise recusa("command precisa de `{prompt}` (ou `stdin: true`)")
    if "continue" in limpa and not any("{session}" in a for a in limpa["continue"]):
        raise recusa("continue precisa de `{session}`")
    saida = str(receita.get("output") or "text")
    if not _SAIDA.fullmatch(saida):
        raise recusa("output: `text` ou `json:campo`")
    seguranca = receita.get("safety", LIVRE)
    if seguranca not in (SEM_FERRAMENTAS, RESTRITO, LIVRE):
        raise recusa("safety: no_tools, restricted ou unrestricted")
    try:
        prazo = float(receita.get("timeout_s", 90))
    except (TypeError, ValueError) as e:
        raise recusa("timeout_s: número") from e
    limpa.update(stdin=usa_stdin, output=saida, safety=seguranca,
                 timeout_s=min(600.0, max(5.0, prazo)))
    return limpa


class Comando(Adaptador):
    interface = "cli"

    def __init__(self, receita: dict[str, Any], *, descoberto: bool = False) -> None:
        r = validar(receita)
        self.receita = r
        self.id, self.nome = r["id"], r["name"]
        self.descoberto = descoberto
        self.sessoes = "continue" in r
        self.seguranca = r["safety"]
        self.prazo_s = r["timeout_s"]
        self.executavel = achar_executavel(r["command"][0])
        self._versao: str | None = None

    def saude(self) -> Saude:
        if not self.executavel:
            return Saude(False, "not_installed",
                         f"o comando `{self.receita['command'][0]}` não foi encontrado")
        if self._versao is None:
            self._versao = versao_de(self.executavel) or ""
        return Saude(True, "ok", self.executavel, self._versao or None)

    def descrever(self) -> dict[str, Any]:
        d = super().descrever()
        # O comando aparece na tela do dono; os argumentos podem carregar
        # caminho ou chave, e não saem daqui.
        d["command"] = self.receita["command"][0]
        return d

    def responder(self, conversa: Conversa, texto: str, *, instrucoes: str,
                  ao_trecho: Trecho | None = None) -> str:
        if not self.executavel:
            raise ErroDoAgente("unavailable", f"{self.nome} não está instalado")
        r = self.receita
        continuando = self.sessoes and conversa.ref is not None
        molde = r["continue"] if continuando else r["command"]
        pedido = texto if self.sessoes else com_historico(conversa, texto)
        if not any("{instructions}" in a for a in molde) and not continuando and instrucoes:
            pedido = f"{instrucoes}\n\n{pedido}"
        trocas = {"{prompt}": pedido, "{session}": conversa.id,
                  "{instructions}": instrucoes}
        argv = [self.executavel]
        for arg in molde[1:]:
            for marca, valor in trocas.items():
                arg = arg.replace(marca, valor)
            argv.append(arg)
        linhas = list(executar(argv, entrada=pedido if r["stdin"] else None,
                               prazo_s=self.prazo_s, cwd=pasta_de_trabalho(self.id)))
        resposta = self._extrair("\n".join(linhas)).strip()
        if not resposta:
            raise ErroDoAgente("bad_response", f"{self.nome} não devolveu texto")
        if self.sessoes:
            conversa.ref = conversa.id
        if ao_trecho is not None:
            ao_trecho(resposta)
        return resposta

    def _extrair(self, saida: str) -> str:
        modo = self.receita["output"]
        if modo == "text":
            return saida
        # JSON: o objeto pode vir depois de avisos; vale a última linha que for
        # um objeto, ou a saída inteira.
        candidatos = [saida] + [ln for ln in reversed(saida.splitlines()) if ln.strip()]
        for bruto in candidatos:
            try:
                valor: Any = json.loads(bruto)
            except ValueError:
                continue
            for parte in modo.split(":", 1)[1].split("."):
                valor = valor.get(parte) if isinstance(valor, dict) else None
            if isinstance(valor, str):
                return valor
        raise ErroDoAgente("bad_response", "a saída não tinha o campo esperado")
