from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from sentence_transformers import SentenceTransformer
from transformers import AutoModel, AutoProcessor

log = logging.getLogger(__name__)


def _features(out: object) -> torch.Tensor:
    if isinstance(out, torch.Tensor):
        return out
    for attr in ("pooler_output", "image_embeds", "text_embeds"):
        v = getattr(out, attr, None)
        if isinstance(v, torch.Tensor):
            return v
    raise TypeError(f"unexpected feature output {type(out)!r}")


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n[n == 0] = 1
    return v / n


class Embedder:
    def __init__(self, clip_model: str, text_model: str, threads: int, clip_dim: int, text_dim: int) -> None:
        torch.set_num_threads(max(1, threads))
        log.info("loading %s", clip_model)
        self.processor = AutoProcessor.from_pretrained(clip_model)
        self.clip = AutoModel.from_pretrained(clip_model).eval()
        log.info("loading %s", text_model)
        self.text = SentenceTransformer(text_model, device="cpu")
        self.lock = threading.Lock()
        self.clip_dim = clip_dim
        self.text_dim = text_dim
        probe = self.clip_text("test")
        if probe.shape[0] != clip_dim:
            raise RuntimeError(f"{clip_model} produces {probe.shape[0]}-d vectors, CLIP_DIM is {clip_dim}")
        probe = self.text_query("test")
        if probe.shape[0] != text_dim:
            raise RuntimeError(f"{text_model} produces {probe.shape[0]}-d vectors, TEXT_DIM is {text_dim}")

    @torch.inference_mode()
    def clip_images(self, paths: list[Path]) -> np.ndarray:
        images = []
        for p in paths:
            with Image.open(p) as im:
                images.append(im.convert("RGB"))
        with self.lock:
            inputs = self.processor(images=images, return_tensors="pt")
            feats = _features(self.clip.get_image_features(**inputs)).float().numpy()
        feats = _normalize(feats)
        mean = feats.mean(axis=0)
        return _normalize(mean[None, :])[0].astype(np.float32)

    @torch.inference_mode()
    def clip_text(self, text: str) -> np.ndarray:
        with self.lock:
            inputs = self.processor(
                text=[text.lower()], padding="max_length", max_length=64, truncation=True, return_tensors="pt"
            )
            feats = _features(self.clip.get_text_features(**inputs)).float().numpy()
        return _normalize(feats)[0].astype(np.float32)

    def text_query(self, text: str) -> np.ndarray:
        with self.lock:
            v = self.text.encode([f"query: {text}"], normalize_embeddings=True)
        return np.asarray(v[0], dtype=np.float32)

    def text_passage(self, text: str) -> np.ndarray:
        with self.lock:
            v = self.text.encode([f"passage: {text}"], normalize_embeddings=True)
        return np.asarray(v[0], dtype=np.float32)
