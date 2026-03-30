//! Embedding pipeline with 4 modes.
//!
//! - `Local` — ONNX runtime (feature-gated with `local-embeddings`)
//! - `Ollama` — HTTP POST to local Ollama server
//! - `ZeroClaw` — Remote ZeroClaw embedding provider
//! - `LlmProvider` — OpenAI-compatible `/v1/embeddings` endpoint
//!
//! The local ONNX mode is gated behind `#[cfg(feature = "local-embeddings")]`
//! to avoid pulling in the `ort` and `tokenizers` dependencies by default.

use std::future::Future;
use std::pin::Pin;

use crate::{Error, Result};

/// Embedding mode enum.
#[derive(Debug, Clone)]
pub enum EmbeddingMode {
    /// Local ONNX model (requires `local-embeddings` feature).
    Local { model_path: String },
    /// Ollama server.
    Ollama { base_url: String, model: String },
    /// ZeroClaw remote embedding provider.
    ZeroClaw { base_url: String, api_key: String },
    /// OpenAI-compatible /v1/embeddings endpoint.
    LlmProvider {
        base_url: String,
        api_key: String,
        model: String,
    },
}

/// Trait for embedding providers.
pub trait EmbeddingProvider: Send + Sync {
    /// Embed a single text string into a vector.
    fn embed(&self, text: &str) -> Pin<Box<dyn Future<Output = Result<Vec<f32>>> + Send + '_>>;

    /// Return the embedding dimensionality.
    fn dimensions(&self) -> usize;
}

// ---------------------------------------------------------------------------
// Ollama provider
// ---------------------------------------------------------------------------

/// Ollama embedding provider.
pub struct OllamaProvider {
    base_url: String,
    model: String,
    dims: usize,
}

impl OllamaProvider {
    pub fn new(base_url: &str, model: &str, dims: usize) -> Self {
        Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            model: model.to_string(),
            dims,
        }
    }
}

impl EmbeddingProvider for OllamaProvider {
    fn embed(&self, text: &str) -> Pin<Box<dyn Future<Output = Result<Vec<f32>>> + Send + '_>> {
        let url = format!("{}/api/embeddings", self.base_url);
        let body = serde_json::json!({
            "model": self.model,
            "prompt": text,
        });

        Box::pin(async move {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .json(&body)
                .send()
                .await
                .map_err(|e| Error::Http(format!("Ollama request failed: {}", e)))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let text = resp.text().await.unwrap_or_default();
                return Err(Error::Http(format!("Ollama returned {}: {}", status, text)));
            }

            let data: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| Error::Http(format!("Ollama JSON parse failed: {}", e)))?;

            let embedding = data["embedding"]
                .as_array()
                .ok_or_else(|| {
                    Error::Embedding("no 'embedding' array in Ollama response".into())
                })?
                .iter()
                .map(|v| v.as_f64().unwrap_or(0.0) as f32)
                .collect();

            Ok(embedding)
        })
    }

    fn dimensions(&self) -> usize {
        self.dims
    }
}

// ---------------------------------------------------------------------------
// OpenAI-compatible provider (ZeroClaw + LlmProvider)
// ---------------------------------------------------------------------------

/// OpenAI-compatible embedding provider.
///
/// Works with any server that implements `/v1/embeddings` (OpenAI, ZeroClaw, etc.).
pub struct OpenAiCompatibleProvider {
    base_url: String,
    api_key: String,
    model: String,
    dims: usize,
}

impl OpenAiCompatibleProvider {
    pub fn new(base_url: &str, api_key: &str, model: &str, dims: usize) -> Self {
        Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            model: model.to_string(),
            dims,
        }
    }
}

impl EmbeddingProvider for OpenAiCompatibleProvider {
    fn embed(&self, text: &str) -> Pin<Box<dyn Future<Output = Result<Vec<f32>>> + Send + '_>> {
        let url = format!("{}/v1/embeddings", self.base_url);
        let body = serde_json::json!({
            "model": self.model,
            "input": text,
        });
        let api_key = self.api_key.clone();

        Box::pin(async move {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {}", api_key))
                .json(&body)
                .send()
                .await
                .map_err(|e| Error::Http(format!("embedding request failed: {}", e)))?;

            if !resp.status().is_success() {
                let status = resp.status();
                let text = resp.text().await.unwrap_or_default();
                return Err(Error::Http(format!(
                    "embedding provider returned {}: {}",
                    status, text
                )));
            }

            let data: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| Error::Http(format!("JSON parse failed: {}", e)))?;

            let embedding = data["data"][0]["embedding"]
                .as_array()
                .ok_or_else(|| {
                    Error::Embedding("no 'data[0].embedding' in response".into())
                })?
                .iter()
                .map(|v| v.as_f64().unwrap_or(0.0) as f32)
                .collect();

            Ok(embedding)
        })
    }

    fn dimensions(&self) -> usize {
        self.dims
    }
}

// ---------------------------------------------------------------------------
// Local ONNX provider (feature-gated)
// ---------------------------------------------------------------------------

#[cfg(feature = "local-embeddings")]
pub struct LocalOnnxProvider {
    _model_path: String,
    dims: usize,
}

#[cfg(feature = "local-embeddings")]
impl LocalOnnxProvider {
    pub fn new(model_path: &str, dims: usize) -> Result<Self> {
        Ok(Self {
            _model_path: model_path.to_string(),
            dims,
        })
    }
}

#[cfg(feature = "local-embeddings")]
impl EmbeddingProvider for LocalOnnxProvider {
    fn embed(&self, _text: &str) -> Pin<Box<dyn Future<Output = Result<Vec<f32>>> + Send + '_>> {
        Box::pin(async {
            Err(Error::Embedding(
                "local ONNX embedding not yet fully implemented".into(),
            ))
        })
    }

    fn dimensions(&self) -> usize {
        self.dims
    }
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/// Create an embedding provider from a mode configuration.
pub fn create_provider(mode: EmbeddingMode, dims: usize) -> Result<Box<dyn EmbeddingProvider>> {
    match mode {
        EmbeddingMode::Ollama { base_url, model } => {
            Ok(Box::new(OllamaProvider::new(&base_url, &model, dims)))
        }
        EmbeddingMode::ZeroClaw { base_url, api_key } => Ok(Box::new(
            OpenAiCompatibleProvider::new(&base_url, &api_key, "qwen3-embedding-0.6b", dims),
        )),
        EmbeddingMode::LlmProvider {
            base_url,
            api_key,
            model,
        } => Ok(Box::new(OpenAiCompatibleProvider::new(
            &base_url, &api_key, &model, dims,
        ))),
        #[cfg(feature = "local-embeddings")]
        EmbeddingMode::Local { model_path } => {
            Ok(Box::new(LocalOnnxProvider::new(&model_path, dims)?))
        }
        #[cfg(not(feature = "local-embeddings"))]
        EmbeddingMode::Local { .. } => Err(Error::Embedding(
            "local embeddings require the 'local-embeddings' feature".into(),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_create_provider_ollama() {
        let provider = create_provider(
            EmbeddingMode::Ollama {
                base_url: "http://localhost:11434".into(),
                model: "qwen3-embedding".into(),
            },
            1024,
        );
        assert!(provider.is_ok());
        assert_eq!(provider.unwrap().dimensions(), 1024);
    }

    #[test]
    fn test_create_provider_zeroclaw() {
        let provider = create_provider(
            EmbeddingMode::ZeroClaw {
                base_url: "https://api.example.com".into(),
                api_key: "test-key".into(),
            },
            1024,
        );
        assert!(provider.is_ok());
        assert_eq!(provider.unwrap().dimensions(), 1024);
    }

    #[test]
    fn test_create_provider_llm() {
        let provider = create_provider(
            EmbeddingMode::LlmProvider {
                base_url: "https://api.openai.com".into(),
                api_key: "test-key".into(),
                model: "text-embedding-3-small".into(),
            },
            1536,
        );
        assert!(provider.is_ok());
        assert_eq!(provider.unwrap().dimensions(), 1536);
    }

    #[test]
    fn test_create_provider_local_without_feature() {
        let provider = create_provider(
            EmbeddingMode::Local {
                model_path: "/tmp/model".into(),
            },
            1024,
        );
        #[cfg(not(feature = "local-embeddings"))]
        assert!(provider.is_err());
        #[cfg(feature = "local-embeddings")]
        assert!(provider.is_ok());
    }
}
