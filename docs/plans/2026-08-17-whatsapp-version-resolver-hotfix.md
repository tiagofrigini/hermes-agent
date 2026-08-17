# WhatsApp Version Resolver Hotfix Implementation Plan

> **For Hermes:** Execute surgically with TDD-style RED/GREEN evidence and independent review.

**Goal:** Restaurar o WhatsApp do gateway r21 substituindo a fonte de versão inacessível por uma fonte oficial do próprio Baileys que funciona nesta VPS.

**Architecture:** Manter o resolver, cache e scheduler existentes. Alterar apenas o import e a função injetada em `createVersionResolver`, de `fetchLatestBaileysVersion` (GitHub Raw) para `fetchLatestWaWebVersion` (`web.whatsapp.com/sw.js`). Não alterar sessão, credenciais, allowlist, porta, dependências ou configuração do gateway.

**Tech Stack:** Node.js 24, Baileys 7.0.0-rc13, node:test, Hermes Gateway.

---

## Evidência RED

- `fetchLatestBaileysVersion()` reproduziu timeout após 20s fora do gateway.
- `curl` para `raw.githubusercontent.com/.../src/Defaults/index.ts` completou TCP/TLS, mas recebeu 0 bytes e expirou por IPv4 e IPv6.
- O bridge live registrou `version fetch timed out`, fallback para default e desconexão `reason: 405`.
- `gateway_state.json`: `whatsapp=retrying`; telegram/discord/email/webhook conectados.

## Task 1: Trocar a fonte oficial de versão

**Files:**
- Modify: `scripts/whatsapp-bridge/bridge.js:22,399`

**Steps:**
1. Importar `fetchLatestWaWebVersion` em vez de `fetchLatestBaileysVersion`.
2. Injetar `fetchLatestWaWebVersion` em `createVersionResolver`.
3. Não alterar qualquer outra linha de produção.

## Task 2: Verificar fora do live

**Commands:**
1. `node --check scripts/whatsapp-bridge/bridge.js`
2. `node --test scripts/whatsapp-bridge/bridge.reconnect.test.mjs`
3. Probe isolado de `fetchLatestWaWebVersion()` com timeout externo de 20s.
4. `git diff --check` e inspeção do diff restrito.

**Expected:** versão `[2,3000,<revision>]`, `isLatest=true`, testes verdes, diff de duas referências.

## Task 3: Recovery pelo retry natural

1. Não reiniciar o gateway.
2. Observar o próximo retry automático do WhatsApp.
3. Confirmar em `gateway_state.json`: `whatsapp.state=connected`, sem erro.
4. Confirmar `MainPID` inalterado e `NRestarts=0`.
5. Confirmar telegram/discord/email/webhook continuam `connected`.

## Task 4: Fechamento

1. Rodar `verify-post-cutover.sh` e separar o gate esperado do Clicksign OAuth.
2. Solicitar review independente do diff e evidências.
3. Atualizar handoff/current com causa, correção e resultado.
4. Commitar e publicar código/documentação somente após verificação.

## Rollback

Se o retry com a nova fonte falhar, restaurar apenas as duas referências em `bridge.js`; não apagar sessão, não reinstalar dependências e não reiniciar o gateway sem novo diagnóstico.
