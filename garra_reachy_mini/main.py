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
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

import numpy as np
from pydantic import BaseModel
from reachy_mini import ReachyMini, ReachyMiniApp

from enum import Enum

from . import armazenamento
from .cerebro import (AVISO_SEM_CEREBRO, FALHA_GENERICA, Cerebro,
                      RespostaCerebro)
from .cerebro import sondar as cerebro_sondar
from . import conversa
from .config import (FALA_MAXIMA_S, FALA_MINIMA_S, FIM_DE_FALA_S, PRE_ROLL_S,
                     FRASES_ESPERA, FRASES_PROGRESSO, SAUDACAO, Config)
from .eventos import FilaEventos
from .robo import intencoes
from .robo.acoes import ControladorRobo
from .robo.backends import BackendSdk, BackendSimulado
from .robo.comportamento import Comportamento
from .robo.daemon_api import DaemonAPI
from . import servicos as servicos_mod
from .servicos import Servicos
from .voz import PreRoll, VozClient, frases, limpar_para_voz
from .web import (ContextoWeb, FrameHub, PonteChat, montar, preparar,
                  resolver_politica)

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
        # Quem está de pé e quem não está. O painel lê isto para dizer ao
        # usuário o que falta, em vez de parecer quebrado.
        self.servicos = Servicos()
        self._midia_ligada = False
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
                "salva": _salva_publica(armazenamento.carregar_config()),
            }

        @self.settings_app.post("/api/config")
        def salvar_config(opcoes: OpcoesSalvas):
            atual = armazenamento.carregar_config()
            # Campo ausente do JSON = preservar; None ou "" = limpar (volta ao
            # padrão). Vale para texto e para número: o form manda null quando o
            # campo numérico está vazio.
            for chave, valor in opcoes.model_dump(exclude_unset=True).items():
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
                 "The Garra gateway did not answer. Open the Reachy page on the "
                 "Garra console and use Configure or reconnect Reachy.",
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
        return disponivel

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
                servicos=self.servicos,
                falar=self._falar_async,
                calar=self._calar_agora,
                nova_sessao=self._nova_sessao_async,
                dir_estatico=Path(__file__).resolve().parent / "static",
            ),
        )
        self.logger.info(
            "Painel do Reachy em %s/reachy — API em %s/api/robot",
            self.politica.url_visivel, self.politica.url_visivel,
        )

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
            stop_event.wait(30.0)
            return
        if not self._midia_ligada:
            reachy_mini.media.start_recording()
            reachy_mini.media.start_playing()
            self._midia_ligada = True
        log.info("Conectado ao robô (mic %sHz, som %sHz).", sr_in, sr_out)

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
        self._trocar_sessao = cerebro.nova_sessao

        def ler_mono() -> np.ndarray:
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
                gestos.ouvindo()
            finally:
                self._lock_fala.release()
            # Nenhuma onda chegou ao alto-falante: ou o TTS falhou frase a
            # frase, ou o turno foi substituído no meio. Quem chamou merece
            # saber a diferença entre isso e uma fala que aconteceu.
            return ResultadoFala.FALADA if houve_audio else ResultadoFala.FALHA

        # A partir daqui o painel também consegue falar pelo robô.
        self._falar_texto = falar

        def processar(audio: np.ndarray, esperas: list[np.ndarray]) -> None:
            dur = audio.size / sr_in
            if dur < FALA_MINIMA_S:
                return
            gestos.pensando()
            t0 = time.time()
            try:
                texto = voz.transcrever(audio, sr_in)
            except Exception:
                self._falhas_voz += 1
                log.exception("Falha no STT (%d seguidas)", self._falhas_voz)
                gestos.ouvindo()
                return
            self._falhas_voz = 0
            if not texto or len(texto) < 2:
                gestos.ouvindo()
                return
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
            futuro = executor.submit(cerebro.perguntar, texto + complemento)

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
                return

            fala = limpar_para_voz(resposta.texto)
            log.info("🤖 GarraIA [%s%s] (%.1fs): %s", resposta.modo,
                     f"/{resposta.erro}" if resposta.erro else "",
                     time.time() - t0, fala)
            controlador.eventos.publicar(
                "chat.message", correlation_id=correlacao, role="assistant",
                content=fala, source="garra", brain=resposta.modo,
            )
            if fala:
                falar(fala, turno=turno)
            else:
                # A frase de espera já saiu pelo alto-falante: drena o eco, senão
                # o próprio robô vira "fala do usuário" no próximo ciclo.
                drenar_mic(0.4)
                gestos.ouvindo()
            controlador.eventos.publicar(
                "voice.turn.completed", correlation_id=correlacao,
                turn_id=turno.id, mode=politica.modo, brain_ms=cerebro_ms,
                total_ms=int((time.monotonic() - turno.inicio) * 1000),
                brain=resposta.modo,
                ack={"decision": turno.ack_decisao,
                     "duration_ms": round(turno.ack_duracao_ms or 0.0, 1),
                     "remaining_ms": turno.metricas.get("ack_restante_ms"),
                     "clear_failed": turno.corte_falhou,
                     "progress_messages": turno.progressos})
            if self._turno is turno:
                self._turno = None
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

            # Calibra o ruído ambiente por ~1,2s (ou usa limiar fixo)
            if cfg.limiar is None:
                amostras = []
                fim = time.time() + 1.2
                while time.time() < fim and not stop_event.is_set():
                    m = ler_mono()
                    if m.size:
                        amostras.append(float(np.sqrt(np.mean(m ** 2))))
                    time.sleep(0.03)
                ambiente = float(np.median(amostras)) if amostras else 0.003
                limiar = max(0.006, ambiente * 4.0)
            else:
                limiar = cfg.limiar
            log.info("Limiar de voz: %.4f. Pode falar com o robô!", limiar)

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

            buffer: list[np.ndarray] = []
            # O que veio antes do limiar. Sem ele o começo da primeira palavra
            # não chega ao STT — ver `voz.PreRoll`.
            pre_roll = PreRoll(int(PRE_ROLL_S * sr_in))
            em_fala = False
            ultimo_som = 0.0
            ultima_conferida = time.time()

            while not stop_event.is_set():
                mono = ler_mono()
                agora = time.time()

                if mono.size == 0:
                    if em_fala and (agora - ultimo_som) > FIM_DE_FALA_S:
                        em_fala = False
                        audio = np.concatenate(buffer) if buffer else np.empty(0, np.float32)
                        buffer.clear()
                        pre_roll.limpar()
                        processar(audio, esperas)
                    elif not em_fala:
                        # Silêncio de verdade: hora de falar notificações pendentes
                        evento = fila.proximo()
                        if evento is not None:
                            fala = limpar_para_voz(evento.texto)
                            if fala:
                                log.info("🔔 Notificação [%s]: %s", evento.tipo, fala)
                                falar(fala)
                            if evento.seq is not None:
                                cerebro.confirmar_falado(evento.seq + 1)
                    stop_event.wait(0.015)
                    continue

                rms = float(np.sqrt(np.mean(mono ** 2)))
                if rms >= limiar:
                    if not em_fala:
                        em_fala = True
                        buffer.clear()
                        # O ataque da palavra está aqui, não no bloco que
                        # acabou de passar do limiar.
                        buffer.extend(pre_roll.drenar())
                    ultimo_som = agora
                    buffer.append(mono)
                elif em_fala:
                    buffer.append(mono)
                    dur = sum(len(b) for b in buffer) / sr_in
                    if (agora - ultimo_som) > FIM_DE_FALA_S or dur > FALA_MAXIMA_S:
                        em_fala = False
                        audio = np.concatenate(buffer)
                        buffer.clear()
                        pre_roll.limpar()
                        processar(audio, esperas)
                else:
                    pre_roll.guardar(mono)

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
                if time.time() - ultima_conferida > RECONFERIR_CEREBRO_S and not em_fala:
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
            # Primeiro a thread, depois o socket: fechar a sessão por baixo de
            # uma consulta em andamento faria o poll levantar antes de ver o
            # evento — ele trata, mas o log ficaria sujo à toa.
            fim_do_ciclo.set()
            poll.join(timeout=2.0)
            cerebro.fechar()
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
    montar(app, ContextoWeb(
        controlador=controlador, hub=hub, eventos=controlador.eventos,
        politica=politica, servicos=servicos,
        chat=PonteChat(cfg.gateway_url, cfg.gateway_key, cfg.agent_id),
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
