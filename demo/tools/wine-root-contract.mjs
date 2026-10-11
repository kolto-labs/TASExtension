// Verified official TinyCore15 Wine11.0 web archive. No game assets.
export const WINE_ROOT = Object.freeze({
  sha256:'2d07dd116b32abfff40f8cb2fad79544f90c88b33f59aff23ea8c0e15aec2277',
  bytes:99556813,
  chunkBytes:8 * 1024 * 1024,
});

// Earlier local GL4ES menu candidate; retained alongside the patched root.
export const LOCAL_WINE_ROOTS = Object.freeze([WINE_ROOT, Object.freeze({
  sha256:'a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5',
  bytes:99450694, chunkBytes:8 * 1024 * 1024,
})]);
