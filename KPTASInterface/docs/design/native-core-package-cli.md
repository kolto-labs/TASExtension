# Native Core package-profile CLI

The native command line now exposes the Core package host through:

```sh
PYTHONPATH=KPTASInterface python3 -m kptas core list
PYTHONPATH=KPTASInterface python3 -m kptas core install telos.tas
PYTHONPATH=KPTASInterface python3 -m kptas core enable telos.tas
PYTHONPATH=KPTASInterface python3 -m kptas core disable telos.tas
PYTHONPATH=KPTASInterface python3 -m kptas core remove telos.tas
```

The profile is stored in `$XDG_CONFIG_HOME/telos/core-packages.json`, or
`~/.config/telos/core-packages.json` when `XDG_CONFIG_HOME` is unset. Use
`telos core --state PATH ACTION ...` to select a different profile.

This is package enrollment and desired-state management. `install` records the
registered first-party package in the profile; it does not download code.
`enable` records the request for the owning Telos Core application to activate
the package. The CLI has no live native workspace connection and therefore
does not claim that TAS is active. Package removal drops only profile
enrollment: code and user data remain on disk. This keeps executable code
trusted and explicitly registered rather than importing module paths from a
manifest.

The CLI's first registered package is `telos.tas`. Its disabled/uninstalled
default makes the optional-package boundary explicit for a new native profile.
The native workspace must still register a real TAS lifecycle adapter and its
`tas.runtime` Core service before package activation can happen there. Until
that application integration is complete, this CLI is useful for profile
management and inspection, not as a claim that the running native GUI can
unload its existing TAS implementation.
