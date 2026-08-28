"""Helper modules for scripts/eval/corpus_gen.py.

Split out of the main generator so the generator module itself stays a thin
CLI + orchestration layer. Every name in this package is internal to the
corpus generator; nothing here is imported by application code.
"""
