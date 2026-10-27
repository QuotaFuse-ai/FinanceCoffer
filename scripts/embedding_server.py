"""OpenAI-compatible embedding endpoint backed by BAAI/bge-m3 on CPU.

Matches the contract FinanceCoffer's query layer expects:
    POST /v1/embeddings {"model": ..., "input": [text, ...]}
    -> {"data": [{"embedding": [1024 floats], "index": 0}], ...}

Pooling follows the model's own 1_Pooling config: CLS token, 1024 dims,
no mean/max pooling. Vectors are L2-normalised here; the query layer
normalises again, which is idempotent for unit vectors.
"""

from __future__ import annotations

import argparse
import math
import os
import threading
from pathlib import Path

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

DEFAULT_MODEL_DIR = Path(os.environ.get("FC_EMBEDDING_MODEL_DIR", "var/models/bge-m3"))
DEFAULT_MODEL_NAME = "BAAI/bge-m3"
DEFAULT_DIM = 1024
MAX_BATCH = 32
MAX_TOKENS = 8192


class EmbeddingRequest(BaseModel):
    model: str | None = None
    input: str | list[str]


class EmbeddingItem(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    object: str = "list"
    model: str
    data: list[EmbeddingItem]


class _Runtime:
    def __init__(self, model_dir: Path, model_name: str, dim: int) -> None:
        self.model_name = model_name
        self.dim = dim
        self._lock = threading.Lock()
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
        self.model = AutoModel.from_pretrained(str(model_dir), dtype=torch.float32)
        self.model.eval()

    def encode(self, texts: list[str]) -> list[list[float]]:
        batch = self.tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=MAX_TOKENS,
            return_tensors="pt",
        )
        with self._lock, torch.inference_mode():
            hidden = self.model(**batch).last_hidden_state
            cls = hidden[:, 0, :].to(torch.float32)
            vectors = cls.cpu().tolist()
        out: list[list[float]] = []
        for vector in vectors:
            norm = math.sqrt(sum(value * value for value in vector))
            if not math.isfinite(norm) or norm == 0.0:
                raise HTTPException(status_code=500, detail="model produced a zero or non-finite vector")
            out.append([value / norm for value in vector])
        return out


runtime: _Runtime | None = None


def create_app(model_dir: Path, model_name: str, dim: int) -> FastAPI:
    app = FastAPI(title="FinanceCoffer embedding service", version="1.0.0")

    @app.on_event("startup")
    def _load() -> None:
        global runtime
        runtime = _Runtime(model_dir, model_name, dim)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok" if runtime is not None else "loading",
            "model": model_name,
            "dim": dim,
            "device": "cpu",
        }

    @app.post("/v1/embeddings", response_model=EmbeddingResponse)
    def embeddings(request: EmbeddingRequest) -> EmbeddingResponse:
        if runtime is None:
            raise HTTPException(status_code=503, detail="model is still loading")
        texts = [request.input] if isinstance(request.input, str) else list(request.input)
        if not texts:
            raise HTTPException(status_code=400, detail="input must contain at least one string")
        if len(texts) > MAX_BATCH:
            raise HTTPException(status_code=400, detail=f"at most {MAX_BATCH} inputs per request")
        if not all(isinstance(text, str) for text in texts):
            raise HTTPException(status_code=400, detail="every input must be a string")
        vectors = runtime.encode(texts)
        for vector in vectors:
            if len(vector) != dim:
                raise HTTPException(status_code=500, detail="embedding dimension does not match configuration")
        return EmbeddingResponse(
            model=runtime.model_name,
            data=[EmbeddingItem(index=index, embedding=vector) for index, vector in enumerate(vectors)],
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve BGE-M3 embeddings over an OpenAI-compatible API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    parser.add_argument("--threads", type=int, default=int(os.environ.get("FC_EMBEDDING_THREADS", "0")))
    args = parser.parse_args()

    if not args.model_dir.is_dir():
        raise SystemExit(f"model directory not found: {args.model_dir}")

    threads = args.threads or max(1, min(8, os.cpu_count() or 1))
    torch.set_num_threads(threads)

    import uvicorn

    uvicorn.run(create_app(args.model_dir, args.model_name, args.dim), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
