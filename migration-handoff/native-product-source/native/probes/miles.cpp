// Miles stream probe: what the real Miles 6.5h reports for the game's streams.
// Console exe, 32-bit. Run in the game directory (Mss32.dll and Miles\ there)
// with an audio device; reads one stream file path per line from stdin and
// prints, tab-separated: path, offset, length, extension, status after open,
// total ms, and whether Miles left the file handle open after close_stream.
// With the argument "play" it also starts each stream and prints how many ms
// of real time pass until its status leaves SMP_PLAYING (4).
//
// Each file is handed over as the game does it (CExoStreamingSoundSource-
// Internal::InitializeSource 0x5dbd30): a file handle positioned at the data,
// in the "\\\\<handle>,<length>.<ext>" form. A RIFF chunk shorter than the file
// is a header in front of MP3 data; a file without RIFF at 0 but with RIFF at
// 470 is a WAV behind a 470-byte prefix; anything else is skipped.

#include <windows.h>
#include <stdio.h>
#include <string.h>

// Dormant source only: preserve C entry/export and assembly symbol linkage.
extern "C" {

typedef int(__stdcall *startup_t)(int, int, unsigned, int, int);
typedef void(__stdcall *handles_t)(void **, void **, void **);
typedef char *(__stdcall *redist_t)(const char *);
typedef void *(__stdcall *open_t)(void *, const char *, int);
typedef void(__stdcall *close_t)(void *);
typedef void(__stdcall *msp_t)(void *, int *, int *);
typedef int(__stdcall *status_t)(void *);
typedef void(__stdcall *start_t)(void *);

int main(int argc, char **argv) {
    int play = argc > 1 && !strcmp(argv[1], "play");
    HMODULE m = LoadLibraryA("Mss32.dll");
    if (!m) { fprintf(stderr, "Mss32.dll not found\n"); return 1; }
    redist_t redist = reinterpret_cast<redist_t>(GetProcAddress(m, "_AIL_set_redist_directory@4"));
    startup_t startup = reinterpret_cast<startup_t>(GetProcAddress(m, "_AIL_quick_startup@20"));
    handles_t handles = reinterpret_cast<handles_t>(GetProcAddress(m, "_AIL_quick_handles@12"));
    open_t open = reinterpret_cast<open_t>(GetProcAddress(m, "_AIL_open_stream@12"));
    close_t close = reinterpret_cast<close_t>(GetProcAddress(m, "_AIL_close_stream@4"));
    msp_t msp = reinterpret_cast<msp_t>(GetProcAddress(m, "_AIL_stream_ms_position@12"));
    status_t status = reinterpret_cast<status_t>(GetProcAddress(m, "_AIL_stream_status@4"));
    start_t start = reinterpret_cast<start_t>(GetProcAddress(m, "_AIL_start_stream@4"));
    if (!redist || !startup || !handles || !open || !close || !msp || !status) { fprintf(stderr, "imports missing\n"); return 1; }
    redist("Miles");
    int ok = startup(1, 0, 44100, 16, 2);
    void *dig = 0;
    handles(&dig, 0, 0);
    fprintf(stderr, "quick_startup -> %d, driver %p\n", ok, dig);
    if (!ok || !dig) return 2;
    char path[1024];
    while (fgets(path, sizeof path, stdin)) {
        path[strcspn(path, "\r\n")] = 0;
        if (!path[0]) continue;
        HANDLE h = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
        if (h == INVALID_HANDLE_VALUE) { printf("%s\t-\t-\t-\t-\tnofile\t-\n", path); continue; }
        DWORD size = GetFileSize(h, NULL), got = 0;
        unsigned char b[512];
        ReadFile(h, b, sizeof b, &got, NULL);
        DWORD off;
        const char *ext;
        if (got >= 8 && !memcmp(b, "RIFF", 4) && *(DWORD *)(b + 4) + 8 < size) { off = *(DWORD *)(b + 4) + 8; ext = ".mp3"; }
        else if (got >= 8 && !memcmp(b, "RIFF", 4)) { off = 0; ext = ".wav"; }
        else if (got >= 474 && !memcmp(b + 470, "RIFF", 4)) { off = 470; ext = ".wav"; }
        else { printf("%s\t-\t-\t-\t-\tskipped\t-\n", path); CloseHandle(h); continue; }
        SetFilePointer(h, (LONG)off, NULL, FILE_BEGIN);
        char name[64];
        snprintf(name, sizeof name, "\\\\\\\\%d,%lu%s", (int)(INT_PTR)h, (unsigned long)(size - off), ext);
        void *s = open(dig, name, 0);
        int total = -1, cur = -1, st = -1;
        double played = -1;
        if (s) {
            st = status(s);
            msp(s, &total, &cur);
            if (play && start) {
                LARGE_INTEGER f, t0, t1;
                QueryPerformanceFrequency(&f);
                start(s);
                QueryPerformanceCounter(&t0);
                do { Sleep(2); QueryPerformanceCounter(&t1); } while (status(s) == 4 && t1.QuadPart - t0.QuadPart < 600 * f.QuadPart);
                played = (double)(t1.QuadPart - t0.QuadPart) * 1000.0 / (double)f.QuadPart;
            }
            close(s);
        }
        DWORD flags = 0;
        int still = GetHandleInformation(h, &flags) ? 1 : 0;
        printf("%s\t%lu\t%lu\t%s\t%d\t%d\t%d", path, (unsigned long)off, (unsigned long)(size - off), ext, st, total, still);
        if (play) printf("\t%.1f", played);
        printf("\n");
        if (still) CloseHandle(h);
    }
    return 0;
}

} // extern "C"
