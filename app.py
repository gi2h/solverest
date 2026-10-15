#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Railway entry point.
Semua logic ada di turnstile_solver.py (FastAPI app self-contained).
"""

import os
import uvicorn
from turnstile_solver import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
    )
