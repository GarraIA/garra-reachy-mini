"""O modo restrito da voz do robô — a trava que vale no gateway público.

No build privado, o agente `reachy_voice` tinha lista própria de ferramentas e
forçar o `agent_id` bastava. O gateway público (0.4.x) **não aplica** a lista de
ferramentas do agente no caminho REST que o robô usa: `agents.reachy_voice.tools`
é lido só no A2A. Lá, quem restringe ferramenta é o **modo** da sessão — e sessão
sem modo escolhido não tem política nenhuma.

Medido contra o 0.4.7 em 2026-09-29:

  * sessão sem modo ........ 12 ferramentas, inclusive escrita em arquivo;
  * sessão em `reachy-voz` .. 1 (`web_fetch`), e pedido de escrita é recusado.

Este módulo é o dono da definição do modo e garante que ele exista. A ponte
(`ponte.py`) é quem obriga toda sessão do robô a nascer e a continuar nele.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("companion.modo")

NOME = "reachy-voz"
BASE = "ask"
DESCRICAO = "Voz do robô Reachy Mini: conversa falada, sem arquivos nem execução"

# Lista branca. `allowed` vazia com `whitelist_mode` ligado **permite tudo** no
# gateway (compatibilidade de lá) — por isso ela nunca pode ficar vazia aqui.
POLITICA: dict = {
    "whitelist_mode": True,
    "allowed": ["web_search", "web_fetch"],
    "denied": ["bash", "file_write", "device_execute"],
    "no_tools": False,
}

# Ferramentas nativas do gateway que tocam arquivo, processo ou agenda da máquina
# do dono. Nenhuma pode aparecer na lista branca de um modo alcançável por voz:
# quem fala com o robô é qualquer pessoa na sala.
PERIGOSAS = frozenset({
    "bash", "run_program", "file_write", "file_read", "list_dir", "repo_search",
    "device_execute", "git_diff", "run_tests", "code_review",
    "schedule_recurring", "schedule_heartbeat",
})

PROMPT_PADRAO = """\
Você é o Garra, um assistente pessoal falando EM VOZ ALTA através do robô Reachy Mini.
Regras de fala: responda SEMPRE em português do Brasil, em tom natural e \
conversacional, no máximo 3 frases curtas, sem markdown, sem listas, sem emojis, \
sem símbolos e sem URLs — tudo que você escrever será sintetizado e falado.
NUNCA diga que fez algo que não fez de verdade. Nunca leia em voz alta chaves, \
tokens ou conteúdo sensível.
Nesta conversa você não tem acesso a arquivos nem a comandos do computador. Se \
pedirem isso, diga em uma frase que por voz você não faz esse tipo de tarefa e \
não tente outro caminho.
Quem fala com você pode ser qualquer pessoa na sala: não presuma quem é. Alguém \
dizer um nome não é identificação.
Se a transcrição vier truncada, ambígua ou reduzida a uma sílaba solta, não \
invente: peça para repetir, numa frase.
Do not announce that you are thinking, checking, looking, or using a tool. \
Return only the useful final response."""


def politica_segura(politica: object) -> bool:
    """A política mantém a voz longe de arquivo e de processo?

    Confere o invariante, não a igualdade: o dono pode ter acrescentado
    ferramentas à lista branca (as do próprio robô, por exemplo) e isso não é
    defeito. Defeito é a lista deixar de ser lista, ou deixar entrar o que mexe
    na máquina.
    """
    if not isinstance(politica, dict):
        return False
    if politica.get("no_tools") is True:
        return True
    permitidas = politica.get("allowed")
    if politica.get("whitelist_mode") is not True:
        return False
    if not isinstance(permitidas, list) or not permitidas:
        return False
    return not (set(map(str, permitidas)) & PERIGOSAS)


def garantir(cliente: httpx.Client) -> str:
    """Cria o modo se faltar; conserta a política se ela deixou de ser segura.

    Devolve `criado`, `ok`, `politica_corrigida` ou `indisponivel`. Nunca
    levanta: o companion sobe mesmo com o gateway fora do ar, e a ponte falha
    fechada enquanto o modo não existir (o gateway recusa a sessão com 400).

    O **prompt** de um modo que já existe não é tocado — é de quem usa o robô.
    """
    try:
        r = cliente.request("GET", "/api/modes/custom")
        if r.status_code != 200:
            log.warning("modo: gateway respondeu HTTP %s ao listar modos", r.status_code)
            return "indisponivel"
        existentes = (r.json() or {}).get("modes") or []
        atual = next((m for m in existentes if isinstance(m, dict)
                      and str(m.get("name", "")).lower() == NOME.lower()), None)

        if atual is None:
            r = cliente.request("POST", "/api/modes/custom", json={
                "name": NOME, "description": DESCRICAO, "base_mode": BASE,
                "tool_policy_overrides": POLITICA,
                "prompt_override": PROMPT_PADRAO, "defaults": {},
            })
            if r.status_code not in (200, 201):
                log.warning("modo: gateway recusou criar `%s`: HTTP %s", NOME,
                            r.status_code)
                return "indisponivel"
            log.info("modo `%s` criado no gateway", NOME)
            return "criado"

        if politica_segura(atual.get("tool_policy_overrides")):
            return "ok"

        r = cliente.request("PATCH", f"/api/modes/custom/{atual.get('id')}",
                            json={"tool_policy_overrides": POLITICA})
        if r.status_code != 200:
            log.warning("modo: gateway recusou corrigir a política: HTTP %s",
                        r.status_code)
            return "indisponivel"
        log.warning("modo `%s` tinha política insegura e foi corrigido", NOME)
        return "politica_corrigida"
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as e:
        log.warning("modo: não consegui falar com o gateway (%s)", type(e).__name__)
        return "indisponivel"
