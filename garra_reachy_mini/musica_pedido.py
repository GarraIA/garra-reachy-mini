"""O pedido de música, entendido sem modelo.

Chega aqui o que a pessoa disse depois da frase de ativação. Sai um
`PedidoMusica` quando é COMANDO para o player do Spotify do PC, ou `None`.

A regra é de frase inteira, ancorada no começo: o verbo vem primeiro ("toca",
"pausa", "coloca na fila"). Isso é o que separa o pedido da conversa sobre
música — "eu gosto de ouvir Hotel California", "se eu disser toca uma música",
"não toca agora" — e é por isso que "toca Não Quero Dinheiro" continua sendo
pedido. Na dúvida, `None`: um falso positivo toca música que ninguém pediu; um
falso negativo só leva a frase ao modelo.

`contextual=True` marca o que só é comando de música quando já há música no
assunto ("continua", "próxima", "mais alto"): sem isso, "continua" pode ser
"continue a história".

O nome pedido (`consulta`) sai do texto ORIGINAL, com acento e maiúscula, sem
caractere de controle e com no máximo `CONSULTA_MAX` caracteres. É dado para
a busca, um elemento de argv no PC — nunca comando.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .robo.intencoes import normalizar

OPERACOES = ("play_generic", "play_track", "play_playlist", "play_found", "queue_track", "search_track",
             "pause", "resume", "next", "previous", "now_playing", "set_volume", "change_volume", "capability")
CONSULTA_MAX = 120
# Mais que isto não é comando falado; nem se tenta.
TEXTO_MAX = 600


@dataclass(frozen=True)
class PedidoMusica:
    operacao: str
    consulta: str | None = None
    volume: int | None = None
    contextual: bool = False


_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")
_BORDA = "\"'“”‘’«».,;:!?()[]{}<> \t"

# O vocativo e a cortesia no começo saem — e também a frase de ativação que a
# música estropiou ("fala gata", "fala galera", "ela agarra", medidos em 30/09),
# que numa sessão de ativação aberta chega junto com o pedido.
_INICIO = re.compile(r"^(?:(?:ei|hey|oi|ok|ola|opa|garra|garraia|robo|reachy|mini|por favor|please|entao|"
                     r"falar? g\w*|fala a garra|ela agarra|"
                     r"(?:use|usa|abre|abra|liga|ligue) o spotify e)(?:\s+|$))+")
_FIM = re.compile(r"(?:\s+(?:por favor|please|agora|ai|pra mim|para mim|pra gente|para a gente|garra|"
                  r"obrigado|obrigada|now|for me|no spotify|pelo spotify|on spotify))+$")

# "Quer/vai tocar?" é convite, não pedido; "pôr" sai porque, sem acento, é "por".
_PEDIR = r"(?:(?:voce |vc )?(?:pode|poderia|podia|consegue|conseguiria)\s+)?"
_ENFEITE = r"(?: ai| la| pra mim| para mim| pra gente| para a gente)?"
# "Toca X" é música. "Coloca/põe/bota X" é pôr qualquer coisa em qualquer lugar —
# só vira música com música dita ("a música X", "X no Spotify").
_TOCAR_FORTE = r"(?:toca|toque|tocar|reproduz|reproduza|reproduzir|solta|solte)" + _ENFEITE
_POR = r"(?:coloca|coloque|colocar|poe|ponha|bota|bote|botar)" + _ENFEITE
_TOCAR = r"(?:" + _TOCAR_FORTE + r"|" + _POR + r")"
_DAR_PLAY = r"(?:da|de|dar|manda|mande|aperta|aperte)\s+(?:um\s+|o\s+)?play"
_OUVIR = r"(?:(?:eu\s+)?(?:quero|queria|gostaria de)\s+ouvir|bora ouvir|vamos ouvir)"
_NUMEROS = {"zero": 0, "cinco": 5, "dez": 10, "quinze": 15, "vinte": 20, "trinta": 30, "quarenta": 40,
            "cinquenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90, "cem": 100}
_NUMERO = (r"(?P<n>\d{1,3}|(?:vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa)(?: e cinco)?|"
           r"zero|cinco|dez|quinze|cem)")


def _c(padrao: str) -> re.Pattern[str]:
    return re.compile("^" + padrao + "$")


# (padrão, operação, contextual). A ordem importa: volume e fila antes de
# "coloca X"; "toca essa" e playlist antes de "toca X"; capacidade antes de tudo.
_REGRAS: list[tuple[re.Pattern[str], str, bool]] = [
    (_c(r"(?:voce |vc )?(?:consegue|sabe|pode|poderia) (?:tocar|colocar|por|botar) (?:musica|musicas)"), "capability", False),
    (_c(r"(?:voce|vc) (?:toca|coloca|poe) (?:musica|musicas)"), "capability", False),
    (_c(r"da (?:para|pra) (?:tocar|colocar|por) (?:musica|musicas)"), "capability", False),
    (_c(r"(?:voce |vc )?(?:controla|mexe|comanda|usa|tem|acessa)(?: n?o)? spotify"), "capability", False),
    (_c(r"can you (?:play|control) (?:music|songs|spotify)"), "capability", False),
    # fila
    (_c(r"(?:adiciona|adicione|adicionar|coloca|coloque|colocar|poe|ponha|por|bota|bote|botar|enfileira|"
        r"enfileire|inclui|inclua) (?P<q>.+?) (?:a|na|para a|pra|pra a|no final da) fila"), "queue_track", False),
    (_c(r"(?:adiciona|adicione|coloca|coloque|poe|ponha|bota|bote|inclui|inclua) na fila (?P<q>.+)"),
     "queue_track", False),
    (_c(r"(?:enfileira|enfileire) (?P<q>.+)"), "queue_track", False),
    (_c(r"add (?P<q>.+?) to (?:the |my )?queue"), "queue_track", False),
    (_c(r"queue (?P<q>.+)"), "queue_track", False),
    # pesquisa sem tocar — só com música dita: "pesquisa o preço do dólar" é internet
    (_c(r"(?:procura|procure|procurar|pesquisa|pesquise|pesquisar|busca|busque|buscar|acha|ache|achar|encontra|"
        r"encontre)(?: a| uma)(?: musica| cancao| faixa)(?: chamada)? (?P<q>.+)"), "search_track", False),
    (_c(r"(?:search for|search|find) (?:the |a )?(?:song|track) (?P<q>.+)"), "search_track", False),
    # o que a pesquisa achou
    (_c(_PEDIR + r"(?:" + _TOCAR + r"|play) (?:essa|esta|ela|isso|essa musica|esta musica|it|that|this one)"),
     "play_found", False),
    # playlist
    (_c(_PEDIR + r"(?:" + _TOCAR + r"|" + _DAR_PLAY + r"(?: n[ao]| em)?|play|" + _OUVIR + r")\s+"
        r"(?:(?:a|na|minha|my|the|uma)\s+)*playlist\s+(?:(?:de|do|da|dos|das|chamada|called)\s+)?(?P<q>.+)"),
     "play_playlist", False),
    # "toca algo do Queen": o que vem depois é o artista
    (_c(_PEDIR + r"(?:" + _TOCAR + r"|" + _OUVIR + r")\s+(?:uma musica|alguma musica|umas musicas|musica|musicas|"
        r"algo|alguma coisa|um som) (?:de|do|da|dos|das) (?P<q>.+)"), "play_track", False),
    # genérico
    (_c(_PEDIR + r"(?:" + _TOCAR + r"|" + _OUVIR + r")\s+(?:uma musica|alguma musica|umas musicas|musica|musicas|"
        r"algo|alguma coisa|um som|som|uma cancao|um pouco de musica)"), "play_generic", False),
    (_c(_PEDIR + _DAR_PLAY), "play_generic", False),
    (_c(r"play (?:some |a |me some )?(?:music|song|songs)"), "play_generic", False),
    # pausa
    (_c(r"(?:pausa|pause|pausar|pausa ai|da (?:um )?pause|de (?:um )?pause|dar (?:um )?pause)"
        r"(?: (?:a|o|na|no|essa|esta) (?:musica|spotify|som|cancao|faixa)| isso| the music| music| it)?"), "pause", False),
    (_c(r"(?:para|pare|parar|interrompe|interrompa) (?:a musica|o spotify|o som|a cancao|a faixa|de tocar)"),
     "pause", False),
    (_c(r"stop (?:the )?music"), "pause", False),
    # retomar
    (_c(r"(?:pode )?(?:continua|continue|continuar|retoma|retome|retomar|despausa|despause|despausar) "
        r"(?:a musica|o spotify|o som|a cancao|a faixa|tocando)"), "resume", False),
    (_c(r"(?:volta|volte|voltar) a tocar(?: a musica)?"), "resume", False),
    (_c(r"(?:pode )?(?:retoma|retome|retomar|despausa|despause|despausar)|resume(?: the music)?"), "resume", False),
    (_c(r"(?:pode )?(?:continua|continue|continuar)"), "resume", True),
    # próxima
    (_c(r"(?:a )?proxima (?:musica|faixa|cancao)"), "next", False),
    (_c(r"(?:pula|pule|pular|passa|passe|avanca|avance)(?: essa| esta| a)? (?:musica|faixa|cancao)"), "next", False),
    (_c(r"(?:troca|troque|muda|mude) (?:de|a) musica"), "next", False),
    (_c(r"(?:toca |poe |coloca |bota )?outra musica"), "next", False),
    (_c(r"(?:next|skip)(?: this)? (?:song|track)"), "next", False),
    (_c(r"(?:a )?proxima|(?:pula|pule|passa|passe)(?: essa| esta)?|next|skip"), "next", True),
    # anterior
    (_c(r"(?:volta|volte|voltar) (?:a|uma|pra|para|pra a|para a) (?:musica|faixa|cancao)(?: anterior)?"),
     "previous", False),
    (_c(r"(?:a )?(?:musica|faixa|cancao) anterior"), "previous", False),
    (_c(r"(?:volta|volte) (?:pra|para|pra a|para a) anterior"), "previous", False),
    (_c(r"(?:previous|last) (?:song|track)|go back (?:a|one) (?:song|track)"), "previous", False),
    (_c(r"(?:a )?anterior"), "previous", True),
    # o que está tocando
    (_c(r"(?:que|qual) (?:musica|cancao|faixa) (?:e essa|e esta|esta tocando|ta tocando|toca agora)"),
     "now_playing", False),
    (_c(r"(?:que|qual) (?:e )?a (?:musica|cancao) que (?:esta|ta) tocando"), "now_playing", False),
    (_c(r"(?:o )?que (?:esta|ta) tocando"), "now_playing", False),
    (_c(r"qual (?:e |eh )?(?:essa|esta) (?:musica|cancao)"), "now_playing", False),
    (_c(r"quem (?:canta|esta cantando|ta cantando) (?:essa|esta) (?:musica|cancao)|quem (?:esta|ta) cantando"),
     "now_playing", False),
    (_c(r"de quem e (?:essa|esta) (?:musica|cancao)"), "now_playing", False),
    (_c(r"qual (?:e )?o nome (?:dessa|desta) (?:musica|cancao)"), "now_playing", False),
    (_c(r"what s playing|what (?:song|track) is (?:this|playing)"), "now_playing", False),
    # faixa pelo nome, com verbo genérico E música dita
    (_c(_PEDIR + r"(?:" + _POR + r"|" + _OUVIR + r")\s+(?:a|uma)\s+(?:musica|cancao|faixa)\s+(?P<q>.+)"),
     "play_track", False),
    # faixa pelo nome — por último, é o padrão mais largo; só com verbo de tocar
    (_c(_PEDIR + r"(?:" + _TOCAR_FORTE + r"|" + _DAR_PLAY + r"(?: n[ao]s?| em)?|play)\s+"
        r"(?:(?:(?:a|na|o|no)\s+)?(?:musica|cancao|faixa)\s+)?(?P<q>.+)"), "play_track", False),
]
# Com "no Spotify" dito, os verbos genéricos também pedem música.
_COM_SPOTIFY: list[tuple[re.Pattern[str], str, bool]] = [
    (_c(r"(?:procura|procure|procurar|pesquisa|pesquise|pesquisar|busca|busque|buscar|acha|ache|achar|encontra|"
        r"encontre|search for|search|find)(?: a musica| a cancao| a faixa)? (?P<q>.+)"), "search_track", False),
    (_c(_PEDIR + r"(?:" + _POR + r"|" + _OUVIR + r")\s+(?:(?:a|uma)\s+(?:musica|cancao|faixa)\s+)?(?P<q>.+)"),
     "play_track", False),
]
_SPOTIFY_DITO = re.compile(r"\b(?:no|pelo|on) spotify\b")
# O que talvez seja música e não deu comando: o modelo recebe a situação da
# música para não dizer que não tem esse recurso.
_TALVEZ = re.compile(r"^(?:" + _POR + r"|" + _OUVIR + r"|procura|procure|pesquisa|pesquise|busca|busque|acha|"
                     r"ache)\s+\S")
_TEM_DONO = re.compile(r"\b(?:lista|listas|caderno|anota|anote|lembra|lembre|lembrete|luz|luzes|lampada|"
                       r"tomada|ar condicionado|tv|televisao|ventilador|cortina|termostato)\b")

_VOLUME_ABSOLUTO = _c(r"(?:(?:coloca|coloque|poe|ponha|bota|bote|deixa|deixe|muda|mude|ajusta|ajuste|set|put)\s+)?"
                      r"(?:o |a |the )?volume(?P<mus> da musica| do spotify| do som| da cancao)?\s+"
                      r"(?:(?:em|no|na|para|pra|pro|a|to|at)\s+)?" + _NUMERO +
                      r"(?: por cento| porcento| percent)?")
_VOLUME_RELATIVO = _c(r"(?P<v>aumenta|aumente|aumentar|aumento|sobe|suba|subir|abaixa|abaixe|abaixar|baixa|baixe|"
                      r"baixar|diminui|diminua|diminuir|reduz|reduza|reduzir)(?P<p1> um pouco| um pouquinho)?"
                      r"(?: o| a| de| do)? (?P<obj>volume|som)(?P<mus> da musica| do spotify| do som| da cancao)?"
                      r"(?P<p2> um pouco| um pouquinho)?")
_VOLUME_SOLTO = _c(r"mais (?P<d>alto|baixo)|volume (?P<e>up|down)|turn (?:it |the volume )?(?P<t>up|down)|"
                   r"(?P<m>mais|menos) volume|volume mais (?P<vm>alto|baixo)")
_SOBE = ("aumenta", "aumente", "aumentar", "aumento", "sobe", "suba", "subir")

# O que vem depois de "toca" e não é música: tocar em alguém, no sino, no violão.
_NAO_E_MUSICA = re.compile(
    r"^(?:(?:n[ao]s?|em|pelo|pela)\b.*|aqui|ai|la|isso aqui|de novo|"
    r"(?:(?:o|a|um|uma|no|na|meu|minha)\s+)?(?:campainha|sino|sinos|telefone|celular|interfone|alarme|buzina|"
    r"sirene|violao|guitarra|piano|bateria|flauta|teclado|sanfona|pandeiro|instrumento|tambor|corneta|trompete|"
    r"saxofone|cavaquinho|ukulele)(?:\s.*)?|comigo|contigo|com voce|com a gente|junto|juntos|pra gente)$")


def _consulta(original: list[str], palavras: list[tuple[str, int]], inicio: int, fim: int) -> str | None:
    """O trecho do texto ORIGINAL que corresponde às palavras [inicio, fim]."""
    if inicio > fim or fim >= len(palavras):
        return None
    texto = " ".join(original[palavras[inicio][1]:palavras[fim][1] + 1]).strip(_BORDA)
    while len(texto) > CONSULTA_MAX and " " in texto:
        texto = texto.rsplit(" ", 1)[0].rstrip(_BORDA)
    texto = texto[:CONSULTA_MAX].strip(_BORDA)
    return texto or None


def _numero(texto: str) -> int | None:
    if texto.isdigit():
        return int(texto)
    base, _, resto = texto.partition(" e ")
    valor = _NUMEROS.get(base)
    if valor is None:
        return None
    return valor + (5 if resto == "cinco" else 0)


def interpretar(texto: str) -> PedidoMusica | None:
    """O comando de música que a frase dá, ou `None` quando ela não dá nenhum."""
    if not isinstance(texto, str) or len(texto) > TEXTO_MAX:
        return None
    original = _CONTROLE.sub(" ", texto).split()
    palavras = [(w, i) for i, tok in enumerate(original) for w in normalizar(tok).split()]
    n = " ".join(w for w, _ in palavras)
    m = _INICIO.match(n)
    pula = len(m.group(0).split()) if m else 0
    m = _FIM.search(n)
    corta = len(m.group(0).split()) if m else 0
    miolo = palavras[pula:len(palavras) - corta]
    n = " ".join(w for w, _ in miolo)
    if not n:
        return None

    m = _VOLUME_ABSOLUTO.match(n)
    if m:
        valor = _numero(m.group("n"))
        if valor is None or not 0 <= valor <= 100:
            return None
        return PedidoMusica("set_volume", volume=valor, contextual=not m.group("mus"))
    m = _VOLUME_RELATIVO.match(n)
    if m:
        passo = 10 if (m.group("p1") or m.group("p2")) else 15
        sinal = 1 if m.group("v") in _SOBE else -1
        musical = bool(m.group("mus")) or m.group("obj") == "som"
        return PedidoMusica("change_volume", volume=sinal * passo, contextual=not musical)
    m = _VOLUME_SOLTO.match(n)
    if m:
        sobe = "alto" in (m.group("d"), m.group("vm")) or "up" in (m.group("e"), m.group("t")) or m.group("m") == "mais"
        return PedidoMusica("change_volume", volume=15 if sobe else -15, contextual=True)
    # "toca isso não", "pausa não": a negação no fim desfaz o pedido.
    if n.endswith(" nao") or n == "nao":
        return None

    # Com "no Spotify" dito, os verbos genéricos entram — antes só do padrão largo.
    regras = (_REGRAS[:-1] + _COM_SPOTIFY + _REGRAS[-1:]
              if _SPOTIFY_DITO.search(" ".join(w for w, _ in palavras)) else _REGRAS)
    for padrao, operacao, contextual in regras:
        m = padrao.match(n)
        if not m:
            continue
        if "q" not in padrao.groupindex:
            return PedidoMusica(operacao, contextual=contextual)
        q = m.group("q")
        if _NAO_E_MUSICA.match(q):
            return None
        inicio = n[:m.start("q")].count(" ")
        consulta = _consulta(original, miolo, inicio, inicio + len(q.split()) - 1)
        if consulta is None:
            return None
        return PedidoMusica(operacao, consulta, contextual=contextual)
    return None


def talvez(texto: str) -> bool:
    """Começa como pedido de música ("coloca X", "quero ouvir X") mas não deu
    comando: o modelo precisa saber que a música existe e como se pede."""
    if not isinstance(texto, str) or len(texto) > TEXTO_MAX or interpretar(texto) is not None:
        return False
    n = normalizar(_CONTROLE.sub(" ", texto))
    m = _INICIO.match(n)
    if m:
        n = n[m.end():]
    return bool(_TALVEZ.match(n)) and not _TEM_DONO.search(n)


__all__ = ["CONSULTA_MAX", "OPERACOES", "PedidoMusica", "interpretar", "talvez"]
