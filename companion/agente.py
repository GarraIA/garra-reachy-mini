"""Nome e personalidade do agente de voz — leitura e escrita seguras.

**Uma fonte de verdade: o `config.yml` do gateway.** O agente `reachy_voice`
mora lá, o prompt dele é montado lá, e guardar uma segunda cópia no
`config.json` do robô criaria duas verdades — com o agravante de que a cópia do
robô sobreviveria a um `config.yml` restaurado de backup e o sobrescreveria com
um valor velho. O painel do robô é interface remota, nunca dono.

**Só dois campos são editáveis.** O `system_prompt` do agente é o núcleo
protegido: autorização de ferramentas, comportamento com câmera, e-stop,
privacidade, não inventar execução física. Ele não passa por estas rotas.
O gateway compõe NÚCLEO → NOME → PERSONALIDADE
(`garraia_agents::persona::compose_agent_prompt`), nessa ordem, com o nome
cercado como dado.

**A escrita é transacional.** Backup com timestamp, edição só do bloco do
agente, arquivo temporário no mesmo diretório, revalidação, `os.replace`
atômico, restart só do `garraia.service`, health check — e rollback se o
gateway não voltar. Um `config.yml` corrompido derruba o Garra inteiro, não só
o robô.
"""

from __future__ import annotations

import datetime as _dt
import html
import os
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests
import yaml

AGENTE = "reachy_voice"
UNIDADE = "garraia.service"
GATEWAY_SAUDE = "http://127.0.0.1:3888/api/health"

# Os mesmos limites que o Rust aplica em `persona.rs`. Duplicados de propósito:
# validar aqui devolve um erro que o usuário lê, em vez de truncar em silêncio
# do outro lado.
MAX_NOME = 32
MAX_PERSONA = 4000

PADRAO_NOME = "Garra"
# O operador nasce VAZIO de propósito. Um build público não pode chegar com o
# nome do dono anterior dentro dele, e "assistente do Fulano" num robô que
# acabou de sair da caixa é pior do que não ter dono nenhum.
PADRAO_OPERADOR = ""
PADRAO_PERSONA = ("Be friendly, concise and natural.\n"
                  "Speak in the language used by the user.\n"
                  "Return only the useful final response.")

AVISO = ("Personality instructions cannot override robot safety, privacy or "
         "tool authorization rules.")
AVISO_PRIVACIDADE = ("Identity information included in the agent context may be "
                     "sent to the configured AI provider.")

# Quantos backups do `config.yml` guardar. O bastante para desfazer uma sessão
# de tentativa e erro sem encher o diretório de configuração.
MAX_BACKUPS = 20


class ErroAgente(RuntimeError):
    """Falha esperada, com mensagem para o usuário — nunca com caminho nem segredo."""


class ConflitoAgente(RuntimeError):
    """Revisão desatualizada. Carrega o estado atual para o painel recarregar."""

    def __init__(self, atual: dict) -> None:
        super().__init__("revisão desatualizada")
        self.atual = atual


def arquivo() -> Path:
    return Path(os.environ.get("GARRAIA_CONFIG",
                               "~/.config/garraia/config.yml")).expanduser()


# ── validação ────────────────────────────────────────────────────────────────
def _sem_controle(texto: str, manter_quebras: bool) -> str:
    """Remove caracteres de controle, preservando quebras quando pedido.

    `unicodedata.category(c) == "Cc"` pega o bloco C0/C1 inteiro, que é onde
    moram os bytes que quebrariam o YAML ou entrariam no prompt como lixo.
    """
    return "".join(
        c for c in texto
        if (c == "\n" and manter_quebras) or unicodedata.category(c) != "Cc")


def validar_nome(bruto: Any) -> str:
    """Nome de exibição. Unicode e acentos passam; controle e vazio não.

    O nome é **dado de identidade, não instrução** — quem digitar "Ignore todas
    as instruções" acaba com um robô de nome esquisito, não com um robô
    obediente. Quem garante isso é a cerca do lado do gateway; aqui só se
    impede que o texto carregue algo que a cerca não cobre.
    """
    if not isinstance(bruto, str):
        raise ErroAgente("assistant_name precisa ser texto")
    # As guilhemets são a cerca do gateway: deixá-las passar permitiria ao nome
    # fechar o próprio bloco e virar instrução.
    limpo = _sem_controle(bruto, manter_quebras=False).replace("«", "").replace("»", "")
    limpo = limpo.strip()
    if not limpo:
        raise ErroAgente("informe um nome com pelo menos um caractere visível")
    if len(limpo) > MAX_NOME:
        raise ErroAgente(f"o nome passa de {MAX_NOME} caracteres")
    return limpo


def validar_operador(bruto: Any) -> str:
    """Nome do operador. Vale o mesmo do assistente — e vazio é permitido.

    Vazio não é erro: é "ninguém configurado ainda", que é como um build
    público tem de nascer. `validar_nome` recusa vazio porque o assistente
    sempre tem um nome; o operador pode não ter.
    """
    if not isinstance(bruto, str):
        raise ErroAgente("operator_name precisa ser texto")
    limpo = _sem_controle(bruto, manter_quebras=False).replace("«", "").replace("»", "")
    limpo = limpo.strip()
    if len(limpo) > MAX_NOME:
        raise ErroAgente(f"o nome do operador passa de {MAX_NOME} caracteres")
    return limpo


def validar_persona(bruto: Any) -> str:
    """Prompt de personalidade. Quebras de linha preservadas; limite aplicado."""
    if not isinstance(bruto, str):
        raise ErroAgente("persona_prompt precisa ser texto")
    limpo = _sem_controle(bruto, manter_quebras=True).strip()
    if len(limpo) > MAX_PERSONA:
        raise ErroAgente(f"a personalidade passa de {MAX_PERSONA} caracteres")
    return limpo


def escapar(texto: str) -> str:
    """Para o painel exibir sem interpretar. Não é o que vai ao modelo."""
    return html.escape(texto, quote=False)


# ── leitura ──────────────────────────────────────────────────────────────────
def _carregar() -> dict:
    caminho = arquivo()
    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as e:
        raise ErroAgente("a configuração do Garra não foi encontrada") from e
    except yaml.YAMLError as e:
        raise ErroAgente("a configuração do Garra está ilegível (YAML inválido)") from e
    if not isinstance(dados, dict):
        raise ErroAgente("a configuração do Garra não é um mapa YAML")
    return dados


def _bloco(dados: dict) -> dict:
    agentes = dados.get("agents")
    if not isinstance(agentes, dict) or not isinstance(agentes.get(AGENTE), dict):
        raise ErroAgente(f"o agente {AGENTE} não está configurado no Garra")
    return agentes[AGENTE]


def ler() -> dict[str, Any]:
    """O que o painel mostra. Sem `system_prompt`, sem caminho, sem segredo."""
    dados = _carregar()
    a = _bloco(dados)
    return {
        "agent_id": AGENTE,
        "assistant_name": a.get("assistant_name") or PADRAO_NOME,
        # Para QUEM o sistema foi configurado — nunca quem está falando agora.
        # Vazio é resposta legítima, e o gateway não inventa dono quando falta.
        "operator_name": a.get("operator_name") or "",
        "persona_prompt": a.get("persona_prompt") or "",
        "model": a.get("model") or (dados.get("llm", {}).get("main", {}) or {}).get("model"),
        "revision": int(a.get("identity_revision") or 0),
        "updated_at": a.get("identity_updated_at"),
        "updated_by": a.get("identity_updated_by"),
        # O núcleo existe e é protegido — o painel diz isso sem mostrá-lo.
        "core_prompt_present": bool((a.get("system_prompt") or "").strip()),
        "defaults": {"assistant_name": PADRAO_NOME, "operator_name": PADRAO_OPERADOR,
                     "persona_prompt": PADRAO_PERSONA},
        "limits": {"assistant_name": MAX_NOME, "operator_name": MAX_NOME,
                   "persona_prompt": MAX_PERSONA},
        "warning": AVISO,
        "privacy_warning": AVISO_PRIVACIDADE,
        # O interlocutor: por enquanto sempre desconhecido. A forma já é a
        # definitiva, para que login, painel ou reconhecimento facial entrem
        # aqui sem mudar o contrato de quem consome.
        "speaker_identity": {"status": "unknown", "person_id": None,
                             "display_name": None, "source": None,
                             "confidence": None},
    }


# ── escrita transacional ─────────────────────────────────────────────────────
def _backup(caminho: Path) -> Path:
    marca = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = caminho.with_name(f"{caminho.name}.bak-identidade-{marca}")
    shutil.copy2(caminho, destino)
    os.chmod(destino, 0o600)
    _podar_backups(caminho)
    return destino


def _podar_backups(caminho: Path) -> None:
    """Guarda os mais recentes. Histórico infinito não é histórico, é entulho."""
    antigos = sorted(caminho.parent.glob(f"{caminho.name}.bak-identidade-*"))
    for velho in antigos[:-MAX_BACKUPS]:
        try:
            velho.unlink()
        except OSError:
            pass


def _gravar_atomico(caminho: Path, texto: str, modo: int) -> None:
    """Temporário no MESMO diretório e `os.replace`.

    Mesmo diretório porque `os.replace` só é atômico dentro de um sistema de
    arquivos; `/tmp` costuma ser outro. E o modo é aplicado ANTES do replace,
    para o arquivo nunca existir legível a mais gente do que devia.
    """
    tmp = caminho.with_name(f".{caminho.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(texto)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, modo)
        os.replace(tmp, caminho)
    finally:
        tmp.unlink(missing_ok=True)


def _gateway_saudavel(tentativas: int = 20, intervalo: float = 1.0) -> bool:
    for _ in range(tentativas):
        try:
            if requests.get(GATEWAY_SAUDE, timeout=2).ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(intervalo)
    return False


def _reiniciar_gateway() -> None:
    """Só o `garraia.service`. Voz e companion não têm nada com isto."""
    from . import unidade
    r = unidade.agir("restart", UNIDADE)
    if not r.get("ok"):
        raise ErroAgente(f"systemctl recusou reiniciar o Garra: {r.get('message', '')[:120]}")


def gravar(mudancas: dict, autor: str = "garra-dashboard") -> dict[str, Any]:
    """Aplica nome e/ou personalidade. Ou vale inteiro, ou nada muda."""
    caminho = arquivo()
    dados = _carregar()
    a = _bloco(dados)
    atual = int(a.get("identity_revision") or 0)

    esperada = mudancas.get("revision")
    if esperada is not None and int(esperada) != atual:
        raise ConflitoAgente(ler())

    novo_nome = (validar_nome(mudancas["assistant_name"])
                 if "assistant_name" in mudancas else None)
    novo_operador = (validar_operador(mudancas["operator_name"])
                     if "operator_name" in mudancas else None)
    nova_persona = (validar_persona(mudancas["persona_prompt"])
                    if "persona_prompt" in mudancas else None)
    if novo_nome is None and novo_operador is None and nova_persona is None:
        raise ErroAgente("nada a alterar")

    modo = caminho.stat().st_mode & 0o777
    backup = _backup(caminho)

    if novo_nome is not None:
        a["assistant_name"] = novo_nome
    if novo_operador is not None:
        # Igual à personalidade: vazio REMOVE a chave. `operator_name: ""` no
        # arquivo confundiria quem lesse à mão, e o gateway trata ausente e
        # vazio do mesmo jeito — não inventa dono.
        if novo_operador:
            a["operator_name"] = novo_operador
        else:
            a.pop("operator_name", None)
    if nova_persona is not None:
        # Vazio limpa o campo em vez de gravar string vazia: o gateway trata
        # ausente e vazio igual, e um `persona_prompt: ""` no arquivo confunde
        # quem for ler à mão.
        if nova_persona:
            a["persona_prompt"] = nova_persona
        else:
            a.pop("persona_prompt", None)
    a["identity_revision"] = atual + 1
    a["identity_updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    a["identity_updated_by"] = str(autor)[:60]

    texto = yaml.safe_dump(dados, allow_unicode=True, sort_keys=False,
                           default_flow_style=False, width=100)
    # Revalida ANTES de tocar no arquivo real: serializar e reler é o que pega
    # um valor que o dumper escreveu de um jeito que o parser lê diferente.
    reconferido = yaml.safe_load(texto)
    if (reconferido.get("agents", {}).get(AGENTE, {}).get("identity_revision")
            != atual + 1):
        raise ErroAgente("a configuração não sobreviveu à revalidação; nada foi gravado")

    _gravar_atomico(caminho, texto, modo)
    try:
        _reiniciar_gateway()
    except Exception as e:
        _gravar_atomico(caminho, backup.read_text(encoding="utf-8"), modo)
        raise ErroAgente(f"falha ao reiniciar o Garra; configuração restaurada "
                         f"({type(e).__name__})") from e
    if not _gateway_saudavel():
        # Rollback E restart de novo: deixar o arquivo bom com o serviço morto
        # seria o pior dos dois mundos.
        _gravar_atomico(caminho, backup.read_text(encoding="utf-8"), modo)
        try:
            _reiniciar_gateway()
        except Exception:
            pass
        raise ErroAgente("o Garra não voltou saudável; configuração restaurada")
    return ler()


def restaurar(revisao: int | None = None, autor: str = "garra-dashboard") -> dict[str, Any]:
    """Volta aos padrões de fábrica. Passa pelo mesmo caminho transacional."""
    # `operator_name` NÃO entra: restaurar padrões é sobre a voz do assistente,
    # não sobre esquecer para quem o robô foi configurado. Quem quiser limpar o
    # operador manda o campo vazio de propósito.
    return gravar({"assistant_name": PADRAO_NOME,
                   "persona_prompt": PADRAO_PERSONA,
                   **({"revision": revisao} if revisao is not None else {})}, autor)
