"""GarraIA no Reachy Mini — o robô vira o corpo do agente Garra.

Fluxo: microfone do robô → Whisper (servidor_voz) → gateway do Garra
(modo completo; reservas: garra ask / OpenRouter) → Chatterbox pt
(servidor_voz) → alto-falante do robô + movimento.

Estados do loop principal — única thread que fala e mexe no áudio:
OUVINDO → TRANSCREVENDO → PENSANDO → FALANDO → OUVINDO. O poller de
notificações roda à parte e apenas enfileira; os eventos são falados quando o
usuário está em silêncio.

Este processo é o **dono único do robô**. Quem quiser movê-lo — painel web,
ferramentas do Garra pelo MCP, atalhos de voz — passa pelo `ControladorRobo`,
que serializa tudo numa thread executora só. É o que impede dois comandos
simultâneos de brigarem pela cabeça.

Portas que ele sobe:
  8042  painel `/reachy`, API REST/WS do robô, página de configurações
        (na rede dentro do robô wireless, onde o daemon da Pollen já está
        aberto sem autenticação; loopback em qualquer outro lugar)
"""

import asyncio
import dataclasses
import random
import threading
import time
from typing import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

import numpy as np
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from reachy_mini import ReachyMini, ReachyMiniApp

from enum import Enum

from . import ao_vivo, armazenamento, ativacao, roteador
from . import caderno as caderno_mod
from .acompanhamento import Acompanhamento
from .cerebro import (AVISO_SEM_CEREBRO, FALHA_GENERICA, Cerebro,
                      RespostaCerebro)
from .cerebro import sondar as cerebro_sondar
from . import conversa
from .config import (FALA_MINIMA_S, FRASES_ESPERA, FRASES_PROGRESSO, SAUDACAO,
                     Config)
from .eventos import FilaEventos
from .robo import intencoes
from .robo.acoes import ControladorRobo
from .robo.backends import BackendSdk, BackendSimulado
from .robo import comportamento as fases
from .robo.comportamento import Comportamento
from .robo.daemon_api import DaemonAPI
from . import servicos as servicos_mod
from . import casa, familia, musica, musica_pedido, relogio, visao
from .segmentador import Segmentador
from .servicos import Servicos
from .sistema import Sistema
from .voz import VozClient, frases, limpar_para_voz
from .web import (ContextoWeb, FrameHub, PonteChat, montar, preparar,
                  resolver_politica)
from .web.integracoes import Ponte as PonteIntegracoes
from .web import trava

# A página é servida sem autenticação no loopback (é o daemon quem sobe o
# uvicorn): nada de segredo sai pelo GET /api/config.
SEGREDOS = ("gateway_key", "voz_token")
MASCARA = "***"

# Quantas falhas seguidas de STT/TTS bastam para concluir que o servidor de voz
# morreu e voltar ao modo painel, em vez de insistir contra um serviço morto.
FALHAS_VOZ_ATE_DESISTIR = 3
# Sem cérebro configurado, avisa no máximo uma vez a cada tanto: repetir a cada
# frase vira ladainha, e ficar mudo parece defeito.
SEM_CEREBRO_INTERVALO_S = 45.0
# De quanto em quanto o loop de voz reconfere gateway e cérebro. Sem isto, o
# estado deles congela no instante em que a voz sobe — e a voz costuma subir
# ANTES de o desktop estar configurado, que é exatamente quando importa.
RECONFERIR_CEREBRO_S = 20.0
# Quanto o laço de voz precisa durar para contar como ciclo saudável. Abaixo
# disso ele voltou por falha (voz sumiu, cérebro sumiu, config mudou), e
# reentrar na hora vira laço apertado — que foi o que esgotou os FDs.
CICLO_VOZ_ESTAVEL_S = 30.0


# Música. Um comando solto ("continua", "próxima", "mais alto") só é sobre
# música até este tanto depois do último comando de música atendido.
MUSICA_ASSUNTO_S = 30 * 60.0
# O pedido ao PC leva de 2 a 8 s (busca, play e a conferência, medidos em
# 30/09). Passou disto sem resposta, o robô avisa que está indo.
MUSICA_AVISO_S = 1.5
# Teto do pedido inteiro: sem resposta, a fala é a de falha, e ninguém toca
# "de novo" por cima. Acima do prazo do cliente (musica.TIMEOUT_S).
MUSICA_PRAZO_S = 30.0
# Música tocando e alguém tentando chamar o robô (a frase de ativação se perdeu
# nela, medido em 30/09): a música fica baixa este tanto, para a repetição ser ouvida.
ESCUTA_ABAFADA_S = 10.0
# O que abaixa a música e mantém o laço vivo se a música estiver tocando.
_MUSICA_SEGUE = ("play_generic", "play_track", "play_playlist", "play_found", "resume", "next", "previous",
                 "now_playing", "queue_track", "search_track")


# Rejeição em sala barulhenta pode acontecer a cada frase dita perto do robô.
# O evento é diagnóstico, não telemetria de tudo que se fala: no máximo um por
# janela, e um contador do que foi agregado.
REJEICAO_INTERVALO_S = 30.0


def troca_de_sessao(cerebro_atual: list) -> "Callable[[], str | None]":
    """A troca de sessão do cérebro EM USO, e não do que existia ao ligar.

    O laço de voz reconstrói o cérebro quando o gateway volta. Amarrar a rota
    ao método do cérebro antigo fazia "nova conversa" responder `ok` sem
    trocar nada: quem atendia era um cérebro já fechado, em modo reserva.
    """
    return lambda: cerebro_atual[0].nova_sessao()


class ResultadoFala(Enum):
    """Por que uma tentativa de fala terminou como terminou.

    `falar()` devolvia `None` e levantava só para "já estou falando". Um `False`
    genérico — ou silêncio — não distingue "o usuário desligou a voz" de "o TTS
    caiu", e essas duas coisas pedem reações opostas de quem chamou: a primeira
    é preferência respeitada, a segunda é defeito.
    """

    FALADA = "spoken"
    DESABILITADA = "speech_output_disabled"
    TTS_INDISPONIVEL = "tts_unavailable"
    FALHA = "playback_failed"


def _salva_publica(salva: dict) -> dict:
    return {k: (MASCARA if k in SEGREDOS and v else v) for k, v in salva.items()}


class OpcoesSalvas(BaseModel):
    gateway_url: str | None = None
    gateway_key: str | None = None
    voz_url: str | None = None
    voz_token: str | None = None
    agent_id: str | None = None
    gateway_model: str | None = None
    provider: str | None = None
    model: str | None = None
    garra_bin: str | None = None
    janela_turnos: int | None = None
    # Ligada pelo companion ao parear: daí em diante endereços e credenciais
    # só mudam com a assinatura do PC (ver web/trava.py).
    trava_pc: bool | None = None


class GarraReachyMini(ReachyMiniApp):
    # Literal obrigatório: o daemon lê este valor do FONTE por regex
    # (`apps/sources/local_common_venv.py:186`) sem importar o módulo, e o
    # repassa ao dashboard sem reescrever nada. `0.0.0.0:8042` é a convenção do
    # template oficial. O `__init__` sobrepõe o atributo de instância com a
    # política resolvida em tempo de execução, e é ela que decide o bind real.
    #
    # Consequência conhecida da plataforma: no navegador de outra máquina
    # `0.0.0.0` aponta para a própria máquina, então o link do dashboard não
    # abre o painel do robô. Não há como consertar do nosso lado — por isso
    # `_anunciar()` imprime a URL que funciona de verdade.
    custom_app_url: str | None = "http://0.0.0.0:8042"
    request_media_backend: str | None = None

    def __init__(self, running_on_wireless: bool = False) -> None:
        # Antes do super(): é ele quem lê `custom_app_url` para montar o uvicorn.
        self.politica = resolver_politica()
        self.custom_app_url = self.politica.url
        super().__init__(running_on_wireless)
        # O porteiro (CORS, origem, token, limite) precisa entrar ANTES de o
        # uvicorn subir — e o `wrapped_run` sobe o servidor antes de chamar
        # `run()`. Ele lê o contexto de `app.state` quando o robô ficar pronto.
        if self.settings_app is not None:
            preparar(self.settings_app, self.politica)
        self.controlador: ControladorRobo | None = None
        self.hub: FrameHub | None = None
        self.comportamento: Comportamento | None = None
        self.chat: PonteChat | None = None
        self.integracoes: PonteIntegracoes | None = None
        # A escuta de contexto e o caderno. Um por app: a memória do que foi
        # ouvido tem de sobreviver entre turnos, e o painel precisa alcançar a
        # MESMA que o laço de voz usa.
        self.acompanhamento: Acompanhamento | None = None
        # Quem está de pé e quem não está. O painel lê isto para dizer ao
        # usuário o que falta, em vez de parecer quebrado.
        self.servicos = Servicos()
        self._midia_ligada = False
        self._gravando = False
        self._sonda_escuta: dict | None = None
        self._trava_sonda = threading.Lock()
        # Configuração nova não pode esperar o backoff de falha (até 60 s): quem
        # aperta "Configure or reconnect Reachy" no console quer ver o resultado
        # agora. `POST /api/config` levanta isto e o supervisor reavalia na hora.
        self._acordar = threading.Event()
        self._falhas_voz = 0
        self._avisou_sem_cerebro = 0.0
        # Turno de conversa corrente. Uma pergunta nova invalida o
        # anterior: sem isso a resposta atrasada de A falaria depois de B.
        self._turno: conversa.Turno | None = None
        self._aleatorio = random.Random()
        # Uma fala por vez. O loop de voz e o botão "falar" do painel disputam o
        # mesmo alto-falante; sem esta trava sairiam duas vozes sobrepostas.
        self._lock_fala = threading.Lock()
        if self.politica.aviso:
            self.logger.warning(self.politica.aviso)

    def _rotas_configuracao(self) -> None:
        if self.settings_app is None:
            return

        @self.settings_app.get("/api/config")
        def obter_config():
            cfg = Config.carregar()
            salva = armazenamento.carregar_config()
            return {
                "efetiva": {
                    "gateway_url": cfg.gateway_url,
                    "voz_url": cfg.voz_url,
                    "agent_id": cfg.agent_id,
                    "gateway_model": cfg.gateway_model,
                    "provider": cfg.provider,
                    "model": cfg.model,
                    "garra_bin": cfg.garra_bin,
                    "janela_turnos": cfg.janela_turnos,
                    "gateway_key_configurada": bool(cfg.gateway_key),
                },
                "salva": _salva_publica(salva),
                "travada": bool(salva.get("trava_pc")),
            }

        desafios = trava.Desafios()

        @self.settings_app.get("/api/config/desafio")
        def desafio_da_config():
            return {"nonce": desafios.emitir(), "expira_s": int(desafios.validade_s)}

        @self.settings_app.post("/api/config")
        def salvar_config(opcoes: OpcoesSalvas, request: Request):
            atual = armazenamento.carregar_config()
            pedido = opcoes.model_dump(exclude_unset=True)
            barrados = trava.recusa(
                atual, pedido, request.headers.get(trava.CABECALHO), desafios,
                request.client.host if request.client else None, MASCARA)
            if barrados:
                # Nada é gravado, nem a parte livre: um pedido meio aceito
                # deixaria quem o fez sem saber o que valeu.
                return JSONResponse(status_code=403, content={"ok": False, "error": {
                    "code": "config_locked", "fields": barrados,
                    "detail": "este robô está pareado com um computador; endereços e "
                              "credenciais são definidos por ele"}})
            # Campo ausente do JSON = preservar; None ou "" = limpar (volta ao
            # padrão). Vale para texto e para número: o form manda null quando o
            # campo numérico está vazio.
            for chave, valor in pedido.items():
                if chave in SEGREDOS and valor == MASCARA:
                    continue  # a UI devolveu a máscara: não sobrescreve o segredo
                if valor is None or valor == "":
                    atual.pop(chave, None)
                else:
                    atual[chave] = valor
            armazenamento.salvar_config(atual)
            # URLs de voz e gateway valem imediatamente — o supervisor relê a
            # configuração a cada rodada e acabou de ser acordado. As demais
            # (limiar, comportamento) ainda são lidas só no arranque.
            self._acordar.set()
            return {"ok": True, "salva": _salva_publica(atual),
                    "aviso": "Voz e gateway aplicados agora; o resto no próximo início."}

    def _poll_notificacoes(self, cerebro_atual: list[Cerebro], fila: FilaEventos,
                           intervalo_s: float, stop_event: threading.Event,
                           fim_do_ciclo: threading.Event) -> None:
        """Thread auxiliar: só enfileira; quem fala é o loop principal.

        Termina com o CICLO, não com o app. Antes esperava só o `stop_event`,
        que só é acionado quando o app inteiro encerra: cada entrada no laço de
        voz iniciava mais uma destas, e as antigas sobreviviam publicando numa
        `fila` que ninguém lia mais. Medido no robô, com tudo em repouso: +1
        thread e +1 socket por minuto, linear, até o limite de descritores.

        O cérebro chega numa lista de um elemento porque o laço o reconstrói
        quando o gateway volta; sem isso esta thread seguiria consultando o
        objeto antigo pelo resto do ciclo.
        """
        while not (fim_do_ciclo.wait(intervalo_s) or stop_event.is_set()):
            try:
                for evento in cerebro_atual[0].novas_mensagens():
                    fila.publicar(evento)
            except Exception:
                self.logger.exception("Falha no poll de notificações")

    # ── montagem da camada de robô + web ───────────────────────────────────
    def _montar_robo(self, reachy_mini: ReachyMini, cfg: Config) -> ControladorRobo:
        """Controlador, câmera e comportamento ambiente.

        As rotas entram aqui, e não no `__init__`, porque o `ControladorRobo`
        precisa do `ReachyMini` — que só existe depois que o `wrapped_run` abre a
        conexão. O uvicorn já está de pé nesse momento; acrescentar rota ao
        router do Starlette depois do arranque funciona (é o que a página de
        configurações sempre fez).
        """
        daemon = DaemonAPI(cfg.robo_api)
        backend = BackendSdk(reachy_mini, daemon)
        controlador = ControladorRobo(
            backend, dir_capturas=armazenamento.diretorio() / "capturas"
        )
        hub = FrameHub(backend, fps_ativo=cfg.camera_fps)
        hub.iniciar()
        # A captura avulsa do SDK devolve None quando o quadro ainda não chegou
        # pelo WebRTC; o hub já mantém o último em memória.
        controlador.fonte_quadro = lambda: (q.jpeg if (q := hub.instantaneo()) else None)
        controlador.iniciar()

        comportamento = Comportamento(
            controlador,
            ativo=cfg.comportamento_ambiente,
            tracking_ambiente=cfg.tracking_ambiente,
            peso_tracking=cfg.tracking_ambiente_peso,
            wobbling=cfg.wobbling_na_fala,
        )
        comportamento.iniciar()

        self.controlador, self.hub, self.comportamento = controlador, hub, comportamento
        self.acompanhamento = Acompanhamento(controlador.eventos)
        # A visão: o mesmo `FrameHub` do painel, um quadro por pedido. O cliente
        # da ponte é montado a cada olhada, com a URL e o token de agora.
        self.olho = visao.Olho(hub, self._cliente_de_visao, controlador.eventos, hora=self._hora_da_casa)
        # A família: o mesmo quadro, comparado com os perfis no computador.
        self.familia = familia.Familia(hub, self._cliente_da_familia, controlador.eventos, hora=self._hora_da_casa)
        # Casa, música, família e diagnóstico pela ponte; o supervisor a
        # reponta quando a configuração muda, como faz com o chat.
        self.integracoes = PonteIntegracoes(cfg.gateway_url, cfg.gateway_key)
        self.servicos.conectar(controlador.eventos)

        estado = controlador.status()
        real = estado.get("mode") == "real"
        self.servicos.marcar(
            "robot", bool(estado.get("connected")),
            codigo="connected" if estado.get("connected") else "disconnected",
            detalhe="real hardware" if real else "simulated (no robot connected)")
        self.servicos.marcar(
            "movement", bool(estado.get("connected")),
            codigo="ok" if estado.get("connected") else "no_daemon",
            detalhe=cfg.robo_api,
            dica="" if estado.get("connected") else
                 "The robot daemon did not answer. Check GARRA_ROBO_API.")
        # Uma vez aqui e de novo no laço periódico: o hub costuma levar mais
        # que a janela para entregar o primeiro quadro, e perder essa corrida
        # deixava a câmera em `no_frame` para sempre.
        servicos_mod.marcar_camera(self.servicos, hub)
        return controlador

    def _marcar_cerebro(self, cfg: Config, codigo: str, descricao: str,
                        disponivel: bool) -> None:
        """Publica `gateway` e `brain` — dois estados, duas causas diferentes."""
        alcancou = codigo == "gateway"
        self.servicos.marcar(
            "gateway", alcancou,
            codigo="ok" if alcancou else "unreachable",
            detalhe=cfg.gateway_url,
            dica="" if alcancou else
                 "The Garra gateway did not answer. Start the Garra companion "
                 "on the desktop (systemctl --user start "
                 "garra-reachy-companion) and check that the gateway is up.",
        )
        self.servicos.marcar(
            "brain", disponivel,
            codigo=codigo if disponivel else "not_configured",
            detalhe=descricao,
            dica="" if disponivel else
                 "Set up the Garra gateway, or pick an AI provider on the "
                 "settings page. Until then the robot obeys the panel and the "
                 "local voice shortcuts, but cannot hold a conversation.",
        )

    def _sondar_cerebro(self, cfg: Config) -> bool:
        """Health check barato — sem construir Cerebro nem criar sessão."""
        disponivel, codigo, descricao = cerebro_sondar(cfg)
        self._marcar_cerebro(cfg, codigo, descricao, disponivel)
        if codigo != "gateway":
            self._marcar_agente_caido(cfg)
        return disponivel

    def _marcar_agente_caido(self, cfg: Config) -> None:
        """O computador responde, e quem não responde é o AGENTE escolhido.

        São duas situações com consertos opostos. "Não alcancei o computador"
        manda olhar rede, firewall e companion; "o Hermes está fora do ar" manda
        olhar o Hermes — ou escolher outro agente, ali mesmo no painel. Sem
        esta distinção, um agente parado aparecia como computador inacessível.
        """
        from .cerebro import agente_ativo
        ativo = agente_ativo(cfg)
        if ativo is None or ativo["available"]:
            return
        self.servicos.marcar("gateway", True, codigo="ok", detalhe=cfg.gateway_url)
        self.servicos.marcar(
            "brain", False, codigo="agent_unavailable",
            detalhe=f"{ativo['name']} ({cfg.gateway_url})",
            dica=f"The computer answers, but the chosen AI agent ({ativo['name']}) "
                 "does not. Start it on the computer, or pick another agent in "
                 "Settings › AI agent.")

    def _avaliar_cerebro(self, cfg: Config) -> Cerebro:
        """Constrói o cérebro e publica o estado dele em `services`.

        Chamado também no arranque, e não só quando o loop de voz sobe: sem
        isso o painel mostrava `brain: starting` indefinidamente num robô sem
        servidor de voz — que é justamente o caso de quem instala da loja.
        O chat do painel não depende da voz, então o estado tem de ser real
        desde o início.
        """
        cerebro = Cerebro(cfg, self.logger)
        cerebro.iniciar()
        codigo, descricao = cerebro.descrever()
        self._marcar_cerebro(cfg, codigo, descricao, cerebro.disponivel)
        return cerebro

    def _anunciar(self, cfg: Config) -> None:
        """Diz nos logs a URL que realmente abre de outra máquina.

        O daemon lê `custom_app_url` do FONTE por regex e a repassa ao dashboard
        sem reescrever nada, então o link que aparece lá é literalmente
        `http://0.0.0.0:8042` — que no navegador de um laptop significa o
        próprio laptop. Quem instalou o app precisa ver o endereço certo em
        algum lugar, e o log do app é onde o dashboard olha.
        """
        import socket

        log = self.logger
        porta = self.politica.porta
        log.info("Painel: %s/reachy", self.politica.url_visivel)
        if self.politica.host == "0.0.0.0":
            try:
                nome = socket.gethostname()
            except OSError:
                nome = "reachy-mini"
            log.info("Da sua máquina, abra: http://%s.local:%d/reachy "
                     "(ou http://<ip-do-robô>:%d/reachy)", nome, porta, porta)
        if self.politica.token:
            log.info("A API exige token. URL completa: %s/reachy?token=%s",
                     self.politica.url_visivel, self.politica.token)
        log.info("Robô: %s · gateway: %s · voz: %s",
                 cfg.robo_api, cfg.gateway_url, cfg.voz_url)

    def _montar_web(self, cfg: Config) -> None:
        if self.settings_app is None or self.controlador is None or self.hub is None:
            return
        self.chat = PonteChat(
            cfg.gateway_url, cfg.gateway_key, cfg.agent_id,
            modelo=cfg.gateway_model, timeout_s=float(cfg.timeout_gateway_s),
        )
        montar(
            self.settings_app,
            ContextoWeb(
                controlador=self.controlador,
                hub=self.hub,
                eventos=self.controlador.eventos,
                politica=self.politica,
                chat=self.chat,
                acompanhamento=self.acompanhamento,
                olho=self.olho,
                familia=self.familia,
                integracoes=self.integracoes,
                servicos=self.servicos,
                falar=self._falar_async,
                calar=self._calar_agora,
                nova_sessao=self._nova_sessao_async,
                atender_musica=self._atender_musica_do_chat,
                detector=self.medida_do_detector,
                fase=self.comportamento.estado_da_voz if self.comportamento else None,
                acordar=self._reavaliar_cerebro,
                sistema=Sistema(cfg.robo_api),
                jeito=self._jeito,
                ajustar_jeito=self._ajustar_jeito,
                vozes=lambda: VozClient(*self._voz_atual()).vozes(),
                sondar_escuta=self._sondar_escuta,
                dir_estatico=Path(__file__).resolve().parent / "static",
            ),
        )
        self.logger.info(
            "Painel do Reachy em %s/reachy — API em %s/api/robot",
            self.politica.url_visivel, self.politica.url_visivel,
        )

    def _reavaliar_cerebro(self) -> None:
        """O agente mudou no computador: atualiza a faixa do painel já.

        Numa thread, e sem tocar no laço de voz. A sessão antiga responde 404
        e o cérebro abre outra sozinho no próximo pedido; derrubar o laço para
        isso deixava o robô surdo por uns dez segundos a cada troca.
        """
        threading.Thread(target=lambda: self._sondar_cerebro(Config.carregar()),
                         daemon=True, name="reavaliar-cerebro").start()

    @staticmethod
    def _voz_atual() -> tuple[str, str | None]:
        cfg = Config.carregar()
        return cfg.voz_url, cfg.voz_token

    # ── o jeito do robô, ajustável pelo painel ───────────────────────────────
    _JEITO = {"ambient_motion": ("ativo", "comportamento_ambiente"),
              "face_tracking": ("tracking_ambiente", "tracking_ambiente"),
              "speech_wobble": ("wobbling", "wobbling_na_fala")}

    def _jeito(self) -> dict[str, bool]:
        c = self.comportamento
        if c is None:
            return {}
        return {"ambient_motion": bool(c.ativo), "face_tracking": bool(c.tracking_ambiente),
                "speech_wobble": bool(c.wobbling)}

    def _ajustar_jeito(self, mudancas: dict[str, bool]) -> dict[str, bool]:
        """Aplica já no robô e grava, para valer também no próximo início."""
        c = self.comportamento
        if c is None:
            return {}
        c.configurar(**{self._JEITO[k][0]: bool(v) for k, v in mudancas.items()
                        if k in self._JEITO})
        salvo = armazenamento.carregar_config()
        for k, v in mudancas.items():
            if k in self._JEITO:
                salvo[self._JEITO[k][1]] = bool(v)
        armazenamento.salvar_config(salvo)
        return self._jeito()

    # ── teste de escuta ──────────────────────────────────────────────────────
    def _sondar_escuta(self, prazo_s: float) -> dict | None:
        """Arma a sonda e espera a próxima fala. `None`: ninguém falou a tempo.

        A fala que a sonda pega NÃO vira turno: o laço a entrega aqui e volta a
        escutar. Uma sonda por vez — a segunda é recusada na hora (`{"busy":
        True}`), em vez de fazer fila: cada espera ocuparia uma thread, e uma
        fila de testes deixaria o robô sem atender ninguém.
        """
        if getattr(self, "_falar_texto", None) is None:
            return None          # sem laço de voz, não há quem ouça
        if not self._trava_sonda.acquire(blocking=False):
            return {"busy": True}
        try:
            sonda = {"evento": threading.Event(), "resultado": None}
            self._sonda_escuta = sonda
            sonda["evento"].wait(prazo_s)
            return sonda["resultado"]
        finally:
            self._sonda_escuta = None
            self._trava_sonda.release()

    async def _falar_async(self, texto: str) -> str:
        """Fala pedida pelo painel. Nunca sobrepõe a fala do loop principal.

        Devolve o código do resultado para a rota poder responder o que de fato
        aconteceu. Um pedido explícito que devolve sucesso e produz silêncio é
        indistinguível de um defeito.
        """
        if self._falar_texto is None:  # pragma: no cover - só antes do run()
            raise RuntimeError("a voz ainda não está pronta")
        r = await asyncio.to_thread(self._falar_texto, texto, False)
        return r.value if isinstance(r, ResultadoFala) else ResultadoFala.FALADA.value

    def _hora_da_casa(self) -> str:
        """A hora das capturas no fuso da casa (`clock_timezone`), e não no do
        sistema do robô, que sai de fábrica em Londres."""
        try:
            fuso = Config.carregar().conversa.get("clock_timezone")
        except Exception:  # noqa: BLE001 — carimbo de hora não derruba a visão
            fuso = ""
        return relogio.hora_curta(fuso)

    def _cliente_de_visao(self) -> visao.ClienteVisao:
        cfg = Config.carregar()
        return visao.ClienteVisao(cfg.gateway_url, cfg.gateway_key)

    def _cliente_da_familia(self) -> familia.ClienteFamilia:
        cfg = Config.carregar()
        return familia.ClienteFamilia(cfg.gateway_url, cfg.gateway_key)

    def _cliente_da_casa(self) -> casa.ClienteCasa:
        cfg = Config.carregar()
        return casa.ClienteCasa(cfg.gateway_url, cfg.gateway_key)

    def _cliente_da_musica(self) -> musica.ClienteMusica:
        """Um cliente (uma sessão HTTP) por destino, e não um por chamada."""
        cfg = Config.carregar()
        chave = (cfg.gateway_url, cfg.gateway_key)
        guardado = self._cliente_musica
        if guardado is None or guardado[0] != chave:
            if guardado is not None:
                guardado[1].sessao.close()
            guardado = (chave, musica.ClienteMusica(cfg.gateway_url, cfg.gateway_key))
            self._cliente_musica = guardado
        return guardado[1]

    def _talvez_chamaram_com_musica(self, texto: str, perto, eventos) -> None:
        """A frase de ativação se perdeu na música, mas alguém tentou: a música
        fica baixa por ESCUTA_ABAFADA_S, para a repetição ser ouvida. Só abaixa
        — o que foi dito sem a frase não é executado."""
        agora = time.monotonic()
        if (not self._musica_tocando or agora >= self._musica_ate or agora < self._escuta_abafada_ate
                or Config.carregar().conversa.get("music_voice_enabled") is not True):
            return
        if perto is None and not musica.pede_atencao(texto):
            return
        self._escuta_abafada_ate = agora + ESCUTA_ABAFADA_S
        rid = f"escuta_{int(time.time() * 1000)}"
        cliente, pool = self._cliente_da_musica(), self._pool_da_musica()
        pool.submit(cliente.abafar, rid)
        devolver = threading.Timer(ESCUTA_ABAFADA_S, lambda: pool.submit(cliente.restaurar, rid))
        devolver.daemon = True
        devolver.start()
        eventos.publicar("music.listen_duck", reason="near_miss" if perto is not None else "keyword",
                         seconds=ESCUTA_ABAFADA_S)

    def _pool_da_musica(self) -> ThreadPoolExecutor:
        """Um pool só, criado uma vez — pelo mesmo motivo do pool do cérebro."""
        atual = self._executor_musica
        if atual is None or getattr(atual, "_shutdown", False):
            atual = ThreadPoolExecutor(max_workers=2, thread_name_prefix="musica")
            self._executor_musica = atual
        return atual

    def _atender_musica(self, pedido: musica_pedido.PedidoMusica, correlacao: str, origem: str,
                        eventos, falar=None) -> str | None:
        """Atende o comando de música e devolve o que foi respondido.

        `None` quando a frase não é sobre música AGORA — comando solto
        ("continua") sem música no assunto — e então ela segue para o modelo.
        O pedido nunca vai ao modelo, e nada da Spotify vai (III.14).
        """
        conv = Config.carregar().conversa
        ligada = conv.get("music_voice_enabled") is True
        if pedido.contextual and (not ligada or not self._musica_em_assunto()):
            return None
        resultado = None
        if pedido.operacao == "capability":
            texto = musica.capacidade(ligada)
        elif not ligada:
            texto = musica.DESLIGADO
        else:
            resultado = self._executar_musica(pedido, correlacao, origem, eventos, falar)
            texto = musica.frase(resultado)
        eventos.publicar("chat.message", correlation_id=correlacao, role="assistant",
                         content=texto, source="garra")
        if falar is None:
            return texto
        falada = None
        try:
            falada = falar(texto)
        except Exception:
            self.logger.exception("Falha ao falar a confirmação da música")
        finally:
            if resultado and (resultado.get("ducked") or {}).get("ducked"):
                self._pool_da_musica().submit(self._cliente_da_musica().restaurar, correlacao)
        # Se a música mudou e a confirmação não saiu, fica registrado assim — e a
        # faixa NÃO é recomeçada para tentar de novo.
        eventos.publicar("music.confirmation", correlation_id=correlacao, spoken=falada is ResultadoFala.FALADA,
                         status=(resultado or {}).get("status"), operation=pedido.operacao)
        return texto

    def _atender_musica_do_chat(self, pedido: musica_pedido.PedidoMusica, correlacao: str) -> str | None:
        """O mesmo atendimento, pelo chat do painel: responde por escrito."""
        if self.controlador is None:
            return None
        return self._atender_musica(pedido, correlacao, "chat", self.controlador.eventos)

    def _executar_musica(self, pedido: musica_pedido.PedidoMusica, correlacao: str, origem: str,
                         eventos, falar=None) -> dict | None:
        requisicao = musica.pedido_da_ferramenta(pedido.operacao, pedido.consulta, pedido.volume,
                                                 correlacao, origem)
        if falar is not None and pedido.operacao in _MUSICA_SEGUE:
            # Abaixar a música para a confirmação é pedido junto: o PC abaixa
            # DEPOIS de conferir que tocou, e o robô devolve o volume ao terminar.
            requisicao["duck"] = True
        eventos.publicar("music.command.received", correlation_id=correlacao, operation=pedido.operacao,
                         source=origem)
        inicio = time.monotonic()
        futuro = self._pool_da_musica().submit(self._cliente_da_musica().comando, requisicao)
        resultado = None
        try:
            if falar is not None:
                try:
                    resultado = futuro.result(timeout=MUSICA_AVISO_S)
                except FuturesTimeout:
                    try:
                        falar(musica.aviso(pedido.operacao))
                    except Exception:
                        # O aviso é cortesia: sem ele, o resultado segue valendo.
                        self.logger.warning("O aviso de espera da música não saiu.", exc_info=True)
                    resultado = futuro.result(timeout=max(1.0, MUSICA_PRAZO_S - (time.monotonic() - inicio)))
            else:
                resultado = futuro.result(timeout=MUSICA_PRAZO_S)
        except FuturesTimeout:
            resultado = None
        except Exception:
            self.logger.exception("Falha inesperada no pedido de música")
            resultado = None
        r = resultado or {}
        eventos.publicar("music.command.result", correlation_id=correlacao, operation=pedido.operacao,
                         status=r.get("status", "bridge_unreachable"), reason=r.get("reason"),
                         verified=bool(r.get("verified")), evidence=r.get("evidence"),
                         duplicate=bool(r.get("duplicate")), device=r.get("device"),
                         duration_ms=int((time.monotonic() - inicio) * 1000))
        self._lembrar_musica(resultado)
        return resultado

    def _musica_em_assunto(self) -> bool:
        """Há música no assunto? A lembrança do robô primeiro; sem ela (o app
        acabou de subir, a música veio do celular), pergunta ao PC."""
        if time.monotonic() < self._musica_ate:
            return True
        agora = self._cliente_da_musica().reproducao()
        if not agora:
            return False
        self._musica_ate = time.monotonic() + MUSICA_ASSUNTO_S
        self._musica_tocando = bool(agora.get("is_playing"))
        return True

    def _lembrar_musica(self, r: dict | None) -> None:
        if not r or r.get("status") in (None, "pending", "unavailable", "failed"):
            return
        self._musica_ate = time.monotonic() + MUSICA_ASSUNTO_S
        visto = r.get("observed") or {}
        self._musica_tocando = (r.get("status") == "playing"
                                or (visto.get("state") == "playing" and visto.get("device") == r.get("device")))

    def medida_do_detector(self) -> dict | None:
        """O que o detector de fala está medindo agora, ou `None` sem laço de
        voz de pé. Só números: piso de ruído, limiares e contagens."""
        detector = getattr(self, "_detector", None)
        return detector.medida() if detector is not None else None

    def _calar_agora(self, motivo: str = "painel") -> None:
        """Corta a fala em curso. Chamado pelo botão de parada do painel."""
        if self._calar is not None:
            self._calar(motivo)

    async def _nova_sessao_async(self) -> str | None:
        """Recomeça a conversa. O cérebro só existe dentro do loop de voz."""
        if self._trocar_sessao is None:
            raise RuntimeError("o cérebro ainda não está pronto")
        return await asyncio.to_thread(self._trocar_sessao)

    # Substituídos dentro de run() pelas funções reais, que têm acesso ao TTS.
    _falar_texto = None
    _calar = None
    _trocar_sessao = None
    # Música: até quando um comando solto ainda é sobre música, e se a última
    # coisa que a ferramenta viu foi música tocando no aparelho alvo.
    _musica_ate = 0.0
    _musica_tocando = False
    _executor_musica: ThreadPoolExecutor | None = None
    _escuta_abafada_ate = 0.0
    _cliente_musica: tuple | None = None

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        cfg = Config.carregar()
        self._rotas_configuracao()
        controlador = self._montar_robo(reachy_mini, cfg)
        self._montar_web(cfg)
        self._avaliar_cerebro(cfg)
        self._anunciar(cfg)
        try:
            self._supervisionar(reachy_mini, stop_event, controlador)
        finally:
            self._encerrar_robo()
            try:
                reachy_mini.media.stop_recording()
                reachy_mini.media.stop_playing()
            except Exception:
                pass
            self.logger.info("Garra Reachy Mini encerrado.")

    def _aplicar_escuta(self, reachy_mini: ReachyMini, ligada: bool, log,
                        eventos=None) -> None:
        """Abre ou LIBERA o microfone, conforme o interruptor da escuta.

        Desligada, a gravação é parada de verdade — não é o app ignorando o que
        o microfone entrega, é o microfone devolvido. O alto-falante segue: o
        painel ainda pode pedir uma fala, e o robô ainda responde no chat.
        """
        if ligada == getattr(self, "_gravando", False):
            return
        try:
            if ligada:
                reachy_mini.media.start_recording()
            else:
                reachy_mini.media.stop_recording()
        except Exception:
            log.exception("Falha ao %s o microfone", "abrir" if ligada else "liberar")
            return
        self._gravando = ligada
        log.info("Microfone %s.", "aberto" if ligada else
                 "liberado (escuta desligada no painel)")
        if eventos is not None:
            eventos.publicar("voice.listening", enabled=ligada)

    def _esperar(self, stop_event: threading.Event, segundos: float) -> None:
        """Espera, mas acorda na hora se o app parar ou a configuração mudar.

        Fatiado porque são dois sinais: o `stop_event` do daemon (que tem 20 s
        para ser obedecido antes do SIGKILL) e o nosso `_acordar`. Esperar só no
        primeiro faria uma configuração nova demorar até 60 s; esperar só no
        segundo faria o Stop demorar o mesmo.
        """
        fim = time.monotonic() + segundos
        while True:
            resta = fim - time.monotonic()
            if resta <= 0 or stop_event.wait(min(0.25, resta)):
                return
            if self._acordar.is_set():
                self._acordar.clear()
                return

    def _executor_cerebro(self) -> ThreadPoolExecutor:
        """O único pool de threads que consulta o cérebro, criado uma vez.

        Um pool por entrada no laço de voz vazava: `shutdown(wait=False)` não
        espera a tarefa em execução, e uma consulta presa contra um gateway que
        aceita a conexão e não responde segura thread e socket até o fim do
        processo.
        """
        atual = getattr(self, "_executor", None)
        if atual is None or getattr(atual, "_shutdown", False):
            atual = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cerebro")
            self._executor = atual
        return atual

    def _encerrar_executor(self, timeout: float = 5.0) -> None:
        """Encerra o pool no fim da vida do app. Idempotente.

        `wait=True` num thread separado com timeout: uma consulta presa não
        pode segurar o encerramento (o daemon mata o app em 20 s), mas também
        não se abandona o pool sem tentar drenar.
        """
        do_som = self._executor_musica
        if do_som is not None:
            self._executor_musica = None
            do_som.shutdown(wait=False, cancel_futures=True)
        atual = getattr(self, "_executor", None)
        if atual is None:
            return
        self._executor = None
        atual.shutdown(wait=False, cancel_futures=True)
        drenar = threading.Thread(
            target=lambda: atual.shutdown(wait=True), daemon=True,
            name="cerebro-drain")
        drenar.start()
        drenar.join(timeout)

    def _supervisionar(self, reachy_mini: ReachyMini, stop_event: threading.Event,
                       controlador: ControladorRobo) -> None:
        """Mantém o app útil enquanto os serviços opcionais vão e voltam.

        A versão anterior parava aqui num `while not voz.esperar_pronto(...)`
        infinito. Isso fazia sentido quando o único usuário tinha um servidor de
        voz com GPU do lado — mas instalado da loja, num robô que nunca vai ter
        um, o botão Start acendia e o robô não fazia absolutamente nada.

        Painel, câmera, movimento, expressões e rastreamento de rosto já subiram
        e não dependem de nada disso. O que este laço faz é esperar a voz sem
        segurar o resto do app como refém, relendo a configuração a cada rodada
        para que a própria página `:8042` consiga consertar a URL sem reiniciar.
        """
        log = self.logger
        espera, avisou = 3.0, False
        while not stop_event.is_set():
            cfg = Config.carregar()
            # Reavaliar aqui, e não só ao entrar no loop de voz: sem servidor de
            # voz o `_laco_voz` nunca roda, e o estado do gateway ficava
            # congelado para sempre num robô recém-instalado — que é justamente
            # quem mais precisa ver se o desktop já respondeu.
            self._sondar_cerebro(cfg)
            if self.chat is not None and self.chat.reconfigurar(
                    cfg.gateway_url, cfg.gateway_key, cfg.agent_id):
                log.info("Chat do painel repontado para %s (agente %s).",
                         cfg.gateway_url, cfg.agent_id)
            if self.integracoes is not None and self.integracoes.reconfigurar(
                    cfg.gateway_url, cfg.gateway_key):
                log.info("Integrações repontadas para %s.", cfg.gateway_url)
            voz = VozClient(cfg.voz_url, cfg.voz_token)
            if voz.pronto():
                saude = {}
                try:
                    saude = voz.saude()
                except Exception:  # a voz caiu entre o pronto() e o saude()
                    pass
                log.info("Servidor de voz OK em %s (%s, multilíngue=%s)",
                         cfg.voz_url, saude.get("device"), saude.get("tts_multilingue"))
                self.servicos.marcar("voice", True, detalhe=cfg.voz_url)
                avisou = False
                entrou_em = time.monotonic()
                self._laco_voz(reachy_mini, stop_event, cfg, controlador, voz)
                # Reentrar na hora é o que transformava uma indisponibilidade
                # prolongada num laço apertado: o laço de voz volta na hora
                # quando o cérebro ou a voz está fora, e cada volta paga o
                # custo de reconstruir tudo. Só zera a espera quando o laço
                # realmente ficou de pé por um tempo; se voltou rápido, é
                # falha, e falha pede recuo com jitter (dois robôs na mesma
                # rede não podem sincronizar suas tentativas).
                if time.monotonic() - entrou_em >= CICLO_VOZ_ESTAVEL_S:
                    espera = 3.0
                else:
                    self._esperar(stop_event, espera * (0.7 + random.random() * 0.6))
                    espera = min(espera * 1.6, 60.0)
                continue

            self.servicos.marcar(
                "voice", False, codigo="unreachable", detalhe=cfg.voz_url,
                dica="Run the optional voice server (tools/servidor_voz.py) and "
                     "point GARRA_VOZ_URL at it.",
            )
            if self.comportamento is not None:
                self.comportamento.fase(fases.FASE_ESPERA, "voice_server_unreachable")
            if not avisou:
                avisou = True
                log.warning(
                    "Servidor de voz indisponível em %s — seguindo sem voz. "
                    "Painel, câmera, movimentos e rastreamento continuam "
                    "funcionando; a voz entra sozinha quando a URL responder.",
                    cfg.voz_url)
            self._esperar(stop_event, espera * (0.7 + random.random() * 0.6))
            espera = min(espera * 1.6, 60.0)

    def _laco_voz(self, reachy_mini: ReachyMini, stop_event: threading.Event,
                  cfg: Config, controlador: ControladorRobo, voz: VozClient) -> None:
        """Conversa por voz. Volta quando o app encerra ou quando a voz cai."""
        log = self.logger
        cerebro = self._avaliar_cerebro(cfg)
        if not cerebro.disponivel:
            log.warning("Nenhum cérebro configurado: o robô ouve e obedece aos "
                        "atalhos locais, mas não conversa.")

        if hasattr(voz, "preferir"):
            voz.preferir(conversa.normalizar(cfg.conversa))
        fila = FilaEventos()
        sr_in = reachy_mini.media.get_input_audio_samplerate()
        sr_out = reachy_mini.media.get_output_audio_samplerate()
        if sr_in <= 0 or sr_out <= 0:
            # O SDK devolve -1 quando não há dispositivo de áudio. Sem isto, a
            # duração calculada fica negativa e o robô fica surdo em silêncio.
            self.servicos.marcar(
                "voice", False, codigo="no_audio_device",
                detalhe=f"mic={sr_in}Hz speaker={sr_out}Hz",
                dica="The robot reported no audio device. Check the microphone "
                     "and speaker in the Reachy Mini dashboard.")
            log.error("Robô sem dispositivo de áudio (mic %sHz, som %sHz); "
                      "seguindo sem voz.", sr_in, sr_out)
            if self.comportamento is not None:
                self.comportamento.fase(fases.FASE_ESPERA, "no_audio_device")
            stop_event.wait(30.0)
            return
        if not self._midia_ligada:
            reachy_mini.media.start_playing()
            self._midia_ligada = True
        self._aplicar_escuta(
            reachy_mini, conversa.normalizar(cfg.conversa)["listening_enabled"], log,
            controlador.eventos)
        log.info("Conectado ao robô (mic %sHz, som %sHz).", sr_in, sr_out)

        # A sessão de ativação: uma por instância do laço de voz, viva entre
        # turnos, morta com o laço. Dois carimbos de tempo — nenhuma thread,
        # nenhum timer, nenhuma task, que é o que o 1.2.1 acabou de custar.
        sessao_ativacao = ativacao.Sessao()
        rejeicoes = {"ultimo": 0.0, "suprimidas": 0}

        # Só agora: acima ainda havia um `return` (robô sem dispositivo de
        # áudio), e uma thread iniciada antes dele ficaria de pé sem ninguém
        # para encerrá-la. O evento a amarra a ESTE ciclo.
        fim_do_ciclo = threading.Event()
        cerebro_atual = [cerebro]
        poll = threading.Thread(
            target=self._poll_notificacoes,
            args=(cerebro_atual, fila, cfg.intervalo_notificacoes_s, stop_event,
                  fim_do_ciclo),
            daemon=True, name="poll-notificacoes",
        )
        poll.start()

        gestos = self.comportamento
        assert gestos is not None
        assert self.acompanhamento is not None

        # O que a tela mostra enquanto o robô ouve. A configuração é relida de
        # tempos em tempos no laço (`escuta`), não a cada pergunta da tela.
        escuta = {"ativacao": ativacao.Ativacao.de(cfg.conversa),
                  "ligada": conversa.normalizar(cfg.conversa)["listening_enabled"],
                  "conferida": 0.0}

        def fase_ao_ouvir() -> tuple[str, str | None]:
            if not escuta["ligada"]:
                return fases.FASE_ESPERA, "listening_disabled"
            ativ = escuta["ativacao"]
            if not ativ.habilitada or sessao_ativacao.aberta(time.monotonic(), ativ):
                return fases.FASE_OUVINDO, None
            return fases.FASE_AGUARDANDO_FRASE, None

        gestos.fase_ao_ouvir = fase_ao_ouvir
        gestos.ouvindo()

        # Único dono do alto-falante. Ver conversa.py: o lock dele cobre a
        # transição de estado, nunca a duração do áudio.
        coordenador = conversa.CoordenadorAudio(reachy_mini.media, sr_out, log)
        # Consultar o modelo numa thread é o que permite responder direto
        # quando dá tempo: o laço principal fica livre para decidir se o aviso
        # falado ainda faz sentido, em vez de bloquear na chamada.
        # Executor ÚNICO do app, não um por entrada no laço.
        #
        # Antes era criado aqui e descartado no `finally` com
        # `shutdown(wait=False)`. Isso cancela só o que está na fila: uma
        # consulta JÁ em execução mantém a thread e o socket dela viva para
        # sempre quando o gateway aceita a conexão e não responde. Como o
        # supervisor reentra neste laço na hora (`continue`), cada volta
        # abandonava mais uma thread e mais um socket — medido: crescimento
        # linear de 1 socket + 1 thread por ciclo, até estourar o limite de
        # FDs. Aí o GLib não consegue criar o pipe do GWakeup, chama
        # G_BREAKPOINT e o processo morre com SIGTRAP, que o daemon reporta
        # como `Process exited with code -5`.
        executor = self._executor_cerebro()

        def calar(motivo: str = "barge-in") -> None:
            """Corta o áudio agora. É o e-stop do alto-falante, sem limite de 1,2 s."""
            alvo = self._turno
            if alvo is not None and alvo.vivo:
                coordenador.cancelar(alvo, motivo)

        self._calar = calar
        # O cérebro é local a esta função; o painel alcança a troca de sessão
        # por aqui, e só enquanto o loop de voz está de pé.
        self._trocar_sessao = troca_de_sessao(cerebro_atual)

        def ler_mono() -> np.ndarray:
            if not getattr(self, "_gravando", False):
                # Escuta desligada: o microfone foi devolvido, não há o que ler.
                return np.empty(0, dtype=np.float32)
            pedaco = reachy_mini.media.get_audio_sample()
            if pedaco is None:
                return np.empty(0, dtype=np.float32)
            return pedaco.mean(axis=1) if pedaco.ndim == 2 else pedaco

        def drenar_mic(segundos: float = 0.0) -> None:
            """Descarta áudio residual (anti-eco) por até `segundos` + 2s de folga."""
            fim = time.time() + segundos
            while not stop_event.is_set():
                if ler_mono().size == 0 and time.time() >= fim:
                    break
                if time.time() >= fim + 2.0:
                    break
                time.sleep(0.02)

        def falar(texto: str, bloqueante: bool = True,
                  turno: conversa.Turno | None = None) -> None:
            """Sintetiza por frase, toca no robô e espera o áudio terminar.

            `bloqueante=False` é o caminho do painel: se o robô já estiver
            falando, desiste em vez de sobrepor duas vozes no mesmo alto-falante.
            `turno` amarra o áudio ao turno corrente — sem ele, uma resposta
            atrasada de um turno já substituído ainda sairia pelo alto-falante.
            """
            # O mestre da saída, lido AGORA e não no arranque do laço: é o que
            # faz o interruptor do painel valer na fala seguinte, sem restart.
            # Antes de tomar a trava, antes do TTS, antes de qualquer estado —
            # desligado, esta função não pode ter efeito colateral nenhum.
            if not conversa.Politica.de(Config.carregar().conversa).saida_habilitada:
                log.info("Fala suprimida: saída de voz desligada na configuração.")
                return ResultadoFala.DESABILITADA
            if not self._lock_fala.acquire(blocking=bloqueante):
                raise RuntimeError("o robô já está falando")
            houve_audio = False
            try:
                gestos.falando()
                inicio, duracao = time.time(), 0.0
                for frase in frases(texto):
                    if stop_event.is_set():
                        break
                    try:
                        onda = voz.falar(frase, sr_out)
                    except Exception:
                        log.exception("Falha no TTS da frase: %r", frase[:80])
                        gestos.erro("tts_failed", "speech synthesis did not answer")
                        continue
                    if onda.size == 0:
                        continue
                    # Pelo coordenador, nunca direto: ele é o único dono do
                    # alto-falante e o que garante que a resposta não sobreponha
                    # um aviso ainda tocando.
                    if turno is not None:
                        if not coordenador.tocar_final(turno, [onda]):
                            break   # outro turno assumiu; esta fala morreu aqui
                    else:
                        reachy_mini.media.push_audio_sample(onda)
                    houve_audio = True
                    duracao += onda.size / sr_out
                restante = inicio + duracao - time.time() + 0.6
                while restante > 0 and not stop_event.is_set():
                    passo = min(restante, 0.1)
                    time.sleep(passo)
                    restante -= passo
                drenar_mic(0.4)
                # O instante em que o alto-falante calou de vez. A margem
                # anti-eco se mede a partir DAQUI, contra o carimbo de captura
                # do áudio — nunca contra o relógio da avaliação, que só chega
                # depois do STT.
                sessao_ativacao.falou_ate = time.monotonic()
                gestos.ouvindo()
            finally:
                self._lock_fala.release()
            # Nenhuma onda chegou ao alto-falante: ou o TTS falhou frase a
            # frase, ou o turno foi substituído no meio. Quem chamou merece
            # saber a diferença entre isso e uma fala que aconteceu.
            return ResultadoFala.FALADA if houve_audio else ResultadoFala.FALHA

        # A partir daqui o painel também consegue falar pelo robô.
        self._falar_texto = falar

        # ── conversa ao vivo ──────────────────────────────────────────────
        # `processar` só ANOTA o chamado aqui; quem atende é o laço, depois que
        # o turno devolveu o controle (ver `atender_ao_vivo`).
        pedido_ao_vivo: dict = {}
        atendente = ao_vivo.Atendente(controlador.eventos.publicar, falar)

        def atender_ao_vivo() -> None:
            """Atende o chamado anotado pelo turno. Roda na thread do laço, que é
            a única que lê o microfone — e a conversa ao vivo precisa dele."""
            chamado = pedido_ao_vivo.pop("chamado", None)
            if chamado is None:
                return
            # A conversa com o Garra não segue aberta por baixo desta: quem
            # chamou a Hera trocou de conversa.
            sessao_ativacao.fechar()
            try:
                atendente.atender(chamado)
            except Exception:
                log.exception("A conversa ao vivo falhou ao atender o chamado")
            gestos.ouvindo()

        bipe = ativacao.bipe(sr_out)

        def confirmar_ativacao(conf: dict) -> None:
            """A frase casou: diz isso com um gesto e um bipe, se estiverem ligados.

            O gesto é enfeite em prioridade ambiente. O bipe é som do robô e
            obedece ao mestre da saída; não entra no coordenador de turnos
            porque não há turno ainda, e não sobrepõe uma fala em andamento.
            """
            prefs = conversa.normalizar(conf)
            if prefs["wake_ack_motion_enabled"]:
                gestos.ativado()
            tocou = False
            if prefs["wake_ack_sound_enabled"] and self._lock_fala.acquire(blocking=False):
                try:
                    # Pelo coordenador, como todo som: é ele que aplica o
                    # mestre da saída de voz.
                    tocou = coordenador.tocar_sinal(bipe, prefs["speech_output_enabled"])
                    if tocou:
                        # Dormir a duração do bipe mantém a ordem dos sons: o
                        # que vier depois (a resposta) entra na fila atrás dele.
                        stop_event.wait(bipe.size / sr_out)
                        sessao_ativacao.falou_ate = time.monotonic()
                except Exception:
                    log.debug("bipe de ativação descartado", exc_info=True)
                finally:
                    self._lock_fala.release()
            controlador.eventos.publicar(
                "voice.wake.acknowledged", sound=tocou,
                motion=bool(prefs["wake_ack_motion_enabled"]))

        def ouvir(audio: np.ndarray, dica: str | None = None):
            """O texto e a confiança por palavra (`None` quando não se sabe).

            Serve ao `VozClient` e a quem só sabe `transcrever` — servidor de
            voz antigo, ou de teste —, que não tem confiança a dar.
            """
            extra = {"dica": dica} if dica else {}
            metodo = getattr(voz, "ouvir", None)
            if metodo is None:
                return voz.transcrever(audio, sr_in, **extra), None
            ouvido = metodo(audio, sr_in, **extra)
            return ouvido.texto, ouvido.confianca

        def processar(audio: np.ndarray, esperas: list[np.ndarray]) -> None:
            dur = audio.size / sr_in
            if dur < FALA_MINIMA_S:
                return
            # O instante em que este áudio COMEÇOU a ser capturado, derivado da
            # própria duração e tomado antes do STT. A margem anti-eco precisa
            # disto e não do relógio da avaliação: o STT leva segundos, e medir
            # depois dele julgaria o áudio errado.
            capturado_em = time.monotonic() - dur
            # Só a fase: o corpo fica parado até se saber que isto é um pedido.
            # Mexer as antenas a cada som ouvido realimentava o detector com o
            # ruído do próprio servo (ver `Comportamento.ouvindo`).
            gestos.fase(fases.FASE_TRANSCREVENDO)
            t0 = time.time()
            try:
                texto, confianca = ouvir(audio)
            except Exception:
                self._falhas_voz += 1
                log.exception("Falha no STT (%d seguidas)", self._falhas_voz)
                gestos.erro("stt_failed", "speech recognition did not answer")
                gestos.ouvindo()
                return
            self._falhas_voz = 0
            if not texto or len(texto) < 2:
                gestos.ouvindo()
                return
            # ── o nome da conversa ao vivo não esconde um "pare" ───────────
            # "Hera, pare" é parada de emergência, nunca chamado: tira-se o nome
            # e o "pare" segue o caminho de sempre — teste de escuta incluído.
            rota_ao_vivo = roteador.Rota.de(Config.carregar().conversa)
            if cfg.atalhos_locais:
                texto = roteador.parada(texto, rota_ao_vivo) or texto
            # ── teste de escuta pedido pelo painel ─────────────────────────
            # A fala é do teste: volta para quem pediu e não vai a lugar
            # nenhum — nem chat, nem contexto, nem agente, nem registro.
            # Menos o "pare": parada de emergência não é assunto de teste, e
            # segue o caminho de sempre mesmo com a sonda armada.
            sonda = getattr(self, "_sonda_escuta", None)
            pare = bool(cfg.atalhos_locais and intencoes.reconhecer(texto).acao == "stop")
            if sonda is not None and not sonda["evento"].is_set() and not pare:
                ativ_teste = ativacao.Ativacao.de(Config.carregar().conversa)
                quantos, como = ativacao.casar_alguma(ativacao.tokens(texto), ativ_teste)
                sonda["resultado"] = {
                    "text": texto, "duration_s": round(dur, 1),
                    "stt_ms": int((time.time() - t0) * 1000),
                    "wake_phrase": bool(quantos), "wake_match": como or None,
                    "wake_enabled": ativ_teste.habilitada}
                sonda["evento"].set()
                gestos.ouvindo()
                return
            # ── frase de ativação ─────────────────────────────────────────
            # ANTES do log, do evento e de tudo a jusante. O `publicar` é o que
            # leva o transcript ao painel e ao histórico: um gate depois dele já
            # teria persistido o que devia sumir. Pelo mesmo motivo o texto
            # rejeitado não entra no log do app.
            conf_wake = Config.carregar()
            ativ = ativacao.Ativacao.de(conf_wake.conversa)
            # ── chamado da conversa ao vivo ───────────────────────────────
            # Antes do portão do Garra: quem chama a Hera não está falando com
            # ele, e o que disse não é conversa da sala para ele guardar nem
            # candidato à segunda escuta dele. Aqui só se anota — nada é
            # publicado, registrado ou enviado; quem atende é o laço.
            chamado_ao_vivo = roteador.chamado(
                texto, rota_ao_vivo, capturado_em=capturado_em,
                falou_ate=sessao_ativacao.falou_ate)
            if chamado_ao_vivo is not None:
                pedido_ao_vivo["chamado"] = chamado_ao_vivo
                return
            # O e-stop é reconhecido ANTES da decisão e é o único bypass: um
            # "pare" que dependa de frase de ativação não é um e-stop.
            e_stop = bool(cfg.atalhos_locais
                          and intencoes.reconhecer(texto).acao == "stop")
            veredicto = ativacao.avaliar(texto, capturado_em, sessao_ativacao,
                                       ativ, time.monotonic(), e_stop=e_stop)
            # ── segunda escuta ────────────────────────────────────────────
            # Pelo microfone do robô o STT erra a frase mais do que acerta:
            # "fala gabi", "fala garne". Quando o COMEÇO do que ele ouviu lembra
            # a frase E ele mesmo duvidava do que escreveu ali, vale perguntar
            # de novo dizendo qual é a frase. Só nesse caso: a frase dada como
            # dica em tudo faz ruído virar chamado, e dada sempre que o começo
            # é parecido faz "fala galera" virar chamado.
            perto, conferencia = None, "not_tried"
            if not veredicto.aceito and veredicto.motivo == "wake_phrase_required":
                preferencias = conversa.normalizar(conf_wake.conversa)
                ouvido = ativacao.tokens(texto)
                # Contra cada frase configurada; fica a que mais se parece, e é
                # com ELA (e com a dica dela) que a segunda escuta confere.
                perto, exame, frase_perto, dica_perto = ativacao.exame_entre(
                    ouvido, ativ, ativacao.confianca_por_token(texto, confianca))
                if (exame in ("confident", "unknown")
                        and preferencias["wake_second_listen_always"]):
                    # Quem ligou "conferir sempre" prefere o robô que às vezes
                    # responde sem ser chamado ao que às vezes não responde.
                    exame = "unsure"
                conferencia = {"confident": "skipped_confident",
                               "unknown": "unknown_confidence"}.get(exame, "not_tried")
                if exame == "unsure" and not (
                        preferencias["wake_second_listen_enabled"] and dica_perto):
                    conferencia = "disabled"
                elif exame == "unsure":
                    try:
                        de_novo, conf_de_novo = ouvir(audio, dica=dica_perto)
                    except Exception:
                        # Não conta em `_falhas_voz`: a primeira escuta deste
                        # mesmo áudio acabou de dar certo.
                        conferencia = "failed"
                        log.warning("A segunda escuta falhou; o enunciado segue recusado.",
                                    exc_info=True)
                    else:
                        conferencia = "rejected"
                        if ativacao.confirma(de_novo, frase_perto):
                            segundo = ativacao.avaliar(
                                de_novo, capturado_em, sessao_ativacao, ativ,
                                time.monotonic())
                            if segundo.aceito:
                                conferencia = "confirmed"
                                # A frase foi conferida pela segunda escuta; o
                                # PEDIDO fica com a escuta que o ouviu melhor.
                                melhor = ativacao.resto_mais_confiavel(
                                    texto, confianca, len(perto["heard"].split()),
                                    de_novo, conf_de_novo, len(frase_perto))
                                texto = de_novo
                                veredicto = dataclasses.replace(
                                    segundo, extras={"wake_match": "second_listen"})
                                if melhor and not segundo.so_ativacao:
                                    veredicto = dataclasses.replace(veredicto, texto=melhor)
                if perto is not None and preferencias["wake_diagnostics_enabled"]:
                    # O único evento que carrega algo do que foi dito sem a
                    # frase — e carrega só o trecho que pareceu a frase.
                    controlador.eventos.publicar(
                        "voice.wake.near_miss", second_listen=conferencia,
                        **{k: perto[k] for k in ("heard", "position", "distance",
                                                 "confidence") if k in perto})
            if not veredicto.aceito:
                # Recusada por falta da frase: é conversa da sala. Com a escuta
                # de contexto ligada ela fica em MEMÓRIA, para acompanhar o
                # próximo pedido; desligada, `ouviu` não guarda coisa alguma.
                # Nada mais muda neste ramo: o evento abaixo segue sem o que
                # foi dito, e o registro do app segue sem uma linha sobre isto.
                if veredicto.motivo == "wake_phrase_required":
                    self.acompanhamento.ouviu(texto, capturado_em, conf_wake.conversa)
                    self._talvez_chamaram_com_musica(texto, perto, controlador.eventos)
                agora_r = time.monotonic()
                if agora_r - rejeicoes["ultimo"] >= REJEICAO_INTERVALO_S:
                    # Evento redigido: faixa de duração, nunca o valor exato, e
                    # nenhum resquício do que foi dito — sem transcript, sem
                    # hash, sem trecho.
                    controlador.eventos.publicar(
                        "voice.wake.rejected", reason=veredicto.motivo,
                        audio_duration_bucket=ativacao.faixa_de_duracao(dur),
                        wake_session_state=veredicto.estado_sessao,
                        suppressed_since_last=rejeicoes["suprimidas"])
                    rejeicoes["ultimo"] = agora_r
                    rejeicoes["suprimidas"] = 0
                else:
                    rejeicoes["suprimidas"] += 1
                gestos.ouvindo()
                return
            if veredicto.abriu_sessao:
                controlador.eventos.publicar(
                    "voice.wake.activated", window_s=ativ.janela_s,
                    session_max_s=ativ.sessao_max_s,
                    match=veredicto.extras.get("wake_match", "exact"))
                confirmar_ativacao(conf_wake.conversa)
            if veredicto.so_ativacao:
                # "Fala Garra" sozinho: a sessão abriu e não há o que responder
                # além do bipe e do gesto acima. Sem esvaziar o microfone, de
                # propósito: quem ouviu o bipe já começa a falar, e esvaziar
                # jogaria fora o começo do pedido. O bipe sozinho não vira
                # trecho — tem menos som que `segmentador.ATIVO_MINIMO_S`.
                gestos.ouvindo()
                return
            texto = veredicto.texto

            log.info("🎤 Você (%.1fs): %s", time.time() - t0, texto)
            correlacao = f"voz_{int(time.time() * 1000)}"
            controlador.eventos.publicar(
                "chat.message", correlation_id=correlacao, role="user",
                content=texto, source="voz",
            )

            # Atalho local: "pare" e "dance" não podem esperar uma ida à nuvem.
            # Ele executa e avisa o modelo, que segue responsável pela fala.
            intencao = intencoes.ResultadoIntencao(tratada=False)
            if cfg.atalhos_locais:
                intencao = intencoes.reconhecer(texto)
            complemento = ""
            if intencao.tratada and intencao.acao:
                r = controlador.submeter(
                    intencao.acao, intencao.params, source="voz",
                    correlation_id=correlacao,
                    esperar=intencao.acao == "stop",
                )
                log.info("⚡ atalho local %s → %s", intencao.acao, r.state)
                complemento = intencao.aviso_para_o_agente(r.action_id, r.message)
                if not intencao.encaminhar_ao_agente:
                    # "pare" responde na hora: consultar o modelo aqui só
                    # atrasaria a confirmação de algo que já aconteceu.
                    if intencao.resposta_imediata:
                        falar(intencao.resposta_imediata)
                    return

            # Música: o comando é entendido aqui, sem modelo, e executado pelo
            # spotatui do PC. O pedido não vai ao modelo, e nada da Spotify vai
            # (III.14). Depois dos atalhos locais: "pare" vale mais que tudo.
            pedido_musica = musica_pedido.interpretar(texto)
            if pedido_musica is not None and self._atender_musica(
                    pedido_musica, correlacao, "voz", controlador.eventos, falar=falar) is not None:
                gestos.ouvindo()
                return

            # Sem cérebro nenhum: os atalhos locais acima ainda obedecem, mas
            # conversar é impossível. Dizer isso a cada frase vira ladainha, e
            # ficar mudo parece defeito — então avisa no máximo uma vez por
            # SEM_CEREBRO_INTERVALO_S.
            if not cerebro.disponivel:
                gestos.ouvindo()
                agora = time.time()
                if agora - self._avisou_sem_cerebro > SEM_CEREBRO_INTERVALO_S:
                    self._avisou_sem_cerebro = agora
                    falar(AVISO_SEM_CEREBRO)
                return

            # ── o turno ────────────────────────────────────────────────────
            # O modelo é consultado JÁ, numa thread. O aviso falado vira um
            # evento agendado que só acontece se a espera passar do limite — e
            # que é cancelado se a resposta chegar antes. Antes daqui a frase
            # saía sempre, inclusive quando o modelo respondia em meio segundo.
            conf_agora = Config.carregar()
            politica = conversa.Politica.de(conf_agora.conversa, conf_agora.arranque)
            turno = conversa.Turno(
                id=f"trn_{int(time.time() * 1000)}", correlacao=correlacao,
                inicio=time.monotonic(), politica=politica)
            turno_anterior = self._turno
            if turno_anterior is not None and turno_anterior.vivo:
                # Pergunta nova durante a anterior: a antiga não fala mais.
                turno_anterior.substituido_por = turno.id
                coordenador.cancelar(turno_anterior, "pergunta nova")
                controlador.eventos.publicar(
                    "voice.turn.cancelled", correlation_id=turno_anterior.correlacao,
                    turn_id=turno_anterior.id, reason="superseded_by",
                    superseded_by=turno.id)
            self._turno = turno
            coordenador.abrir(turno)
            controlador.eventos.publicar(
                "voice.turn.started", correlation_id=correlacao, turn_id=turno.id,
                mode=politica.modo, ack_delay_ms=politica.ack_atraso_ms)

            t0 = time.time()
            # O que acompanha o pedido: o que se falava na sala e o caderno.
            # Só AQUI, depois de todo retorno antecipado acima — ativação
            # sozinha, atalho local, robô sem cérebro —, porque colher gasta o
            # contexto, e gastá-lo num turno que não consulta o modelo seria
            # jogá-lo fora. O chat e o registro seguem mostrando `texto`, que é
            # o que a pessoa disse.
            # "O que você está vendo?": um quadro da câmera vira texto e vai
            # junto. Só quando o pedido pede para olhar; nunca em laço.
            visto = None
            if visao.pede_visao(texto):
                visto = self.olho.olhar(texto, conf_agora.conversa,
                                        correlacao=correlacao, origem="voz")
            # "Quem está aí?": o mesmo quadro, comparado com os perfis da
            # família (nunca pelo modelo de visão). Só com o interruptor.
            pessoas = None
            if familia.pede_reconhecer(texto):
                pessoas = self.familia.reconhecer(conf_agora.conversa,
                                                  correlacao=correlacao, origem="voz")
            # Pedido sobre aparelho: a situação real do controle da casa vai
            # junto, para o modelo não concluir "não tenho essa capacidade".
            situacao_casa = None
            if casa.pede_casa(texto):
                situacao_casa = casa.situacao(self._cliente_da_casa().estado())
            # Falou de música sem dar um comando: o modelo fica sabendo que a
            # capacidade existe e como se pede — nada da Spotify.
            situacao_musica = None
            if (musica.fala_de_musica(texto, time.monotonic() < self._musica_ate)
                    or musica_pedido.talvez(texto)):
                situacao_musica = musica.situacao(conf_agora.conversa.get("music_voice_enabled") is True)
            mensagem = self.acompanhamento.montar(texto, time.monotonic(),
                                                  conf_agora.conversa,
                                                  correlacao=correlacao, visao=visto,
                                                  pessoas=pessoas, situacao_casa=situacao_casa,
                                                  situacao_musica=situacao_musica)
            futuro = executor.submit(cerebro.perguntar, mensagem + complemento)
            gestos.pensando()
            gestos.fase(fases.FASE_PROCESSANDO)
            # Música tocando no PC: abaixa enquanto o modelo pensa e o robô fala,
            # e devolve no fim do turno. Cortesia — falhar não muda nada.
            musica_baixa = (self._musica_tocando and conf_agora.conversa.get("music_voice_enabled") is True)
            if musica_baixa:
                self._pool_da_musica().submit(self._cliente_da_musica().abafar, correlacao)

            # Prazos ABSOLUTOS a partir do início do turno. Encadear as esperas
            # daria ack+progresso, e um progresso de 10 s viraria 14 s.
            prazo_ack = politica.prazo_ack(turno.inicio)
            # Nunca antes do aviso: com um progresso configurado mais curto que
            # o acknowledgement, as duas frases sairiam coladas.
            prazo_prog = max(politica.prazo_progresso(turno.inicio), prazo_ack)
            def esperar_cerebro(ate: float | None) -> RespostaCerebro | None:
                """A resposta, ou `None` se o prazo venceu antes.

                Erro do cérebro vira resposta de falha aqui dentro: deixá-lo
                subir mataria o laço de voz e o robô ficaria mudo até o daemon
                reiniciar o app.
                """
                try:
                    if ate is None:
                        return futuro.result()
                    return futuro.result(timeout=max(0.0, ate - time.monotonic()))
                except FuturesTimeout:
                    return None
                except Exception:
                    log.exception("Falha inesperada ao consultar o cérebro")
                    return RespostaCerebro(False, FALHA_GENERICA, "nenhum",
                                           "erro", erro="excecao")

            resposta = None
            if not politica.pode_avisar and not politica.pode_progredir:
                # Fala automática desligada: nem prazo, nem fila, nem síntese.
                # Esperar pelos prazos aqui só atrasaria a resposta final para
                # não dizer nada no fim — o processamento segue imediatamente.
                turno.ack_estado = conversa.DESABILITADO
                controlador.eventos.publicar(
                    "voice.turn.acknowledgement",
                    correlation_id=correlacao, turn_id=turno.id,
                    decision=conversa.DESABILITADO,
                    reason=politica.motivo_silencio(), audio_enqueued=False)
                resposta = esperar_cerebro(None)
            for prazo, tipo in ((prazo_ack, "ack"), (prazo_prog, "progresso")):
                if resposta is not None:
                    break
                resposta = esperar_cerebro(prazo)
                if resposta is not None:
                    break
                if not turno.vivo:
                    break
                # O aviso só sai se o prazo venceu de verdade — é esta linha
                # que separa "o modelo demorou" de "toda pergunta ganha frase".
                falas = esperas if tipo == "ack" else progressos
                pode = (politica.pode_avisar if tipo == "ack" else
                        politica.pode_progredir
                        and turno.progressos < politica.max_progresso)
                if falas and pode:
                    onda = self._aleatorio.choice(falas)
                    if coordenador.tocar_ack(turno, onda):
                        if tipo == "progresso":
                            turno.progressos += 1
                        controlador.eventos.publicar(
                            "voice.turn.acknowledgement",
                            correlation_id=correlacao, turn_id=turno.id, kind=tipo,
                            decision=conversa.TOCANDO, audio_enqueued=True,
                            at_ms=int((time.monotonic() - turno.inicio) * 1000))
                elif not pode:
                    controlador.eventos.publicar(
                        "voice.turn.acknowledgement",
                        correlation_id=correlacao, turn_id=turno.id, kind=tipo,
                        decision=conversa.DESABILITADO,
                        reason=politica.motivo_silencio(), audio_enqueued=False)
            if resposta is None:
                resposta = esperar_cerebro(None)

            cerebro_ms = int((time.time() - t0) * 1000)
            if not resposta.ok:
                gestos.erro("agent_failed", str(resposta.erro or resposta.modo))
            decisao = coordenador.resolver_ack(turno)
            if decisao != "none":
                log.debug("Turno %s: aviso %s.", turno.id, decisao)

            if not turno.vivo:
                # Substituído ou cancelado enquanto o modelo pensava: o texto é
                # descartado. Ferramenta física já executada NÃO se desfaz — o
                # que se evita aqui é só a fala fora de hora.
                log.info("Resposta do turno %s descartada (%s).", turno.id,
                         turno.substituido_por or "cancelado")
                controlador.eventos.publicar(
                    "voice.turn.cancelled", correlation_id=correlacao,
                    turn_id=turno.id, reason="late_response", brain_ms=cerebro_ms)
                if musica_baixa:
                    self._pool_da_musica().submit(self._cliente_da_musica().restaurar, correlacao)
                return

            # Primeiro o caderno, depois a limpeza para a voz: a diretiva de
            # anotação é executada e SAI do texto antes de qualquer coisa ser
            # falada, mostrada no chat ou escrita no registro.
            recebida = self.acompanhamento.receber(
                resposta.texto, conf_agora.conversa, correlacao=correlacao)
            fala = limpar_para_voz(recebida)
            log.info("🤖 GarraIA [%s%s] (%.1fs): %s", resposta.modo,
                     f"/{resposta.erro}" if resposta.erro else "",
                     time.time() - t0, fala)
            controlador.eventos.publicar(
                "chat.message", correlation_id=correlacao, role="assistant",
                content=fala, source="garra", brain=resposta.modo,
                agent=getattr(resposta, "agente", None),
            )
            try:
                if fala:
                    falar(fala, turno=turno)
                else:
                    # A frase de espera já saiu pelo alto-falante: drena o eco, senão
                    # o próprio robô vira "fala do usuário" no próximo ciclo.
                    drenar_mic(0.4)
                    gestos.ouvindo()
            finally:
                if musica_baixa:
                    self._pool_da_musica().submit(self._cliente_da_musica().restaurar, correlacao)
            controlador.eventos.publicar(
                "voice.turn.completed", correlation_id=correlacao,
                turn_id=turno.id, mode=politica.modo, brain_ms=cerebro_ms,
                total_ms=int((time.monotonic() - turno.inicio) * 1000),
                brain=resposta.modo, agent=getattr(resposta, "agente", None),
                ack={"decision": turno.ack_decisao,
                     "duration_ms": round(turno.ack_duracao_ms or 0.0, 1),
                     "remaining_ms": turno.metricas.get("ack_restante_ms"),
                     "clear_failed": turno.corte_falhou,
                     "progress_messages": turno.progressos})
            if self._turno is turno:
                self._turno = None
            # A janela de inatividade renova AGORA, não quando o enunciado foi
            # aceito: entre uma coisa e outra houve modelo, TTS e dreno do
            # microfone, e renovar na aceitação fecharia a sessão justamente
            # quando a pessoa pôde voltar a falar. O teto da sessão não é
            # tocado — ele conta desde a ativação e nunca renova.
            sessao_ativacao.renovar(time.monotonic())
            # Só agora o histórico pode ser marcado como falado.
            cerebro.confirmar_falado(resposta.marca)

        try:
            # Pré-sintetiza frases de espera: o robô reage em ~1s.
            #
            # Só o que a política permite tocar. Não é otimização: sintetizar
            # uma frase que nunca vai sair custou 11,4 s medidos entre o loop
            # de voz subir e o robô ficar pronto, e deixava seis áudios de
            # filler carregados num app configurado para não falar filler.
            atual = Config.carregar()
            politica_inicial = conversa.Politica.de(atual.conversa, atual.arranque)
            esperas: list[np.ndarray] = []
            progressos: list[np.ndarray] = []
            if politica_inicial.pode_avisar:
                for f in FRASES_ESPERA:
                    if stop_event.is_set():
                        return
                    try:
                        onda = voz.falar(f, sr_out)
                        if onda.size:
                            esperas.append(onda)
                    except Exception:
                        pass
            if politica_inicial.pode_progredir:
                for f in FRASES_PROGRESSO:
                    if stop_event.is_set():
                        return
                    try:
                        onda = voz.falar(f, sr_out)
                        if onda.size:
                            progressos.append(onda)
                    except Exception:
                        pass
            log.info("%d frases de espera e %d de progresso prontas "
                     "(fala automática %s).", len(esperas), len(progressos),
                     "ligada" if politica_inicial.fala_automatica else "DESLIGADA")

            # Calibra o ruído ambiente por ~1,2 s — só como ponto de partida: o
            # detector segue medindo enquanto escuta. Um número tirado na
            # partida e nunca mais revisto foi o que deixou o robô surdo (ver
            # `segmentador.py`).
            ambiente = None
            if cfg.limiar is None:
                amostras = []
                fim = time.time() + 1.2
                while time.time() < fim and not stop_event.is_set():
                    m = ler_mono()
                    if m.size:
                        amostras.append(float(np.sqrt(np.mean(m ** 2))))
                    time.sleep(0.03)
                ambiente = float(np.median(amostras)) if amostras else 0.003
            prefs_voz = conversa.normalizar(Config.carregar().conversa)
            detector = Segmentador(
                sr_in, piso_inicial=ambiente, limiar_fixo=cfg.limiar,
                sensibilidade=conversa.ganho_do_microfone(prefs_voz["mic_sensitivity"]),
                fim_de_fala_s=prefs_voz["vad_silence_ms"] / 1000.0)
            self._detector = detector
            log.info("Limiar de voz: %.4f (%s). Pode falar com o robô!",
                     detector.limiar_abre,
                     "fixo" if cfg.limiar is not None else "acompanha o ruído")

            # A saudação sai sem pergunta nenhuma, uma vez, quando o loop sobe:
            # é fala automática como qualquer outra, e obedece ao mesmo mestre.
            if politica_inicial.pode_saudar:
                falar(SAUDACAO)
            else:
                controlador.eventos.publicar(
                    "voice.startup.greeting", decision=conversa.DESABILITADO,
                    reason=("automatic_speech_disabled"
                            if not politica_inicial.fala_automatica
                            else "spoken_greeting_disabled"),
                    audio_enqueued=False)
                log.info("Saudação de arranque desligada; nenhum áudio tocado.")

            ultima_conferida = time.time()

            def conferir_escuta(agora: float) -> None:
                """Uma vez por segundo: o painel mudou algo que vale AGORA?

                É o que faz o interruptor da escuta, a sensibilidade e as
                preferências de fala valerem sem reiniciar o app, e o que leva a
                tela de "ouvindo" a "aguardando a frase" quando a sessão expira
                em silêncio.
                """
                if agora - escuta["conferida"] < 1.0:
                    return
                escuta["conferida"] = agora
                conf_escuta = Config.carregar().conversa
                escuta["ativacao"] = ativacao.Ativacao.de(conf_escuta)
                prefs_voz = conversa.normalizar(conf_escuta)
                detector.ajustar(
                    sensibilidade=conversa.ganho_do_microfone(prefs_voz["mic_sensitivity"]),
                    fim_de_fala_s=prefs_voz["vad_silence_ms"] / 1000.0)
                if hasattr(voz, "preferir"):
                    voz.preferir(prefs_voz)
                ligada = prefs_voz["listening_enabled"]
                if ligada != escuta["ligada"]:
                    escuta["ligada"] = ligada
                    self._aplicar_escuta(reachy_mini, ligada, log, controlador.eventos)
                    detector.descartar()
                    if ligada:
                        drenar_mic(0.2)
                if not detector.em_fala:
                    gestos.atualizar_fase_de_escuta()

            while not stop_event.is_set():
                mono = ler_mono()
                agora = time.time()
                conferir_escuta(agora)

                if mono.size == 0:
                    trecho = detector.sem_audio(agora)
                    if trecho is not None:
                        processar(trecho.audio, esperas)
                        atender_ao_vivo()
                    elif not detector.em_fala:
                        # O que foi ouvido há mais tempo que a janela sai da
                        # memória agora, e não na próxima fala.
                        self.acompanhamento.podar(time.monotonic())
                        # Silêncio de verdade: hora de falar notificações pendentes
                        evento = fila.proximo()
                        if evento is not None:
                            fala = limpar_para_voz(
                                caderno_mod.sem_diretivas(evento.texto))
                            if fala:
                                log.info("🔔 Notificação [%s]: %s", evento.tipo, fala)
                                falar(fala)
                            if evento.seq is not None:
                                cerebro.confirmar_falado(evento.seq + 1)
                    stop_event.wait(0.015)
                    continue

                # Onde a fala começa e termina é com o detector: ele guarda o
                # ataque da palavra, acompanha o ruído e nunca passa da duração
                # máxima.
                trecho = detector.bloco(mono, agora)
                if trecho is not None:
                    processar(trecho.audio, esperas)
                    atender_ao_vivo()

                if self._acordar.is_set():
                    # Configuração nova. Voltar ao supervisor é o caminho mais
                    # curto para reavaliar TUDO (voz, gateway, cérebro) e
                    # reentrar já com o cliente de chat repontado.
                    #
                    # Consumir o sinal ANTES de voltar: o supervisor reentra
                    # aqui em seguida, e um sinal ainda levantado faria este
                    # `return` disparar de novo na hora — laço apertado entre as
                    # duas funções, sem nunca ouvir o microfone.
                    self._acordar.clear()
                    log.info("Configuração mudou; reavaliando os serviços.")
                    return
                if time.time() - ultima_conferida > RECONFERIR_CEREBRO_S and not detector.em_fala:
                    ultima_conferida = time.time()
                    # A câmera junto: o status dela mentia depois de perder a
                    # corrida do arranque, e mentira de status é o defeito que
                    # mais custa a achar.
                    servicos_mod.marcar_camera(self.servicos, self.hub)
                    cfg = Config.carregar()
                    if self._sondar_cerebro(cfg) and not cerebro.disponivel:
                        log.info("Cérebro voltou; reconstruindo.")
                        antigo = cerebro
                        cerebro = self._avaliar_cerebro(cfg)
                        cerebro_atual[0] = cerebro
                        antigo.fechar()   # a sessão keep-alive do antigo
                    # E a voz? Esperar o STT falhar três vezes para perceber que
                    # ela caiu custa três falas do usuário, cada uma até o
                    # timeout de 60 s. Uma sondagem barata enquanto ninguém fala
                    # devolve o app ao supervisor em segundos.
                    if not voz.pronto(timeout=2.0):
                        log.warning("Servidor de voz sumiu; voltando ao modo sem voz.")
                        return

                if self._falhas_voz >= FALHAS_VOZ_ATE_DESISTIR:
                    # A voz caiu no meio da conversa. Voltar ao supervisor faz o
                    # app cair para o modo painel e reconectar sozinho quando ela
                    # voltar, em vez de gastar STT contra um servidor morto.
                    log.warning("Servidor de voz falhou %d vezes seguidas; "
                                "voltando ao modo sem voz.", self._falhas_voz)
                    return
        finally:
            gestos.fase_ao_ouvir = None
            gestos.fase(fases.FASE_ESPERA, "voice_loop_stopped")
            # Primeiro a thread, depois o socket: fechar a sessão por baixo de
            # uma consulta em andamento faria o poll levantar antes de ver o
            # evento — ele trata, mas o log ficaria sujo à toa.
            fim_do_ciclo.set()
            poll.join(timeout=2.0)
            cerebro.fechar()
            self._detector = None
            self._falar_texto = None
            self._calar = None
            self._trocar_sessao = None
            # O executor sobrevive ao laço de propósito (ver acima). Quem o
            # encerra é `_encerrar_executor`, no fim da vida do app.
            self.servicos.marcar(
                "voice", False, codigo="stopped",
                detalhe="voice loop not running")

    def _encerrar_robo(self) -> None:
        """Derruba as threads do robô na ordem certa, sem deixar órfã."""
        self._encerrar_executor()
        for nome, encerrar in (
            ("comportamento", getattr(self.comportamento, "encerrar", None)),
            ("câmera", getattr(self.hub, "encerrar", None)),
            ("controlador", getattr(self.controlador, "encerrar", None)),
        ):
            if encerrar is None:
                continue
            try:
                encerrar()
            except Exception:
                self.logger.debug("falha ao encerrar %s", nome, exc_info=True)


def _rodar_simulado() -> None:
    """Sobe painel e API sem robô nenhum, para desenvolver e diagnosticar.

    Não é o `--sim` do daemon (MuJoCo): aqui não há daemon nenhum. O
    `BackendSimulado` aceita as ações e devolve `executed: false`, que é o que
    mantém a promessa de honestidade de ponta a ponta.
    """
    import logging

    import uvicorn
    from fastapi import FastAPI

    logging.basicConfig(level=logging.INFO)
    cfg = Config.carregar()
    politica = resolver_politica()
    backend = BackendSimulado()
    controlador = ControladorRobo(backend, dir_capturas=armazenamento.diretorio() / "capturas")
    controlador.iniciar()
    hub = FrameHub(backend)
    servicos = Servicos(controlador.eventos)
    servicos.marcar("robot", False, codigo="simulated",
                    detalhe="simulated backend; no physical robot",
                    dica="Start the app from the Reachy Mini dashboard to "
                         "drive a real robot.")
    servicos.marcar("movement", False, codigo="simulated",
                    detalhe="actions are accepted but never executed")
    servicos.marcar("camera", False, codigo="simulated")
    servicos.marcar("voice", False, codigo="not_running")
    servicos.marcar("gateway", False, codigo="not_running")
    servicos.marcar("brain", False, codigo="not_running")
    app = FastAPI(title="Garra Reachy Mini (simulated)")
    preparar(app, politica)
    # As integrações também no simulado: é assim que o painel se valida num
    # PC com a ponte de verdade, sem robô. O quadro é o sintético do backend.
    eventos = controlador.eventos
    montar(app, ContextoWeb(
        controlador=controlador, hub=hub, eventos=eventos,
        politica=politica, servicos=servicos,
        chat=PonteChat(cfg.gateway_url, cfg.gateway_key, cfg.agent_id),
        acompanhamento=Acompanhamento(eventos),
        olho=visao.Olho(hub, lambda: visao.ClienteVisao(cfg.gateway_url, cfg.gateway_key), eventos),
        familia=familia.Familia(hub, lambda: familia.ClienteFamilia(cfg.gateway_url, cfg.gateway_key), eventos),
        integracoes=PonteIntegracoes(cfg.gateway_url, cfg.gateway_key),
        dir_estatico=Path(__file__).resolve().parent / "static",
    ))
    print(f"\nModo simulado (sem robô). Painel em {politica.url_visivel}/reachy\n")
    try:
        uvicorn.run(app, host=politica.host, port=politica.porta, log_level="warning")
    finally:
        hub.encerrar()
        controlador.encerrar()


if __name__ == "__main__":
    import sys

    if "--simulado" in sys.argv:
        _rodar_simulado()
    else:
        app = GarraReachyMini()
        try:
            app.wrapped_run()
        except KeyboardInterrupt:
            app.stop()
