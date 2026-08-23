"""进程内指标计数器（线程安全），供监控看板读取。

用于统计 API 错误/限流次数等运行时指标。进程重启会清零，
看板与 app 同进程运行，可直接读到同一份计数。
"""
import threading

_lock = threading.Lock()
_counters = {}


def inc(key, n=1):
    with _lock:
        _counters[key] = _counters.get(key, 0) + n


def snapshot():
    with _lock:
        return dict(_counters)
