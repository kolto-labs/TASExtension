#include "physical_input.hpp"

namespace telos {
namespace {
bool bit(const u8 *keys, u8 key) { return (keys[key >> 3] & (1u << (key & 7))) != 0; }
void set_bit(u8 *keys, u8 key, bool down) {
    const auto mask = static_cast<u8>(1u << (key & 7));
    if (down) keys[key >> 3] |= mask;
    else keys[key >> 3] &= static_cast<u8>(~mask);
}
u8 dik(const MSG &message) {
    if (message.wParam == VK_PAUSE) return 0xc5;
    if (message.wParam == VK_SNAPSHOT) return 0xb7;
    const auto scan = static_cast<u8>((static_cast<u32>(message.lParam) >> 16) & 0x7f);
    return scan ? static_cast<u8>(scan | ((message.lParam & (1u << 24)) ? 0x80 : 0)) : 0;
}
}

void PhysicalInputCollector::neutralize() {
    // A neutral candidate releases the same captured virtual DI devices; do
    // not clear their buffers or manufacture a second input device here.
    head_ = count_ = 0;
    delivered_ = input_t{};
    wheel_remainder_ = 0;
    ++revision_;
}

void PhysicalInputCollector::bind(HWND real_window) {
    window_ = real_window;
    enabled_ = focused_ = overflow_ = false;
    memset(observed_keys_, 0, sizeof observed_keys_);
    memset(suppressed_keys_, 0, sizeof suppressed_keys_);
    observed_buttons_ = suppressed_buttons_ = 0;
    neutralize();
}

PhysicalInputCollector::Status PhysicalInputCollector::set_enabled(bool enabled) {
    if (enabled == enabled_) return status();
    if (enabled && overflow_) return Status::overflow;
    enabled_ = enabled;
    neutralize();
    memcpy(suppressed_keys_, observed_keys_, sizeof suppressed_keys_);
    suppressed_buttons_ = observed_buttons_;
    // Only a deliberate disable/re-enable recovers an overflow, after the
    // owner has reported lost host input and parked at a durable gate.
    if (!enabled) overflow_ = false;
    return status();
}

void PhysicalInputCollector::set_focus(bool focused) {
    if (focused == focused_) return;
    focused_ = focused;
    neutralize();
    memcpy(suppressed_keys_, observed_keys_, sizeof suppressed_keys_);
    suppressed_buttons_ = observed_buttons_;
}

void PhysicalInputCollector::discard_pending() {
    neutralize();
    memcpy(suppressed_keys_, observed_keys_, sizeof suppressed_keys_);
    suppressed_buttons_ = observed_buttons_;
}

void PhysicalInputCollector::suppress_key(u8 key) {
    if (!key) return;
    set_bit(suppressed_keys_, key, true);
    // Preserve unrelated queued host input. Only this tool key is removed;
    // invalidate prepared tickets because their input prefix changed.
    u32 kept=0;
    for(u32 i=0;i<count_;i++){
        Event event=events_[(head_+i)%capacity];
        if(event.kind==Kind::key&&event.control==key)continue;
        events_[(head_+kept)%capacity]=event;kept++;
    }
    count_=kept;
    set_bit(delivered_.keys,key,false);
    ++revision_;
}

void PhysicalInputCollector::push(Event event) {
    if (!enabled_ || !focused_ || overflow_) return;
    // A contiguous motion tail has only one observable absolute position at
    // the next admitted frame. Retain the last position without consuming a
    // bounded queue slot for every host mouse sample. Never merge across key,
    // button or wheel events; their ordering remains intact.
    if (event.kind == Kind::move && count_) {
        Event &tail = events_[(head_ + count_ - 1) % capacity];
        if (tail.kind == Kind::move) {
            if (tail.x != event.x || tail.y != event.y) {
                tail = event;
                // Unlike appending, replacing a tail may change an already
                // prepared prefix. Its ticket must then refuse before staging.
                ++revision_;
            }
            return;
        }
    }
    if (count_ == capacity) { overflow_ = true; ++revision_; return; }
    events_[(head_ + count_) % capacity] = event;
    ++count_;
    // Appending a tail does not alter a prepared prefix. Source/focus changes,
    // overflow and commit invalidate it; new host events wait for next frame.
}

void PhysicalInputCollector::observe(const MSG &message) {
    if (!window_ || message.hwnd != window_) return;
    const auto kind = message.message;
    if (kind == WM_KEYDOWN || kind == WM_SYSKEYDOWN ||
        kind == WM_KEYUP || kind == WM_SYSKEYUP) {
        const auto key = dik(message);
        if (!key) return;
        const bool down = kind == WM_KEYDOWN || kind == WM_SYSKEYDOWN;
        const bool was_down = bit(observed_keys_, key);
        set_bit(observed_keys_, key, down);
        if (bit(suppressed_keys_, key)) {
            if (!down) set_bit(suppressed_keys_, key, false);
            return;
        }
        // First-seen autorepeat after focus loss is not a new player press.
        if (down && !was_down && (message.lParam & (1u << 30))) {
            set_bit(suppressed_keys_, key, true);
            return;
        }
        if (down != was_down) push({Kind::key, key, down, 0, 0,
            down && kind == WM_SYSKEYDOWN && message.wParam == VK_F4 &&
            (message.lParam & (1u << 29)) != 0});
        return;
    }
    u8 button = 0xff;
    bool down = false;
    switch (kind) {
    case WM_LBUTTONDOWN: case WM_LBUTTONDBLCLK: button = 0; down = true; break;
    case WM_LBUTTONUP: button = 0; break;
    case WM_RBUTTONDOWN: case WM_RBUTTONDBLCLK: button = 1; down = true; break;
    case WM_RBUTTONUP: button = 1; break;
    case WM_MBUTTONDOWN: case WM_MBUTTONDBLCLK: button = 2; down = true; break;
    case WM_MBUTTONUP: button = 2; break;
    default: break;
    }
    if (button != 0xff) {
        const auto mask = static_cast<u8>(1u << button);
        const bool was_down = (observed_buttons_ & mask) != 0;
        if (down) observed_buttons_ |= mask;
        else observed_buttons_ &= static_cast<u8>(~mask);
        if (suppressed_buttons_ & mask) {
            if (!down) suppressed_buttons_ &= static_cast<u8>(~mask);
            return;
        }
        if (down != was_down) push({Kind::button, button, down,
              static_cast<i32>(static_cast<int16_t>(message.lParam & 0xffff)),
              static_cast<i32>(static_cast<int16_t>((static_cast<u32>(message.lParam) >> 16) & 0xffff))});
    } else if (kind == WM_MOUSEMOVE) {
        push({Kind::move, 0, false,
              static_cast<i32>(static_cast<int16_t>(message.lParam & 0xffff)),
              static_cast<i32>(static_cast<int16_t>((static_cast<u32>(message.lParam) >> 16) & 0xffff))});
    } else if (kind == WM_MOUSEWHEEL) {
        push({Kind::wheel, 0, false,
              static_cast<i32>(static_cast<int16_t>((message.wParam >> 16) & 0xffff)), 0});
    }
}

PhysicalInputCollector::Status PhysicalInputCollector::status() const {
    if (overflow_) return Status::overflow;
    return enabled_ ? Status::ready : Status::inactive;
}

PhysicalInputCollector::Prepared PhysicalInputCollector::prepare() const {
    Prepared result;
    result.status = status();
    result.revision = revision_;
    if (result.status != Status::ready) return result;
    result.input = delivered_;
    result.input.rel_x = result.input.rel_y = result.input.wheel = 0;
    result.wheel_remainder = wheel_remainder_;
    u8 changed_keys[32]{};
    u8 changed_buttons = 0;
    for (u32 i = 0; i < count_; ++i) {
        const auto &event = events_[(head_ + i) % capacity];
        // A press and release between boundaries become separate admitted
        // frames. Their physical subframe timing is intentionally unclaimed.
        if (event.kind == Kind::key) {
            if (bit(changed_keys, event.control)) break;
            set_bit(changed_keys, event.control, true);
            set_bit(result.input.keys, event.control, event.down);
            result.altf4 = result.altf4 || (event.altf4 &&
                (bit(result.input.keys, 0x38) || bit(result.input.keys, 0xb8)));
        } else if (event.kind == Kind::button) {
            const auto mask = static_cast<u8>(1u << event.control);
            if (changed_buttons & mask) break;
            changed_buttons |= mask;
            if (event.down) result.input.buttons |= mask;
            else result.input.buttons &= static_cast<u8>(~mask);
            result.input.has_abs = 1;
            result.input.abs_x = event.x;
            result.input.abs_y = event.y;
        } else if (event.kind == Kind::move) {
            result.input.has_abs = 1;
            result.input.abs_x = event.x;
            result.input.abs_y = event.y;
        } else {
            const auto amount = result.wheel_remainder + event.x;
            result.input.wheel += amount / WHEEL_DELTA;
            result.wheel_remainder = amount % WHEEL_DELTA;
        }
        ++result.consumed;
    }
    return result;
}

PhysicalInputCollector::Status PhysicalInputCollector::commit(const Prepared &prepared) {
    if (status() != Status::ready) return status();
    if (prepared.status != Status::ready || prepared.revision != revision_ ||
        prepared.consumed > count_) return Status::stale;
    delivered_ = prepared.input;
    delivered_.wheel = delivered_.rel_x = delivered_.rel_y = 0;
    wheel_remainder_ = prepared.wheel_remainder;
    head_ = (head_ + prepared.consumed) % capacity;
    count_ -= prepared.consumed;
    ++revision_;
    return Status::ready;
}

} // namespace telos
