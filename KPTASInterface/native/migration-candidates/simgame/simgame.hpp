#pragma once

#include <windows.h>
#include <stdint.h>

#include "../state-engine/kst.hpp"

/* Reuse the staged history module; both candidate headers share the staged
 * state-engine ABI rather than depending on active C source headers. */
#include "../other/hist.hpp"

extern "C" {
void __cdecl simgame_entry(void);
}
