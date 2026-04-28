from __future__ import annotations

from app.convex_pull_worker import ConvexPullWorker


if __name__ == "__main__":
    worker = ConvexPullWorker()
    worker.run_forever()
