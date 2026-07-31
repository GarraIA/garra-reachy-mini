#!/usr/bin/env bash
# Instala o Garra para o modo reserva do app (o modo completo usa o gateway
# `garra start` e não depende deste script). Idempotente; nunca sobrescreve
# config existente. Ferramenta de instalação LOCAL — a publicação no Hugging
# Face Spaces NÃO executa isto; rode uma vez em cada máquina.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${GARRA_BIN_DIR:-$APP_DIR/bin}"
DASH_DIR="${GARRA_REACHY_DIR:-$HOME/.config/garra_reachy_mini}"
LOCAL_SRC="${GARRA_SRC_DIR:-$HOME/Documents/Projetos/GarraIA}"
REPO_PUBLICO="michelbr84/GarraRUST"   # instalador oficial do Garra

diga()  { printf '\033[1;36m[garra]\033[0m %s\n' "$*"; }
aviso() { printf '\033[1;33m[aviso]\033[0m %s\n' "$*"; }

normalizar_nome() { # o installer oficial instala como "garraia"; o app aceita ambos
  if [ -x "$BIN_DIR/garraia" ] && [ ! -e "$BIN_DIR/garra" ]; then
    ln -sf garraia "$BIN_DIR/garra"
  fi
}

binario_existente() {
  command -v garra 2>/dev/null || command -v garraia 2>/dev/null \
    || { [ -x "$BIN_DIR/garra" ] && echo "$BIN_DIR/garra"; } || true
}

OS="$(uname -s)"; ARCH="$(uname -m)"

# ── Camada A: já instalado? ─────────────────────────────────────────────────
EXISTENTE="$(binario_existente)"
if [ -n "$EXISTENTE" ]; then
  diga "Garra já instalado: $EXISTENTE ($("$EXISTENTE" --version 2>/dev/null || echo '?'))"
  # O app lançado pelo daemon tem PATH mínimo e só procura em <app>/bin,
  # ~/.config/garra_reachy_mini/bin e ~/.local/bin: materializa o binário lá.
  case "$EXISTENTE" in
    "$BIN_DIR"/*|"$DASH_DIR"/bin/*|"$HOME/.local/bin"/*) ;;
    *)
      mkdir -p "$BIN_DIR"
      ln -sf "$EXISTENTE" "$BIN_DIR/garra"
      diga "Symlink em $BIN_DIR/garra (para o app lançado pelo daemon achá-lo)."
      ;;
  esac
else
  case "$OS/$ARCH" in
    Linux/x86_64|Darwin/arm64|Darwin/aarch64)
      # ── Camada B: installer oficial (binário pré-compilado) ──────────────
      diga "Baixando o Garra pelo installer oficial ($REPO_PUBLICO)..."
      mkdir -p "$BIN_DIR"
      if curl -fsSL "https://raw.githubusercontent.com/$REPO_PUBLICO/main/install.sh" \
        | GARRAIA_SKIP_INIT=1 GARRAIA_SKIP_START=1 GARRAIA_INSTALL_DIR="$BIN_DIR" sh; then
        normalizar_nome
        diga "Instalado em $BIN_DIR"
      else
        aviso "Installer oficial falhou; tentando compilar do código local."
      fi
      ;;
    Linux/aarch64|Linux/arm64)
      aviso "Não há binário linux-aarch64 pré-compilado no release atual do Garra."
      aviso "Opções para o Pi do Reachy wireless:"
      aviso "  1) neste desktop: cross build --release --target aarch64-unknown-linux-gnu --package garraia"
      aviso "     e copie o binário para $BIN_DIR/garra no robô;"
      aviso "  2) no próprio Pi: cargo build --release (Rust 1.93+, pkg-config, libssl-dev);"
      aviso "  3) sem binário, o app usa o modo reserva OpenRouter por HTTP (só precisa da chave)."
      ;;
    *)
      aviso "Plataforma $OS/$ARCH sem binário pré-compilado."
      ;;
  esac

  # ── Camada C: compilar do clone local ─────────────────────────────────────
  if [ -z "$(binario_existente)" ] && [ -d "$LOCAL_SRC" ] && command -v cargo >/dev/null 2>&1; then
    diga "Compilando do código local em $LOCAL_SRC (pode demorar)..."
    # Sob set -e, uma falha de build aqui abortaria o script antes de criar o
    # .env e de dar as instruções finais — justamente o degrau de reserva.
    if cargo build --release --manifest-path "$LOCAL_SRC/Cargo.toml" --package garraia; then
      mkdir -p "$BIN_DIR"
      for nome in garra garraia; do
        if [ -x "$LOCAL_SRC/target/release/$nome" ]; then
          cp "$LOCAL_SRC/target/release/$nome" "$BIN_DIR/$nome"
        fi
      done
      normalizar_nome
    else
      aviso "Compilação local falhou (veja o erro acima); seguindo para o modo reserva."
    fi
  fi

  if [ -z "$(binario_existente)" ]; then
    aviso "Sem binário do garra. O modo reserva usará OpenRouter por HTTP se"
    aviso "OPENROUTER_API_KEY estiver definida; o modo completo (gateway) não é afetado."
  fi
fi

# checagem de dependência do binário dinâmico
if [ "$OS" = "Linux" ] && ! ldconfig -p 2>/dev/null | grep -q 'libssl\.so\.3'; then
  aviso "libssl3 não encontrada — o binário do garra precisa dela (apt install libssl3)."
fi

# ── Config: nunca sobrescreve ───────────────────────────────────────────────
BIN_FINAL="$(binario_existente)"
if [ -n "$BIN_FINAL" ] && "$BIN_FINAL" config check >/dev/null 2>&1; then
  diga "Config do Garra OK ($("$BIN_FINAL" config check 2>/dev/null | grep -m1 'file' || true))"
else
  mkdir -p "$DASH_DIR"
  if [ ! -f "$DASH_DIR/.env" ]; then
    # O .env.example vem com todas as chaves comentadas de propósito: um
    # placeholder ativo faria o app achar que tem credencial e chamar a API.
    cp "$APP_DIR/.env.example" "$DASH_DIR/.env"
    aviso "Criei $DASH_DIR/.env — descomente e preencha sua OPENROUTER_API_KEY."
  fi
  aviso "Config do Garra ausente/incompleta. Para o modo completo, rode 'garra init'"
  aviso "e depois 'garra start' na máquina que fará o papel de cérebro."
fi

# Espelho para o caminho dashboard (app instalado no apps_venv, sem esta pasta)
mkdir -p "$DASH_DIR"
if [ -d "$BIN_DIR" ] && [ ! -e "$DASH_DIR/bin" ]; then
  ln -sfn "$BIN_DIR" "$DASH_DIR/bin"
fi

# binario_existente termina em `|| true`: testar o STATUS daria sempre 0, então
# o fallback tem de vir do VALOR já resolvido acima.
diga "Pronto. Teste rápido: ${BIN_FINAL:-(sem binário)} ask --json -p openrouter -m anthropic/claude-haiku-4.5 'diga oi'"
