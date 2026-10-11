// Presentation only: keep the live iframe and the one TAS workspace in place.
// Browser is the default; an explicit saved layout choice is restored for KOTOR.
export function mountTasPresentation(state = null) {
  const button = document.getElementById('b-tas-presentation');
  const gamePane = document.getElementById('original-pane');
  const preferenceKey = 'telos.tas-presentation';

  const readPreference = () => {
    try { return localStorage.getItem(preferenceKey) === 'overlay'; }
    catch { return false; }
  };

  if (!button || !gamePane) throw new Error('TAS presentation elements are missing.');
  const events = new AbortController();
  const observers = [];
  const destroy = () => { events.abort(); observers.forEach(observer => observer.disconnect()); };
  try {
    let preferredOverlay = state?.preferredOverlay ?? readPreference();
    const setMode = (overlay, persist = false) => {
      const focused = document.activeElement;
      const mode = overlay ? 'overlay' : 'browser';
      const changed = document.body.dataset.tasPresentation !== mode;
      document.body.dataset.tasPresentation = mode;
      button.setAttribute('aria-pressed', String(overlay));
      // A pressed toggle keeps one name: true always means game-overlay mode.
      button.textContent = 'Game overlay';
      button.title = overlay
        ? 'Return to the surrounding score, branches, and console'
        : 'Present the same TAS controls over KOTOR';
      if (persist) {
        preferredOverlay = overlay;
        try { localStorage.setItem(preferenceKey, overlay ? 'overlay' : 'browser'); }
        catch { /* This visit still keeps the selected presentation. */ }
      }
      // Reuse the existing layout owner, including on wide-screen mode changes.
      if (changed) window.dispatchEvent(new Event('telos:presentation-change'));
      // Restoring the saved overlay after choosing KOTOR hides that source tab.
      if (changed && overlay && focused?.closest('.center-head .tabs')) button.focus({ preventScroll: true });
      // Existing canvas renderers already redraw on resize. No transport event,
      // focus transfer into the guest, or runtime/session mutation belongs here.
      window.dispatchEvent(new Event('resize'));
    };

    const updateAvailability = () => {
      const available = document.body.dataset.tasExtension !== 'disabled';
      button.disabled = gamePane.hidden || !available;
      setMode(available && !gamePane.hidden && preferredOverlay);
      if (gamePane.hidden) {
        button.title = 'Select Play KOTOR to use the game overlay';
      }
    };

    button.addEventListener('click', () => {
      if (!gamePane.hidden) setMode(document.body.dataset.tasPresentation !== 'overlay', true);
    }, { signal:events.signal });
    window.addEventListener('telos:shared-presentation', event => {
      const request = event.detail;
      if (!request || !['read', 'apply'].includes(request.action)) return;
      try {
        if (request.action === 'apply') {
          if (typeof request.overlay !== 'boolean') throw new Error('Invalid presentation preference.');
          preferredOverlay = request.overlay;
          localStorage.setItem(preferenceKey, preferredOverlay ? 'overlay' : 'browser');
          updateAvailability();
        }
        request.overlay = preferredOverlay;
        request.applied = true;
      } catch (error) { request.error = error.message; }
    }, { signal:events.signal });
    // app.js has already admitted this shortcut before requesting console focus.
    // Reveal its panel synchronously without duplicating keyboard policy.
    window.addEventListener('telos:reveal-console', () => {
      if (document.body.dataset.tasPresentation === 'overlay'
        && !document.body.classList.contains('tas-ui-hidden')) setMode(false, true);
    }, { signal:events.signal });
    const availabilityObserver = new MutationObserver(updateAvailability);
    observers.push(availabilityObserver);
    availabilityObserver.observe(gamePane, {
      attributes: true, attributeFilter: ['hidden'],
    });
    const extensionObserver = new MutationObserver(updateAvailability);
    observers.push(extensionObserver);
    extensionObserver.observe(document.body, { attributes: true, attributeFilter: ['data-tas-extension'] });
    let workspaceHidden = document.body.classList.contains('tas-ui-hidden');
    const visibilityObserver = new MutationObserver(() => {
      const hidden = document.body.classList.contains('tas-ui-hidden');
      if (hidden === workspaceHidden) return;
      workspaceHidden = hidden;
      // Keep keyboard recovery in the parent UI; never focus the guest iframe.
      const centerView = () => [
        '.center-head [aria-selected="true"]',
        '.center-head .tabs [aria-pressed="true"]',
        '.center-head .tabs button',
      ].flatMap(selector => [...document.querySelectorAll(selector)])
        .find(node => !node.disabled && node.getClientRects().length);
      const target = hidden ? document.getElementById('b-show-workspace')
        : button.disabled ? centerView() : button;
      target?.focus({ preventScroll: true });
      window.dispatchEvent(new Event('resize'));
    });
    observers.push(visibilityObserver);
    visibilityObserver.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    updateAvailability();
    return {
      snapshot: () => ({ preferredOverlay }),
      restore(saved) { preferredOverlay = saved.preferredOverlay; updateAvailability(); },
      destroy,
    };
  } catch (error) {
    destroy();
    throw error;
  }
}
