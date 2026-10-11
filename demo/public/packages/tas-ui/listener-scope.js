// Package-owned subscriptions can be suspended without losing editor state.
export class ListenerScope {
  constructor() { this.active = true; this.controller = new AbortController(); this.listeners = []; this.observers = []; }
  listen(target, type, handler, options = {}) {
    if (!target) return;
    const config = typeof options === 'boolean' ? { capture: options } : { ...options };
    const entry = { target, type, handler, config };
    this.listeners.push(entry);
    if (this.active) target.addEventListener(type, handler, { ...config, signal: this.controller.signal });
  }
  observe(target, callback, options) {
    const observer = new MutationObserver(records => { if (this.active) callback(records); });
    const entry = { target, observer, options }; this.observers.push(entry);
    if (this.active) observer.observe(target, options);
  }
  prune() {
    // Dynamic branch/history rows may have been replaced since the last paint.
    this.listeners = this.listeners.filter(entry => {
      if (!(entry.target instanceof Node) || entry.target.isConnected) return true;
      entry.target.removeEventListener(entry.type, entry.handler, entry.config);
      return false;
    });
  }
  suspend() {
    if (!this.active) return;
    this.active = false; this.controller.abort();
    for (const { observer } of this.observers) observer.disconnect();
    this.prune();
  }
  resume() {
    if (this.active) return;
    this.prune(); this.active = true; this.controller = new AbortController();
    for (const { target, type, handler, config } of this.listeners) target.addEventListener(type, handler, { ...config, signal: this.controller.signal });
    for (const { target, observer, options } of this.observers) if (target.isConnected) observer.observe(target, options);
  }
}
