#!/usr/bin/env python3
"""Generate ``corpus.json`` — the DEP-4 PII / credential filter corpus.

PRD-04 G4 acceptance: 0 leaks on the negative set (no negative item may be
``allow``), <= 2 % false blocks on the positive set (positive items must be
``allow``). Every item carries its exact expected decision so the three
runners assert identical behaviour item-for-item:

* Rust:   ``rust/totalreclaw-core/tests/pii_corpus.rs``      (``cargo test``)
* WASM:   ``tests/parity/pii/pii-parity.test.ts``            (``npx tsx``)
* PyO3:   ``python/tests/test_pii_parity.py``                (``pytest``)

SYNTHETIC DATA ONLY. Every sensitive-looking value below is fake-shaped
(``FAKE`` bodies, repeated hex) or a PUBLIC test vector: PSP test card
numbers, ISO 13616 registry example IBANs, BIP-39 test vectors already used
by this repo's fixtures, the jwt.io example token, the AWS documentation
example key, the Anvil/Hardhat default account #0 key (already in
``tests/parity/fixtures``), SSA-voided SSNs, and check-digit-valid numbers
built from sequential digits. None belongs to a real person or account.

Push-protection hygiene: provider formats that GitHub secret scanning blocks
on push (SendGrid ``SG.<22>.<43>``, Twilio ``SK<32 hex>``, Stripe live keys of
24+ chars, Hugging Face ``hf_…``, Slack ``xox[baprs]-…``, complete PEM
BEGIN/END blocks) are deliberately NOT in this file — the ``src/secrets.rs``
and ``src/pii.rs`` unit tests build those shapes at runtime instead.

Run from the repo root:  python3 tests/parity/pii/generate_corpus.py
Output is deterministic (insertion order, UTF-8, 2-space indent, trailing newline).
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "corpus.json"

items: list[dict] = []


def neg(category: str, expect: str, text: str, note: str | None = None) -> None:
    n = sum(1 for i in items if i["set"] == "negative" and i["category"] == category) + 1
    item = {
        "id": f"neg-{category.replace('_', '-')}-{n:02d}",
        "set": "negative",
        "expect": expect,
        "category": category,
        "text": text,
    }
    if note:
        item["note"] = note
    items.append(item)


def pos(text: str) -> None:
    n = sum(1 for i in items if i["set"] == "positive") + 1
    items.append({"id": f"pos-{n:03d}", "set": "positive", "expect": "allow", "text": text})


# ---- api_key (block) --------------------------------------------------------
neg("api_key", "block", "My OpenAI key is sk-proj-FAKEexampleKEY0123456789abcdefFAKEexampleKEY01")
neg("api_key", "block", "ANTHROPIC_API_KEY=sk-ant-api03-" + "FAKE0000" * 11)
neg("api_key", "block", "old key: sk-FAKEfake1234567890FAKEfake1234567890FAKEfake12")
neg("api_key", "block", "my GitHub token ghp_FAKE1234567890abcdefFAKE1234567890ab")
neg("api_key", "block", "ghu_FAKE1234567890abcdefFAKE1234567890ab is the user token")
neg("api_key", "block", "server token ghs_FAKE1234567890abcdefFAKE1234567890ab")
neg("api_key", "block", "AWS access key AKIAIOSFODNN7EXAMPLE", "AWS documentation example key")
neg("api_key", "block", "stripe live key sk_live_FAKE0000FAKE0000FAKE00")
neg("api_key", "block", "test mode uses sk_test_FAKE0000FAKE0000FAKE00")
neg("api_key", "block", "The Mapbox api key is pk9FAKEa1b2c3d4e5f6g7h8i9")
neg("api_key", "block", "client_secret = 'FAKE9f8e7d6c5b4a3210zyxw'")
neg("api_key", "block", "STRIPE_SECRET_KEY=rk_FAKE_51Habcdefghijk0123")
neg("api_key", "block", "Save this: GITHUB_API_KEY: FAKE0123456789abcdefFAKE")

# ---- auth_token (block) -----------------------------------------------------
neg("auth_token", "block",
    "session JWT eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", "jwt.io example token")
neg("auth_token", "block", "Authorization: Bearer FAKEtoken0123456789abcdefXYZ")
neg("auth_token", "block", "use bearer FAKE.abc123.def456.ghi789jkl for the staging API")
neg("auth_token", "block", "Authorization: Basic dXNlcjpGQUtFcGFzc3dvcmQ=")
neg("auth_token", "block", "access_token=FAKEya29a0ARrdaM0123456789")
neg("auth_token", "block", "my refresh token is FAKE1//0gAbCdEf0123456789")

# ---- password (block) -------------------------------------------------------
neg("password", "block", "My wifi password is Blue-Fox-72")
neg("password", "block", "password: P@ssw0rd!2024")
neg("password", "block", "The password for my gmail account is Tr0ub4dor&3")
neg("password", "block", "A senha do banco é Lisboa#2024")
neg("password", "block", "mi contraseña es Madrid2024!")
neg("password", "block", "Mein Passwort ist Sommer2024")
neg("password", "block", "mon mot de passe est Soleil2024")
neg("password", "block", "Server login: postgres://admin:FakePass123@db.example.com:5432/app")
neg("password", "block", "clone with https://alice:ghFAKE123@git.example.com/repo.git")
neg("password", "block", "My card PIN is 4821")
neg("password", "block", "PIN code: 739104")

# ---- private_key ------------------------------------------------------------
ANVIL_KEY_0 = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
neg("private_key", "block", "private key 0x" + ANVIL_KEY_0,
    "Anvil/Hardhat default account #0 (public test key already in repo fixtures)")
neg("private_key", "block", "PRIVATE_KEY=" + ANVIL_KEY_0, "Anvil/Hardhat default account #0")
neg("private_key", "block", "wallet signing key: " + "1234567890abcdef" * 4)
neg("private_key", "block", "my seed in hex is " + "00112233445566778899aabbccddeeff" * 4)
neg("private_key", "block", "my server key file starts with -----BEGIN RSA PRIVATE KEY-----")
neg("private_key", "block", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAFAKE")
neg("private_key", "block", "backup xprv9s21ZrQH143K" + "FAKExprvFAKExprv" * 6 + "abc",
    "fake-shaped extended private key")
neg("private_key", "flag", "deploy tx 0x" + "abcdef0123456789" * 4,
    "hash-shaped hex is flagged: indistinguishable from a raw key")
neg("private_key", "flag",
    "sha256 of the backup: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "SHA-256 of the empty string")
neg("private_key", "flag", "fedcba9876543210" * 4)

# ---- seed_phrase ------------------------------------------------------------
ABANDON = "abandon " * 11 + "about"
ZOO = "zoo " * 11 + "wrong"
LEGAL12 = "legal winner thank year wave sausage worth useful legal winner thank yellow"
LEGAL24 = ("legal winner thank year wave sausage worth useful legal winner thank year "
           "wave sausage worth useful legal winner thank year wave sausage worth title")
neg("seed_phrase", "block", ABANDON, "BIP-39 public test vector")
neg("seed_phrase", "block", "my recovery words: " + ZOO, "BIP-39 public test vector")
neg("seed_phrase", "block", " ".join(f"{i + 1}. {w}" for i, w in enumerate(LEGAL12.split())),
    "BIP-39 public test vector, numbered")
neg("seed_phrase", "block", LEGAL12.upper(), "BIP-39 public test vector, upper-case")
neg("seed_phrase", "block", ", ".join(LEGAL12.split()), "BIP-39 public test vector, comma-separated")
neg("seed_phrase", "block", LEGAL24, "BIP-39 public test vector, 24 words")
neg("seed_phrase", "block", "seed phrase:\n" + "\n".join(ABANDON.split()),
    "BIP-39 public test vector, one word per line")
neg("seed_phrase", "flag", ("abandon " * 12).strip(), "12 wordlist words, checksum invalid")
neg("seed_phrase", "flag", ("zoo " * 12).strip(), "12 wordlist words, checksum invalid")
neg("seed_phrase", "flag",
    "abandon ability able about above absent absorb abstract absurd abuse access accident",
    "12 wordlist words, checksum invalid")

# ---- payment_card (block) — public PSP test card numbers ---------------------
for text in [
    "Visa 4111 1111 1111 1111 exp 12/29",
    "card number 4242-4242-4242-4242",
    "Mastercard 5555555555554444",
    "My MC is 2223 0031 2200 3222",
    "Amex 3782 822463 10005",
    "Discover card 6011111111111117",
    "JCB 3530 1113 3330 0000",
    "Diners 3056 930902 5904",
    "UnionPay 6200 0000 0000 0005",
    "O meu cartão é 5105 1051 0510 5100",
    "Maestro 6759 6498 2643 8453",
    "Pay with 4000056655665556 please",
]:
    neg("payment_card", "block", text, "public PSP test card number")

# ---- bank_account (block) — ISO 13616 registry example IBANs ------------------
for text in [
    "IBAN: PT50 0002 0123 1234 5678 9015 4",
    "DE89 3704 0044 0532 0130 00",
    "GB82 WEST 1234 5698 7654 32",
    "transfer to FR14 2004 1010 0505 0001 3M02 606",
    "NL91ABNA0417164300",
    "Belgian account BE68 5390 0754 7034 is mine",
    "es91 2100 0418 4502 0005 1332",
    "IT60 X054 2811 1010 0000 0123 456",
    "NO93 8601 1117 947",
    "CH93 0076 2011 6238 5295 7",
]:
    neg("bank_account", "block", text, "ISO 13616 registry example IBAN")

# ---- government_id ----------------------------------------------------------
neg("government_id", "block", "NIF 123456789", "synthetic PT NIF (check digit valid)")
neg("government_id", "block", "O meu número de contribuinte é 234 567 899", "synthetic PT NIF")
neg("government_id", "block", "tax ID: 500000000", "synthetic PT NIPC")
neg("government_id", "flag", "NIF: 123456780", "PT NIF keyword, check digit invalid")
neg("government_id", "block", "Cartão de Cidadão 12345678 9 ZZ1", "synthetic PT CC number (check valid)")
neg("government_id", "block", "doc 87654321 0 ZY8", "synthetic PT CC number, no keyword")
neg("government_id", "flag", "doc 11223344 5 ZX8", "PT CC shape, check digit invalid")
neg("government_id", "block", "DNI 12345678Z", "canonical ES DNI example")
neg("government_id", "block", "Mi DNI es 87654321-X", "synthetic ES DNI")
neg("government_id", "block", "NIE X1234567L", "synthetic ES NIE")
neg("government_id", "block", "codice fiscale RSSMRA85T10A562S", "canonical IT codice fiscale example")
neg("government_id", "block", "BNCLRA90A41F205I", "synthetic IT codice fiscale")
neg("government_id", "flag", "RSSMRA85T10A562X", "IT codice fiscale shape, check letter invalid")
neg("government_id", "block", "numéro de sécurité sociale 1 85 05 78 006 084 91", "synthetic FR NIR (key valid)")
neg("government_id", "block", "295067512345634", "synthetic FR NIR, contiguous")
neg("government_id", "block", "My SSN is 078-05-1120", "SSA-voided 'Woolworth' example number")
neg("government_id", "block", "social security number 219099999", "SSA-voided advertising example number")
neg("government_id", "flag", "number 123-45-6789 on the form", "SSN shape without keyword")
neg("government_id", "flag", "SSN 666-12-3456", "SSN keyword, invalid area 666")
neg("government_id", "block", "passport number is X1234567", "synthetic passport number")
neg("government_id", "block", "Número de passaporte: CB123456", "synthetic PT passport number")
neg("government_id", "block", "driver's license number 123456789", "synthetic licence number")
neg("government_id", "block", "BSN: 123456782", "synthetic NL BSN")
neg("government_id", "block", "Steuer-ID 12345678901", "synthetic DE tax id")
neg("government_id", "block", "PESEL 44051401359", "synthetic PL PESEL")

# ---- positive set (must be allowed) -----------------------------------------
for text in [
    "My doctor is Dr. Ana Sousa at Hospital da Luz.",
    "My doctor is Dr. X",
    "The user's email is ana.sousa@example.com",
    "Call me on +351 912 345 678",
    "My phone number is 912 345 678",
    "Office line: (415) 555-0134",
    "+1 415 555 0134",
    "UK office +44 20 7946 0958",
    "Brazil contact +55 11 91234-5678",
    "Brazil mobile +5511912345678",
    "My wife's name is Maria and my son is João.",
    "I live at Rua Augusta 123, 1100-048 Lisboa",
    "My daughter's birthday is 2014-05-17",
    "Meeting at 14:30 on 2026-10-03",
    "My salary is 45,000 EUR per year",
    "Flight TP1234 departs at 09:15",
    "Order #123456789 shipped yesterday",
    "Tracking number 1Z999AA10123456784",
    "ISBN 978-0-306-40615-7",
    "ISBN 9780306406157",
    "The build timestamp was 1727400000000",
    "My Ethereum address is 0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
    "Commit a1b2c3d4e5f60718293a4b5c6d7e8f9012345678 fixed the bug",
    "Request id 550e8400-e29b-41d4-a716-446655440000",
    "I use 1Password as my password manager",
    "I changed my password yesterday",
    "The password policy requires 12 characters",
    "Remember that my password hint is my first pet's name",
    "My PIN is something only I know",
    "Pin this: I prefer dark mode",
    "I pinned the message about the offsite",
    "API keys should be rotated every 90 days",
    "We store the API key in AWS Secrets Manager",
    "const apiKey = process.env.OPENAI_API_KEY",
    "I use scikit-learn (sk-learn) for ML",
    "GitHub tokens start with ghp_",
    "Our API uses bearer tokens that expire after one hour",
    "Use the Authorization header with a Bearer token",
    "JWTs have three base64url parts separated by dots",
    "I use a Visa card for travel",
    "My card ends in 4242",
    "Portuguese IBANs start with PT50",
    "Send the invoice to accounts@example.pt with the NIF on it",
    "The tax number field is mandatory on invoices",
    "My passport expires in 2027",
    "I need a passport photo for the visa",
    "I renewed my citizen card last month",
    "The SSN format is XXX-XX-XXXX",
    "I have 12 cats and 3 dogs",
    "I ran 21 km this morning",
    "Room 1234 at the Hilton",
    "My employee id is 4471",
    "License plate 12-AB-34",
    "The router admin page is 192.168.1.1",
    "The dev server runs on port 8080",
    "Repo: https://github.com/p-diogo/totalreclaw",
    "Connect to postgres://localhost:5432/mydb",
    "My blood type is O+",
    "I am allergic to penicillin",
    "Prefers PostgreSQL over MySQL for analytics",
    "Always use explicit type hints in Python",
    "Chose Rust over Go because of memory safety",
    "Works at Acme Corp as a staff engineer",
    "Moved from Porto to Lisbon in 2023",
    "Wants to run a marathon in under 4 hours",
    "Uses Signal because it's end-to-end encrypted",
    "I love the ocean, the sunset and the music at the beach",
    "happy family travel summer beach ocean sunset dinner music dance party",
    "My recovery plan after surgery is six weeks of rest",
    "The seed round closed at 2 million euros",
    "My private key storage is a Ledger hardware wallet",
    "Chave pública: 0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
    "Ana's number is 213 456 789",
    "Postal code 1000-001",
    "My daughter was born on 17/05/2014",
    "The meeting code is 123 456 789",
    "Invoice 2026-000123 paid",
    "My Portuguese NIF is required for the gym contract",
    "I keep my IBAN in the banking app, not here",
    "Social security appointment on Monday",
    "The token budget for the model is 200000 tokens",
    "The secret key to good bread is patience",
    "My password is long and random",
    "The Wi-Fi network is called HomeNet5G",
    "Hermes agent name is John",
    "My GitHub username is p-diogo",
    "Uses the Anvil default account for local testing",
    "O meu médico é o Dr. Rui Costa, no centro de saúde de Alvalade",
    "Prefiro café sem açúcar",
    "Mi dentista se llama Dra. López",
    "Mein Hausarzt ist Dr. Schmidt in Berlin",
    "Mon médecin est le Dr Martin",
    "Is vegetarian since 2019",
    "Has a meeting with the bank manager on Friday about the mortgage",
    "Pays rent of 1200 euros on the 1st of each month",
    "Her son's school is Escola Básica nº 1 de Lisboa",
]:
    pos(text)

# ---- supplementary batch (implementation review, written before running) ---
# A second, independent batch so the G4 numbers are not measured only on items
# written alongside the detector. Same synthetic-only rules as above.

# Credentials in shapes the first batch does not cover.
neg("api_key", "block", "the api key for the weather service is 9f8e7d6c5b4a39281706f5e4d3c2b1a0")
neg("api_key", "block", "curl -H 'x-api-key: FAKE1a2b3c4d5e6f7g8h9i0j' https://api.example.com")
neg("api_key", "block", "Telegram bot token: 123456789:AAFAKEfakeFAKEfakeFAKEfakeFAKEfake12",
    "fake-shaped Telegram bot token")
neg("auth_token", "block", "The bot token is FAKE0123abcdEFGH4567ijkl")
neg("password", "block", "my Bitwarden master password is Correct-Horse-Battery-9")
neg("password", "block", "pwd: Gr33n-Tea!")
neg("password", "block", "The wifi pass is Blue-Fox-72")
neg("password", "block", "password = 'hunter22'")
neg("password", "block", "db login is admin / password Hunter2!x")
neg("payment_card", "block", "Card: 4111111111111111, CVV 123, exp 08/28", "public PSP test card number")
neg("payment_card", "block", "my amex 378282246310005", "public PSP test card number")
neg("payment_card", "block", "card 4111.1111.1111.1111", "public PSP test card number, dotted")
neg("bank_account", "block", "My IBAN is PT50000201231234567890154", "ISO 13616 registry example IBAN")
neg("bank_account", "block", "iban gb82west12345698765432", "ISO 13616 registry example IBAN, lower-case")
neg("government_id", "block", "My SSN: 078 05 1120", "SSA-voided 'Woolworth' example number")
neg("government_id", "block", "SSN#078051120", "SSA-voided 'Woolworth' example number")
neg("government_id", "block", "My National Insurance number is QQ 12 34 56 C",
    "HMRC reserved example NINO prefix QQ")
neg("government_id", "block", "NINO: QQ123456C", "HMRC reserved example NINO prefix QQ")
neg("government_id", "block", "My Aadhaar number is 2345 6789 0124", "synthetic Aadhaar-shaped number")
neg("private_key", "block",
    "bitcoin private key (WIF): 5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ",
    "Bitcoin wiki public example WIF key")
neg("private_key", "block", "Solana private key: 4" + "FAKEsoLanaKeyBase58" * 4 + "abcdefghijk",
    "fake-shaped base58 key")

# Near-misses that must stay allowed (commit hashes, UUIDs, phone numbers in
# normal chat, password-manager talk, versions, everyday numbers).
for text in [
    "My password manager is Bitwarden",
    "I store all my passwords in Bitwarden",
    "Password: see Bitwarden",
    "The password is in the Bitwarden vault under NAS",
    "Reset the router password tomorrow",
    "My GitHub 2FA is a YubiKey",
    "git checkout 3f9b6ec",
    "Reverted commit 8eb7b4c5d2a1f0e9b8c7d6e5f4a3b2c1d0e9f8a7 on main",
    "Cherry-picked 5352cec and 9f36cc5 onto main",
    "Trace id 123e4567-e89b-12d3-a456-426614174000",
    "The UUID for the staging tenant is 6f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f",
    "Call me at 415-555-0134 tonight",
    "Text my mum on 07700 900123",
    "My number changed to 912 345 678, save it",
    "WhatsApp me on 00351912345678",
    "The office phone is 213 456 7890 ext 12",
    "My phone's IMEI is 490154203237518",
    "The PIN pad at the gym is broken",
    "Set a 6-digit PIN on the new phone",
    "My bank is Millennium BCP",
    "Our IBAN validation uses python-stdnum",
    "Bank reference RF18 5390 0754 7034",
    "The API key rotation runs every Monday at 09:00",
    "API key: not set yet",
    "Use the OPENAI_API_KEY environment variable",
    "Bearer authentication is configured in nginx",
    "The JWT expiry is 3600 seconds",
    "Docker image tag v2.5.3-rc8",
    "Version 2.7.0-rc.1 of the core crate",
    "The tx went through on block 18934567",
    "AWS region eu-west-1, account 123456789012",
    "The Zoom meeting ID is 845 2931 7744",
    "Postcode SW1A 1AA",
    "My flight confirmation code is QX7T2B",
    "ISBN 978-3-16-148410-0",
    "I wrote my recovery phrase on paper and keep it in a safe",
    "abandon the project if it slips another month",
    "Shopping list: apple banana bread butter cheese chicken coffee cream egg garlic lemon milk",
]:
    pos(text)

# ---- held-out batch (written after the detector fixes, run once) ----------
# Measured once before any further detector change; the PR body reports that
# first-run result separately from the final numbers.
neg("password", "block", "My Netflix password: Pizza4ever!")
neg("password", "block", "The PIN for the house alarm is 2580")
neg("password", "block", "senha do wifi: casa2024!")
neg("password", "block", "DB_PASSWORD=s3cr3t-Value")
neg("password", "block", "MYSQL_ROOT_PASSWORD: example123")
neg("password", "block", "My bank card PIN is 0000")
neg("api_key", "block", "AWS secret access key wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "AWS documentation example secret key")
neg("api_key", "block", "export ZAI_API_KEY=0123456789abcdef0123456789abcdef.FAKEfakeFAKEfake",
    "fake-shaped z.ai key")
neg("auth_token", "block", "Authorization: token FAKEghp1234567890abcdefFAKE")
neg("auth_token", "block", "the token is ya29.FAKEa0ARrdaM0123456789")
neg("payment_card", "block", "Visa card 4012 8888 8888 1881 exp 01/30", "public PSP test card number")
neg("payment_card", "block", "Mastercard 5105-1051-0510-5100", "public PSP test card number")
neg("payment_card", "block", "card no 3714 496353 98431", "public PSP test card number")
neg("bank_account", "block", "IBAN DE89370400440532013000", "ISO 13616 registry example IBAN")
neg("government_id", "block", "SSN 219-09-9999", "SSA-voided advertising example number")
neg("government_id", "block", "Passport: 123456789", "synthetic passport number")
neg("government_id", "block", "My NHS number is 943 476 5919", "NHS Digital documentation example number")
neg("government_id", "block", "Mein Personalausweis hat die Nummer L01X00T47",
    "German ID card specimen number")
neg("government_id", "block", "CPF 123.456.789-09", "synthetic Brazilian CPF (check digits valid)")
neg("seed_phrase", "block",
    "seed: legal winner thank year wave sausage worth useful legal winner thank yellow",
    "BIP-39 public test vector")
neg("private_key", "block",
    "ETH private key 0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318",
    "web3.js documentation example key")
neg("private_key", "block", "ssh key file: -----BEGIN OPENSSH PRIVATE KEY-----")
neg("password", "block", "router login admin, password Zx9-Lq2-Wm7")
neg("api_key", "block", "my api key for openrouter is FAKEor0123456789abcdefFAKE")

for text in [
    "I forgot my Netflix password again",
    "The Wi-Fi password is on the fridge",
    "Change the DB_PASSWORD variable before deploy",
    "My card was declined at the supermarket",
    "Send 50 euros to Maria via MB Way",
    "Our wedding anniversary is 12/06/2015",
    "Commit 1a2b3c4 broke the build",
    "The PR number is 711",
    "Build #4111 failed on CI",
    "Pull request https://github.com/graphprotocol/totalreclaw/pull/711",
    "The SHA of the release tag is 8eb7b4c",
    "user id 1234567890123",
    "Unix time 1730600000",
    "Lat/long 38.7223, -9.1393",
    "My phone is +351 21 123 4567",
    "Ring me on 0800 123 4567",
    "I'm on +1 (212) 555-0198",
    "The token limit is 128k",
    "I have a session at 3pm",
    "Bearer of bad news: the trip is cancelled",
    "The secret to my lasagna is nutmeg",
    "My API usage this month was 1.2M tokens",
    "My Bitwarden vault has 340 items",
    "I use a YubiKey 5C NFC for 2FA",
    "His Social Security benefits start in 2027",
    "I need to renew my driver's license next month",
    "My passport number is in the travel folder",
    "The IBAN field on the form was empty",
    "Room 101, floor 3, building B",
    "Seed the database with test data before running migrations",
    "We planted tomato seeds in spring",
    "Recovery from the flu took two weeks",
    "ISO 27001 audit scheduled for March",
    "Kubernetes secret name is db-credentials",
    "The private key never leaves the hardware wallet",
    "SSH fingerprint SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
    "Bitcoin address 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    "Solana wallet address 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
    "My Ethereum wallet seed is on a steel plate",
    "Password reset email arrived at 10:42",
    "My PIN changed after the bank sent a new card",
]:
    pos(text)

corpus = {
    "version": 1,
    "description": (
        "DEP-4 PII/credential filter corpus (generated by generate_corpus.py - do not hand-edit). "
        "Every value is synthetic, fake-shaped, or a public test vector; none belongs to a real "
        "person or account. Negative items must not be 'allow'; positive items must be 'allow'."
    ),
    "items": items,
}

if __name__ == "__main__":
    negatives = sum(1 for i in items if i["set"] == "negative")
    positives = len(items) - negatives
    OUT.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.name}: {len(items)} items ({negatives} negative, {positives} positive)")
