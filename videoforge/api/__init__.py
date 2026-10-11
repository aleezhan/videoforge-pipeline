"""HTTP layer of the control panel: pipeline files -> JSON for the browser.

The API keeps no state of its own: every request reads the disk with the same
functions the CLI uses. The panel is a viewer and console on top of episode,
resource, library and account files, which remain the single source of truth.
"""
