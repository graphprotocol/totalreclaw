//! First-use setup wizard for TotalReclaw.
//!
//! Handles:
//! - Recovery phrase generation/import (saved to `~/.totalreclaw/credentials.json`)
//! - Embedding mode selection (saved to `~/.totalreclaw/embedding-config.json`)

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

use crate::embedding::EmbeddingMode;
use crate::Result;

// ---------------------------------------------------------------------------
// Config paths
// ---------------------------------------------------------------------------

/// Get the TotalReclaw config directory (~/.totalreclaw/).
pub fn config_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".totalreclaw")
}

/// Get the credentials file path.
pub fn credentials_path() -> PathBuf {
    config_dir().join("credentials.json")
}

/// Get the embedding config file path.
pub fn embedding_config_path() -> PathBuf {
    config_dir().join("embedding-config.json")
}

// ---------------------------------------------------------------------------
// Credentials
// ---------------------------------------------------------------------------

/// Stored credentials.
#[derive(Serialize, Deserialize)]
pub struct Credentials {
    pub recovery_phrase: String,
}

/// Load credentials from disk.
pub fn load_credentials() -> Result<Option<Credentials>> {
    let path = credentials_path();
    if !path.exists() {
        return Ok(None);
    }
    let data = std::fs::read_to_string(&path)?;
    let creds: Credentials =
        serde_json::from_str(&data).map_err(|e| crate::Error::Crypto(e.to_string()))?;
    Ok(Some(creds))
}

/// Save credentials to disk.
pub fn save_credentials(creds: &Credentials) -> Result<()> {
    let dir = config_dir();
    std::fs::create_dir_all(&dir)?;
    let path = credentials_path();
    let data = serde_json::to_string_pretty(creds)
        .map_err(|e| crate::Error::Crypto(e.to_string()))?;
    std::fs::write(&path, data)?;

    // Restrict permissions on Unix
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Embedding config
// ---------------------------------------------------------------------------

/// Persisted embedding configuration.
#[derive(Serialize, Deserialize)]
pub struct EmbeddingConfig {
    pub mode: String,
    pub model: Option<String>,
    pub dimensions: usize,
    pub ollama_url: Option<String>,
    pub provider_url: Option<String>,
    pub api_key_env: Option<String>,
}

/// Load embedding config from disk.
pub fn load_embedding_config() -> Result<Option<EmbeddingConfig>> {
    let path = embedding_config_path();
    if !path.exists() {
        return Ok(None);
    }
    let data = std::fs::read_to_string(&path)?;
    let config: EmbeddingConfig =
        serde_json::from_str(&data).map_err(|e| crate::Error::Embedding(e.to_string()))?;
    Ok(Some(config))
}

/// Save embedding config to disk.
pub fn save_embedding_config(config: &EmbeddingConfig) -> Result<()> {
    let dir = config_dir();
    std::fs::create_dir_all(&dir)?;
    let path = embedding_config_path();
    let data = serde_json::to_string_pretty(config)
        .map_err(|e| crate::Error::Embedding(e.to_string()))?;
    std::fs::write(&path, data)?;
    Ok(())
}

/// Convert saved config to EmbeddingMode.
pub fn config_to_mode(config: &EmbeddingConfig) -> EmbeddingMode {
    match config.mode.as_str() {
        "local" => EmbeddingMode::Local {
            model_path: config
                .model
                .clone()
                .unwrap_or_else(|| "onnx-community/harrier-oss-v1-270m-ONNX".into()),
        },
        "ollama" => EmbeddingMode::Ollama {
            base_url: config
                .ollama_url
                .clone()
                .unwrap_or_else(|| "http://localhost:11434".into()),
            model: config
                .model
                .clone()
                .unwrap_or_else(|| "nomic-embed-text".into()),
        },
        "zeroclaw" => EmbeddingMode::ZeroClaw {
            base_url: config
                .provider_url
                .clone()
                .unwrap_or_default(),
            api_key: std::env::var(
                config
                    .api_key_env
                    .as_deref()
                    .unwrap_or("ZEROCLAW_EMBEDDING_API_KEY"),
            )
            .unwrap_or_default(),
        },
        "llm" | _ => EmbeddingMode::LlmProvider {
            base_url: config
                .provider_url
                .clone()
                .unwrap_or_else(|| "https://api.openai.com".into()),
            api_key: std::env::var(
                config
                    .api_key_env
                    .as_deref()
                    .unwrap_or("OPENAI_API_KEY"),
            )
            .unwrap_or_default(),
            model: config
                .model
                .clone()
                .unwrap_or_else(|| "text-embedding-3-small".into()),
        },
    }
}

/// Generate a fresh BIP-39 mnemonic.
pub fn generate_mnemonic() -> String {
    // Generate 128 bits of entropy for a 12-word mnemonic
    use rand::RngCore;
    let mut entropy = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut entropy);
    let mnemonic = bip39::Mnemonic::from_entropy(&entropy)
        .expect("Failed to generate mnemonic from 16 bytes");
    mnemonic.to_string()
}

// ---------------------------------------------------------------------------
// Third-party crate for home directory
// ---------------------------------------------------------------------------

mod dirs {
    use std::path::PathBuf;

    pub fn home_dir() -> Option<PathBuf> {
        std::env::var_os("HOME")
            .or_else(|| std::env::var_os("USERPROFILE"))
            .map(PathBuf::from)
    }
}
