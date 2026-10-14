"""Open a configurable overlay or workspace view of the existing session owner."""
from __future__ import annotations

import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=47610)
    parser.add_argument('--display')
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--draft-path')
    parser.add_argument('--ui-config', help='versioned UI preferences TOML, separate from the run plan')
    parser.add_argument('--layout', choices=('overlay', 'workspace'), help='override the UI presentation preset')
    parser.add_argument('--interface', choices=('saved', 'visible', 'hidden'), default='saved',
                        help='start with Stasis shown, hidden, or as last toggled (default)')
    args = parser.parse_args(argv)
    if args.display:
        os.environ['DISPLAY'] = args.display
    os.environ['KPTAS_UI_INTERFACE'] = args.interface
    from .workspace_session import WorkspaceController
    from .workspace_window import run_workspace
    return run_workspace(WorkspaceController(args.host, args.port, args.timeout), draft_path=args.draft_path, ui_config=args.ui_config, layout=args.layout)


if __name__ == '__main__':
    raise SystemExit(main())
