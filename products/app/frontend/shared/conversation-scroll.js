/* Give a submitted turn reading space without pulling users away from history. */
export function createConversationScroll(stream, composer) {
  let anchor = null;
  let following = true;
  let frame = 0;
  let entering = false;
  let turnRevision = 0;
  let savedScroll = 0;
  const bottomGap = 40;
  const viewportHeight = () => {
    const bounds = stream.getBoundingClientRect();
    const composerTop = composer?.getBoundingClientRect().top;
    const bottom = composerTop > bounds.top ? Math.min(bounds.bottom, composerTop) : bounds.bottom;
    return Math.max(0, bottom - bounds.top);
  };
  const coordinate = node => node.getBoundingClientRect().top - stream.getBoundingClientRect().top + stream.scrollTop;
  const measure = () => {
    if (!anchor) return null;
    if (!anchor.isConnected || !stream.contains(anchor)) anchor = [...stream.querySelectorAll('.message--user')].at(-1);
    if (!anchor) { reset(); return null; }
    const height = viewportHeight();
    if (height < 80) return null;
    const visible = [...stream.children].filter(node => !node.hidden && node.getBoundingClientRect().height > 0);
    const last = visible.at(-1) || anchor;
    const top = coordinate(anchor);
    const tail = coordinate(last) + last.getBoundingClientRect().height;
    const inset = Math.min(72, Math.max(20, height * .14));
    // Padding shrinks as this reply grows; existing content never gains a fake height.
    const space = Math.max(0, stream.clientHeight - height)
      + Math.max(0, height - inset - (tail - top) - bottomGap);
    stream.style.setProperty('--conversation-turn-space', `${space}px`);
    return {top: Math.max(0, top - inset, tail - height + bottomGap), tail, height};
  };
  function refresh({previousScrollTop} = {}) {
    if (!anchor) return false;
    const layout = measure();
    if (layout && !entering) {
      if (following) stream.scrollTop = layout.top;
      else if (Number.isFinite(previousScrollTop)) stream.scrollTop = previousScrollTop;
      savedScroll = stream.scrollTop;
    }
    return true;
  }
  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(() => { frame = 0; refresh(); });
  };
  const interrupt = () => { following = false; entering = false; };
  const wheel = event => { if (event.deltaY < 0) interrupt(); };
  const keydown = event => {
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) interrupt();
  };
  const scroll = () => {
    if (!entering && stream.scrollTop < savedScroll - 1) following = false;
    if (!entering && !following && stream.scrollTop > savedScroll) {
      const layout = measure();
      if (layout && layout.tail - stream.scrollTop <= layout.height - bottomGap + 24) following = true;
    }
    savedScroll = stream.scrollTop;
  };
  function reset() {
    turnRevision += 1;
    anchor = null; entering = false; following = true;
    stream.style.removeProperty('--conversation-turn-space');
    delete stream.dataset.turnAnchored;
  }
  const mutation = new MutationObserver(schedule);
  mutation.observe(stream, {childList: true, subtree: true, characterData: true});
  const resize = new ResizeObserver(schedule);
  resize.observe(stream);
  stream.addEventListener('wheel', wheel, {passive: true});
  stream.addEventListener('touchstart', interrupt, {passive: true});
  stream.addEventListener('pointerdown', interrupt, {passive: true});
  stream.addEventListener('keydown', keydown);
  stream.addEventListener('scroll', scroll, {passive: true});
  stream.addEventListener('load', schedule, true);
  const controller = {
    beginTurn(node) {
      const revision = ++turnRevision;
      anchor = node; following = true; entering = true;
      stream.dataset.turnAnchored = 'true';
      requestAnimationFrame(() => {
        if (revision !== turnRevision || !anchor || !entering) return;
        const layout = measure();
        if (layout) stream.scrollTo({top: layout.top, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
        // Streaming updates can resize the turn while its one entry scroll settles.
        window.setTimeout(() => {
          if (revision === turnRevision && entering) { entering = false; refresh(); }
        }, 450);
      });
    },
    refresh,
    reset,
    destroy() {
      reset(); cancelAnimationFrame(frame); mutation.disconnect(); resize.disconnect();
      stream.removeEventListener('wheel', wheel); stream.removeEventListener('touchstart', interrupt);
      stream.removeEventListener('pointerdown', interrupt);
      stream.removeEventListener('keydown', keydown); stream.removeEventListener('scroll', scroll);
      stream.removeEventListener('load', schedule, true);
      delete stream._conversationScroll;
    },
  };
  stream._conversationScroll = controller;
  return controller;
}
