#pragma once

class KThread;

namespace TelosControl {
enum class State : unsigned { Running = 0, Pausing = 1, Paused = 2, Stepping = 3 };

// Stable bit positions for the browser TAS input mask. Keep these values in
// sync with the canonical key-token list accepted by the page bridge.
enum class InputKey : unsigned {
    W, A, S, D, Q, E, Space, Tab, Escape, Enter,
    Up, Down, Left, Right,
    Num1, Num2, Num3, Num4, Num5, Num6, Num7, Num8, Num9,
    I,
    Count
};

// The multi-threaded BoxedWine runtime calls these at guest-dispatch and
// kernel-wait boundaries. A thread counts as parked only after it reaches one
// of those boundaries; thread creation and exit remain visible to the barrier
// until the worker is actually registered or unregistered. This is an
// execution barrier, not a savestate, and it does not identify a KotOR frame.
void registerThread(KThread* thread);
void unregisterThread(KThread* thread);
void requestThreadTermination(KThread* thread);
void enterKernelWait(KThread* thread);
void leaveKernelWait(KThread* thread);
void guestDispatchBoundary(KThread* thread);
bool pauseRequested();
#if defined(KPTAS_DISPATCH_TRACE)
void beginGuestDispatch(KThread* thread, unsigned eip);
// tableIndex is UINT_MAX for a sparse interpreter-PC sample; otherwise it is
// the compiled WASM table slot that began execution.
void noteGuestJitBlock(KThread* thread, unsigned eip, unsigned tableIndex);
void endGuestDispatch(KThread* thread);
#endif
void notePresent(KThread* thread);
// Input-batch status codes are returned unchanged through the C exports:
// begin: 0 accepted, 1 invalid (zero) sequence, 2 already open,
//        3 prior batch still has queued events, 4 stale/replayed sequence.
// end:   0 closed and all tagged events consumed, 1 no matching open batch,
//        2 closed while tagged events remain queued, 3 closed with no events.
unsigned beginInputDispatch(unsigned sequence);
unsigned endInputDispatch(unsigned sequence);
unsigned tagInputEvent(unsigned displayId, unsigned eventSerial);
void consumeInputEvent(unsigned displayId, unsigned eventSerial);
}

extern "C" {
// pause(): 1 while workers are reaching safe points, 2 if already quiescent.
// step_dispatch(): 0 accepted, 1 not fully paused, 2 another step is active,
//                  3 no runnable guest worker is parked at a dispatch point.
// step_present(): 0 accepted, 1 not fully paused, 2 another step is active,
//                 3 no guest worker can produce a presentation.
unsigned telos_vm_pause();
unsigned telos_vm_resume();
unsigned telos_vm_step_dispatch();
unsigned telos_vm_step_present();
// Score-only: wait for this native batch consumption, then the next GL present.
// Same 0/1/2/3 results as step_present; 4 refuses an invalid batch sequence.
unsigned telos_vm_input_present_version();
unsigned telos_vm_step_input_present(unsigned sequence);
unsigned telos_vm_status();
unsigned telos_vm_active_threads();
unsigned telos_vm_parked_threads();
// Five control-map lifecycle counts, captured together; no CPU/memory snapshot.
unsigned telos_vm_copy_thread_state_counts(unsigned* output, unsigned capacity);
// Same snapshot in five 12-bit fields; all-one bits refuse overflow/unavailable.
unsigned long long telos_vm_thread_state_counts();
// Paused-only rows [thread ID, process ID, state: 2 KernelWait or 3 Paused].
// Returns UINT_MAX on invalid buffer, incomplete capacity, or a lost barrier.
// No registers, host pointers, instruction location, or wait cause are exposed.
unsigned telos_vm_copy_paused_threads(unsigned* output, unsigned capacity);
// Diagnostic build only: write up to capacity records [guest thread ID, guest
// EIP, dispatch age in ms, kernel-wait flag, process ID, EAX, EBX, ECX, EDX,
// ESP] for long-running
// dispatches or guest threads blocked in a kernel wait for >= 1000 ms.
unsigned telos_vm_copy_stalled_dispatches(unsigned* output, unsigned capacity);
// Diagnostic-only rows [thread ID, process ID, dispatch-entry EIP, latest
// execution sample EIP, table index (UINT_MAX means interpreter), sample
// sequence, dispatch age ms, kernel-wait flag].
unsigned telos_vm_copy_jit_dispatch_progress(unsigned* output, unsigned capacity);
// Browser-facing diagnostics use a fixed scratch buffer, avoiding allocation
// while a worker may be unable to reach the pause barrier. UI owner only.
unsigned telos_vm_dispatch_progress_version();
unsigned telos_vm_sample_dispatch_progress();
const unsigned* telos_vm_dispatch_progress_buffer();
// Diagnostic-only rows [thread ID, process ID, state (Starting, Running,
// KernelWait, Paused, Exiting), in-dispatch flag, dispatch-entry EIP,
// latest block EIP, table index, block sequence, dispatch age ms].
unsigned telos_vm_copy_thread_states(unsigned* output, unsigned capacity);
// Copy the mapped image name for a guest address. Returns copied bytes.
unsigned telos_vm_copy_module_name(unsigned pid, unsigned eip, char* output, unsigned capacity);
// Process IDs are enumerated in hash-table order; zero means the index is invalid.
unsigned telos_vm_process_count();
// Complete selected-PID saved x86 register capture under the pause barrier.
// Up to 128 rows, 12 U32 words: tid,pid,state,eip,eax,ecx,edx,ebx,esp,ebp,esi,edi.
// UINT_MAX means refused; zero means this process has no registered workers.
unsigned telos_vm_copy_paused_registers(unsigned pid, unsigned* output, unsigned capacity);
unsigned telos_vm_process_id_at(unsigned index);
// Fully paused X11 presented-GL-window display owner PID. Zero means unknown,
// UINT_MAX means barrier refused. Attribution only, not Windows image identity.
unsigned telos_vm_paused_presentation_owner();
// Copy executable basename while fully paused; UINT_MAX means refused.
// Capacity includes the terminator, ranges from 2 to 1025; no partial name.
unsigned telos_vm_copy_paused_process_name(unsigned pid, char* output, unsigned capacity);
unsigned telos_vm_copy_process_exe_name(unsigned pid, char* output, unsigned capacity);
unsigned telos_vm_copy_process_command_line(unsigned pid, char* output, unsigned capacity);
unsigned telos_vm_copy_fd_name(unsigned pid, unsigned fd, char* output, unsigned capacity);
unsigned telos_vm_module_file_offset(unsigned pid, unsigned eip);
// read(): 0 success, 1 not fully paused, 2 unknown PID, 3 invalid range or buffer,
//          4 unreadable guest range, 5 request exceeds 64 KiB.
unsigned telos_memory_read(unsigned pid, unsigned address, unsigned length, void* output);
// Number of host GL swap/present calls observed. This is not a frame clock.
unsigned telos_vm_present_count_low();
unsigned telos_input_begin(unsigned sequence);
unsigned telos_input_end(unsigned sequence);
unsigned telos_input_last_sequence();
unsigned telos_input_last_event_count();
unsigned telos_input_last_present_low();
// Copy nine input bookkeeping words under the complete pause barrier:
// active sequence, generated events, consumed events, open flag, last started
// sequence, last completed sequence, last completed event count/present,
// tracked queued events. 0 success; 1 boundary unavailable; 2 invalid buffer;
// 3 inconsistent bookkeeping. This is X-event telemetry, not game input proof.
unsigned telos_vm_copy_paused_input_state(unsigned* output, unsigned capacity);
#if defined(__EMSCRIPTEN__)
// Apply the desired held state in one validated operation. Key bits use
// TelosControl::InputKey positions; button bit 0 is left and bit 1 is right.
// mousePresent is 0/1 and coordinates are canvas pixels in [0, 9999]. The
// caller supplies one writable unsigned for the number of transitions sent.
// Returns 0 when all generated events were already consumed, 1-4 for
// begin-sequence rejection, 10 for invalid input, 11 when no native target
// exists, 12 on close failure, 13 while generated events await guest
// consumption, 14 when only mouse position changed without a queued X event,
// or 15 when key/button transitions generated no guest-consumable event.
unsigned telos_input_apply(unsigned sequence, unsigned keysMask, unsigned buttonsMask,
                           unsigned mousePresent, unsigned mouseX, unsigned mouseY,
                           unsigned* eventsDispatched);
// Stable prefix length of the compiled input-key table, for page negotiation.
unsigned telos_input_key_count();
// Best-effort release for browser blur/page teardown. This is deliberately
// unsequenced; ordinary score playback must use telos_input_apply().
unsigned telos_input_release_all();
#endif
}
