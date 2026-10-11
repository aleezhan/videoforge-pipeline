"""videoforge: a file-based pipeline for producing short and long-form video.

Package layout:

- ``core``: data contracts (storyboard, manifest), the ``Stage`` interface,
  the runner, ffmpeg helpers and measurements;
- ``stages``: one module per pipeline stage, registered by name;
- ``providers``: adapters for external services, each with a free mock;
- ``clips``: highlight selection from your own long-form videos;
- ``intel``: channel analysis (the ``analyze_channel`` workflow);
- ``api``: FastAPI backend of the local control panel;
- ``cli``: the ``videoforge`` command.
"""

__version__ = "0.1.0"
