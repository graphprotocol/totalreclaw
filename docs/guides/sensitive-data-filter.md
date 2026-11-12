# Sensitive-data filter

> Draft copy, pending approval (internal #549). The block message below is the same one shown to users.

TotalReclaw checks every memory before it is stored, and keeps secrets and sensitive identifiers out of your vault unless you say otherwise. The check runs on your own machine, inside the TotalReclaw client, before anything is encrypted or written on-chain.

## What it catches

| Category | Examples | How it is recognised |
|---|---|---|
| API keys | OpenAI, Anthropic, GitHub, GitLab, Google, AWS, Stripe, Slack, Hugging Face, npm, PyPI, Groq, xAI, OpenRouter, Telegram bot tokens; `api_key = …`, `client_secret: …`, `GITHUB_TOKEN=…` | Provider prefixes, or a key name followed by a long value |
| Access tokens | JWTs, `Bearer …`, `Authorization: Basic …` / `token …`, `access_token=…` | Token shape or header syntax |
| Passwords and PINs | "my wifi password is …", "PIN code: …", `DB_PASSWORD=…`, `postgres://user:pass@host` | A password/PIN keyword followed by a secret-looking value, or credentials inside a URL |
| Private keys | 64/128-character hex keys, PEM `PRIVATE KEY` blocks, `xprv…` extended keys, base58/WIF keys | Shape. Hex and base58 strings count as a key when a word like "private key" or "seed" comes before them |
| Wallet recovery phrases | 12–24 words from the BIP-39 word list | Word list plus the BIP-39 checksum |
| Payment cards | Visa, Mastercard, Amex, Discover, JCB, Diners, UnionPay, Maestro | Issuer prefix, length and the Luhn check digit |
| Bank accounts | IBANs | Country length and the IBAN mod-97 check |
| Government IDs | Portuguese NIF and Cartão de Cidadão, Spanish DNI/NIE, Italian codice fiscale, French social security number, US SSN, Brazilian CPF; passport, ID card, driving licence, National Insurance, NHS, Aadhaar, BSN, Steuer-ID and PESEL numbers named as such | Official check digits, or the ID's name followed by the number |

Not filtered: names, e-mail addresses, phone numbers, street addresses, dates, and facts *about* a sensitive item that leave out the value ("I use a hardware wallet", "my doctor is Dr. Sousa"). The extraction prompt also asks the model not to extract secrets, so most never reach the filter.

The filter works on patterns. It can miss a secret written in an unusual way, so don't rely on it alone.

## What happens

- **Automatic memory** (Hermes extraction every few turns, the end-of-session summary, mirrors of Hermes' built-in memory tool): anything the filter catches is dropped. Only the category is logged, never the text.
- **When you ask the agent to remember something:**
  - a clear match (a valid card number, a checksum-valid recovery phrase, a named password…) is **not stored**. The agent tells you, in the words shown below, and stores it only if you confirm;
  - a weaker match (a hex string with no "private key" label, an SSN-shaped number with no "SSN" label, a failed check digit) is stored, because you asked.

The messages are the same on every client:

> Not saved: this looks like it contains a payment card number. TotalReclaw doesn't store financial identifiers unless you confirm you want it saved.

To store it anyway, tell the agent you really want it saved. The agent then repeats the call with `allow_sensitive: true`. Your vault is end-to-end encrypted, so this is your call to make. The filter exists so nothing lands there by accident.

## For integrators

- Core function: `pii_filter(text)` in `totalreclaw-core`. WASM `piiFilter`, PyO3 `pii_filter`. Both return `{"decision":"allow"}` or `{"decision":"block"|"flag","category":"…","message":"…"}`.
- Blocked explicit remember result (Hermes): `{"stored": false, "blocked": "sensitive", "category": "…", "message": "…", "override": "allow_sensitive"}`.
- Contract: [client-consistency spec](../specs/totalreclaw/client-consistency.md#sensitive-data-pii--credential-filter). Test corpus: `tests/parity/pii/corpus.json`.
- Wired into Hermes only so far. The MCP server, the OpenClaw plugin, NanoClaw's automatic hooks and ZeroClaw do not run the filter yet; see Known Gaps in the repository `CLAUDE.md`.
