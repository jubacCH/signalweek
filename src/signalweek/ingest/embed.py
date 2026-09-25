"""Local sentence embeddings for headline dedup.

Runs ``sentence-transformers/all-MiniLM-L6-v2`` through ONNX Runtime. No torch
and no hosted API. The model files go into the Docker image at build time
(``python -m signalweek.ingest.embed download <dir>``), so the app never
downloads anything at runtime.

Embeddings are mean-pooled over the attention mask and L2-normalised, which
matches what ``sentence-transformers`` produces for this model. Because the
vectors are unit length, cosine similarity is just a dot product.
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import numpy as np

MODEL_REPO = "sentence-transformers/all-MiniLM-L6-v2"
# Pinned so a rebuild can never pick up different weights.
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MODEL_DIM = 384
# ``(remote path, local name, sha256 or None)``. The sha256 of model.onnx is
# its Git LFS object id on the Hub.
MODEL_FILES: tuple[tuple[str, str, str | None], ...] = (
    (
        "onnx/model.onnx",
        "model.onnx",
        "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    ),
    ("tokenizer.json", "tokenizer.json", None),
)
# The model was trained on sequences up to 256 tokens; headlines are far shorter.
MAX_TOKENS = 128
BATCH_SIZE = 64

MODEL_DIR_ENV = "SIGNALWEEK_EMBED_MODEL_DIR"
DEFAULT_MODEL_DIR = "/opt/signalweek-model"


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray:
        """Return a ``(len(texts), MODEL_DIM)`` float32 array of unit vectors."""
        ...


class OnnxMiniLMEmbedder:
    """all-MiniLM-L6-v2 on ONNX Runtime (CPU)."""

    def __init__(self, model_dir: str | Path) -> None:
        import onnxruntime
        from tokenizers import Tokenizer

        model_dir = Path(model_dir)
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = onnxruntime.SessionOptions()
        options.log_severity_level = 3
        # Explicit thread counts stop ORT from pinning threads to cores, which
        # fails inside LXC containers.
        options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_dir / "model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {i.name for i in self._session.get_inputs()}

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, MODEL_DIM), dtype=np.float32)
        out = [
            self._embed_batch(texts[i : i + BATCH_SIZE]) for i in range(0, len(texts), BATCH_SIZE)
        ]
        return np.concatenate(out, axis=0)

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        encodings = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feeds)[0]
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-9, None)
        return normalize(pooled)


def normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return (vectors / np.clip(norms, 1e-12, None)).astype(np.float32)


def model_dir() -> Path:
    return Path(os.environ.get(MODEL_DIR_ENV, DEFAULT_MODEL_DIR))


def model_available(directory: str | Path | None = None) -> bool:
    directory = Path(directory) if directory is not None else model_dir()
    return all((directory / local).is_file() for _, local, _ in MODEL_FILES)


@lru_cache(maxsize=1)
def get_default_embedder() -> Embedder:
    """Load the baked-in model once per process."""
    directory = model_dir()
    if not model_available(directory):
        raise RuntimeError(
            f"embedding model not found in {directory}; "
            f"run `python -m signalweek.ingest.embed download {directory}`"
        )
    return OnnxMiniLMEmbedder(directory)


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def download(directory: str | Path) -> None:
    """Fetch the pinned model files from the Hugging Face Hub (build time only)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for remote, local, sha256 in MODEL_FILES:
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{remote}"
        target = directory / local
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
            raise RuntimeError(f"checksum mismatch for {url}")
        target.write_bytes(data)
        print(f"{target} ({len(data)} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "download":
        sys.exit("usage: python -m signalweek.ingest.embed download <dir>")
    download(sys.argv[2])
