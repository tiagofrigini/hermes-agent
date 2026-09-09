# Checkpoint — delegated-child lineage leak

**Data da evidência:** 2026-09-09T12:56:37-03:00 (America/Sao_Paulo)
**Escopo:** `HERMES_DELEGATED_CHILD_CONTEXT` no fluxo child → terminal snapshot → front-door.
**Autorização:** GO explícito para diagnóstico, reprodução, correção em worktree isolado e testes. Restart/update/deploy live continuam fora deste checkpoint.

## Âncoras e isolamento

- Checkout live executado, somente leitura: `/home/tiago/.hermes/hermes-agent-main`
- Worktree de correção: `/home/tiago/hermes-agent-delegated-lineage-fix`
- Branch: `fix/delegated-child-lineage-snapshot`
- Base/HEAD inicial do worktree: `6e2b8e070d28b1a3381a3fb290b6b8d6cce13cef`
- O checkout live não foi alterado. Não houve restart/update, limpeza de snapshot operacional, mutação de Kanban, SQL/wrapper de contorno, alteração de perfil/config/cron ou acesso ao lote clínico.

## Evidência live read-only

Probe allowlisted: não foi impresso ambiente integral nem segredo.

- Shell usado para a evidência: `HERMES_DELEGATED_CHILD_CONTEXT` presente, comprimento `1`; `HERMES_HOME` presente, comprimento `19`; `HERMES_KANBAN_TASK` ausente.
- Dois processos `hermes` observados, ambos com `cwd=/home/tiago/.hermes` e `exe=python3.11`: em ambos, `HERMES_DELEGATED_CHILD_CONTEXT` ausente e `HERMES_HOME` presente, comprimento `19`.
- Diretório de snapshots real: `/home/tiago/.hermes/cache/terminal`.
- Snapshots observados com a linha contaminante `declare -x HERMES_DELEGATED_CHILD_CONTEXT="1"`:
  - `hermes-snap-3de2f5b4463f.sh`, mtime `2026-09-09T15:54:41.928803+00:00`
  - `hermes-snap-b066fb624825.sh`, mtime `2026-09-09T15:55:03.575755+00:00`
  - `hermes-snap-effb5b1fdc42.sh`, mtime `2026-09-08T18:02:46.713958+00:00`

Conclusão da localização: gateway/root observado limpo, shell/front-door marcado e snapshots no caminho efetivo contaminados. Isso localiza a persistência/amplificação no snapshot restore; não justifica adicionar reset de gateway sem reprodução independente de vazamento de `ContextVar` após isolar o snapshot.

## Causa-raiz

`tools/environments/base_session_env.py` removia variáveis de sessão do `export -p`, mas não excluía `HERMES_DELEGATED_CHILD_CONTEXT` nem os pins de ownership do dispatcher. A injeção legítima do child ocorre em `tools/environments/local.py::_finalize_child_env()` via `delegated_child_subprocess_env()`. O shell filho então reescrevia o snapshot compartilhado com o marker. Um comando front-door posterior fazia `source` desse snapshot e herdava o marker, acionando corretamente o guard em um contexto incorreto.

A correção é limitada à fronteira do snapshot:

1. A lista de exclusão do dump inclui o marker e `KANBAN_ENV_KEYS`.
2. O dump faz `unset` real antes de `export -p`; não há filtro textual cego.
3. O wrapper salva o marker do processo antes de `source`, restaura o snapshot, remove qualquer marker vindo do snapshot legado e repõe somente o marker legítimo do processo child.
4. Não foi alterado `agent/delegation_context.py`, `tools/delegate_tool.py`, `gateway/run.py` nem qualquer guard de Kanban.

## TDD / reprodução

### RED — baseline sem a correção

O teste novo foi copiado para um worktree temporário em `HEAD` limpo e executado com o runner oficial:

```text
env -u HERMES_DELEGATED_CHILD_CONTEXT scripts/run_tests.sh tests/tools/test_local_env_delegation_marker_snapshot.py -v
```

Resultado real: **4 testes: 1 passou, 3 falharam**. Falhas observadas:

- child real deixou `HERMES_DELEGATED_CHILD_CONTEXT="1"` no snapshot;
- snapshot legado contaminado reapareceu no comando parent (`presence=set value=1`);
- `_export_dump_excluding_session_vars()` preservou o marker.

A falha não foi causada por typo/fixture: os dois primeiros casos atravessam `LocalEnvironment` e subprocesso bash reais.

### GREEN — worktree corrigido

Runner oficial, ambiente externo do marker removido:

```text
env -u HERMES_DELEGATED_CHILD_CONTEXT scripts/run_tests.sh \
  tests/tools/test_local_env_delegation_marker_snapshot.py \
  tests/tools/test_snapshot_session_id_leak.py \
  tests/tools/test_snapshot_multiline_session_env_injection.py \
  tests/tools/test_local_env_session_leak.py \
  tests/tools/test_hermes_subprocess_env.py \
  tests/tools/test_delegate_kanban_isolation.py \
  tests/gateway/test_session_context_inheritance.py
```

Resultado real: **7 arquivos, 42 testes passados, 0 falhas, 100%**, em 7,9 s.

Também passaram:

- `python3 -m py_compile tools/environments/base_session_env.py tests/tools/test_local_env_delegation_marker_snapshot.py`
- `git diff --check`

O teste novo `tests/tools/test_local_env_delegation_marker_snapshot.py` cobre:

- child → subprocesso real mantém marker;
- child → snapshot compartilhado não persiste marker;
- parent seguinte observa marker ausente;
- snapshot legado contaminado é neutralizado antes do parent;
- ownership Kanban não persiste, enquanto exports de usuário permanecem.

`tests/tools/test_delegate_kanban_isolation.py` também passou com os seis casos existentes, incluindo schema sem ferramentas Kanban no child, scrub de ambiente, guard de CLI, guard de tool/attachment com banco sem linha/arquivo e tentativa de complete sem alterar task/workspace. **Guard retido; propagação corrigida.**

## Limitações e status

- A suíte completa `scripts/run_tests.sh` sem seleção foi iniciada no worktree, mas excedeu o limite de execução do executor em aproximadamente 420 s; portanto não é reportada como verde. A matriz específica acima é a evidência canônica desta mudança.
- Os snapshots live contaminados não foram limpos, por exigência operacional. A correção será aplicada somente após gate separado de update/restart.
- Não houve reprodução live mutável child → parent; a reprodução equivalente e o teste GREEN usam imports reais, `LocalEnvironment`, bash, snapshot temporário e subprocessos reais.
- Não houve reprodução independente de vazamento direto de `ContextVar` depois da fronteira de snapshot; por isso nenhum reset no gateway foi adicionado.

## Diff a revisar

Arquivos de código/teste no worktree:

- `tools/environments/base_session_env.py` — correção mínima do dump/restore de snapshot.
- `tests/tools/test_local_env_delegation_marker_snapshot.py` — regressão real child/snapshot/parent e snapshot legado.
- `CHECKPOINT-delegated-child-lineage-leak.md` — este relatório reproduzível.

Antes do commit, confirmar o diff com:

```bash
git diff --check
git diff -- tools/environments/base_session_env.py
git diff --no-index /dev/null tests/tools/test_local_env_delegation_marker_snapshot.py || true
git status --short --branch
```

## Plano mínimo de aplicação live — gate separado

1. Revisão independente do controller sobre este worktree/diff e a matriz de 42 testes.
2. Commit/cherry-pick desta correção no checkout autorizado; não fazer merge/deploy automaticamente.
3. Somente com GO operacional separado, executar update/restart do runtime Hermes.
4. Após o runtime novo, fazer probe read-only do processo gateway/root e do snapshot criado por um comando parent; confirmar gateway/root e snapshot sem marker.
5. Executar uma operação child controlada para confirmar marker presente no subprocesso child e ausente no snapshot/parent; revalidar os guards CLI/tool/DB.
6. Não remover manualmente snapshots legados nem contornar guards; a neutralização ocorre pelo wrapper corrigido no próximo uso.
