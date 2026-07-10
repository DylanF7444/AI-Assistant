from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from typing import Protocol


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FallbackEmbeddingProvider:
    def __init__(
        self,
        primary: EmbeddingProvider,
        fallback: EmbeddingProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            embeddings = await self.primary.embed(texts)
        except Exception:
            return await self.fallback.embed(texts)
        if len(embeddings) != len(texts) or any(not item for item in embeddings):
            return await self.fallback.embed(texts)
        return embeddings


class OllamaEmbeddingProvider:
    def __init__(
        self,
        model: str,
        endpoint: str = "http://127.0.0.1:11434/api/embeddings",
        timeout_seconds: float = 10,
    ) -> None:
        self.model = model
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed_sync, texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for text in texts:
            payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
            request = urllib.request.Request(
                self.endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    body = json.load(response)
            except urllib.error.URLError:
                raise
            embedding = body.get("embedding")
            if not isinstance(embedding, list) or not all(
                isinstance(value, int | float) for value in embedding
            ):
                raise ValueError("Ollama embedding response did not include an embedding")
            embeddings.append([float(value) for value in embedding])
        return embeddings


class LexicalFallbackEmbeddingProvider:
    STOP_WORDS = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "my",
        "of",
        "on",
        "the",
        "to",
    }
    SYNONYMS = {
        "calculate": ("calculator", "arithmetic", "math"),
        "compute": ("calculator", "arithmetic", "math"),
        "launch": ("open", "application", "start"),
        "open": ("launch", "application", "start"),
        "browser": ("web", "navigate"),
        "find": ("search", "locate"),
        "move": ("relocate", "transfer"),
        "minimize": ("hide", "dismiss"),
        "summarize": ("summary", "condense"),
    }

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = self._tokens(text)
        for token in tokens:
            self._add_token(vector, token, weight=1.0)
            for synonym in self.SYNONYMS.get(token, ()):
                self._add_token(vector, synonym, weight=0.6)
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector

    def _tokens(self, text: str) -> list[str]:
        return [
            token
            for token in re.findall(r"[a-z0-9]+", text.casefold())
            if token not in self.STOP_WORDS
        ]

    def _add_token(self, vector: list[float], token: str, *, weight: float) -> None:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest, "little") % self.dimensions
        vector[index] += weight
