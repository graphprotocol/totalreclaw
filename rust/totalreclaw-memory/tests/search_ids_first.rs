//! Ids-first subgraph search (internal #666, port of public #703).
//!
//! `search_candidates` now queries blind indexes for fact ids only and loads
//! each unique fact's payload once. These tests run the real function against
//! a small in-process HTTP mock of the relay's `/v1/subgraph` endpoint and
//! check:
//!
//!   1. Parity: the same facts as the old full-payload loop. The old loop is
//!      replayed in `old_search` (sequential 5-trapdoor batches, stop before a
//!      batch once `max_candidates` facts are in hand, first hit wins). The
//!      old result came out of a `HashMap`, so its order was arbitrary; the
//!      new result is in first-hit order.
//!   2. Fewer bytes than the old loop's responses.
//!   3. Each payload fetched once, at most 6 queries in flight.
//!   4. A failed payload batch drops only its own facts.
//!
//! Run: cargo test --test search_ids_first

use std::collections::HashSet;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use totalreclaw_memory::relay::{RelayClient, RelayConfig};
use totalreclaw_memory::search::{search_candidates, SubgraphFact};

const OWNER: &str = "0xabc0000000000000000000000000000000000001";
const PAYLOAD_FIELDS: [&str; 8] = [
    "id",
    "encryptedBlob",
    "encryptedEmbedding",
    "decayScore",
    "timestamp",
    "createdAt",
    "isActive",
    "contentFp",
];

// ---------------------------------------------------------------------------
// In-memory subgraph
// ---------------------------------------------------------------------------

struct Vault {
    facts: Vec<Value>,
    /// (blind index id, hash, index into `facts`)
    indexes: Vec<(String, String, usize)>,
}

fn hex(n: usize, seed: u64) -> String {
    let mut out = String::new();
    let mut x = seed;
    while out.len() < n {
        x = (x.wrapping_mul(1_103_515_245).wrapping_add(12_345)) & 0x7fff_ffff;
        out.push_str(&format!("{:08x}", x));
    }
    out.truncate(n);
    out
}

/// `n` facts (~1 KB blob, ~2.6 KB embedding as hex). Fact i is indexed under
/// `common`, `mod3-<i%3>` and `w<i>`.
fn make_vault(n: usize, inactive: &[usize]) -> Vault {
    let mut facts = Vec::new();
    let mut indexes = Vec::new();
    for i in 0..n {
        let id = format!("fact-{:04}", i);
        facts.push(json!({
            "id": id,
            "encryptedBlob": format!("0x{}", hex(2000, i as u64 + 1)),
            "encryptedEmbedding": hex(5200, i as u64 + 7),
            "decayScore": "0.9",
            "timestamp": (1_700_000_000 + i).to_string(),
            "createdAt": (1_700_000_000 + i).to_string(),
            "isActive": !inactive.contains(&i),
            "contentFp": hex(64, i as u64 + 3),
        }));
        for hash in [
            "common".to_string(),
            format!("mod3-{}", i % 3),
            format!("w{}", i),
        ] {
            indexes.push((format!("{}:{}", hash, id), hash, i));
        }
    }
    Vault { facts, indexes }
}

fn project(fact: &Value, selection: &str) -> Value {
    let mut out = serde_json::Map::new();
    for f in PAYLOAD_FIELDS {
        let present = selection
            .split(|c: char| !c.is_alphanumeric())
            .any(|tok| tok == f);
        if present {
            out.insert(f.to_string(), fact[f].clone());
        }
    }
    Value::Object(out)
}

fn is_active(v: &Vault, i: usize) -> bool {
    v.facts[i]["isActive"].as_bool().unwrap()
}

/// Resolve one GraphQL request. Returns (kind, data).
fn resolve(v: &Vault, query: &str, vars: &Value) -> (&'static str, Value) {
    if query.contains("blindIndexes") {
        let trapdoors: HashSet<&str> = vars["trapdoors"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t.as_str().unwrap())
            .collect();
        let first = vars["first"].as_u64().unwrap() as usize;
        let mut rows: Vec<&(String, String, usize)> = v
            .indexes
            .iter()
            .filter(|(_, h, i)| trapdoors.contains(h.as_str()) && is_active(v, *i))
            .collect();
        rows.sort_by(|a, b| b.0.cmp(&a.0)); // orderBy: id desc
        let selection = query
            .split("fact {")
            .nth(1)
            .and_then(|s| s.split('}').next())
            .unwrap_or("");
        let entries: Vec<Value> = rows
            .into_iter()
            .take(first)
            .map(|(id, _, i)| json!({ "id": id, "fact": project(&v.facts[*i], selection) }))
            .collect();
        return ("blind", json!({ "blindIndexes": entries }));
    }
    if query.contains("id_in") {
        let ids: HashSet<&str> = vars["ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t.as_str().unwrap())
            .collect();
        let first = vars["first"].as_u64().unwrap() as usize;
        // Reverse vault order on purpose: the client must not rely on the
        // subgraph returning facts in request order.
        let facts: Vec<Value> = (0..v.facts.len())
            .rev()
            .filter(|i| ids.contains(v.facts[*i]["id"].as_str().unwrap()) && is_active(v, *i))
            .take(first)
            .map(|i| project(&v.facts[i], query))
            .collect();
        return ("facts", json!({ "facts": facts }));
    }
    ("other", json!({}))
}

struct Rec {
    kind: &'static str,
    vars: Value,
    bytes: usize,
}

struct Mock {
    vault: Vault,
    log: Mutex<Vec<Rec>>,
    in_flight: AtomicUsize,
    max_in_flight: AtomicUsize,
    /// Fail (HTTP 500) the payload batch that contains this fact id.
    fail_facts_with: Mutex<Option<String>>,
}

impl Mock {
    fn new(vault: Vault) -> Arc<Self> {
        Arc::new(Self {
            vault,
            log: Mutex::new(Vec::new()),
            in_flight: AtomicUsize::new(0),
            max_in_flight: AtomicUsize::new(0),
            fail_facts_with: Mutex::new(None),
        })
    }
    fn bytes(&self) -> usize {
        self.log.lock().unwrap().iter().map(|r| r.bytes).sum()
    }
    fn count(&self, kind: &str) -> usize {
        self.log
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.kind == kind)
            .count()
    }
}

fn handle(stream: std::net::TcpStream, mock: Arc<Mock>) {
    let mut writer = stream.try_clone().unwrap();
    let mut reader = BufReader::new(stream);
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            return;
        }
        let mut len = 0usize;
        loop {
            let mut h = String::new();
            reader.read_line(&mut h).unwrap();
            if h == "\r\n" || h.is_empty() {
                break;
            }
            let lower = h.to_ascii_lowercase();
            if let Some(v) = lower.strip_prefix("content-length:") {
                len = v.trim().parse().unwrap();
            }
        }
        let mut body = vec![0u8; len];
        reader.read_exact(&mut body).unwrap();
        let req: Value = serde_json::from_slice(&body).unwrap();

        let now = mock.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        mock.max_in_flight.fetch_max(now, Ordering::SeqCst);
        std::thread::sleep(std::time::Duration::from_millis(5));

        let (kind, data) = resolve(
            &mock.vault,
            req["query"].as_str().unwrap(),
            &req["variables"],
        );
        let fail = kind == "facts"
            && mock
                .fail_facts_with
                .lock()
                .unwrap()
                .as_deref()
                .is_some_and(|id| {
                    req["variables"]["ids"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|v| v == id)
                });
        let (status, resp) = if fail {
            ("500 Internal Server Error", "boom".to_string())
        } else {
            ("200 OK", json!({ "data": data }).to_string())
        };
        mock.log.lock().unwrap().push(Rec {
            kind,
            vars: req["variables"].clone(),
            bytes: if fail { 0 } else { resp.len() },
        });
        mock.in_flight.fetch_sub(1, Ordering::SeqCst);
        let out = format!(
            "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
            status,
            resp.len(),
            resp
        );
        if writer.write_all(out.as_bytes()).is_err() {
            return;
        }
    }
}

fn serve(mock: Arc<Mock>) -> RelayClient {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let m = mock.clone();
            std::thread::spawn(move || handle(stream.unwrap(), m));
        }
    });
    RelayClient::new(RelayConfig {
        relay_url: format!("http://{}", addr),
        auth_key_hex: "00".repeat(32),
        wallet_address: OWNER.into(),
        is_test: true,
        chain_id: 84532,
    })
}

/// The pre-#666 loop: sequential 5-trapdoor batches with full payloads.
/// Returns (facts, response bytes).
fn old_search(v: &Vault, trapdoors: &[String], max_candidates: usize) -> (Vec<Value>, usize) {
    let old_query = "blindIndexes { id fact { id encryptedBlob encryptedEmbedding decayScore timestamp createdAt isActive contentFp } }";
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    let mut bytes = 0;
    for chunk in trapdoors.chunks(5) {
        if out.len() >= max_candidates {
            break;
        }
        let (_, data) = resolve(v, old_query, &json!({ "trapdoors": chunk, "first": 1000 }));
        bytes += json!({ "data": data }).to_string().len();
        for e in data["blindIndexes"].as_array().unwrap() {
            let f = &e["fact"];
            if f["isActive"] != json!(false) && seen.insert(f["id"].as_str().unwrap().to_string()) {
                out.push(f.clone());
            }
        }
    }
    (out, bytes)
}

fn to_json(facts: &[SubgraphFact]) -> Vec<Value> {
    facts
        .iter()
        .map(|f| serde_json::to_value(f).unwrap())
        .collect()
}

fn sorted(mut v: Vec<Value>) -> Vec<Value> {
    v.sort_by(|a, b| a["id"].as_str().cmp(&b["id"].as_str()));
    v
}

fn tds(list: &[&str]) -> Vec<String> {
    list.iter().map(|s| s.to_string()).collect()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[tokio::test]
async fn small_vault_parity_and_first_hit_order() {
    let mock = Mock::new(make_vault(12, &[4]));
    let relay = serve(mock.clone());
    let trapdoors = tds(&["mod3-1", "w2", "w4"]);
    let got = search_candidates(&relay, &trapdoors, 250).await.unwrap();
    let (old, _) = old_search(&mock.vault, &trapdoors, 250);

    assert_eq!(
        sorted(to_json(&got)),
        sorted(old.clone()),
        "same facts and fields"
    );
    let ids: Vec<&str> = got.iter().map(|f| f.id.as_str()).collect();
    assert_eq!(ids, ["fact-0002", "fact-0010", "fact-0007", "fact-0001"]);
    assert_eq!(mock.count("blind"), 1);
    assert_eq!(mock.count("facts"), 1);
}

#[tokio::test]
async fn many_batches_parity_bytes_and_concurrency() {
    let mock = Mock::new(make_vault(300, &[5, 50, 99]));
    let relay = serve(mock.clone());
    // 4 overlapping + 46 single-fact trapdoors = 10 batches over 2 waves.
    let mut trapdoors = tds(&["common", "mod3-0", "mod3-1", "mod3-2"]);
    trapdoors.extend((0..46).map(|i| format!("w{}", i * 3)));

    for max in [1000, 250] {
        mock.log.lock().unwrap().clear();
        let got = search_candidates(&relay, &trapdoors, max).await.unwrap();
        let (old, old_bytes) = old_search(&mock.vault, &trapdoors, max);
        assert_eq!(
            sorted(to_json(&got)),
            sorted(old),
            "max={max}: same facts as the old loop"
        );
        assert!(!got
            .iter()
            .any(|f| ["fact-0005", "fact-0050", "fact-0099"].contains(&f.id.as_str())));

        let fetched: Vec<String> = mock
            .log
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.kind == "facts")
            .flat_map(|r| {
                r.vars["ids"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap().to_string())
                    .collect::<Vec<_>>()
            })
            .collect();
        let unique: HashSet<&String> = fetched.iter().collect();
        assert_eq!(unique.len(), fetched.len(), "each payload fetched once");

        let new_bytes = mock.bytes();
        println!(
            "max={max}: {} facts, {new_bytes} bytes vs {old_bytes} old",
            got.len()
        );
        assert!(
            new_bytes < old_bytes,
            "fewer bytes: {new_bytes} vs {old_bytes}"
        );
    }
    assert!(mock.max_in_flight.load(Ordering::SeqCst) <= 6);
    assert!(
        mock.max_in_flight.load(Ordering::SeqCst) > 1,
        "batches ran concurrently"
    );
}

#[tokio::test]
async fn payloads_fetched_in_batches_of_100() {
    let mock = Mock::new(make_vault(450, &[]));
    let relay = serve(mock.clone());
    let trapdoors: Vec<String> = (0..450).map(|i| format!("w{}", i)).collect();
    let got = search_candidates(&relay, &trapdoors, 1000).await.unwrap();
    let (old, _) = old_search(&mock.vault, &trapdoors, 1000);
    assert_eq!(
        to_json(&got),
        old,
        "first-hit order matches the old loop's hit order"
    );
    assert_eq!(mock.count("facts"), 5);
    assert!(mock.max_in_flight.load(Ordering::SeqCst) <= 6);
}

#[tokio::test]
async fn failed_payload_batch_drops_only_its_facts() {
    let mock = Mock::new(make_vault(250, &[]));
    // The first payload batch holds the first 100 hits, fact-0004 among them.
    *mock.fail_facts_with.lock().unwrap() = Some("fact-0004".into());
    let relay = serve(mock.clone());
    let trapdoors: Vec<String> = (0..250).map(|i| format!("w{}", i)).collect();
    let got = search_candidates(&relay, &trapdoors, 1000).await.unwrap();
    assert_eq!(got.len(), 150);
    assert!(!got.iter().any(|f| f.id == "fact-0004"));
}

#[tokio::test]
async fn no_hits_no_payload_query() {
    let mock = Mock::new(make_vault(5, &[]));
    let relay = serve(mock.clone());
    let got = search_candidates(&relay, &tds(&["nope"]), 250)
        .await
        .unwrap();
    assert!(got.is_empty());
    assert_eq!(mock.count("facts"), 0);
}
