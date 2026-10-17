"""Lets the test modules be run by dotted name too (python -m unittest tests.test_serve), not only through
discover -s tests: put this folder on sys.path so `import tmphome` (and the fixtures path) resolve."""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
