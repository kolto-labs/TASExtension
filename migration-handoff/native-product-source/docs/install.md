# Install Telos

The Python command line installs on its own. Hyperstim is optional.

```sh
python -m pip install .
telos --help
```

For the desktop controls, install the overlay extra:

```sh
python -m pip install '.[overlay]'
```

Python 3.11 or newer is required. The base command line uses the standard
library. The desktop overlay uses PySide6. Recording videos also needs FFmpeg.

The wheel contains the command line and default K1 keymap. It does not contain
KotOR, game assets, Wine, or a prebuilt native shim. To drive the game, configure
your own game installation and a qualified `dinput8.dll` in `telos.toml`, including
its SHA-256. Keep the DLL used to create a holorecord; a newer DLL cannot silently
replace its recorded environment.

Build the Python wheel with `python -m build --wheel`. Build the native shim
separately with `sh native/shim/build.sh` after installing its documented Zig
toolchain. A successful build alone does not qualify a native restore profile.

Scores can use `@include k1-keymap.score` after installation. Project include
directories take precedence over the bundled file, so custom bindings remain
under the author's control.

K1 native restore is being qualified against specific builds and environments.
K2 does not yet have an accepted native shim profile. Browser holorecord
import/export preserves the recording; it does not execute native game restores.
