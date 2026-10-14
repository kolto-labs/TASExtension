#ifndef BOXEDWINE_GL4ES_BRIDGE_H
#define BOXEDWINE_GL4ES_BRIDGE_H
#ifdef BOXEDWINE_GL4ES
#include "gl4esinit.h"
extern "C" {
void gl4es_Stub(void*, ...);
void* NewGLState(void*, int);
void ActivateGLState(void*);
void DeleteGLState(void*);
void gl4es_InitializeDefaultARBPrograms(void);
}
// The upstream lookup table also contains a generic, untyped no-op. It is
// not a translated implementation and cannot be called through Wasm typed
// dispatch. Never route a desktop call directly to raw GLES as a fallback.
inline void* boxedwineGL4ESProcAddress(const char* name) {
    void* result = gl4es_GetProcAddress(name);
    // Several legacy entry points have typed Emscripten handlers in glstub.c.
    // Some implement the available semantics (for example GetPolygonStipple's
    // default mask and texture residency); others validate and report the GL
    // error for operations this GLES context cannot perform. Their addresses
    // must remain callable so those handlers can provide their defined result.
    // Only the generic variadic fallback is an unimplemented dispatch target.
    bool stub = result == reinterpret_cast<void*>(&gl4es_Stub);
    if (stub) {
        kwarn_fmt("GL4ES translation gap: %s resolves to an unimplemented stub", name);
        return nullptr;
    }
    return result;
}
#endif
#endif
