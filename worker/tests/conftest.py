import os

os.environ.setdefault("DATABASE_URL", "postgresql://test@localhost/test")
os.environ.setdefault("INTERNAL_TOKEN", "test")
os.environ.setdefault("PUBLIC_URL", "https://ms.example")
