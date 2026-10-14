# Stasis core: C++ port of the Python side (started 2026-10-04 22:45)

Goal (owner directive): no Python anywhere in releases. Ship only native .dll (Windows) and .so (Linux).
Every Python module under src/kptas is ported to C++17 with identical behaviour and identical file formats.

Rules for every port agent:
- One subsystem per agent, its own folder src/native/core/<subsystem>/, namespace stasis::<subsystem>.
- Public header <subsystem>/<subsystem>.hpp; implementation .cpp files; a CMakeLists-free build line in <subsystem>/BUILD.txt
  (g++ -std=c++17 -O2 -fPIC -c ... then ar rcs lib<subsystem>.a).
- Port faithfully: same names (snake_case -> snake_case), same validation messages, same on-disk formats byte for byte.
- Only the C++ standard library plus header-only libs vendored into src/native/core/third_party/ (toml++ for TOML, if needed).
- Cross-subsystem types: declare what you need in your header; list dependencies in <subsystem>/DEPS.md.
- No unit tests (owner rule). Verify by compiling (g++ -fsyntax-only / building the .a) and by a small CLI driver that
  reproduces a real CLI command of the Python version, compared against the Python output on real files.
- Write PROGRESS.md in your folder: modules ported, lines, what's left, exact Python file:function coverage.
