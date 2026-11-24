"""Pi (pi coding agent) — conversa pela CLI, em modo de impressão.

    pi -p --mode json --no-tools --no-extensions --no-context-files \\
       --session-dir <pasta> --session-id <id> --system-prompt "<papel>" "<pedido>"

`--session-id` cria a sessão se ela não existe e a continua se existe, então o
mesmo comando serve ao primeiro turno e aos seguintes. `--no-tools` é a trava:
sem ela o Pi executa `bash` sem perguntar (medido em 24/11/2026, v0.87.1). As
sessões ficam numa pasta só deste adaptador, longe das do dono.

O Pi escolhe o modelo pelas credenciais que encontra. Nesta máquina ele usa a
OpenRouter pela `OPENROUTER_API_KEY` do ambiente do companion; sem credencial
nenhuma, o `pi auth check` diz que falta — e a saúde repete isso.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from .base import (LIVRE, SEM_FERRAMENTAS, Adaptador, Conversa, ErroDoAgente,
                   Saude, Trecho, achar_executavel, ambiente_do_agente,
                   executar, pasta_de_trabalho, versao_de)


class Pi(Adaptador):
    id = "pi"
    nome = "Pi"
    interface = "cli"
    sessoes = True
    streaming = True
    prazo_s = 90.0

    def __init__(self, executavel: str | None = None) -> None:
        self.executavel = executavel or achar_executavel(
            "pi", ("~/.local/bin/pi", "~/.pi/agent/bin/pi"))
        self.modelo = ""                 # vazio = o que o Pi escolher
        self.com_ferramentas = False
        self._versao: str | None = None

    @property
    def seguranca(self) -> str:  # type: ignore[override]
        return LIVRE if self.com_ferramentas else SEM_FERRAMENTAS

    def saude(self) -> Saude:
        if not self.executavel:
            return Saude(False, "not_installed", "o comando `pi` não foi encontrado")
        if self._versao is None:
            self._versao = versao_de(self.executavel)
        if self._versao is None:
            return Saude(False, "error", "`pi --version` não respondeu")
        return Saude(True, "ok", self.executavel, self._versao)

    def credenciais(self) -> tuple[bool, str]:
        """O Pi tem como falar com algum modelo? Só no teste: custa 0,3 s."""
        try:
            r = subprocess.run([self.executavel, "auth", "check", "--json", "--no-refresh"],
                               capture_output=True, text=True, timeout=15,
                               env=ambiente_do_agente(), stdin=subprocess.DEVNULL)
            dados = json.loads(r.stdout or "{}")
        except (OSError, ValueError, subprocess.SubprocessError):
            return True, ""              # não deu para saber: o turno dirá
        return dados.get("status") == "ready", str(dados.get("status") or "")

    def opcoes(self) -> list[dict[str, Any]]:
        return [
            {"key": "model", "type": "text", "value": self.modelo, "max": 80},
            {"key": "tools", "type": "choice",
             "value": "completo" if self.com_ferramentas else "conversa",
             "choices": [{"value": "conversa", "safety": SEM_FERRAMENTAS},
                         {"value": "completo", "safety": LIVRE, "local_only": True}]},
        ]

    def configurar(self, opcoes: dict[str, Any], *, local: bool = False) -> None:
        if "model" in opcoes:
            modelo = str(opcoes["model"] or "").strip()
            if len(modelo) > 80 or any(c.isspace() for c in modelo) or modelo.startswith("-"):
                raise ErroDoAgente("refused", "nome de modelo inválido")
            self.modelo = modelo
        if "tools" in opcoes:
            if opcoes["tools"] not in ("conversa", "completo"):
                raise ErroDoAgente("refused", "nível de ferramentas desconhecido")
            if opcoes["tools"] == "completo" and not local:
                raise ErroDoAgente("refused", "só neste computador se libera o Pi completo")
            self.com_ferramentas = opcoes["tools"] == "completo"

    def responder(self, conversa: Conversa, texto: str, *, instrucoes: str,
                  ao_trecho: Trecho | None = None) -> str:
        if not self.executavel:
            raise ErroDoAgente("unavailable", "Pi não está instalado")
        pasta = pasta_de_trabalho(self.id)
        argv = [self.executavel, "-p", "--mode", "json", "--thinking", "off",
                "--no-extensions", "--no-context-files",
                "--session-dir", str(pasta / "sessoes"), "--session-id", conversa.id]
        if not self.com_ferramentas:
            argv.append("--no-tools")
        if self.modelo:
            argv += ["--model", self.modelo]
        if instrucoes:
            argv += ["--system-prompt", instrucoes]
        # `--` antes do pedido: a transcrição pode começar com hífen.
        argv += ["--", texto]

        partes: list[str] = []
        erro: str | None = None
        for linha in executar(argv, prazo_s=self.prazo_s, cwd=pasta):
            try:
                evento = json.loads(linha)
            except ValueError:
                continue
            if not isinstance(evento, dict):
                continue
            if evento.get("type") == "message_update":
                delta = evento.get("assistantMessageEvent") or {}
                if delta.get("type") == "text_delta" and isinstance(delta.get("delta"), str):
                    partes.append(delta["delta"])
                    if ao_trecho is not None:
                        ao_trecho(delta["delta"])
                elif delta.get("type") == "error":
                    erro = str(delta.get("error") or delta.get("reason") or "erro do modelo")
            elif evento.get("type") in ("error", "agent_error"):
                erro = str(evento.get("message") or evento.get("error") or "erro")
        resposta = "".join(partes).strip()
        if not resposta:
            if erro and any(p in erro.lower() for p in ("auth", "api key", "credential", "401")):
                raise ErroDoAgente("auth", erro)
            raise ErroDoAgente("bad_response", erro or "o Pi não devolveu texto")
        conversa.ref = conversa.id
        return resposta
