/* CivicMind — landing page motion: scroll reveals, count-up stats, nav shadow, scroll progress.
   Everything is decorative; with "reduce motion" the page shows its final state immediately. */
const CMMotion = (() => {
  const reduce = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const fmt = (n) => Number(n).toLocaleString('en-IN');

  /** Count a stat up from 0 to n (ease-out), or set it directly when motion is reduced. */
  function countUp(el, n) {
    const target = Number(n) || 0;
    if (!el) return;
    if (reduce || target === 0) { el.textContent = fmt(target); return; }
    const t0 = performance.now(), dur = Math.min(1600, 600 + target * 4);
    const step = (t) => {
      const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(Math.round(target * e));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function init() {
    document.documentElement.classList.add(reduce ? 'motion-off' : 'motion-on');
    const items = document.querySelectorAll('.reveal');
    if (reduce || !('IntersectionObserver' in window)) {
      items.forEach(el => el.classList.add('in'));
    } else {
      const io = new IntersectionObserver((entries) => entries.forEach(en => {
        if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); }
      }), { threshold: 0.15, rootMargin: '0px 0px -40px 0px' });
      items.forEach(el => io.observe(el));
    }
    const nav = document.querySelector('.nav'), bar = document.querySelector('.scroll-progress');
    const onScroll = () => {
      const h = document.documentElement;
      if (nav) nav.classList.toggle('scrolled', h.scrollTop > 8);
      if (bar) bar.style.transform = `scaleX(${h.scrollTop / Math.max(1, h.scrollHeight - h.clientHeight)})`;
    };
    addEventListener('scroll', onScroll, { passive: true });
    onScroll();
    // Gentle parallax of the hero network with the pointer (desktop only).
    const net = document.querySelector('.hero-net');
    if (net && !reduce && matchMedia('(pointer: fine)').matches) {
      document.querySelector('.hero').addEventListener('pointermove', (e) => {
        const x = (e.clientX / innerWidth - 0.5) * 14, y = (e.clientY / innerHeight - 0.5) * 10;
        net.style.transform = `translate3d(${x}px, ${y}px, 0) scale(1.03)`;
      });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  return { countUp, reduced: reduce };
})();
