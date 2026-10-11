#pragma once

#include "../state-engine/kst.hpp"

extern "C" {
void hist_init(const WCHAR *pages_path, u32 maxticks);
void hist_page(u64 tick, u32 vaddr, u8 mclass, u16 prot, u16 form, const u8 key[16], const void *data);
void hist_tick_end(u64 tick);
void hist_truncate(u64 tick);
u32 hist_write_w(u64 target, const WCHAR *path, u32 *data_pages);
u32 hist_npages(void);
u64 hist_bytes(void);
}
