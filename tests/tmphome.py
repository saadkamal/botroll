"""Imported by every test module: HOME points at a throwaway dir, so no test reads or writes the real ~/.botroll."""
import atexit
import os
import shutil
import tempfile

HOME = os.path.realpath(tempfile.mkdtemp(prefix="botroll-home-"))
os.environ["HOME"] = HOME
atexit.register(shutil.rmtree, HOME, True)
