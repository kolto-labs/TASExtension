#pragma once
#include "shim.hpp"

extern "C" void wire_physical_observe(const MSG *host_message);

namespace telos {

// Host state belongs outside shim_hg: restoring a game must not restore the
// physical keyboard, focus, tool mode or an unconsumed host event. One native
// gate thread owns this object. It never calls game code or admits a frame.
class PhysicalInputCollector final {
public:
    enum class Status { ready, inactive, overflow, stale };
    struct Prepared {
        input_t input{};
        u64 revision{};
        u32 consumed{};
        i32 wheel_remainder{};
        bool altf4{};
        Status status{Status::inactive};
    };

    // The owner supplies the validated REAL game HWND, not a virtual alias.
    // Call only at the acknowledged input-source handoff, after draining host
    // events. Enabling quarantines all observed held controls until release.
    void bind(HWND real_window);
    Status set_enabled(bool enabled);
    void set_focus(bool focused);
    // A verified restore invalidates unconsumed physical events. The owner
    // parks and stages neutral; held host controls remain quarantined.
    void discard_pending();
    void suppress_key(u8 dik);
    void observe(const MSG &host_message);
    Prepared prepare() const;
    // Commit only AFTER di_can_stage succeeds and di_stage commits this exact
    // candidate. A stale candidate must never be staged. The owning gate must
    // not pump/observe between prepare and stage/commit.
    Status commit(const Prepared &prepared);
    Status status() const;

private:
    enum class Kind : u8 { key, button, move, wheel };
    struct Event { Kind kind; u8 control; bool down; i32 x; i32 y; bool altf4{}; };
    static constexpr u32 capacity = 128;
    HWND window_{};
    bool enabled_{}, focused_{}, overflow_{};
    u8 observed_keys_[32]{}, suppressed_keys_[32]{};
    u8 observed_buttons_{}, suppressed_buttons_{};
    Event events_[capacity]{};
    u32 head_{}, count_{};
    u64 revision_{1};
    input_t delivered_{};
    i32 wheel_remainder_{};
    void push(Event event);
    void neutralize();
};

} // namespace telos
