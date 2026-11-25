# Roteiro da apresentação (5 min)

## Antes de começar (checklist)
- [ ] `.env` com o webhook real; Discord aberto no celular e/ou no PC
- [ ] Terminal 1 na pasta do projeto com o venv ativo
- [ ] Terminal 2 com `python -m http.server 8000` já rodando
- [ ] Repositório do GitHub aberto no navegador, no README
- [ ] Rodar `python main.py --once` uma vez antes, para o banco já ter o estado inicial

## 1. O problema (1 min)
"Eu só descobria que um serviço estava fora do ar quando precisava dele. Conferir site por site à mão é repetitivo, e certificado SSL vencendo passa despercebido. Automatizei isso: o monitor verifica sozinho e me avisa no Discord só quando algo muda."

## 2. A arquitetura (1 min), mostrando o diagrama do README
"O gatilho é um timer. A cada ciclo o programa lê o targets.yaml e verifica todos os alvos em paralelo: HTTP com requests, certificado com ssl e porta com socket. Grava no SQLite e compara com o último estado. Se mudou, monta um JSON de embed e faz POST no webhook do Discord. Segredos ficam no .env; o YAML só tem o nome da variável."

## 3. Live demo (2 min)
1. Terminal 1: `python main.py` (mostrar o log com UP e a latência de cada alvo)
2. Terminal 2: `Ctrl+C` no http.server → no próximo ciclo chega o alerta 🔴 no Discord
3. Terminal 2: `python -m http.server 8000` de novo → chega o 🟢 "voltou ao normal" com a duração
4. `python main.py --report` → relatório de uptime

Dica: para a demo andar rápido, coloque `CHECK_INTERVAL_SECONDS=10` no `.env`.

## 4. Perguntas prováveis e respostas

**Por que não alerta a cada ciclo?** O `alerts.evaluate` compara com o estado salvo na tabela `state` e só gera evento quando o status efetivo muda. Isso evita spam.

**O que é `failures_before_alert`?** O número de falhas seguidas antes de declarar DOWN. Com valor 2, uma oscilação de um ciclo não gera alarme falso.

**E se o Discord estiver fora?** `Notifier.send` tenta 3 vezes com espera exponencial (2s, 4s) em erro de rede e 5xx. No 429 espera o `retry_after` que o Discord informa. No 4xx (webhook inválido) não repete, porque repetir não resolve. Se não entregou, o `run_cycle` não salva o novo estado, então o alerta é reenviado no próximo ciclo.

**E se um site travar?** Todo request tem `timeout`. As verificações rodam em paralelo (ThreadPoolExecutor), então um site lento não atrasa os outros. `run_check` captura qualquer exceção e transforma em DOWN, e o loop do `main` tem um `try/except` geral: o monitor nunca morre silenciosamente.

**Onde estão as credenciais?** No `.env`, que está no `.gitignore`. O `.env.example` só tem valores de exemplo. O YAML referencia o nome da variável (`token_env`). A URL do webhook nunca aparece no log, porque contém o token.

**Que autenticação é usada?** O webhook do Discord leva o token na própria URL. A API do GitHub usa `Authorization: Bearer <token>`. Também há suporte a Basic Auth (`requests` gera o cabeçalho Base64).

**Onde está o processamento de dados estruturados?** Entrada em YAML validada em `config.py`. Saída em JSON em `notifier.py`, com cor e título conforme o status, agrupamento em lotes de 10 embeds (limite do Discord) e a regra de recuperação. Também há agregação SQL (`uptime_summary`: COUNT, SUM e AVG).

**Por que SQLite?** Não precisa de servidor, é só um arquivo, e guarda o estado entre execuções. Isso permite rodar com `--once` pelo cron sem perder a memória do último status.

**Como o SSL é verificado?** `get_ssl_days_left` abre um handshake TLS com `ssl.create_default_context()` (que valida a cadeia e o hostname), lê o `notAfter` do certificado e calcula os dias restantes. Certificado inválido ou vencido faz o próprio `requests` falhar com SSLError → DOWN.

## Ajustes que o professor pode pedir ao vivo
- **Adicionar um site:** novo item em `targets.yaml` (name + url)
- **Mudar o limite de lentidão:** `latency_warn_ms` em `defaults`
- **Mudar a cor do alerta:** `COLORS` em `monitor/notifier.py`
- **Mudar o texto do título:** `_event_embed` em `monitor/notifier.py`
- **Mudar o intervalo:** `CHECK_INTERVAL_SECONDS` no `.env`
- **Exigir um texto na página:** `keyword:` no alvo
