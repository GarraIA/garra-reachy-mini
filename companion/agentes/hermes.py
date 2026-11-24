"""Hermes Agent (Nous Research) — o agente padrão do robô.

Conversa pela CLI, que é a interface estável dele nesta máquina (a API
compatível com a OpenAI existe no código, mas vem desligada):

    hermes chat -t <ferramentas> --format stream-json -q "<pedido>"     # 1º turno
    hermes chat --resume <session_id> --format stream-json -q "<pedido>" # seguintes

Medido em 24/11/2026 (v0.23.1): o primeiro turno devolve o `session_id` no
evento `result`; `--resume` mantém a memória da conversa **e** a restrição de
ferramentas escolhida na criação. Passar `-t` de novo ao retomar é recusado
pelo Hermes — e não deve ser tentado: deixa para trás uma sessão sem restrição.

**As ferramentas são a trava.** Sem `-t`, o Hermes na CLI aprova sozinho o que
decide executar (terminal, arquivos, automação do computador) e pesquisa as
outras conversas do dono. Quem fala com o robô é qualquer pessoa na sala, e o
que o microfone ouve é texto não confiável; por isso a sessão da voz nasce com
uma lista curta, e alargá-la é decisão que só o dono toma, neste computador.
"""

from __future__ import annotations

import json
from typing import Any

from . import persona
from .base import (LIVRE, RESTRITO, SEM_FERRAMENTAS, Adaptador, Conversa,
                   ErroDoAgente, Saude, Trecho, achar_executavel, com_historico,
                   executar, neutralizar, nome_do_executavel, pasta_de_trabalho,
                   versao_de)

# O que cada nível entrega ao Hermes em `-t`. `todo` é a menor lista que ele
# aceita (planejamento de tarefas, sem efeito fora da conversa).
FERRAMENTAS = {
    "conversa": "todo",
    "web": "web,todo",
    "completo": None,          # sem `-t`: tudo o que o dono habilitou na CLI
}
SEGURANCA = {"conversa": SEM_FERRAMENTAS, "web": RESTRITO, "completo": LIVRE}


class Hermes(Adaptador):
    id = "hermes"
    nome = "Hermes"
    interface = "cli"
    sessoes = True
    streaming = True
    # Menor que o prazo do robô (120 s), de propósito: se o Hermes não
    # responder, ainda sobra tempo para o agente reserva antes de o robô desistir.
    prazo_s = 75.0

    def __init__(self, executavel: str | None = None) -> None:
        self.executavel = executavel or achar_executavel(
            "hermes", ("~/.local/bin/hermes", "~/.hermes/hermes-agent/.hermes/bin/hermes"))
        self.ferramentas = "web"
        self._versao: str | None = None

    @property
    def seguranca(self) -> str:  # type: ignore[override]
        return SEGURANCA[self.ferramentas]

    def saude(self) -> Saude:
        if not self.executavel:
            return Saude(False, "not_installed", "o comando `hermes` não foi encontrado")
        if self._versao is None:
            self._versao = versao_de(self.executavel)
        if self._versao is None:
            return Saude(False, "error", "`hermes --version` não respondeu")
        return Saude(True, "ok", nome_do_executavel(self.executavel), self._versao)

    def opcoes(self) -> list[dict[str, Any]]:
        return [{
            "key": "tools", "type": "choice", "value": self.ferramentas,
            "choices": [
                {"value": "conversa", "safety": SEM_FERRAMENTAS},
                {"value": "web", "safety": RESTRITO},
                # Terminal, arquivos e o resto: só o dono, neste computador.
                {"value": "completo", "safety": LIVRE, "local_only": True},
            ],
        }]

    def configurar(self, opcoes: dict[str, Any], *, local: bool = False) -> None:
        nivel = opcoes.get("tools")
        if nivel is None:
            return
        if nivel not in FERRAMENTAS:
            raise ErroDoAgente("refused", "nível de ferramentas desconhecido")
        if nivel == "completo" and not local:
            raise ErroDoAgente("refused", "só neste computador se libera o Hermes completo")
        self.ferramentas = nivel

    def responder(self, conversa: Conversa, texto: str, *, instrucoes: str,
                  ao_trecho: Trecho | None = None) -> str:
        if not self.executavel:
            raise ErroDoAgente("unavailable", "Hermes não está instalado")
        argv = [self.executavel, "chat", "--format", "stream-json"]
        texto = neutralizar(texto)
        if conversa.ref:
            argv += ["--resume", conversa.ref]
            # Sem prompt de sistema na CLI, o papel do primeiro turno vira
            # mensagem antiga e o Hermes volta a ser o Hermes. Um lembrete
            # curto, a cada turno, mantém o nome e o jeito de falar.
            aviso = persona.lembrete(neutralizar(instrucoes)) if instrucoes else ""
            pedido = f"{aviso}\n\n{texto}" if aviso else texto
        else:
            lista = FERRAMENTAS[self.ferramentas]
            if lista:
                argv += ["-t", lista]
            # Sessão nova para uma conversa que já tinha história (o Hermes
            # esqueceu a anterior): o que a ponte lembra vai junto, para o
            # assunto não recomeçar do zero.
            if conversa.mensagens:
                texto = neutralizar(com_historico(conversa, texto))
            # O Hermes não tem flag de prompt de sistema na CLI: o papel vai na
            # frente do primeiro pedido, e a sessão o carrega dali em diante.
            pedido = f"{neutralizar(instrucoes)}\n\n{texto}" if instrucoes else texto
        # `--query=…` num argumento só: a transcrição pode começar com hífen
        # ("- Oi, tudo bem?"), e separada do `-q` ela seria lida como flag.
        argv.append(f"--query={pedido}")

        partes: list[str] = []
        final: str | None = None
        sessao: str | None = None
        erro: str | None = None
        try:
            for linha in executar(argv, prazo_s=self.prazo(conversa),
                                  cwd=pasta_de_trabalho(self.id)):
                try:
                    evento = json.loads(linha)
                except ValueError:
                    continue            # banner ou aviso fora do protocolo
                if not isinstance(evento, dict):
                    continue
                tipo = evento.get("type")
                if tipo == "text" and isinstance(evento.get("text"), str):
                    partes.append(evento["text"])
                    if ao_trecho is not None:
                        ao_trecho(evento["text"])
                elif tipo == "result":
                    sessao = evento.get("session_id") or sessao
                    if isinstance(evento.get("text"), str):
                        final = evento["text"]
                    if evento.get("error") or evento.get("exit_code") not in (0, None):
                        erro = str(evento.get("error") or f"exit {evento.get('exit_code')}")
        except ErroDoAgente as e:
            perdida = any(p in e.interno.lower() for p in ("no session", "session not found"))
            if conversa.ref and e.codigo == "unavailable" and perdida:
                # A sessão não existe mais do lado do Hermes (apagada, ou outro
                # perfil): quem chama abre outra, em vez de insistir nesta. Só
                # quando o Hermes DIZ isso — um erro passageiro do provedor não
                # é motivo para jogar a conversa fora.
                conversa.ref = None
                raise ErroDoAgente("session_lost", e.detalhe, e.interno) from e
            raise
        if erro:
            raise ErroDoAgente("bad_response", erro)
        resposta = (final if final is not None else "".join(partes)).strip()
        if not resposta:
            raise ErroDoAgente("bad_response", "o Hermes não devolveu texto")
        if sessao:
            conversa.ref = sessao
        return resposta
