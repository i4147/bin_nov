#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations
import site
import sys


print("Python:", sys.executable)
print("user base:", site.getuserbase())
print("user site-packages:", site.getusersitepackages())
print("user site enabled:", site.ENABLE_USER_SITE)
print("\nsys.path:")
print("\n".join(sys.path))
