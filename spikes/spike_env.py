"""
Shared env setup for all spike scripts.
Must be imported BEFORE any SDK imports to unset CLAUDECODE.
"""
import os
# The SDK refuses to launch inside another Claude Code session.
# We unset this so spikes can run from within Claude Code.
os.environ.pop("CLAUDECODE", None)
