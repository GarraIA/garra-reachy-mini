"""A escuta de contexto e o caderno na tela do painel.

O interruptor que manda a conversa da sala para fora de casa não pode ser um
checkbox a mais: a dica tem de dizer o que acontece. E o que o robô anotou tem
de aparecer em algum lugar que não dependa de acreditar no modelo.
"""

from __future__ import annotations

import pathlib
import re

PAINEL = pathlib.Path(__file__).resolve().parents[1] / "garra_reachy_mini" / "static"
HTML = (PAINEL / "reachy.html").read_text(encoding="utf-8")
JS = (PAINEL / "reachy.js").read_text(encoding="utf-8")
I18N = (PAINEL / "i18n.js").read_text(encoding="utf-8")
EN = I18N[I18N.index("  en: {"):I18N.index("  pt: {")]
PT = I18N[I18N.index("  pt: {"):]

CHAVES = ("conversa.contexto", "conversa.contexto_dica", "conversa.contexto_janela",
          "conversa.contexto_esquecer", "conversa.contexto_estado",
          "conversa.contexto_esquecido", "conversa.caderno", "conversa.caderno_dica",
          "caderno.titulo", "caderno.vazio", "caderno.atualizar", "caderno.apagar_item",
          "caderno.apagar_lista", "caderno.itens", "caderno.lido")


def valor(idioma: str, chave: str) -> str:
    m = re.search(rf"'{re.escape(chave)}':\s*(['\"])(.*?)(?<!\\)\1,?\s*\n", idioma)
    assert m, f"{chave} não tem tradução"
    return m.group(2)


def funcao(nome: str) -> str:
    inicio = JS.index(f"function {nome}(")
    return JS[inicio:JS.index("\n}\n", inicio)]


# ─── os controles existem ────────────────────────────────────────────────────
def test_os_controles_estao_no_cartao_da_conversa() -> None:
    bloco = HTML[HTML.index('id="conversa-bloco"'):HTML.index('id="btn-conversa-salvar"')]
    for ident in ("conversa-contexto", "conversa-contexto-janela", "btn-contexto-esquecer",
                  "conversa-contexto-estado", "conversa-caderno"):
        assert f'id="{ident}"' in bloco, ident


def test_a_janela_do_painel_tem_os_limites_do_servidor() -> None:
    campo = HTML[HTML.index('id="conversa-contexto-janela"') - 60:][:200]
    assert 'min="30"' in campo and 'max="300"' in campo


def test_a_escuta_de_contexto_e_subordinada_a_frase_de_ativacao() -> None:
    """Sem a frase não existe ambiente; o painel não pode oferecer o que não faz nada."""
    linha = HTML[:HTML.index('id="conversa-contexto"')].rsplit("<label", 1)[1]
    assert 'data-conversa-wake-sub="1"' in linha
    assert HTML.index('id="conversa-wake"') < HTML.index('id="conversa-contexto"')


def test_o_caderno_tem_um_cartao_proprio() -> None:
    assert 'id="caderno-bloco"' in HTML and 'id="caderno-listas"' in HTML
    assert HTML.index('id="conversa-bloco"') < HTML.index('id="caderno-bloco"')


# ─── ler e gravar ────────────────────────────────────────────────────────────
def test_o_painel_grava_os_tres_campos() -> None:
    corpo = JS[JS.index("$('btn-conversa-salvar').addEventListener"):]
    corpo = corpo[:corpo.index("}));")]
    assert "ambient_context_enabled: $('conversa-contexto').checked" in corpo
    assert "ambient_context_window_s: Number($('conversa-contexto-janela').value)" in corpo
    assert "notebook_enabled: $('conversa-caderno').checked" in corpo


def test_config_sem_as_chaves_aparece_desligada() -> None:
    """`=== true`, e não `!== false`: o padrão destes é DESLIGADO."""
    pinta = funcao("pintarConversa")
    assert "$('conversa-contexto').checked = c.ambient_context_enabled === true" in pinta
    assert "$('conversa-caderno').checked = c.notebook_enabled === true" in pinta


def test_o_botao_de_esquecer_chama_a_rota_e_nao_mostra_conteudo() -> None:
    assert "api('/api/robot/ambient-context', { method: 'DELETE' })" in JS
    estado = funcao("carregarContexto")
    assert "api('/api/robot/ambient-context')" in estado
    assert "utterances" in estado


# ─── o caderno na tela ───────────────────────────────────────────────────────
def test_o_caderno_e_lido_e_apagado_pelas_rotas_do_robo() -> None:
    assert "api('/api/robot/notebook')" in funcao("carregarCaderno")
    assert "method: 'DELETE'" in funcao("apagarDoCaderno")
    assert "/api/robot/notebook/" in funcao("apagarDoCaderno")


def test_o_que_foi_anotado_e_escapado_antes_de_virar_html() -> None:
    """O item foi escrito por um modelo que leu conversa de sala."""
    pinta = funcao("pintarCaderno")
    assert "esc(item)" in pinta and "esc(nome)" in pinta
    # As interpolações MAIS INTERNAS: é nelas que o valor entra no HTML. As de
    # fora só juntam pedaços que já passaram por aqui.
    internas = re.findall(r"\$\{([^{}`]*)\}", pinta)
    assert len(internas) >= 6
    for interpolacao in internas:
        assert interpolacao.strip().startswith("esc(") or interpolacao.strip() == "i", (
            f"`{interpolacao}` entra no HTML sem escapar")


def test_o_nome_da_lista_vai_codificado_na_url() -> None:
    assert "encodeURIComponent(lista)" in funcao("apagarDoCaderno")


def test_o_caderno_se_atualiza_quando_o_robo_anota() -> None:
    trata = funcao("tratarEvento")
    caso = trata[trata.index("case 'notebook.changed'"):]
    assert "carregarCaderno()" in caso[:caso.index("break")]


def test_o_painel_carrega_o_caderno_e_o_estado_da_escuta_ao_abrir() -> None:
    arranque = JS[JS.index("(async function iniciar()"):]
    assert "carregarCaderno()" in arranque and "carregarContexto()" in arranque


# ─── o que a tela diz ────────────────────────────────────────────────────────
def test_tudo_tem_traducao_nos_dois_idiomas() -> None:
    for chave in CHAVES:
        assert valor(EN, chave) and valor(PT, chave), chave
    usadas = set(re.findall(r'data-i18n(?:-title)?="((?:conversa\.(?:contexto|caderno)|caderno\.)[^"]*)"',
                            HTML))
    assert usadas and usadas <= set(CHAVES), usadas - set(CHAVES)


def test_toda_chave_que_o_script_pede_existe_nos_dois_idiomas() -> None:
    """Chave sem tradução aparece na tela como `caderno.lido`, crua."""
    usadas = set(re.findall(r"\bt\('([a-z_]+\.[a-z0-9_.]+)'", JS))
    faltam = sorted(k for k in usadas if f"'{k}'" not in EN or f"'{k}'" not in PT)
    assert faltam == []


def test_a_dica_diz_para_onde_a_conversa_vai() -> None:
    """Sem eufemismo: quem liga tem de saber que a conversa sai de casa."""
    pt, en = valor(PT, "conversa.contexto_dica"), valor(EN, "conversa.contexto_dica")
    assert "nuvem" in pt and "memória" in pt and "disco" in pt
    assert "cloud" in en and "memory" in en and "disk" in en


def test_a_dica_da_frase_de_ativacao_deixou_de_prometer_o_que_nao_vale_sempre() -> None:
    pt, en = valor(PT, "conversa.wake_dica"), valor(EN, "conversa.wake_dica")
    assert "escuta de contexto" in pt.lower()
    assert "context" in en.lower()


def test_a_dica_do_caderno_diz_onde_ele_fica() -> None:
    assert "arquivo" in valor(PT, "conversa.caderno_dica")
    assert "file" in valor(EN, "conversa.caderno_dica")
