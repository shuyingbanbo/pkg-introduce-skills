import logging
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("worker")

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
CANCEL_PORT = int(os.environ.get("CANCEL_PORT", 8080))

QUEUE_PREFIX = "queue:ai:"
ACTIVE_SET   = "queues:ai:active"
LOCK_PREFIX  = "lock:ai:"
JOB_PREFIX   = "job:ai:"
LOGS_PREFIX  = "logs:ai:"
LOCK_TTL     = 7200
# ROS 批量任务（依赖展开 + 串行构建）必然超过 2h，锁按 mode 延长，
# 否则多副本部署下锁过期会重复拉起同一 job
LOCK_TTL_ROS = 21600


def make_redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD or None, decode_responses=True)


def pick_next_job(r):
    """Fair-share: shuffle active project queues, pop one job each round."""
    projects = list(r.smembers(ACTIVE_SET))
    if not projects:
        return None, None
    random.shuffle(projects)
    for proj in projects:
        job_id = r.lpop(f"{QUEUE_PREFIX}{proj}")
        if job_id:
            if r.llen(f"{QUEUE_PREFIX}{proj}") == 0:
                r.srem(ACTIVE_SET, proj)
            return proj, job_id
    return None, None


def start_cancel_server():
    """启动取消任务的 HTTP server，监听 POST /cancel/<job_id>"""
    r = make_redis()

    class CancelHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.startswith("/cancel/"):
                job_id = self.path[len("/cancel/"):]
                job = r.hgetall(f"{JOB_PREFIX}{job_id}")
                if not job:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error": "job not found"}')
                    return
                r.hset(f"{JOB_PREFIX}{job_id}", "status", "failed")
                r.hset(f"{JOB_PREFIX}{job_id}", "error", "cancelled by user")
                import json as _j
                r.rpush(f"{LOGS_PREFIX}{job_id}",
                        _j.dumps({"msg": "Job cancelled by user", "done": True, "status": "failed"}))
                log.info("Job %s cancelled via HTTP", job_id)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": "cancelled"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # 静默 HTTP 访问日志

    server = HTTPServer(("0.0.0.0", CANCEL_PORT), CancelHandler)
    log.info("Cancel server listening on :%d", CANCEL_PORT)
    server.serve_forever()


def main():
    log.info("ai-importer-worker starting (redis=%s:%d)", REDIS_HOST, REDIS_PORT)
    r = make_redis()

    # 启动取消服务
    t = threading.Thread(target=start_cancel_server, daemon=True)
    t.start()

    while True:
        try:
            proj, job_id = pick_next_job(r)
            if not job_id:
                time.sleep(1)
                continue

            # 检查任务是否已被取消（排队中被取消的任务仍在队列中）
            job_meta = r.hgetall(f"{JOB_PREFIX}{job_id}")
            job_status = job_meta.get("status")
            if job_status == "cancelled":
                log.info("Job %s was cancelled while queued, skipping", job_id)
                continue

            # ROS 任务锁 TTL 按 mode 延长（批量 ROS 任务必然超过普通 2h）
            _ttl = LOCK_TTL_ROS if job_meta.get("mode") == "ros" else LOCK_TTL
            locked = r.set(f"{LOCK_PREFIX}{job_id}", "1", nx=True, ex=_ttl)
            if not locked:
                log.warning("Job %s lock conflict (should not happen), skipping", job_id)
                continue

            log.info("Starting job %s for project %s", job_id, proj)
            try:
                from job_runner import run_job
                run_job(r, proj, job_id)
            except Exception:
                log.exception("Job %s crashed", job_id)
                r.hset(f"{JOB_PREFIX}{job_id}", "status", "failed")
                import json as _j
                r.rpush(f"{LOGS_PREFIX}{job_id}",
                        _j.dumps({"msg": "Worker internal error", "done": True, "status": "failed"}))
            finally:
                r.delete(f"{LOCK_PREFIX}{job_id}")
                log.info("Released lock for %s", job_id)

        except redis.RedisError as exc:
            log.error("Redis error: %s — retry in 5s", exc)
            time.sleep(5)
            r = make_redis()
        except Exception:
            log.exception("Unexpected error in main loop")
            time.sleep(1)


if __name__ == "__main__":
    main()

