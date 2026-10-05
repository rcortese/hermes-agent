# Delegação de desenvolvimento com fallback explícito

Esta alteração é **somente source**. Não ativa configuração, não modifica auth,
Honcho, rotas do coordenador/auxiliares/produtividade/visão nem opções de Extra
Usage. Não houve canário, chamada de teste de provedor ou esgotamento real de cota.

## Seleção e autoridade

`tasks[].purpose` aceita `general` (default) ou `development`. É independente de
`category` (`simples`, `analitica`, `complexa`; default `analitica`). A dificuldade
nunca implica desenvolvimento. O modelo só seleciona um propósito e uma categoria;
não pode enviar modelo, provedor, cadeia, credenciais ou endpoint.

Cada linha existente de `delegation.categories` continua sendo a rota `general`.
Uma chave opcional `development` dentro dessa linha contém **sua configuração
completa e própria**. Se a rota solicitada não existir, o lote falha antes de
resolver credenciais ou criar filhos; não cai implicitamente na rota general.
Toda a tabela, inclusive linhas development não utilizadas, é validada primeiro.

As duas modalidades conservam os tetos de iterações existentes: **40 / 80 / 120**.
O proprietário ainda pode reduzir `max_iterations` dentro do teto da categoria;
omissão usa o teto, booleanos e valores superiores são rejeitados. Overrides do
chamador seguem proibidos no modo de categorias. Trocar provedor ou repetir uma
conversa para validar output_schema **não concede orçamento novo**. Trata-se do
orçamento agregado de chamadas reportadas pelo loop, não de um limite de HTTP:
retries de transporte e finalização mantêm a semântica nativa. Contabilização
incerta esgota a concessão e é sinalizada.

## Contrato de configuração

Cada rota exige `model`, `provider`, `reasoning_effort` (`low`, `medium`, `high`),
`allowed_models` (inclui o primário) e `fallback_providers` explícito. Não herda
cadeia da linha general nem do pai. `fallback_providers: []` desabilita fallback.

Com alternativas não vazias, `allowed_routes` é obrigatório e deve listar pares
exatos `{provider, model}` autorizados, incluindo primário e cada alternativa.
Se presente mesmo com cadeia vazia, também deve autorizar o primário.
`allowed_models` continua compatível com o contrato antigo de validação do
primário; alternativas são autorizadas por **par**, não apenas pelo modelo.

Cada alternativa e cada entrada de `allowed_routes` aceita somente essas duas
chaves. Campos como `base_url`, `api_key`, `key_env`, `api_key_env`, `api_mode`,
`token` e `request_overrides` são rejeitados. Strings vazias ou com espaços nas
bordas não são reparadas. Repetições de pares e retorno ao primário na cadeia
são rejeitados. A ordem das alternativas é preservada; não há descoberta de
provedores, scheduler novo ou fallback implícito.

Exemplo de **sub-row**, não configuração ativada (substituir
`MODELO_CODEX_DO_OWNER` pelo identificador exato escolhido pelo proprietário):

```yaml
# Dentro de delegation.categories.simples; manter os campos general existentes.
development:
  provider: custom:kimi-api
  model: k3-256k
  reasoning_effort: low
  max_iterations: 40
  allowed_models: [k3-256k]
  allowed_routes:
    - {provider: custom:kimi-api, model: k3-256k}
    - {provider: openai-codex, model: MODELO_CODEX_DO_OWNER}
  fallback_providers:
    - {provider: openai-codex, model: MODELO_CODEX_DO_OWNER}
```

Para `analitica` e `complexa`, a proposta é inverter primário/alternativa para
Codex → Kimi, com effort `medium` / `high` e tetos 80 / 120. As rotas general
ficam intactas. O provedor nomeado `custom:kimi-api` deve já resolver a assinatura
Kimi Code, modelo `k3-256k`, endpoint literal **`https://api.kimi.ai/coding/v1`**.
Endpoint e credencial pertencem à configuração do provedor nomeado, nunca ao
pedido de tarefa ou ao par de fallback. Este patch não cria esse provedor nem
altera o endpoint para outro domínio. Extra Usage deve permanecer OFF nos dois
serviços; a implementação não muda essa decisão nem verifica seu estado remoto.

Tarefas development não aceitam imagens. A rejeição inclui imagens herdadas do
argumento interno/top-level normalizado; `images: []` não acrescenta imagens.
Tarefas general e o caminho de visão anterior continuam inalterados.

## Execução e recibos

A cadeia validada entra em `routing_cfg` do filho e percorre a resolução nativa
`_resolve_child_fallback_chain` → `fallback_model` → `_try_activate_fallback`.
O motor nativo continua responsável por elegibilidade, credenciais, classificação,
cooldown, transporte e preservação da conversa. Não se promete troca só em cota:
a política nativa pode utilizar uma alternativa por outras falhas classificadas.

A observação local envolve somente o método bound do filho, cuja assinatura
encaminha `reason=None, reset_at=None` ao motor. Não altera eligibility, ordem,
retorno ou orçamento. Somente ativações bem-sucedidas geram transições.

Resultados de categoria incluem:

- `purpose`, `category`, primário configurado, effort e limite configurado;
- `configured_chain`: primário seguido das alternativas explícitas;
- `effective_provider`, `effective_model`: rota observada no momento do recibo,
  preservando `requested_provider` para distinguir provedores custom nomeados;
- `fallback_transitions`: origem, destino e `reason` de `FailoverReason.value`;
  motivo ausente/não tipado vira `unknown`, nunca diagnóstico/log bruto;
- `api_calls` agregado e o indicador de contabilização incerta já existente.

Falhas e timeouts também recebem os identificadores de rota. Um timeout não
substitui o contador real de atividade por zero nem prova término do worker.
Transições esgotadas não viram troca fictícia. Recibos são snapshots independentes.
Chamadas legadas/auxiliares sem tabela de categorias mantêm sua forma anterior.

## Verificação offline

As suítes `tests/tools/test_delegation_categories.py` e
`tests/tools/test_delegation_category_fallback.py` cobrem defaults, separação de
propósitos, allowlists exatas, cadeia de múltiplos pares, campos proibidos,
rejeição antes de resolver credenciais, imagens normalizadas, registry → loader
real → construção de runtime por tarefa, motor nativo nos dois sentidos,
esgotamento, enum dos recibos, falhas/timeouts e orçamento compartilhado com
retry de output_schema. A suíte nova nega conexões de socket; apenas as fronteiras
de provedor/cliente são simuladas. Não há chamada de completion real.

Usar `scripts/run_tests.sh`, com `/opt/hermes/.venv/bin/python`, HOME temporário,
ambiente inicial vazio, cache pytest desabilitado e temporários/bytecode fora do
candidato. Os recibos de execução registram comando, resultados e hashes. Testes
source não constituem revisão independente, promoção, disponibilidade de assinatura
ou prova de funcionamento live; esses gates permanecem com o coordenador.
