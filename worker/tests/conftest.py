import importlib.util
import os
import sys
import types

os.environ.setdefault("DATABASE_URL", "postgresql://test@localhost/test")
os.environ.setdefault("INTERNAL_TOKEN", "test")
os.environ.setdefault("PUBLIC_URL", "https://ms.example")

if importlib.util.find_spec("torch") is None:
    stub = types.ModuleType("memesearch.embeddings")
    stub.Embedder = stub.AudioTagger = object
    sys.modules["memesearch.embeddings"] = stub
