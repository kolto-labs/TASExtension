"""Directory durability admission, separate from an experimental Win32 probe.

A successful probe is an API result, not proof of power-loss durability.
On os.name == "nt" sync_dir uses the Win32 FlushFileBuffers directory flush, which is unvalidated.
"""
from pathlib import Path
import os

class DurabilityUnavailable(OSError):
    pass

def capability(platform=None):
    platform=os.name if platform is None else platform
    if platform=='nt':
        return {'platform':platform,'directory_barrier':True,
                'native_windows_validated':False,'power_loss_guaranteed':False,
                'reason':('Win32 FlushFileBuffers on a FILE_FLAG_BACKUP_SEMANTICS directory handle; '
                          'unvalidated, not a qualified power-loss barrier')}
    return {'platform':platform,'directory_barrier':platform=='posix',
            'native_windows_validated':False,'power_loss_guaranteed':False,
            'reason':('POSIX directory fsync required; filesystem/device contract still applies'
                      if platform=='posix' else 'No qualified Windows directory durability backend; strict saves refused')}

def require_directory_barrier(platform=None):
    c=capability(platform)
    if not c['directory_barrier']:raise DurabilityUnavailable(c['reason'])

def sync_dir(path):
    require_directory_barrier()
    if os.name=='nt':
        probe_windows_directory(path)   # Win32 FlushFileBuffers on the directory handle (unvalidated)
        return
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)

class _Win32:
    """Exact Windows ABI declarations; only instantiated on Windows."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c=c;self.k=c.WinDLL('kernel32',use_last_error=True)
        self.k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
        self.k.CreateFileW.restype=w.HANDLE
        self.k.FlushFileBuffers.argtypes=[w.HANDLE];self.k.FlushFileBuffers.restype=w.BOOL
        self.k.CloseHandle.argtypes=[w.HANDLE];self.k.CloseHandle.restype=w.BOOL
        self.invalid=c.c_void_p(-1).value
    def open(self,path):return self.k.CreateFileW(path,0x40000000,7,None,3,0x02000000,None)
    def flush(self,h):return self.k.FlushFileBuffers(h)
    def close(self,h):return self.k.CloseHandle(h)
    def error(self):return self.c.get_last_error()

def probe_windows_directory(path,*,api=None):
    """Explicit diagnostic only. Never enables or publishes a durable save.

    GENERIC_WRITE, SHARE_READ|WRITE|DELETE, OPEN_EXISTING, BACKUP_SEMANTICS.
    No elevation, volume-wide flush, path fallback, or swallowed native errors.
    """
    if api is None:
        if os.name!='nt':raise DurabilityUnavailable('Win32 probe requires native Windows')
        api=_Win32()
    path=os.fspath(path)
    if '\0' in path:raise ValueError('NUL in path')
    h=api.open(path)
    if h is None or h==api.invalid:
        raise OSError(api.error(),'CreateFileW directory probe failed',path)
    failure=None
    try:
        if not api.flush(h):failure=OSError(api.error(),'FlushFileBuffers directory probe failed',path)
    finally:
        if not api.close(h) and failure is None:failure=OSError(api.error(),'CloseHandle directory probe failed',path)
    if failure is not None:raise failure
    return {'api_flush_completed':True,'directory_durability_qualified':False,
            'power_loss_guaranteed':False,'save_published':False}
