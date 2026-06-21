# Decisões arquiteturais/técnicas

Registro de decisões com o "porquê". Append-only — não edita entradas antigas.

<!-- Formato:
## YYYY-MM-DD — Título curto da decisão
**Motivo:** por que foi decidido assim.
**Alternativas consideradas:** o que ficou de fora e por quê.
-->

## 2026-06-21 — Interface = servidor MCP próprio
**Motivo:** o agente (Claude Code) chama tools nativas (get_balance, get_quote, get_positions, place_order, cancel_order, get_portfolio...) sem depender de Bash/CLI. Mais elegante e seguro para a skill `/invest`; é o "MCP do IBKR" que o usuário queria.
**Alternativas consideradas:** CLI em Python (mais simples, descartado por ser menos integrado); híbrido lib+CLI+MCP (overengineering para o início).

## 2026-06-21 — Conexão IBKR = Client Portal API (CPAPI), não TWS/ib_async
**Motivo:** o requisito firme de fracionário (aportes por valor em US$, DCA, rebalanceamento) só é oficialmente suportado via CPAPI usando o campo `cashQty` (changelog IBKR 27/03/2026). O TWS API/ib_async tem suporte ambíguo e historicamente quebrado para fracionário de equity (erros 10242/10243, quantidade decimal rejeitada/truncada). Insight: o quick_invest JÁ usava CPAPI — só mandava `quantity` (inteiro) em vez de `cashQty`. Logo, não é migração, é usar o campo certo.
**Alternativas consideradas:** ib_async/TWS (revertida — pior para fracionário); FIX API (institucional, fora de escopo).

## 2026-06-21 — Arquitetura hexagonal (ports & adapters), CPAPI-only por enquanto
**Motivo:** robustez e profissionalismo vêm do DESIGN, não de duplicar conexão. Definir `BrokerPort`, `MarketDataPort`, `AuthPort` e implementar tudo sobre CPAPI agora. Permite plugar um adapter de dados em ib_async no futuro sem reescrever nada.
**Alternativas consideradas:** híbrido CPAPI (execução) + ib_async (dados) AGORA — descartado: com OAuth headless exigiria 2 sessões headless + 2 usernames (regra "1 brokerage session por username") = foot-gun e ruim para repo público reproduzível.

## 2026-06-21 — Auth = OAuth headless como alvo, Gateway (login manual) como fallback
**Motivo:** objetivo final é schedule autônomo sem navegador → OAuth. Mas OAuth da Web API para conta varejo pessoal pode exigir solicitar acesso à IBKR (e há OAuth 1.0a mais complexo), o que não pode bloquear o início. Auth atrás de `AuthPort` com adapter de Gateway garantido permite rodar desde o dia 1 e trocar para OAuth depois.
**Alternativas consideradas:** Gateway-only (não atinge o objetivo headless); OAuth-only desde já (risco de bloqueio por liberação da IBKR).

## 2026-06-21 — OAuth descartado: auth só via Gateway + tickle (revisão da decisão de auth)
**Motivo:** pesquisa (fontes oficiais IBKR, fev/2025) confirmou que **OAuth da Web API NÃO está disponível para conta de varejo/pessoa física** — OAuth 1.0a é só institucional/terceiro registrado (processo de Compliance de 3-6 semanas); OAuth 2.0 individual está "em consideração, sem ETA". Logo, o único caminho real é **Client Portal Gateway + login manual no navegador + keep-alive `/tickle`**. O `AuthPort` continua existindo (bom design), mas só haverá o adapter de Gateway.
**Implicações operacionais confirmadas:** sessão expira ~6min sem tickle (tickle a cada ~60s); duração máx 24h com reset à meia-noite; manutenção diária ~01:00 local derruba a sessão (agendar DCA fora disso); **1 brokerage session por username** → usar username dedicado ao bot; `GET /iserver/accounts` obrigatório antes de operar; rate limit 10 req/s no gateway; conta live precisa estar aberta/fundeada/IBKR Pro mesmo p/ usar só o paper.
**Alternativas consideradas:** OAuth headless (era o alvo — agora inviável p/ varejo).

## 2026-06-21 — Validação contra conta real confirma a aposta arquitetural (CPAPI + cashQty)
**Motivo:** o sistema foi testado ponta a ponta contra a conta live `U24235856` e funcionou: auth/connected OK, saldo lido (US$8.87), cotação e posições. As flags da conta confirmam o requisito-chave: `supportsCashQty:true` e `supportsFractions:true` (e `liteUser:false` → Pro). Ou seja, a decisão de ir de CPAPI por causa do fracionário estava certa e é executável nesta conta.
**Achados operacionais:** o build de 2023 do gateway NÃO é problema (serverVersion em runtime = Build 10.46.1l, Jun/2026 — conecta no backend atual). O login só destravou com restart limpo do gateway + aba anônima + sem sessão concorrente (receita na memória global [[ibkr-gateway-login]]). Paper não conectava nesta máquina; a real conecta.

## 2026-06-21 — Escopo = paper primeiro + trava dura para live
**Motivo:** segurança. Opera de fato na conta paper `U24235856`. `live` existe atrás de flag explícita, com dry-run como padrão, confirmação obrigatória e limite de valor.
**Alternativas consideradas:** só leitura/simulação primeiro (lento demais); paper-only sem caminho p/ live (não atende objetivo final).
