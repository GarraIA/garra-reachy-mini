"""O painel em cinco telas: INÍCIO · CONVERSA · CONTROLE (Robô, Casa, Música) ·
APPS · AJUSTES.

Sem navegador aqui: o que se prova é a estrutura que o CSS e o JS assumem —
uma nav com no máximo cinco itens, cada um apontando para uma tela que
existe, o STOP no cabeçalho (sempre à vista), os cartões novos com os ids
que o JS liga, e cada texto com tradução nos dois idiomas. O que precisa de
pixel (320 px sem rolagem, 44×44) é conferido por captura de tela no
`docs/plans` da missão.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAINEL = Path(__file__).resolve().parent.parent / "garra_reachy_mini" / "static"
HTML = (PAINEL / "reachy.html").read_text(encoding="utf-8")
JS = (PAINEL / "reachy.js").read_text(encoding="utf-8")
CSS = (PAINEL / "reachy.css").read_text(encoding="utf-8")
I18N = (PAINEL / "i18n.js").read_text(encoding="utf-8")

TELAS = ["inicio", "conversa", "controle", "apps", "ajustes"]


def _dicionario(idioma: str) -> set[str]:
    inicio = I18N.index(f"  {idioma}: {{")
    fim = I18N.index("\n  },", inicio)
    return set(re.findall(r"^\s+'([a-z_.]+)':", I18N[inicio:fim], re.M))


# ─── telas e navegação ───────────────────────────────────────────────────────
def test_as_cinco_telas_existem_nesta_ordem() -> None:
    posicoes = [HTML.index(f'id="tela-{t}" data-tela="{t}"') for t in TELAS]
    assert posicoes == sorted(posicoes)
    assert HTML.count('class="tela"') == 5


def test_a_nav_tem_no_maximo_cinco_itens_e_cada_um_leva_a_uma_tela() -> None:
    nav = HTML[HTML.index('<nav id="nav-telas"'):HTML.index("</nav>")]
    itens = re.findall(r'href="#([a-z]+)" data-tela="([a-z]+)" class="nav-item"', nav)
    assert 1 <= len(itens) <= 5
    assert [h for h, _ in itens] == TELAS and [d for _, d in itens] == TELAS


def test_a_nav_fica_no_topo_junto_do_stop_mas_fora_do_cabecalho() -> None:
    topo = HTML[HTML.index('<div id="topo">'):HTML.index("<main>")]
    cabecalho = topo[topo.index("<header>"):topo.index("</header>")]
    assert 'id="estop"' in cabecalho and 'id="nav-telas"' in topo
    # fora do <header>: o `backdrop-filter` dele prenderia a barra fixa
    assert 'id="nav-telas"' not in cabecalho
    assert "#topo { position: sticky; top: 0; z-index: 20; }" in CSS
    # e no celular vira barra inferior fixa, com alvo de 44 px
    assert "position: fixed; left: 0; right: 0; bottom: 0" in CSS
    assert "min-height: 44px" in CSS


def test_o_controle_tem_tres_subabas() -> None:
    controle = HTML[HTML.index('id="tela-controle"'):HTML.index('id="tela-apps"')]
    assert re.findall(r'href="#controle/([a-z]+)" role="tab" data-sub="([a-z]+)"', controle) == [
        ("robo", "robo"), ("casa", "casa"), ("musica", "musica")]
    for sub in ("robo", "casa", "musica"):
        assert f'id="sub-{sub}" data-sub="{sub}"' in controle


def test_o_endereco_manda_na_tela() -> None:
    assert "window.addEventListener('hashchange', mostrarTela)" in JS
    assert "const TELAS = ['inicio', 'conversa', 'controle', 'apps', 'ajustes']" in JS
    assert "const SUBS = ['robo', 'casa', 'musica']" in JS
    # a tela escondida é `hidden`, e o CSS não deixa o grid atropelar isso
    assert ".tela[hidden], .sub[hidden] { display: none; }" in CSS


@pytest.mark.parametrize("tela,ids", [
    ("inicio", ["status-lista", "moldura-camera", "resumo-integracoes", "rapidos"]),
    ("conversa", ["chat-corpo", "caderno-bloco"]),
    ("controle", ["joystick", "expressoes-principais", "sel-danca", "casa-bloco", "musica-bloco"]),
    ("apps", ["lista-apps"]),
    ("ajustes", ["conversa-bloco", "visao-bloco", "familia-bloco", "identidade-bloco", "diagnostico-bloco",
                 "agentes-bloco", "logs"]),
])
def test_cada_tela_tem_os_seus_cartoes(tela, ids) -> None:
    inicio = HTML.index(f'id="tela-{tela}"')
    seguinte = TELAS.index(tela) + 1
    fim = HTML.index(f'id="tela-{TELAS[seguinte]}"') if seguinte < len(TELAS) else HTML.index("</main>")
    trecho = HTML[inicio:fim]
    for i in ids:
        assert f'id="{i}"' in trecho, i


# ─── cartões novos ───────────────────────────────────────────────────────────
def test_a_visao_tem_todos_os_controles_pedidos() -> None:
    bloco = HTML[HTML.index('id="visao-bloco"'):HTML.index('id="familia-bloco"')]
    for i in ("visao-ligada", "visao-provedor", "visao-local-endpoint", "visao-local-modelo", "visao-nuvem-modelo",
              "visao-nuvem", "visao-nuvem-aviso", "btn-visao-salvar", "btn-visao-testar", "btn-visao-analisar",
              "visao-estado", "visao-resultado"):
        assert f'id="{i}"' in bloco, i
    assert '<option value="auto"' in bloco and '<option value="local"' in bloco and '<option value="openrouter"' in bloco
    # o aviso de privacidade acompanha a caixa da nuvem
    assert "$('visao-nuvem-aviso').hidden = !$('visao-nuvem').checked" in JS
    assert "vision_cloud_enabled: ligada && $('visao-nuvem').checked" in JS  # desligar a visão desliga a nuvem


def test_a_casa_configura_e_mostra_mas_nao_executa() -> None:
    bloco = HTML[HTML.index('id="casa-bloco"'):HTML.index('id="sub-musica"')]
    assert 'type="password" id="casa-token"' in bloco
    for i in ("casa-url", "btn-casa-conectar", "btn-casa-remover", "casa-voz", "btn-casa-entidades", "casa-entidades"):
        assert f'id="{i}"' in bloco, i
    # nenhum botão de "ligar luz": o painel não executa aparelhos
    assert "device_execute" not in JS and "/api/robot/casa/executar" not in JS
    assert "'/api/robot/casa/voz'" in JS


def test_a_musica_aponta_o_loopback_do_pc_e_nao_promete_voz() -> None:
    bloco = HTML[HTML.index('id="musica-bloco"'):HTML.index('id="tela-apps"')]
    assert "http://127.0.0.1:8125/spotify/conectar" in bloco
    assert 'data-musica="play"' in bloco and 'data-musica="pause"' in bloco
    assert 'id="musica-volume"' in bloco and 'id="musica-dispositivos"' in bloco
    for idioma in ("en", "pt"):
        inicio = I18N.index(f"  {idioma}: {{")
        trecho = I18N[inicio:I18N.index("\n  },", inicio)]
        politica = re.search(r"'musica\.politica': '((?:[^'\\]|\\.)*)'", trecho).group(1)
        assert "Spotify" in politica and ("Voice control" in politica or "Comando por voz" in politica)


def test_a_familia_exige_consentimento_na_tela_e_no_js() -> None:
    bloco = HTML[HTML.index('id="familia-bloco"'):HTML.index('id="identidade-bloco"')]
    for i in ("familia-ligada", "familia-perfis", "familia-nome", "familia-consente", "familia-por", "familia-menor",
              "familia-responsavel", "btn-familia-criar", "btn-familia-testar", "familia-resultado"):
        assert f'id="{i}"' in bloco, i
    assert "if (!consent.accepted || !consent.by)" in JS
    assert "if (menor && !consent.guardian)" in JS
    # apagar pede dois cliques, sem `confirm()` (trava a página e não traduz)
    codigo = "\n".join(l for l in JS.splitlines() if not l.strip().startswith("//"))
    assert "confirm(" not in codigo and "alert(" not in codigo
    assert "familia.apagar_confirmar" in JS


def test_o_diagnostico_separa_nao_configurado_de_indisponivel() -> None:
    assert "function itensDoRegistro" in JS
    assert "'/api/robot/diagnostico'" in JS
    from_js = JS[JS.index("function itensDoRegistro"):JS.index("function pintarDiagnostico")]
    assert "not_configured" in from_js and "unavailable" in from_js


def test_itens_do_registro_leem_a_frase_do_gateway() -> None:
    """A frase real do gateway 0.4.7 vira três itens com motivo."""
    import json
    import subprocess

    frase = ("11 visivel(is) · indisponivel: device_execute (no_devices), device_read (no_devices) "
             "· nao configurado: bash (not_configured)")
    trecho = JS[JS.index("function itensDoRegistro"):JS.index("function pintarDiagnostico")]
    script = trecho + f"\nconsole.log(JSON.stringify(itensDoRegistro({json.dumps(frase)})));"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        pytest.skip(f"node indisponível: {r.stderr[:80]}")
    saida = json.loads(r.stdout)
    assert saida["visiveis"] == 11
    assert saida["itens"] == [
        {"nome": "device_execute", "estado": "unavailable", "motivo": "no_devices"},
        {"nome": "device_read", "estado": "unavailable", "motivo": "no_devices"},
        {"nome": "bash", "estado": "not_configured", "motivo": "not_configured"},
    ]
    # o terceiro grupo do gateway ("fora do ar") e nomes com barra
    frase2 = "9 visivel(is) · fora do ar: github__list_issues (failed), mcp/* (retrying)"
    script2 = trecho + f"\nconsole.log(JSON.stringify(itensDoRegistro({json.dumps(frase2)})));"
    r2 = subprocess.run(["node", "-e", script2], capture_output=True, text=True, timeout=20)
    saida2 = json.loads(r2.stdout)
    assert saida2["visiveis"] == 9
    assert saida2["itens"] == [
        {"nome": "github__list_issues", "estado": "unhealthy", "motivo": "failed"},
        {"nome": "mcp/*", "estado": "unhealthy", "motivo": "retrying"},
    ]


# ─── tradução e acessibilidade ───────────────────────────────────────────────
def test_todo_texto_da_tela_tem_traducao_nos_dois_idiomas() -> None:
    en, pt = _dicionario("en"), _dicionario("pt")
    assert en == pt, en ^ pt
    usadas = set(re.findall(r'data-i18n(?:-[a-z-]+)?="([a-z_.]+)"', HTML))
    usadas |= {m for m in re.findall(r"\bt\('([a-z_.]+)'", JS)}
    faltam = sorted(k for k in usadas if k not in en)
    assert faltam == [], faltam


@pytest.mark.parametrize("prefixo,codigos", [
    ("visao.erro_", ["disabled", "camera_unavailable", "stale_frame", "bridge_unreachable", "provider_offline",
                     "model_missing", "model_without_vision", "cloud_disabled", "auth_failed", "timeout",
                     "bad_image", "provider_error"]),
    ("familia.amostra_", ["no_face", "many_faces", "too_small", "blurry", "too_dark", "too_bright", "no_embedding",
                          "enough_samples", "different_person"]),
    ("familia.erro_", ["disabled", "camera_unavailable", "stale_frame", "bridge_unreachable", "service_offline",
                       "timeout", "bad_image", "service_error", "not_found", "consent_required",
                       "guardian_required", "invalid_name"]),
    ("casa.estado_", ["not_configured", "pending", "ok", "broken"]),
    ("musica.estado_", ["not_configured", "not_connected", "connected"]),
    ("musica.erro_", ["premium_required", "no_active_device", "rate_limited", "auth_expired", "not_connected"]),
    ("diag.status_", ["ok", "warning", "error", "not_configured", "skipped"]),
    ("diag.", ["unavailable", "not_configured", "unhealthy", "ok", "unknown"]),
    ("familia.erro_", ["store_corrupt", "bridge_invalid_response", "bridge_token_rejected"]),
])
def test_os_codigos_do_servidor_tem_texto(prefixo, codigos) -> None:
    en = _dicionario("en")
    faltam = [c for c in codigos if f"{prefixo}{c}" not in en]
    assert faltam == [], faltam


def test_botoes_e_links_tem_foco_visivel_e_a_nav_tem_rotulo() -> None:
    assert "button:focus-visible" in CSS and ".nav-item:focus-visible" in CSS
    assert 'data-i18n-aria-label="nav.rotulo"' in HTML
    assert "aria-current" in JS


def test_nada_escuta_em_todas_as_interfaces_por_causa_do_painel() -> None:
    assert "0.0.0.0" not in HTML and "0.0.0.0" not in JS
