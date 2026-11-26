# Local fault tier (real relay + Postgres + stub Pimlico)

Tier 2 of the #646/#650 test plan. The real Python client talks to a real relay
checkout and a throwaway Postgres; a stub (`stub_chain.py`, the same fake
ERC-4337 world as the unit tier, on a real clock) plays Pimlico and the chain
node, and tests inject faults into it.

```bash
docker run -d --name d23-relay-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=relay -p 55433:5432 postgres:16
git -C <relay> worktree add <dir> origin/main     # variant "today"
(cd <dir> && npm ci)
cd python
TR_RELAY_DIR=<dir> TR_RELAY_VARIANT=today PYTHONPATH=src:. pytest tests/integration_local -q --tb=short
# relay PR #87 (revert refunds): check out its head, then TR_RELAY_VARIANT=pr87
# and with WRITE_REFUND_DROPPED_OPS on: TR_RELAY_VARIANT=pr87_drops
```

`TR_PG_CONTAINER` / `TR_PG_PORT` override the container name (`d23-relay-pg`) and
host port (`55433`). The tests skip themselves when `TR_RELAY_DIR` is unset.
Throwaway recovery phrases are generated in memory per account and are never
printed or written.
