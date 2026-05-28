from __future__ import annotations

import os
from groq import AsyncGroq


class GroqKeyRotator:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("Groq API 키가 최소 1개 필요합니다.")
        self._keys = keys
        self._index = 0

    @property
    def current_key(self) -> str:
        return self._keys[self._index]

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def rotate(self) -> None:
        self._index = (self._index + 1) % len(self._keys)

    def make_client(self) -> AsyncGroq:
        return AsyncGroq(api_key=self.current_key)

    @classmethod
    def from_key_string(cls, key_string: str) -> "GroqKeyRotator":
        keys = [k.strip() for k in key_string.split(",") if k.strip()]
        if not keys:
            raise ValueError("유효한 Groq API 키가 없습니다.")
        return cls(keys)

    @classmethod
    def from_env(cls) -> "GroqKeyRotator":
        key_string = os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")
        return cls.from_key_string(key_string)
