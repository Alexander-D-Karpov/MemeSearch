import asyncio
import threading
import time

import numpy as np

from memesearch import api
from memesearch.api import MemeIn, QueryIn


class SlowEmbedder:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def text_passage(self, text):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(0.3)
        with self.lock:
            self.running -= 1
        return np.zeros(4, dtype=np.float32)

    def clip_text(self, q):
        return np.zeros(4, dtype=np.float32)

    def text_query(self, q):
        return np.zeros(4, dtype=np.float32)


def test_search_queries_skip_the_background_queue():
    emb = SlowEmbedder()

    async def run():
        api.state.embedder = emb
        api.state.background = asyncio.Semaphore(1)
        jobs = [asyncio.create_task(api.embed_meme(MemeIn(image_paths=[], text=f"meme {i}"))) for i in range(3)]
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await api.embed_query(QueryIn(q="кот"))
        query_took = time.monotonic() - start
        await asyncio.gather(*jobs)
        return query_took

    query_took = asyncio.run(run())
    assert query_took < 0.2
    assert emb.peak == 1
