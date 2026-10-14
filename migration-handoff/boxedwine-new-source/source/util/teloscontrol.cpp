#include "boxedwine.h"
#include "teloscontrol.h"
#include "../emulation/softmmu/kmemory_soft.h"
#include "../emulation/softmmu/soft_ram.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#if defined(__EMSCRIPTEN__)
#include <emscripten/emscripten.h>
#include "../x11/x11.h"
#define TELOS_DIAGNOSTIC_EXPORT EMSCRIPTEN_KEEPALIVE
#else
#define TELOS_DIAGNOSTIC_EXPORT
#endif

namespace {
enum class ThreadState : unsigned char { Starting, Running, KernelWait, Paused, Exiting };
constexpr unsigned kMaxInputEventsPerBatch = 64U;

struct ControlState {
    std::mutex mutex;
    std::condition_variable changed;
    std::unordered_map<KThread*, ThreadState> threads;
#if defined(KPTAS_DISPATCH_TRACE)
    struct DispatchTrace {
        U32 eip = 0;
        std::chrono::steady_clock::time_point startedAt;
        std::atomic<U32> blockSequence{0};
        std::atomic<U32> lastBlockEip{0};
        std::atomic<U32> lastTableIndex{0};
    };
    std::unordered_map<KThread*, std::unique_ptr<DispatchTrace>> runningDispatches;
#endif
    bool pauseRequested = false;
    KThread* stepOwner = nullptr;
    bool stepPermit = false;
    bool runUntilPresent = false;
    unsigned targetPresent = 0;
    unsigned inputPresentSequence = 0;
    std::atomic<unsigned> presents{0};
    unsigned inputSequence = 0;
    unsigned inputEvents = 0;
    unsigned inputConsumed = 0;
    bool inputOpen = false;
    unsigned lastStartedInputSequence = 0;
    bool hasStartedInputSequence = false;
    std::unordered_map<unsigned long long, unsigned> eventSequences;
    std::atomic<unsigned> lastInputSequence{0};
    std::atomic<unsigned> lastInputEventCount{0};
    std::atomic<unsigned> lastInputPresent{0};
};

thread_local unsigned currentInputSequence = 0;
thread_local KThread* fastRunningThread = nullptr;
#if defined(KPTAS_DISPATCH_TRACE)
using DispatchTrace = ControlState::DispatchTrace;
thread_local KThread* activeDispatchThread = nullptr;
thread_local DispatchTrace* activeDispatchTrace = nullptr;
#endif
std::atomic<bool> pauseRequestedFast{false};

ControlState& control() {
    static ControlState value;
    return value;
}

bool allParked(const ControlState& state) {
    for (const auto& entry : state.threads) {
        // Exiting is a request/state label, not proof that the host worker has
        // stopped. It must remain part of the barrier until unregisterThread()
        // removes it after execution and cleanup have actually finished.
        if (entry.second != ThreadState::KernelWait && entry.second != ThreadState::Paused) return false;
    }
    return true;
}

bool readResidentGuestMemory(KMemory& memory, U32 address, void* output, U32 length) {
    if (!output || !length || (U64)address + length > 0x100000000ULL) return false;
    KMemoryData* data = memory.getData();
    if (!data) return false;

    // The Wasm export runs on the browser event thread, not a guest KThread.
    // Preflight the complete range, accepting only readable pages with an
    // existing RAM backing. File/invalid/unmaterialized pages are rejected;
    // they must never fault in from this thread or borrow guest-thread state.
    U32 cursor = address;
    U32 remaining = length;
    while (remaining) {
        const U32 pageIndex = cursor >> K_PAGE_SHIFT;
        const U32 offset = cursor & K_PAGE_MASK;
        const U32 chunk = std::min(remaining, K_PAGE_SIZE - offset);
        MMU& mmu = data->mmu[pageIndex];
        const PageType type = mmu.getPageType();
        const RamPage ram = mmu.getRamPageIndex();
        if (!(mmu.flags & PAGE_READ) ||
            (type != PageType::Ram && type != PageType::Code && type != PageType::CopyOnWrite) ||
            !ram.value || !ramPageGet(ram)) return false;
        cursor += chunk;
        remaining -= chunk;
    }

    U8* destination = static_cast<U8*>(output);
    cursor = address;
    remaining = length;
    while (remaining) {
        const U32 pageIndex = cursor >> K_PAGE_SHIFT;
        const U32 offset = cursor & K_PAGE_MASK;
        const U32 chunk = std::min(remaining, K_PAGE_SIZE - offset);
        const RamPage ram = data->mmu[pageIndex].getRamPageIndex();
        ::memcpy(destination, ramPageGet(ram) + offset, chunk);
        destination += chunk;
        cursor += chunk;
        remaining -= chunk;
    }
    return true;
}

unsigned parkedCount(const ControlState& state) {
    unsigned count = 0;
    for (const auto& entry : state.threads) {
        if (entry.second == ThreadState::KernelWait || entry.second == ThreadState::Paused) ++count;
    }
    return count;
}

void waitIfPaused(KThread* thread, bool mayTakeStepPermit) {
    if (!thread) return;
    // ponytail: the lock/map/broadcast ceiling remains for pause and step transitions;
    // ordinary guest dispatch only needs this identity check and atomic load.
    if (thread == fastRunningThread && !pauseRequestedFast.load(std::memory_order_acquire)) return;
    ControlState& state = control();
    std::unique_lock<std::mutex> lock(state.mutex);
    auto it = state.threads.find(thread);
    if (it == state.threads.end()) { fastRunningThread = nullptr; return; }
    if (it->second == ThreadState::Exiting) { fastRunningThread = nullptr; return; }

    // Frame stepping temporarily releases all parked workers. The first GL
    // present closes that interval and sends every worker back to this gate.
    if (state.runUntilPresent) {
        it->second = ThreadState::Running;
        fastRunningThread = thread;
        state.changed.notify_all();
        return;
    }

    if (!state.pauseRequested) {
        it->second = ThreadState::Running;
        fastRunningThread = thread;
        state.changed.notify_all();
        return;
    }

    // A worker that just returned from its one permitted dispatch re-enters
    // the barrier here. Do not let a second worker consume the same permit.
    if (state.stepOwner == thread) {
        state.stepOwner = nullptr;
        it->second = ThreadState::Paused;
        fastRunningThread = nullptr;
        state.changed.notify_all();
    }
    while (state.pauseRequested) {
        it = state.threads.find(thread);
        if (it == state.threads.end()) { fastRunningThread = nullptr; return; }
        if (it->second == ThreadState::Exiting) { fastRunningThread = nullptr; return; }
        // step_present() leaves pauseRequested set and uses runUntilPresent
        // to release every worker already sleeping in this loop. Checking
        // only before entering the loop makes a wake ineffective: each worker
        // sees pauseRequested and immediately waits again, so no GL present
        // can ever close the step.
        if (state.runUntilPresent) {
            it->second = ThreadState::Running;
            fastRunningThread = thread;
            state.changed.notify_all();
            return;
        }
        if (mayTakeStepPermit && state.stepPermit && it->second == ThreadState::Paused) {
            state.stepPermit = false;
            state.stepOwner = thread;
            it->second = ThreadState::Running;
            fastRunningThread = thread;
            state.changed.notify_all();
            return;
        }
        // A spurious wake must not rebroadcast an unchanged paused state:
        // otherwise paused workers continually wake one another. Commands
        // still broadcast, and the first transition to Paused still does too.
        if (it->second != ThreadState::Paused) {
            it->second = ThreadState::Paused;
            state.changed.notify_all();
        }
        fastRunningThread = nullptr;
        state.changed.wait(lock);
    }
    it = state.threads.find(thread);
    if (it != state.threads.end()) {
        it->second = ThreadState::Running;
        fastRunningThread = thread;
    } else {
        fastRunningThread = nullptr;
    }
    state.changed.notify_all();
}
}

namespace TelosControl {
void registerThread(KThread* thread) {
    if (!thread) return;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    state.threads[thread] = ThreadState::Starting;
    state.changed.notify_all();
}

void unregisterThread(KThread* thread) {
    if (!thread) return;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    state.threads.erase(thread);
#if defined(KPTAS_DISPATCH_TRACE)
    state.runningDispatches.erase(thread);
#endif
    if (state.stepOwner == thread) state.stepOwner = nullptr;
    state.changed.notify_all();
}

void requestThreadTermination(KThread* thread) {
    if (!thread) return;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    auto it = state.threads.find(thread);
    if (it != state.threads.end()) it->second = ThreadState::Exiting;
    if (state.stepOwner == thread) state.stepOwner = nullptr;
    state.changed.notify_all();
}

void enterKernelWait(KThread* thread) {
    if (!thread) return;
    if (fastRunningThread == thread) fastRunningThread = nullptr;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    auto it = state.threads.find(thread);
    if (it != state.threads.end() && it->second != ThreadState::Exiting) it->second = ThreadState::KernelWait;
    state.changed.notify_all();
}

void leaveKernelWait(KThread* thread) {
    // A kernel wait may become eligible for a pause while it sleeps. Once it
    // wakes, gate it before it can return to guest execution. It cannot consume
    // a single-dispatch permit from inside an in-flight guest dispatch.
    // enterKernelWait() can be called by an SDL owner thread on behalf of a
    // guest caller blocked in synchronous proxying. fastRunningThread is
    // thread-local, so that call cannot invalidate the guest caller's cached
    // fast-path state. Clear it on the returning guest thread before checking
    // the barrier, otherwise it can remain marked KernelWait while executing.
    if (thread && fastRunningThread == thread) fastRunningThread = nullptr;
    waitIfPaused(thread, false);
}

void guestDispatchBoundary(KThread* thread) {
    waitIfPaused(thread, true);
}

bool pauseRequested() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    return state.pauseRequested;
}

#if defined(KPTAS_DISPATCH_TRACE)
void beginGuestDispatch(KThread* thread, U32 eip) {
    if (!thread) return;
    auto trace = std::make_unique<ControlState::DispatchTrace>();
    trace->eip = eip;
    trace->startedAt = std::chrono::steady_clock::now();
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    DispatchTrace* stableTrace = trace.get();
    state.runningDispatches[thread] = std::move(trace);
    activeDispatchThread = thread;
    activeDispatchTrace = stableTrace;
}

void noteGuestJitBlock(KThread* thread, U32 eip, U32 tableIndex) {
    DispatchTrace* trace = activeDispatchTrace;
    if (!thread || activeDispatchThread != thread || !trace) return;
    const U32 sequence = trace->blockSequence.fetch_add(1U, std::memory_order_acq_rel);
    if (sequence & 1U) {
        // A single CPU worker owns this record; return to an even state if a
        // diagnostic interruption ever left a stale in-progress marker.
        trace->blockSequence.fetch_add(1U, std::memory_order_release);
        return;
    }
    trace->lastBlockEip.store(eip, std::memory_order_relaxed);
    trace->lastTableIndex.store(tableIndex, std::memory_order_relaxed);
    trace->blockSequence.fetch_add(1U, std::memory_order_release);
}

void endGuestDispatch(KThread* thread) {
    if (!thread) return;
    if (activeDispatchThread == thread) {
        activeDispatchThread = nullptr;
        activeDispatchTrace = nullptr;
    }
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    state.runningDispatches.erase(thread);
}
#endif

void notePresent(KThread* thread) {
    ControlState& state = control();
    const unsigned count = state.presents.fetch_add(1, std::memory_order_acq_rel) + 1;
    bool stopAtBoundary = false;
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        const unsigned consumedPresent = state.lastInputPresent.load(std::memory_order_relaxed);
        const unsigned afterConsumption = count - consumedPresent;
        const bool inputReady = state.inputPresentSequence &&
            state.lastInputSequence.load(std::memory_order_acquire) == state.inputPresentSequence &&
            afterConsumption > 0U && afterConsumption < 0x80000000U;
        if (state.runUntilPresent && (state.inputPresentSequence ? inputReady : count >= state.targetPresent)) {
            state.inputPresentSequence = 0;
            state.runUntilPresent = false;
            state.stepOwner = nullptr;
            state.stepPermit = false;
            state.pauseRequested = true;
            pauseRequestedFast.store(true, std::memory_order_release);
            stopAtBoundary = true;
            state.changed.notify_all();
        }
    }
    if (stopAtBoundary) waitIfPaused(thread, false);
}

unsigned beginInputDispatch(unsigned sequence) {
    currentInputSequence = 0;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!sequence) return 1U;
    if (state.inputOpen) return 2U;
    // Do not replace the only outstanding dispatch while X events from it are
    // still queued. Sequence arithmetic is modular so a full 32-bit counter
    // wrap remains valid, while duplicate and backwards values are rejected.
    if (state.inputSequence && state.inputEvents > state.inputConsumed) return 3U;
    if (state.hasStartedInputSequence) {
        const unsigned delta = sequence - state.lastStartedInputSequence;
        if (!delta || delta >= 0x80000000U) return 4U;
    }
    state.inputSequence = sequence;
    state.inputEvents = 0;
    state.inputConsumed = 0;
    state.inputOpen = true;
    state.lastStartedInputSequence = sequence;
    state.hasStartedInputSequence = true;
    currentInputSequence = sequence;
    return 0U;
}

unsigned endInputDispatch(unsigned sequence) {
    currentInputSequence = 0;
    if (!sequence) return 1U;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (state.inputSequence != sequence || !state.inputOpen) return 1U;
    state.inputOpen = false;
    // A zero-event dispatch is not evidence that the guest received input.
    // Leave the published acknowledgement untouched, then clear the empty
    // batch so it cannot block a later sequence.
    if (!state.inputEvents) {
        state.inputSequence = 0;
        return 3U;
    }
    if (state.inputEvents == state.inputConsumed) {
        state.lastInputEventCount.store(state.inputEvents, std::memory_order_relaxed);
        state.lastInputPresent.store(state.presents.load(std::memory_order_acquire), std::memory_order_relaxed);
        state.lastInputSequence.store(sequence, std::memory_order_release);
        state.inputSequence = 0;
        return 0U;
    }
    return 2U;
}

unsigned tagInputEvent(unsigned displayId, unsigned eventSerial) {
    if (!currentInputSequence) return 0;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.inputOpen || state.inputSequence != currentInputSequence) return 0;
    if (state.inputEvents >= kMaxInputEventsPerBatch) return 0;
    const unsigned long long eventKey = (static_cast<unsigned long long>(displayId) << 32) | eventSerial;
    state.eventSequences[eventKey] = currentInputSequence;
    ++state.inputEvents;
    return currentInputSequence;
}

void consumeInputEvent(unsigned displayId, unsigned eventSerial) {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    const unsigned long long eventKey = (static_cast<unsigned long long>(displayId) << 32) | eventSerial;
    auto found = state.eventSequences.find(eventKey);
    if (found == state.eventSequences.end()) return;
    const unsigned sequence = found->second;
    state.eventSequences.erase(found);
    if (state.inputSequence != sequence) return;
    ++state.inputConsumed;
    if (!state.inputOpen && state.inputConsumed >= state.inputEvents) {
        state.lastInputEventCount.store(state.inputEvents, std::memory_order_relaxed);
        state.lastInputPresent.store(state.presents.load(std::memory_order_acquire), std::memory_order_relaxed);
        state.lastInputSequence.store(sequence, std::memory_order_release);
        state.inputSequence = 0;
    }
}
}

extern "C" unsigned telos_vm_pause() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    state.pauseRequested = true;
    pauseRequestedFast.store(true, std::memory_order_release);
    state.runUntilPresent = false;
    state.inputPresentSequence = 0;
    state.stepPermit = false;
    state.stepOwner = nullptr;
    state.changed.notify_all();
    return allParked(state) ? 2U : 1U;
}

extern "C" unsigned telos_vm_resume() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    state.pauseRequested = false;
    pauseRequestedFast.store(false, std::memory_order_release);
    state.stepPermit = false;
    state.stepOwner = nullptr;
    state.runUntilPresent = false;
    state.inputPresentSequence = 0;
    state.changed.notify_all();
    return 0;
}

extern "C" unsigned telos_vm_step_present() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || !allParked(state)) return 1U;
    if (state.runUntilPresent || state.stepOwner || state.stepPermit) return 2U;
    // A presentation step cannot make progress until at least one runnable
    // worker is parked at a guest-dispatch boundary. Kernel-waiting workers
    // count as quiescent for pause, but cannot produce the requested present.
    bool hasRunnableWorker = false;
    for (const auto& entry : state.threads) {
        if (entry.second == ThreadState::Paused) {
            hasRunnableWorker = true;
            break;
        }
    }
    if (!hasRunnableWorker) return 3U;
    state.inputPresentSequence = 0;
    state.targetPresent = state.presents.load(std::memory_order_acquire) + 1U;
    state.runUntilPresent = true;
    state.changed.notify_all();
    return 0U;
}

// Score-only operation: presentations before event consumption do not finish
// the step. The browser reports them separately; none are simulation frames.
extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_input_present_version() { return 1U; }
extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_step_input_present(unsigned sequence) {
    if (!sequence) return 4U;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || !allParked(state)) return 1U;
    if (state.runUntilPresent || state.stepOwner || state.stepPermit) return 2U;
    if (state.inputOpen || state.lastStartedInputSequence != sequence ||
        !state.inputEvents || state.inputEvents > kMaxInputEventsPerBatch ||
        state.inputConsumed > state.inputEvents ||
        (state.inputSequence != sequence && state.lastInputSequence.load(std::memory_order_acquire) != sequence)) return 4U;
    bool hasRunnableWorker = false;
    for (const auto& entry : state.threads) {
        if (entry.second == ThreadState::Paused) { hasRunnableWorker = true; break; }
    }
    if (!hasRunnableWorker) return 3U;
    state.inputPresentSequence = sequence;
    state.runUntilPresent = true;
    state.changed.notify_all();
    return 0U;
}

extern "C" unsigned telos_vm_step_dispatch() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || !allParked(state)) return 1U;
    if (state.stepOwner || state.stepPermit) return 2U;
    for (const auto& entry : state.threads) {
        if (entry.second == ThreadState::Paused) {
            state.stepPermit = true;
            state.changed.notify_all();
            return 0U;
        }
    }
    // All workers may be in kernel waits. Do not wake one and pretend that
    // this was one CPU dispatch; wait for a runnable worker or resume normally.
    return 3U;
}

extern "C" unsigned telos_vm_status() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested) return static_cast<unsigned>(TelosControl::State::Running);
    if (state.runUntilPresent || state.stepOwner || state.stepPermit) return static_cast<unsigned>(TelosControl::State::Stepping);
    return allParked(state) ? static_cast<unsigned>(TelosControl::State::Paused)
                            : static_cast<unsigned>(TelosControl::State::Pausing);
}

extern "C" unsigned telos_vm_active_threads() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    return static_cast<unsigned>(state.threads.size());
}

extern "C" unsigned telos_vm_parked_threads() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    return parkedCount(state);
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_thread_state_counts(unsigned* output, unsigned capacity) {
    if (!output || capacity != 5U) return 0xffffffffU;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (state.threads.size() > 4096U) return 0xffffffffU;
    unsigned counts[5] = {};
    for (const auto& entry : state.threads) {
        const unsigned index = static_cast<unsigned>(entry.second);
        if (index >= 5U) return 0xffffffffU;
        ++counts[index];
    }
    std::copy(counts, counts + 5U, output);
    return 5U;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned long long telos_vm_thread_state_counts() {
    unsigned counts[5] = {};
    if (telos_vm_copy_thread_state_counts(counts, 5U) != 5U) return ~0ULL;
    unsigned total = 0;
    unsigned long long packed = 0;
    for (unsigned index = 0; index < 5U; ++index) {
        if (counts[index] > 4095U) return ~0ULL;
        total += counts[index];
        packed |= static_cast<unsigned long long>(counts[index]) << (index * 12U);
    }
    return total <= 4095U ? packed : ~0ULL;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_paused_threads(unsigned* output, unsigned capacity) {
    if (!output || !capacity || capacity > 4096U) return 0xffffffffU;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || state.runUntilPresent || state.stepOwner || state.stepPermit ||
        state.threads.empty() || !allParked(state) || state.threads.size() > capacity) return 0xffffffffU;
    unsigned copied = 0;
    for (const auto& entry : state.threads) {
        output[copied * 3U] = entry.first->id;
        output[copied * 3U + 1U] = entry.first->process ? entry.first->process->id : 0U;
        output[copied * 3U + 2U] = static_cast<unsigned>(entry.second);
        ++copied;
    }
    return copied;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_paused_registers(unsigned pid, unsigned* output, unsigned capacity) {
    if (!pid || !output || !capacity || capacity > 128U) return 0xffffffffU;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || state.runUntilPresent || state.stepOwner || state.stepPermit ||
        state.threads.empty() || !allParked(state)) return 0xffffffffU;
    KProcessPtr process = KSystem::getProcess(pid);
    if (!process || process->id != pid) return 0xffffffffU;
    unsigned copied = 0;
    for (const auto& entry : state.threads) {
        KThread* thread = entry.first;
        if (!thread->process || thread->process->id != pid) continue;
        if (!thread->cpu || copied == capacity) return 0xffffffffU;
        // The control mutex prevents kernel waiters from reopening guest
        // dispatch and protects parked thread lifetime. These are saved CPU
        // values; a kernel wait does not identify a wait cause or next opcode.
        CPU* cpu = thread->cpu;
        const unsigned base = copied * 12U;
        output[base] = thread->id;
        output[base + 1U] = pid;
        output[base + 2U] = static_cast<unsigned>(entry.second);
        output[base + 3U] = cpu->eip.u32;
        for (unsigned index = 0; index < 8U; ++index) output[base + 4U + index] = cpu->reg[index].u32;
        ++copied;
    }
    return copied;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_stalled_dispatches(unsigned* output, unsigned capacity) {
#if defined(KPTAS_DISPATCH_TRACE)
    if (!output || !capacity) return 0;
    capacity = std::min(capacity, 64U);
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    const auto now = std::chrono::steady_clock::now();
    unsigned copied = 0;
    for (const auto& entry : state.runningDispatches) {
        auto threadState = state.threads.find(entry.first);
        if (threadState == state.threads.end() || !entry.second ||
            (threadState->second != ThreadState::Running && threadState->second != ThreadState::KernelWait)) continue;
        const auto age = std::chrono::duration_cast<std::chrono::milliseconds>(now - entry.second->startedAt).count();
        if (age < 1000) continue;
        const unsigned base = copied * 10U;
        output[base] = entry.first->id;
        output[base + 1U] = entry.second->eip;
        output[base + 2U] = static_cast<unsigned>(std::min<long long>(age, 0xffffffffLL));
        output[base + 3U] = threadState->second == ThreadState::KernelWait ? 1U : 0U;
        output[base + 4U] = entry.first->process ? entry.first->process->id : 0U;
        // Kernel-waiting workers are stopped inside a blocking syscall. The
        // control mutex prevents leaveKernelWait() from reopening the guest
        // dispatch while this snapshot reads the saved syscall registers.
        if (threadState->second == ThreadState::KernelWait && entry.first->cpu) {
            CPU* cpu = entry.first->cpu;
            output[base + 5U] = cpu->reg[0].u32; // eax
            output[base + 6U] = cpu->reg[3].u32; // ebx
            output[base + 7U] = cpu->reg[1].u32; // ecx
            output[base + 8U] = cpu->reg[2].u32; // edx
            output[base + 9U] = cpu->reg[4].u32; // esp
        } else {
            std::fill(output + base + 5U, output + base + 10U, 0U);
        }
        if (++copied == capacity) break;
    }
    return copied;
#else
    (void)output;
    (void)capacity;
    return 0;
#endif
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_jit_dispatch_progress(unsigned* output, unsigned capacity) {
#if defined(KPTAS_DISPATCH_TRACE)
    if (!output || !capacity) return 0;
    capacity = std::min(capacity, 64U);
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    const auto now = std::chrono::steady_clock::now();
    unsigned copied = 0;
    for (const auto& entry : state.runningDispatches) {
        auto threadState = state.threads.find(entry.first);
        if (threadState == state.threads.end() || !entry.second) continue;
        const DispatchTrace& trace = *entry.second;
        U32 sequenceBefore = 0, sequenceAfter = 0, blockEip = 0, tableIndex = 0;
        bool consistentSnapshot = false;
        for (unsigned attempt = 0; attempt < 8U; ++attempt) {
            sequenceBefore = trace.blockSequence.load(std::memory_order_acquire);
            if (sequenceBefore & 1U) continue;
            blockEip = trace.lastBlockEip.load(std::memory_order_relaxed);
            tableIndex = trace.lastTableIndex.load(std::memory_order_relaxed);
            sequenceAfter = trace.blockSequence.load(std::memory_order_acquire);
            if (sequenceBefore == sequenceAfter) {
                consistentSnapshot = true;
                break;
            }
        }
        if (!consistentSnapshot) {
            // Diagnostics must not hold the browser-facing control mutex while
            // waiting on a hot worker. Expose the last atomic values as a
            // best-effort sample and let the next poll retry consistency.
            sequenceAfter = trace.blockSequence.load(std::memory_order_acquire) & ~1U;
            blockEip = trace.lastBlockEip.load(std::memory_order_relaxed);
            tableIndex = trace.lastTableIndex.load(std::memory_order_relaxed);
        }
        const auto age = std::chrono::duration_cast<std::chrono::milliseconds>(now - trace.startedAt).count();
        const unsigned base = copied * 8U;
        output[base] = entry.first->id;
        output[base + 1U] = entry.first->process ? entry.first->process->id : 0U;
        output[base + 2U] = trace.eip;
        output[base + 3U] = blockEip;
        output[base + 4U] = tableIndex;
        output[base + 5U] = sequenceAfter;
        output[base + 6U] = static_cast<unsigned>(std::min<long long>(age, 0xffffffffLL));
        output[base + 7U] = threadState->second == ThreadState::KernelWait ? 1U : 0U;
        if (++copied == capacity) break;
    }
    return copied;
#else
    (void)output;
    (void)capacity;
    return 0;
#endif
}

namespace {
unsigned browserDispatchProgress[64U * 8U] = {};
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_dispatch_progress_version() {
#if defined(KPTAS_DISPATCH_TRACE)
    return 1U;
#else
    return 0U;
#endif
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_sample_dispatch_progress() {
    return telos_vm_copy_jit_dispatch_progress(browserDispatchProgress, 64U);
}

extern "C" TELOS_DIAGNOSTIC_EXPORT const unsigned* telos_vm_dispatch_progress_buffer() {
    return browserDispatchProgress;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_thread_states(unsigned* output, unsigned capacity) {
#if defined(KPTAS_DISPATCH_TRACE)
    if (!output || !capacity) return 0;
    capacity = std::min(capacity, 64U);
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    const auto now = std::chrono::steady_clock::now();
    unsigned copied = 0;
    for (const auto& entry : state.threads) {
        const unsigned base = copied * 9U;
        const auto traceEntry = state.runningDispatches.find(entry.first);
        const bool inDispatch = traceEntry != state.runningDispatches.end() && traceEntry->second;
        output[base] = entry.first->id;
        output[base + 1U] = entry.first->process ? entry.first->process->id : 0U;
        output[base + 2U] = static_cast<unsigned>(entry.second);
        output[base + 3U] = inDispatch ? 1U : 0U;
        if (inDispatch) {
            const DispatchTrace& trace = *traceEntry->second;
            output[base + 4U] = trace.eip;
            output[base + 5U] = trace.lastBlockEip.load(std::memory_order_relaxed);
            output[base + 6U] = trace.lastTableIndex.load(std::memory_order_relaxed);
            output[base + 7U] = trace.blockSequence.load(std::memory_order_acquire) & ~1U;
            const auto age = std::chrono::duration_cast<std::chrono::milliseconds>(now - trace.startedAt).count();
            output[base + 8U] = static_cast<unsigned>(std::min<long long>(age, 0xffffffffLL));
        } else {
            std::fill(output + base + 4U, output + base + 9U, 0U);
        }
        if (++copied == capacity) break;
    }
    return copied;
#else
    (void)output;
    (void)capacity;
    return 0;
#endif
}

extern "C" unsigned telos_vm_process_count() { return KSystem::getProcessCount(); }

extern "C" unsigned telos_vm_process_id_at(unsigned index) { return KSystem::getProcessIdAt(index); }

// Attribution to the X11 display that owns the presented GL window only.
// This does not identify or hash a Windows executable and returns no names,
// command lines, paths or window titles.
extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_paused_presentation_owner() {
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || state.runUntilPresent || state.stepOwner || state.stepPermit ||
        state.threads.empty() || !allParked(state)) return 0xffffffffU;
#if defined(__EMSCRIPTEN__)
    XServer* server = XServer::getServer(true);
    if (!server) return 0;
    const XWindowPtr window = server->getFakeFullScreenWindow();
    if (!window || !window->mapped()) return 0;
    const DisplayDataPtr display = server->getDisplayDataById(window->displayId);
    if (!display || !display->processId) return 0;
    const KProcessPtr process = KSystem::getProcess(display->processId);
    if (!process || process->id != display->processId ||
        server->getFakeFullScreenWindow() != window || !window->mapped()) return 0;
    return process->id;
#else
    return 0;
#endif
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_paused_process_name(unsigned pid, char* output, unsigned capacity) {
    if (!pid || !output || capacity < 2U || capacity > 1025U) return 0xffffffffU;
    output[0] = '\0';
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || state.runUntilPresent || state.stepOwner || state.stepPermit ||
        state.threads.empty() || !allParked(state)) return 0xffffffffU;
    KProcessPtr process = KSystem::getProcess(pid);
    if (!process || process->id != pid) return 0xffffffffU;
    const std::string executable(process->exe.c_str(), process->exe.length());
    const auto separator = executable.find_last_of("/\\");
    const std::string name = executable.substr(separator == std::string::npos ? 0 : separator + 1);
    if (name.size() >= capacity) return 0xffffffffU;
    if (!name.empty()) std::memcpy(output, name.data(), name.size());
    output[name.size()] = '\0';
    return static_cast<unsigned>(name.size());
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_process_exe_name(unsigned pid, char* output, unsigned capacity) {
    if (!output || !capacity) return 0;
    output[0] = '\0';
    KProcessPtr process = KSystem::getProcess(pid);
    if (!process) return 0;
    const BString& name = process->exe;
    const unsigned length = static_cast<unsigned>(std::min<int>(name.length(), static_cast<int>(capacity - 1U)));
    if (length) std::memcpy(output, name.c_str(), length);
    output[length] = '\0';
    return length;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_process_command_line(unsigned pid, char* output, unsigned capacity) {
    if (!output || !capacity) return 0;
    output[0] = '\0';
    KProcessPtr process = KSystem::getProcess(pid);
    if (!process) return 0;
    const BString& commandLine = process->commandLine;
    const unsigned length = static_cast<unsigned>(std::min<int>(commandLine.length(), static_cast<int>(capacity - 1U)));
    if (length) std::memcpy(output, commandLine.c_str(), length);
    output[length] = '\0';
    return length;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_fd_name(unsigned pid, unsigned fd, char* output, unsigned capacity) {
    if (!output || !capacity) return 0;
    output[0] = '\0';
    KProcessPtr process = KSystem::getProcess(pid);
    if (!process) return 0;
    KFileDescriptorPtr descriptor = process->getFileDescriptor(fd);
    if (!descriptor || !descriptor->kobject) return 0;
    BString name = descriptor->kobject->selfFd();
    const unsigned length = static_cast<unsigned>(std::min<int>(name.length(), static_cast<int>(capacity - 1U)));
    if (length) std::memcpy(output, name.c_str(), length);
    output[length] = '\0';
    return length;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_module_name(unsigned pid, unsigned eip, char* output, unsigned capacity) {
    if (!output || !capacity) return 0;
    output[0] = '\0';
    KProcessPtr process = KSystem::getProcess(pid);
    if (!process) return 0;
    BString name = process->getModuleName(eip);
    const unsigned length = static_cast<unsigned>(std::min<int>(name.length(), static_cast<int>(capacity - 1U)));
    if (length) std::memcpy(output, name.c_str(), length);
    output[length] = '\0';
    return length;
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_module_file_offset(unsigned pid, unsigned eip) {
    KProcessPtr process = KSystem::getProcess(pid);
    return process ? process->getModuleEip(eip) : 0U;
}

// ABI v1 output: version, path bytes, mapped start, extent low/high,
// file-offset availability, file offset low/high. Offsets remain exact U64.
extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_paused_mapping_abi_version() {
#ifdef BOXEDWINE_MULTI_THREADED
    return 1U;
#else
    return 0U;
#endif
}

extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_paused_mapping(
        unsigned pid, unsigned address, char* path, unsigned capacity, unsigned* info) {
    if (!path || !info || capacity < 1U || capacity > 4097U) return 3U;
    path[0] = '\0';
    std::fill_n(info, 8, 0U);
#ifndef BOXEDWINE_MULTI_THREADED
    return 8U;
#else
    ControlState& state = control();
    std::unique_lock<std::mutex> paused(state.mutex, std::try_to_lock);
    if (!paused.owns_lock()) return 6U;
    if (!state.pauseRequested || state.runUntilPresent || state.stepOwner || state.stepPermit ||
            state.threads.empty() || !allParked(state)) return 1U;
    KProcessPtr process = KSystem::getProcess(pid);
    if (!pid || !process || process->id != pid || !process->memory) return 2U;
    MappedFilePtr mapping;
    const U32 captured = process->trySnapshotMappedFileForRange(address, 1U, mapping);
    if (captured == 1U) return 6U;
    if (captured == 2U) return 4U;
    if (captured == 3U) return 3U;
    if (captured != 0U || !mapping) return 7U;
    if (!mapping->file || !mapping->file->openFile || !mapping->file->openFile->node) return 4U;
    const U64 start = mapping->address;
    if (!mapping->len || start + mapping->len < start || start + mapping->len > 0x100000000ULL ||
            address < start || (U64)address >= start + mapping->len) return 3U;
    const BString& name = mapping->file->openFile->node->path;
    if (name.length() < 1) return 4U;
    const unsigned length = static_cast<unsigned>(name.length());
    if (length >= capacity) return 5U; // Never return a truncated identity.
    U64 offset = 0;
    const bool offsetAvailable = mapping->tryGetFileOffset((U64)address - start, offset);
    std::memcpy(path, name.c_str(), length);
    path[length] = '\0';
    info[0] = 1U;
    info[1] = length;
    info[2] = mapping->address;
    info[3] = static_cast<unsigned>(mapping->len);
    info[4] = static_cast<unsigned>(mapping->len >> 32U);
    info[5] = offsetAvailable ? 1U : 0U;
    info[6] = offsetAvailable ? static_cast<unsigned>(offset) : 0U;
    info[7] = offsetAvailable ? static_cast<unsigned>(offset >> 32U) : 0U;
    return 0U;
#endif
}

extern "C" unsigned telos_memory_read(unsigned pid, unsigned address, unsigned length, void* output) {
    if (length > 64U * 1024U) return 5U;
    if (!address || !length || !output || (U64)address + length > 0x100000000ULL) return 3U;

    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || state.runUntilPresent || state.stepOwner || state.stepPermit || !allParked(state)) return 1U;
    KProcessPtr process = KSystem::getProcess(pid);
    if (!process || process->id != pid || !process->memory) return 2U;
    if (!readResidentGuestMemory(*process->memory, address, output, length)) return 4U;
    return 0U;
}

extern "C" unsigned telos_vm_present_count_low() {
    return control().presents.load(std::memory_order_relaxed);
}

extern "C" unsigned telos_input_begin(unsigned sequence) { return TelosControl::beginInputDispatch(sequence); }
extern "C" unsigned telos_input_end(unsigned sequence) { return TelosControl::endInputDispatch(sequence); }
extern "C" unsigned telos_input_last_sequence() { return control().lastInputSequence.load(std::memory_order_acquire); }
extern "C" unsigned telos_input_last_event_count() { return control().lastInputEventCount.load(std::memory_order_relaxed); }
extern "C" unsigned telos_input_last_present_low() { return control().lastInputPresent.load(std::memory_order_relaxed); }
extern "C" TELOS_DIAGNOSTIC_EXPORT unsigned telos_vm_copy_paused_input_state(unsigned* output, unsigned capacity) {
    if (!output || capacity != 9U) return 2U;
    ControlState& state = control();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (!state.pauseRequested || state.runUntilPresent || state.stepOwner || state.stepPermit ||
        state.threads.empty() || !allParked(state)) return 1U;
    if (state.inputEvents > kMaxInputEventsPerBatch || state.inputConsumed > state.inputEvents ||
        state.eventSequences.size() > kMaxInputEventsPerBatch) return 3U;
    const unsigned captured[9] = {
        state.inputSequence, state.inputEvents, state.inputConsumed, state.inputOpen ? 1U : 0U,
        state.lastStartedInputSequence, state.lastInputSequence.load(std::memory_order_acquire),
        state.lastInputEventCount.load(std::memory_order_relaxed),
        state.lastInputPresent.load(std::memory_order_relaxed),
        static_cast<unsigned>(state.eventSequences.size()),
    };
    std::copy(captured, captured + 9U, output);
    return 0U;
}
