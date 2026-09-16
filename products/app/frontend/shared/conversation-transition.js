/** Own only the message surface animation; never clone conversation content. */
export function createConversationTransition(surface, { reducedMotion = () => globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches } = {}) {
  let revision = 0;
  let animation = null;
  function cancel() {
    revision += 1;
    animation?.cancel();
    animation = null;
  }
  async function exit() {
    cancel();
    const current = revision;
    if (!surface?.animate || reducedMotion()) return true;
    animation = surface.animate([{ opacity: 1 }, { opacity: 0.55 }], {
      duration: 70, easing: 'ease-out', fill: 'forwards',
    });
    await animation.finished.catch(() => {});
    const isCurrent = revision === current;
    return isCurrent;
  }
  function enter() {
    cancel();
    if (!surface?.animate || reducedMotion()) return;
    const incoming = surface.animate([{ opacity: 0.55 }, { opacity: 1 }], {
      duration: 250, easing: 'cubic-bezier(.22, 1, .36, 1)',
    });
    animation = incoming;
    incoming.finished.catch(() => {}).finally(() => {
      if (animation === incoming) animation = null;
    });
  }
  return { exit, enter, cancel };
}
