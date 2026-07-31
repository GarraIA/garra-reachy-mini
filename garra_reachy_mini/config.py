"""Configuração do garra_reachy_mini.

Precedência por opção: variável de ambiente > config.json (página de
configurações) > padrão. Assim o mesmo pacote funciona no desktop (tudo em
127.0.0.1) e instalado no robô wireless (URLs apontando para o IP do
computador que roda o Garra e o servidor de voz).
"""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import armazenamento

_log = logging.getLogger(__name__)

# Valores-sentinela do .env.example: presentes mas inúteis. Tratados como
# ausentes para o app cair no aviso honesto em vez de chamar a API com lixo.
PLACEHOLDERS = {"coloque_sua_chave_aqui", "cole_sua_chave_aqui", "changeme",
                "your_api_key_here", "sk-xxx"}

# ── Detecção de fala (herdado do ponte_garraia.py) ─────────────────────────
FIM_DE_FALA_S = 0.8      # silêncio que encerra a fala do usuário
FALA_MINIMA_S = 0.4      # ignora ruídos mais curtos que isso
FALA_MAXIMA_S = 25.0     # trava de segurança

FRASES_ESPERA = [
    "Hmm, deixa eu pensar.",
    "Boa pergunta, um instante.",
    "Já te respondo.",
    "Deixa eu ver isso.",
]

SAUDACAO = "Olá! Aqui é o GarraIA falando pelo Reachy Mini. Pode falar comigo."

# Persona dos modos reserva (garra ask / OpenRouter direto), que são LLM puro:
# deixa claro que não há ferramentas, para o modelo não prometer ações.
PERSONA_BASICA = (
    "Você é o GarraIA, assistente pessoal do Michel, falando em voz alta pelo "
    "robô Reachy Mini em MODO BÁSICO: seu modo completo está fora do ar e você "
    "está sem acesso a ferramentas, agenda, e-mail, internet ou outros agentes. "
    "Não prometa executar tarefas; se pedirem algo assim, diga que fará quando "
    "o modo completo voltar. Responda SEMPRE em português do Brasil, em tom "
    "natural, no máximo 3 frases curtas, sem markdown, listas, emojis ou símbolos."
)

# Reforço anexado a cada turno dos modos reserva. No gateway não é preciso:
# o agente reachy_voice já tem essas regras no system prompt.
BREVIDADE = (
    "\n\n(Responda para ser falado em voz alta: no máximo três frases curtas, "
    "português do Brasil, sem markdown.)"
)


def chave_real(valor: str | None) -> str | None:
    """None se a "chave" for vazia ou um placeholder do .env.example."""
    limpo = (valor or "").strip()
    return limpo if limpo and limpo not in PLACEHOLDERS else None


def _valor_dotenv(bruto: str) -> str:
    """Valor de uma linha de .env, sem comentário no fim nem aspas.

    Entre aspas o `#` é literal; fora delas, ` #` ou `\t#` inicia comentário —
    o formato `CHAVE=valor  # explicação` é comum e antes virava parte do valor.
    """
    bruto = bruto.strip()
    if len(bruto) >= 2 and bruto[0] in "'\"" and bruto[-1] == bruto[0]:
        return bruto[1:-1]
    if bruto.startswith("#"):   # `CHAVE=   # explicação` → sem valor
        return ""
    return re.split(r"\s#", bruto, maxsplit=1)[0].strip()


def _carregar_dotenv() -> None:
    """Carrega ~/.config/garra_reachy_mini/.env no ambiente (sem sobrescrever).

    Necessário porque, lançado pelo daemon, o app herda um ambiente que não
    passou pelo shell do usuário — e o CWD do subprocess do garra é
    imprevisível para o dotenvy dele.
    """
    arquivo = armazenamento.diretorio() / ".env"
    try:
        linhas = arquivo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, bruto = linha.partition("=")
        chave, valor = chave.strip(), _valor_dotenv(bruto)
        if chave and valor and chave not in os.environ and valor not in PLACEHOLDERS:
            os.environ[chave] = valor


def _opcao(salvo: dict, env: str, chave: str, padrao):
    valor = os.environ.get(env)
    if valor:
        return valor
    if salvo.get(chave) not in (None, ""):
        return salvo[chave]
    return padrao


def _num(valor, padrao, conversor):
    """Converte tolerando lixo: valor inválido cai no padrão, com aviso.

    Config.carregar() roda no início de run() e no GET /api/config — uma
    exceção aqui deixaria o robô sem app e sem página de configurações.
    """
    if valor is None:
        return padrao
    try:
        return conversor(valor)
    except (TypeError, ValueError):
        _log.warning("Valor inválido %r na configuração; usando %r.", valor, padrao)
        return padrao


def _bool(valor, padrao: bool) -> bool:
    """Lê booleano de env/config.json tolerando as grafias usuais."""
    if valor is None or valor == "":
        return padrao
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().lower() in ("1", "true", "yes", "sim", "on")


def _chave_gateway_do_garra() -> str | None:
    """Lê gateway.api_key do config.yml do próprio Garra, quando na mesma máquina."""
    candidatos = []
    if os.environ.get("GARRAIA_CONFIG_DIR"):
        candidatos.append(Path(os.environ["GARRAIA_CONFIG_DIR"]))
    candidatos += [Path("~/.config/garraia").expanduser(), Path("~/.garraia").expanduser()]
    for pasta in candidatos:
        arquivo = pasta / "config.yml"
        if not arquivo.is_file():
            continue
        try:
            import yaml

            dados = yaml.safe_load(arquivo.read_text(encoding="utf-8")) or {}
            chave = (dados.get("gateway") or {}).get("api_key")
            return str(chave) if chave else None
        except Exception:
            return None
    return None


@dataclass
class Config:
    gateway_url: str
    gateway_key: str | None        # Bearer: só exigido com GARRAIA_LOCK_LEGACY,
                                   # mas enviado sempre que existir
    agent_id: str                  # agente nomeado do Garra (por MENSAGEM, não por sessão)
    gateway_model: str | None      # override de modelo por mensagem (None = padrão do Garra)
    provider: str                  # provider do modo reserva (garra ask)
    model: str                     # modelo do modo reserva (formato OpenRouter)
    timeout_gateway_s: int
    timeout_ask_s: int
    voz_url: str
    janela_turnos: int             # memória client-side dos modos reserva
    garra_bin: str | None
    intervalo_notificacoes_s: float
    limiar: float | None           # limiar de voz fixo (None = auto-calibrar)
    # ── robô ───────────────────────────────────────────────────────────────
    robo_api: str                  # REST do daemon do robô (moves, apps, tracking)
    comportamento_ambiente: bool   # micro-movimentos durante a conversa
    atalhos_locais: bool           # atalho determinístico antes de chamar o cérebro
    tracking_ambiente: bool        # olhar para o usuário durante a conversa
    tracking_ambiente_peso: float
    wobbling_na_fala: bool         # balanço da cabeça reativo ao áudio (daemon)
    camera_fps: float

    @classmethod
    def carregar(cls) -> "Config":
        _carregar_dotenv()
        salvo = armazenamento.carregar_config()
        return cls(
            gateway_url=str(_opcao(salvo, "GARRA_GATEWAY_URL", "gateway_url",
                                   "http://127.0.0.1:3888")).rstrip("/"),
            gateway_key=chave_real(_opcao(salvo, "GARRA_GATEWAY_KEY", "gateway_key", None))
            or _chave_gateway_do_garra(),
            agent_id=str(_opcao(salvo, "GARRA_AGENT_ID", "agent_id", "reachy_voice")),
            gateway_model=_opcao(salvo, "GARRA_GATEWAY_MODEL", "gateway_model", None),
            provider=str(_opcao(salvo, "GARRA_PROVIDER", "provider", "openrouter")),
            model=str(_opcao(salvo, "GARRA_MODEL", "model", "anthropic/claude-haiku-4.5")),
            timeout_gateway_s=_num(_opcao(salvo, "GARRA_TIMEOUT_GATEWAY_S",
                                          "timeout_gateway_s", None), 120, int),
            timeout_ask_s=_num(_opcao(salvo, "GARRA_TIMEOUT_ASK_S",
                                      "timeout_ask_s", None), 60, int),
            voz_url=str(_opcao(salvo, "GARRA_VOZ_URL", "voz_url",
                               "http://127.0.0.1:8123")).rstrip("/"),
            janela_turnos=_num(_opcao(salvo, "GARRA_HISTORICO_TURNOS",
                                      "janela_turnos", None), 8, int),
            garra_bin=_opcao(salvo, "GARRA_BIN", "garra_bin", None),
            intervalo_notificacoes_s=_num(_opcao(salvo, "GARRA_NOTIFICACOES_S",
                                                 "intervalo_notificacoes_s", None),
                                          4.0, float),
            limiar=_num(_opcao(salvo, "GARRA_LIMIAR", "limiar", None), None, float),
            robo_api=str(_opcao(salvo, "GARRA_ROBO_API", "robo_api",
                                "http://reachy-mini.local:8000")).rstrip("/"),
            comportamento_ambiente=_bool(
                _opcao(salvo, "GARRA_COMPORTAMENTO_AMBIENTE", "comportamento_ambiente", None), True),
            atalhos_locais=_bool(
                _opcao(salvo, "GARRA_ATALHOS_LOCAIS", "atalhos_locais", None), True),
            tracking_ambiente=_bool(
                _opcao(salvo, "GARRA_TRACKING_AMBIENTE", "tracking_ambiente", None), False),
            tracking_ambiente_peso=_num(
                _opcao(salvo, "GARRA_TRACKING_PESO", "tracking_ambiente_peso", None), 0.35, float),
            wobbling_na_fala=_bool(
                _opcao(salvo, "GARRA_WOBBLING", "wobbling_na_fala", None), True),
            camera_fps=_num(_opcao(salvo, "GARRA_CAMERA_FPS", "camera_fps", None), 12.0, float),
        )
