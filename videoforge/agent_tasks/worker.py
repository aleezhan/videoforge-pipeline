"""A worker survives a UI server restart; state is always read from disk."""

import sys

from videoforge.agent_tasks.engine import run

if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
