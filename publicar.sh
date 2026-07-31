#!/usr/bin/env bash
# Publica o app no Hugging Face Spaces, que é de onde o "Discover apps" do robô
# tira a lista: o daemon consulta os Spaces com a tag `reachy_mini_python_app`
# (reachy_mini/apps/sources/hf_space.py). Não há PR, formulário nem aprovação —
# Space público com a tag certa aparece.
#
#   bash publicar.sh privado    # 1º: Space privado, para instalar e testar no robô
#   bash publicar.sh publico    # 2º: promove a público, depois dos testes reais
#   bash publicar.sh oficial    # 3º: PR opcional p/ a prateleira curada da Pollen
#
# Requer `hf auth login` feito antes. O token NUNCA é lido, impresso ou gravado
# por este script: quem o usa é a biblioteca, direto do armazenamento do HF.
#
# Por que a API e não o `reachy-mini-app-assistant publish`: o publish dele faz
# `git init` + `git add .` + `git commit -m "Initial commit"` nesta pasta e
# aborta se não houver o que commitar (o nosso caso, árvore limpa); e o
# `git push --set-upstream space` trocaria o upstream do `main` do GitHub para o
# Hugging Face. O fallback de API dele existe justamente para token OAuth como o
# nosso, mas sobe a pasta INTEIRA — incluindo `__pycache__`, `build/` e
# `.egg-info`. Aqui subimos exatamente o que o git rastreia.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$APP_DIR")"
PY="$BASE_DIR/reachy_mini_env/bin/python"
ASSISTANT="$BASE_DIR/reachy_mini_env/bin/reachy-mini-app-assistant"

# Identidades fixas. Deliberadamente NÃO derivadas do GitHub: os namespaces são
# diferentes de propósito (GitHub GarraIA, Hugging Face michelbr).
DONO_HF="michelbr"
ESPACO="garra_reachy_mini"
REPO_GH="GarraIA/garra-reachy-mini"

ETAPA="${1:-}"
case "$ETAPA" in
  privado|publico|oficial) ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac

# ── conferências que precedem qualquer escrita ──────────────────────────────
[ "$(git -C "$APP_DIR" rev-parse --show-toplevel)" = "$APP_DIR" ] \
  || { echo "ERRO: raiz do git não é $APP_DIR" >&2; exit 1; }
[ "$(git -C "$APP_DIR" remote get-url origin)" = "https://github.com/$REPO_GH.git" ] \
  || { echo "ERRO: origin não é $REPO_GH" >&2; exit 1; }
[ -z "$(git -C "$APP_DIR" status --porcelain)" ] \
  || { echo "ERRO: árvore suja — commite antes de publicar" >&2; git -C "$APP_DIR" status --short >&2; exit 1; }

CONTA="$("$BASE_DIR/reachy_mini_env/bin/hf" auth whoami 2>/dev/null | sed -n 's/^user=//p')"
[ "$CONTA" = "$DONO_HF" ] \
  || { echo "ERRO: conta do Hugging Face é '${CONTA:-nenhuma}', esperada '$DONO_HF'." >&2
       echo "      Rode: hf auth login" >&2; exit 1; }

SHA="$(git -C "$APP_DIR" rev-parse HEAD)"
VERSAO="$(grep -m1 '^version' "$APP_DIR/pyproject.toml" | cut -d'"' -f2)"
case "$ETAPA" in privado) VIS="private";; publico) VIS="public";; *) VIS="unchanged";; esac

cat <<FIM

GitHub repository:    $REPO_GH
Hugging Face account: $DONO_HF
Hugging Face Space:   $DONO_HF/$ESPACO
Visibility:           $VIS
Source commit:        $SHA
App version:          $VERSAO

FIM

if [ "$ETAPA" = "oficial" ]; then
  # Abre PR no dataset pollen-robotics/reachy-mini-official-app-store. É o ÚNICO
  # passo com aprovação humana, e serve só para destaque na prateleira curada:
  # não é requisito para instalar nem para aparecer no Discover apps.
  exec "$ASSISTANT" publish "$APP_DIR" "Garra Reachy Mini $VERSAO" --official
fi

echo "[CHECK] validação oficial do Reachy Mini App Assistant"
"$ASSISTANT" check "$APP_DIR" >/dev/null || { echo "check falhou" >&2; exit 1; }
echo "[OK] passou"

"$PY" - "$ETAPA" "$DONO_HF/$ESPACO" "$APP_DIR" "$SHA" "$VERSAO" <<'PY'
import subprocess, sys
from huggingface_hub import HfApi

etapa, repo_id, app_dir, sha, versao = sys.argv[1:6]
api = HfApi()   # o token vem do armazenamento do HF; nunca passa por aqui

existe = api.repo_exists(repo_id, repo_type="space")

if etapa == "publico" and not existe:
    sys.exit(f"ERRO: {repo_id} não existe. Publique privado e teste no robô antes.")

if not existe:
    api.create_repo(repo_id, repo_type="space", private=True, space_sdk="static")
    print(f"[OK] Space criado privado: {repo_id}")
else:
    print(f"[OK] Space já existe: {repo_id}")

# Exatamente o que o git rastreia — nada de __pycache__, build/ ou .egg-info.
rastreados = subprocess.check_output(
    ["git", "-C", app_dir, "ls-files"], text=True).split()
print(f"[..] enviando {len(rastreados)} arquivos rastreados pelo git")

api.upload_folder(
    folder_path=app_dir,
    repo_id=repo_id,
    repo_type="space",
    commit_message=f"Garra Reachy Mini {versao} ({sha[:12]})",
    allow_patterns=rastreados,
    delete_patterns=["*"],   # remoções no git também somem do Space
)

if etapa == "publico":
    api.update_repo_settings(repo_id, repo_type="space", private=False)
    print("[OK] Space agora é PÚBLICO")

info = api.space_info(repo_id)
print(f"[OK] privado={info.private} sha={info.sha[:12]} tags={info.tags}")
print(f"     https://huggingface.co/spaces/{repo_id}")
PY
