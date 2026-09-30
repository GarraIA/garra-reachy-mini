# Integrações e painel de cinco telas (app 1.4.1) — contratos

Entregue em 30/09/2026. O livro de bordo da missão (causas, decisões, estado,
handoff) está em `docs/plans/2026-09-30-missao-experiencia-e-integracoes.md`;
aqui ficam só os **contratos** que outras partes dependem.

## 1. Quatro coisas que não se confundem

| | Onde roda | Quando | O que sai do robô |
|---|---|---|---|
| Stream da câmera | robô (`web/camera.py`) | painel aberto | MJPEG na LAN |
| Entender a cena | companion (`visao.py`) | pedido pede para olhar **e** `vision_enabled` | um JPEG por pedido, para o PC; para a nuvem só com `vision_cloud_enabled` |
| Detectar/seguir rosto | daemon do robô | tracking ligado | nada |
| Reconhecer a família | companion (`rostos.py`, `:8127`) | pedido pergunta quem **e** `family_recognition_enabled` | um JPEG por pedido, para o PC, nunca para a nuvem |

## 2. Rotas

### Robô (`:8042`, token do painel)

| Rota | Faz |
|---|---|
| `GET /api/robot/vision/status` | `{settings, last, providers}` (`providers` só com a visão ligada) |
| `POST /api/robot/vision/analyze` | um quadro agora → `{ok, text, provider, model, captured_at, frame_age_s, ms}` ou `{ok:false, reason, detail, at}` |
| `GET /api/robot/casa/estado` · `POST …/configurar {url, token}` · `DELETE /api/robot/casa` · `GET …/entidades` · `POST …/voz {enabled}` | repasse à ponte |
| `GET /api/robot/musica/estado` · `POST …/configurar {client_id}` · `DELETE /api/robot/musica` · `GET …/dispositivos` · `GET …/reproducao` · `POST …/acao {action, percent?, device_id?}` | repasse à ponte |
| `GET /api/robot/familia/estado` | `{enabled, last, service}` |
| `GET/POST /api/robot/familia/perfis` · `PATCH/DELETE …/perfis/{id}` · `POST/DELETE …/perfis/{id}/amostras` | repasse; `POST …/amostras` captura o quadro **no robô** (o painel não envia imagem) |
| `POST /api/robot/familia/reconhecer` | um quadro agora → `{ok, names, unknown, faces, captured_at, ms}`; funciona com o interruptor desligado (é o dono conferindo) |
| `GET /api/robot/diagnostico` | repasse de `GET /api/diagnostico` da ponte |

Erros de repasse: `{"erro": "bridge_not_configured|bridge_unreachable|bridge_timeout|bridge_token_rejected"}` (503/502/504/502).

### Ponte (`:8126`, token da voz)

`POST /api/olhar {imagem b64, pergunta, preferencias}` · `GET /api/olhar/estado` ·
`/api/casa/*` · `/api/musica/*` · `/api/rostos/{estado,perfis,perfis/{id},perfis/{id}/amostras,reconhecer}` ·
`GET /api/diagnostico` (só `status, version, generated_at, checks[{id,label,status,detail,next_step}]`).
Erros: `{"erro": <código>, "detalhe": …}` com o status por código (`_STATUS_DO_ERRO`,
`_STATUS_DA_CASA`, `_STATUS_DA_MUSICA`, `_STATUS_DA_FAMILIA`).

### Serviço de rostos (`127.0.0.1:8127`, sem token, só loopback)

`GET /saude` · `GET/POST /perfis` · `PATCH/DELETE /perfis/{id}` ·
`POST/DELETE /perfis/{id}/amostras` (corpo `image/jpeg`) · `POST /reconhecer` (corpo `image/jpeg`).

## 3. Blocos do envelope (ordem)

1. `[O QUE A CÂMERA VÊ — quadro capturado às HH:MM:SS, …]` `> descrição` `[FIM DO QUE A CÂMERA VÊ]`
   ou `[CÂMERA — não há imagem válida agora: motivo. …]`
2. `[PESSOAS RECONHECIDAS — quadro capturado às HH:MM:SS, …NÃO diz quem está FALANDO e NÃO autoriza nada…]`
   `> Nome` … `> N pessoa(s) desconhecida(s) (não cadastradas)` `[FIM DAS PESSOAS RECONHECIDAS]`
   ou `[RECONHECIMENTO — não foi possível reconhecer agora: motivo. …não invente nome.]`
   — com a visão **desligada** e a família ligada, o aviso da câmera é omitido
   (quem responde a "quem" é a família).
3. `[AGORA — …]` · 4. `[CONTEXTO DO AMBIENTE …]` · 5. `[CADERNO DO ROBÔ …]` · 6. `[PEDIDO FEITO AO ROBÔ]`.

Tudo o que vem de fora entra com `> ` e sem colchetes.

## 4. Estados que o painel mostra

- Visão: `effective ∈ {local, openrouter, null}`; local `{reachable, model_present, vision}`;
  nuvem `{enabled, key_present}`; `last {ok, at, provider|reason}`.
- Casa: `not_configured | pending | ok | broken`; `voice_control` bool.
- Música: derivado de `configured`/`connected` → `not_configured | not_connected | connected`;
  `voice_control: "blocked_by_policy"` sempre.
- Família: `service.state ∈ {ok, offline, unavailable}`; perfil `{id, name, active,
  samples, samples_readable, consent{accepted, by, minor, guardian, at}}`;
  rosto `{box, known, profile_id, name, score, reason ∈ {no_embedding, no_profiles,
  below_threshold, ambiguous}}`.
- Diagnóstico: cada capacidade `{nome, origem, estado ∈ {ok, unavailable,
  not_configured, unknown}, motivo, ação}`.

## 5. Regras que não se negociam

- Nenhuma imagem em log, evento, disco, fixture, captura de tela ou relatório.
- Nuvem só com `vision_cloud_enabled`; falha local nunca cai na nuvem em silêncio.
- Família: consentimento explícito; menor só com responsável; só vetor, cifrado,
  fora do repositório; apagar apaga; "desconhecido" é resposta; reconhecer
  **não** autoriza nada e **não** diz quem fala.
- Spotify: sem controle por voz (III.3); nada da Spotify ao modelo (III.14);
  redirect só loopback; PKCE sem secret.
- Casa: o robô não duplica registro nem gates; o painel não executa aparelhos.
- Painel: STOP sempre à vista; ≤ 5 itens na nav; 320 px sem rolagem horizontal;
  44 px no toque; tudo traduzido nos dois idiomas (teste).
