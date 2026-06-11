import os
import sys
import logging
import threading
import time
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from typing import Optional

# ... (other imports and code remain unchanged)

class _TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            refill = elapsed * self.rate
            if refill > 0:
                self.tokens = min(self.capacity, self.tokens + refill)
                self.last_refill = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

# ... (rest of the file remains unchanged)
