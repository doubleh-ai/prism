"""结构化日志模块 — 按事件类型记录"""
import time, logging, functools

log = logging.getLogger("stock-app")

class Event:
    """日志事件构造器"""
    def __init__(self, action: str):
        self.data = {"action": action, "ts": int(time.time() * 1000)}

    def who(self, user: str) -> "Event":
        self.data["user"] = user; return self

    def what(self, **kw) -> "Event":
        self.data.update(kw); return self

    def how_long(self, t0: float) -> "Event":
        self.data["elapsed_ms"] = int((time.time() - t0) * 1000); return self

    def info(self):
        parts = [f"{k}={v}" for k, v in self.data.items()]
        log.info(" | ".join(parts))

    def warn(self):
        parts = [f"{k}={v}" for k, v in self.data.items()]
        log.warning(" | ".join(parts))

    def error(self):
        parts = [f"{k}={v}" for k, v in self.data.items()]
        log.error(" | ".join(parts))

    @staticmethod
    def request(user: str, skill: str, q: str):
        e = Event("chat_request")
        e.data.update({"user": user, "skill": skill, "q": q[:80]})
        return e

    @staticmethod
    def tool(user: str, name: str, detail: str):
        return Event("tool_call").who(user).what(name=name, detail=detail[:100])

    @staticmethod
    def claude(user: str, pid: int, duration: float, tokens: int, chunks: int):
        return Event("claude_done").who(user).what(pid=pid, duration_ms=duration, tokens=tokens, chunks=chunks)

    @staticmethod
    def auth(user: str, result: str):
        return Event("auth").who(user).what(result=result)

    @staticmethod
    def conv(user: str, action: str, cid: str = ""):
        return Event("conv").who(user).what(op=action, conv_id=cid[:12])

    @staticmethod
    def rate_hit(user: str, endpoint: str):
        return Event("rate_limit").who(user).what(endpoint=endpoint)


# 性能装饰器
def timed(what: str):
    """记录函数执行时间"""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            t0 = time.time()
            result = await fn(*a, **kw)
            ms = int((time.time() - t0) * 1000)
            log.info(f"perf | op={what} | elapsed_ms={ms}")
            return result
        return wrapper
    return deco
