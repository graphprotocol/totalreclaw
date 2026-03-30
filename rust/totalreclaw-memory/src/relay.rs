//! Relay HTTP client for the TotalReclaw managed service.
//!
//! All communication with the relay server at `api.totalreclaw.xyz`.
//! Proxies bundler (JSON-RPC) and subgraph (GraphQL) requests.

use serde::{Deserialize, Serialize};

use crate::{Error, Result};

/// Relay client for TotalReclaw managed service.
#[derive(Clone)]
pub struct RelayClient {
    client: reqwest::Client,
    relay_url: String,
    auth_key_hex: String,
    wallet_address: String,
    is_test: bool,
}

/// Response from POST /v1/register.
#[derive(Deserialize)]
pub struct RegisterResponse {
    pub success: bool,
    pub user_id: Option<String>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
}

/// Response from POST /v1/addresses/resolve.
#[derive(Deserialize)]
pub struct ResolveResponse {
    pub address: Option<String>,
    pub error: Option<String>,
}

/// A raw GraphQL response wrapper.
#[derive(Deserialize)]
pub struct GraphQLResponse<T> {
    pub data: Option<T>,
    pub errors: Option<Vec<serde_json::Value>>,
}

/// Billing status response.
#[derive(Deserialize)]
pub struct BillingStatus {
    pub tier: Option<String>,
    pub facts_used: Option<u64>,
    pub facts_limit: Option<u64>,
    pub features: Option<serde_json::Value>,
}

/// Configuration for the relay client.
#[derive(Clone, Debug)]
pub struct RelayConfig {
    pub relay_url: String,
    pub auth_key_hex: String,
    pub wallet_address: String,
    pub is_test: bool,
}

impl RelayClient {
    /// Create a new relay client.
    pub fn new(config: RelayConfig) -> Self {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()
            .unwrap_or_default();

        Self {
            client,
            relay_url: config.relay_url.trim_end_matches('/').to_string(),
            auth_key_hex: config.auth_key_hex,
            wallet_address: config.wallet_address,
            is_test: config.is_test,
        }
    }

    /// Common headers for all relay requests.
    fn headers(&self) -> reqwest::header::HeaderMap {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            "X-TotalReclaw-Client",
            "zeroclaw-memory".parse().unwrap(),
        );
        if !self.auth_key_hex.is_empty() {
            headers.insert(
                "Authorization",
                format!("Bearer {}", self.auth_key_hex).parse().unwrap(),
            );
        }
        if !self.wallet_address.is_empty() {
            headers.insert(
                "X-Wallet-Address",
                self.wallet_address.parse().unwrap(),
            );
        }
        if self.is_test {
            headers.insert("X-TotalReclaw-Test", "true".parse().unwrap());
        }
        headers
    }

    /// Register with the relay server. Idempotent.
    pub async fn register(&self, auth_key_hash: &str, salt_hex: &str) -> Result<String> {
        #[derive(Serialize)]
        struct Body<'a> {
            auth_key_hash: &'a str,
            salt: &'a str,
        }

        let resp = self
            .client
            .post(format!("{}/v1/register", self.relay_url))
            .headers(self.headers())
            .json(&Body {
                auth_key_hash,
                salt: salt_hex,
            })
            .send()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        let body: RegisterResponse = resp
            .json()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        if body.success {
            Ok(body.user_id.unwrap_or_default())
        } else {
            Err(Error::Http(
                body.error_message
                    .unwrap_or_else(|| "Registration failed".into()),
            ))
        }
    }

    /// Resolve Smart Account address from the relay.
    pub async fn resolve_address(&self, auth_key_hex: &str) -> Result<String> {
        #[derive(Serialize)]
        struct Body<'a> {
            auth_key: &'a str,
        }

        let resp = self
            .client
            .post(format!("{}/v1/addresses/resolve", self.relay_url))
            .headers(self.headers())
            .json(&Body {
                auth_key: auth_key_hex,
            })
            .send()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        let body: ResolveResponse = resp
            .json()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        body.address
            .ok_or_else(|| Error::Http(body.error.unwrap_or_else(|| "No address returned".into())))
    }

    /// Execute a GraphQL query against the subgraph via relay proxy.
    pub async fn graphql<T: for<'de> Deserialize<'de>>(
        &self,
        query: &str,
        variables: serde_json::Value,
    ) -> Result<T> {
        let resp = self
            .client
            .post(format!("{}/v1/subgraph", self.relay_url))
            .headers(self.headers())
            .json(&serde_json::json!({
                "query": query,
                "variables": variables,
            }))
            .send()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(Error::Http(format!("GraphQL HTTP {}: {}", status, text)));
        }

        let gql: GraphQLResponse<T> = resp
            .json()
            .await
            .map_err(|e| Error::Http(format!("GraphQL parse error: {}", e)))?;

        gql.data
            .ok_or_else(|| Error::Http("GraphQL returned no data".into()))
    }

    /// Submit a protobuf payload via the bundler proxy.
    pub async fn submit_protobuf(&self, payload: &[u8]) -> Result<SubmitResult> {
        // The relay's bundler endpoint accepts raw protobuf for direct submission
        // when using the X-TotalReclaw-Direct-Submit header.
        // Fallback: wrap in JSON-RPC eth_sendUserOperation format.
        let payload_hex = hex::encode(payload);

        let resp = self
            .client
            .post(format!("{}/v1/bundler", self.relay_url))
            .headers(self.headers())
            .json(&serde_json::json!({
                "jsonrpc": "2.0",
                "method": "eth_sendUserOperation",
                "params": [{
                    "callData": format!("0x{}", payload_hex),
                }],
                "id": 1,
            }))
            .send()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        let body: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        Ok(SubmitResult {
            tx_hash: body["result"]["txHash"]
                .as_str()
                .unwrap_or("")
                .to_string(),
            user_op_hash: body["result"]["userOpHash"]
                .as_str()
                .unwrap_or("")
                .to_string(),
            success: body["result"]["success"].as_bool().unwrap_or(false),
        })
    }

    /// Health check against the relay.
    pub async fn health_check(&self) -> Result<bool> {
        let resp = self
            .client
            .get(format!("{}/health", self.relay_url))
            .send()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        Ok(resp.status().is_success())
    }

    /// Get billing status.
    pub async fn billing_status(&self) -> Result<BillingStatus> {
        let resp = self
            .client
            .get(format!(
                "{}/v1/billing/status?wallet_address={}",
                self.relay_url, self.wallet_address
            ))
            .headers(self.headers())
            .send()
            .await
            .map_err(|e| Error::Http(e.to_string()))?;

        resp.json()
            .await
            .map_err(|e| Error::Http(e.to_string()))
    }

    /// Get the relay URL.
    pub fn relay_url(&self) -> &str {
        &self.relay_url
    }

    /// Get the wallet address.
    pub fn wallet_address(&self) -> &str {
        &self.wallet_address
    }
}

/// Result of submitting a UserOp.
#[derive(Debug)]
pub struct SubmitResult {
    pub tx_hash: String,
    pub user_op_hash: String,
    pub success: bool,
}
