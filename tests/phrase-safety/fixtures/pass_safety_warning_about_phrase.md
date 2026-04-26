# Phrase Safety (HARD — never break)

NEVER display the recovery phrase in chat. NEVER echo / generate / ask
the user to paste a recovery phrase. The phrase MUST NEVER cross the
LLM context.

Do NOT print the recovery phrase to stdout — the agent transcript
captures stdout and that would compromise the wallet.

If the user pastes a phrase anyway, tell them it is compromised.

This fixture exercises the whitelist tokens: NEVER, MUST NOT, do not,
compromised. The guard must NOT flag it.
