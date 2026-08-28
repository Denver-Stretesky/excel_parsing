"""Single source of truth for the app version.

Bump this when you cut a new build. It's read by:
  - app.py (window title)
  - app.spec (CFBundleShortVersionString in the .app's Info.plist)
  - the zip-filename one-liner in the README
"""

__version__ = "0.7.6"
