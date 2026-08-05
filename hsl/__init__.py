"""hsl -- launch Houdini Solaris renders from a .hip file.

Two halves, deliberately kept apart:

  * ``hsl.inspector`` imports ``hou`` and must run under hython.
  * everything else runs in plain Python and talks to the inspector over JSON.
"""

__version__ = "0.3.0"

from .manifest import SceneManifest  # noqa: F401
