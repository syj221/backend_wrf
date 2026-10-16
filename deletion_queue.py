"""One bounded FIFO for task and forcing deletion; never replay on restart."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def timings(plan, position=0):
    now=plan.get('finished_at',time.time())
    started=plan.get('started_at')
    return dict(queue_position=position,started_at=started,
                wait_seconds=round(max(0,(started or now)-plan.get('reconciliation_accepted_at',plan['created_at'])),1),
                execution_seconds=round(max(0,now-started),1) if started else 0)


class DeletionQueue:
    def __init__(self):
        self.worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='wrf-deletion')
        self.cleanup_worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix="wrf-space-cleanup")
        self._lock=threading.RLock()
        self._order=[]

    def position(self, identifier):
        with self._lock:
            return self._order.index(identifier) if identifier in self._order else 0

    def submit(self, identifier, callback, started):
        with self._lock:
            if identifier in self._order:
                return
            if len(self._order)>=8:
                raise ValueError('删除队列已满，请稍后再试')
            self._order.append(identifier)
            def run():
                try:
                    started(time.time())
                    callback()
                finally:
                    with self._lock:
                        self._order.remove(identifier)
            try:
                self.worker.submit(run)
            except Exception:
                self._order.remove(identifier)
                raise

    def cleanup_submit(self, callback):
        return self.cleanup_worker.submit(callback)

    def close(self):
        self.worker.shutdown(wait=True,cancel_futures=True)
        self.cleanup_worker.shutdown(wait=True,cancel_futures=True)
