"""Server-side image variant generation for cover photos.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: src/images/__init__.py
Deps:   n/a (package marker)
Tested: tests/test_cover_image_perf_contract.py

Key responsibilities:
- Package namespace for src/images/variants.py.

Design constraints:
- No package-level state or side effects — import cost stays at zero until
  a caller reaches into variants.py.
"""

from __future__ import annotations
