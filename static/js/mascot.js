/**
 * static/js/mascot.js — Shared Dot mascot state utility.
 *
 * Provides setMascotState(el, state) and entryAnimateMascot(el, defaultState)
 * for any page that renders the Dot mascot SVG.
 *
 * Valid states: 'idle' | 'working' | 'happy' | 'sad' | 'waiting'
 * Each state maps to a CSS class (dot-{state}) defined in app.css.
 */

/**
 * Transition the Dot mascot SVG element to a new state.
 * Removes all dot-* state classes and applies the new one.
 *
 * @param {Element} el    - The .dot-mascot SVG element
 * @param {string}  state - One of: idle, working, happy, sad, waiting
 */
function setMascotState(el, state) {
  if (!el) return;
  ['dot-idle', 'dot-working', 'dot-happy', 'dot-sad', 'dot-waiting', 'dot-entering']
    .forEach(function (c) { el.classList.remove(c); });
  el.classList.add('dot-' + state);
}

/**
 * Play a brief entry animation (wave-in bounce) on page load, then settle into
 * the given default state. Call this on DOMContentLoaded for every page with a mascot.
 *
 * @param {Element} el           - The .dot-mascot SVG element (may be null — no-op)
 * @param {string}  defaultState - State to settle into after the entry animation (default: 'idle')
 * @param {number}  delay        - ms before starting the animation (default: 80)
 */
function entryAnimateMascot(el, defaultState, delay) {
  if (!el) return;
  defaultState = defaultState || 'idle';
  delay = typeof delay === 'number' ? delay : 80;

  // Remove any existing state classes
  ['dot-idle', 'dot-working', 'dot-happy', 'dot-sad', 'dot-waiting', 'dot-entering']
    .forEach(function (c) { el.classList.remove(c); });

  setTimeout(function () {
    el.classList.add('dot-entering');
    // After the wave-in animation completes (~500ms), apply the real default state
    setTimeout(function () {
      el.classList.remove('dot-entering');
      el.classList.add('dot-' + defaultState);
    }, 520);
  }, delay);
}

/**
 * Briefly flash the mascot into a state, then revert to the previous state.
 *
 * @param {Element} el          - The .dot-mascot SVG element
 * @param {string}  flashState  - State to flash into (e.g. 'happy')
 * @param {string}  revertState - State to revert to after flash
 * @param {number}  duration    - How long to stay in flash state (ms, default 900)
 */
function flashMascotState(el, flashState, revertState, duration) {
  if (!el) return;
  duration = typeof duration === 'number' ? duration : 900;
  setMascotState(el, flashState);
  setTimeout(function () {
    setMascotState(el, revertState);
  }, duration);
}

/**
 * Wire up a tap/click poke interaction on the mascot SVG.
 * Printo tips over toward the poke direction, then springs back.
 * Call this on DOMContentLoaded after entryAnimateMascot().
 *
 * @param {Element} el - The .dot-mascot SVG element
 */
function setupMascotPoke(el) {
  if (!el) return;

  el.style.cursor = 'pointer';
  el.setAttribute('title', 'Tap me!');
  el.setAttribute('aria-label', 'Printo — tap to interact');

  el.addEventListener('pointerdown', function (e) {
    // Don't interrupt a mid-poke animation or entry wave
    if (el.classList.contains('dot-poked') || el.classList.contains('dot-entering')) return;

    var rect = el.getBoundingClientRect();
    var pokeRight = e.clientX >= (rect.left + rect.width / 2);

    el.classList.add('dot-poked');
    el.classList.toggle('dot-poke-right', pokeRight);
    el.classList.toggle('dot-poke-left', !pokeRight);

    // Clean up after the 0.65s animation finishes — timeout is more reliable
    // than animationend on SVG <g> elements across Chromium versions.
    setTimeout(function () {
      el.classList.remove('dot-poked', 'dot-poke-right', 'dot-poke-left');
    }, 700);
  });
}

/**
 * Launch a confetti shower originating from the given element.
 * Uses the Web Animations API directly — no CSS @keyframes or custom properties
 * needed, so it works reliably on older Chromium builds (Pi, kiosk hardware).
 * Cleans itself up after all animations finish. No-op if reduced-motion is set.
 *
 * @param {Element} originEl - Element to use as the burst origin (e.g. mascot)
 */
function launchConfetti(originEl) {
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (typeof Element.prototype.animate !== 'function') return; // Web Animations API check

  var colors = ['#C2410C', '#15803D', '#F59E0B', '#2563EB', '#DB2777', '#7C3AED', '#0EA5E9', '#EA580C'];
  var rect   = originEl ? originEl.getBoundingClientRect() : null;
  var cx     = rect ? rect.left + rect.width  / 2 : window.innerWidth  / 2;
  var cy     = rect ? rect.top  + rect.height / 2 : window.innerHeight / 3;

  var container = document.createElement('div');
  container.setAttribute('aria-hidden', 'true');
  container.style.cssText = 'position:fixed;inset:0;pointer-events:none;overflow:hidden;z-index:9500;';
  document.body.appendChild(container);

  var total    = 48;
  var maxEnd   = 0;

  for (var i = 0; i < total; i++) {
    var p      = document.createElement('div');
    var angle  = (i / total) * Math.PI * 2 + (Math.random() - 0.5) * 0.4;
    var speed  = 100 + Math.random() * 200;
    var tx     = Math.cos(angle) * speed;
    var ty     = Math.sin(angle) * speed - (80 + Math.random() * 100); // bias upward
    var size   = 5 + Math.random() * 6;
    var isCirc = i % 4 === 0;
    var isStrp = i % 4 === 3;
    var dur    = 800 + Math.floor(Math.random() * 700);  // 0.8–1.5 s in ms
    var delay  = Math.floor(Math.random() * 250);        // 0–250 ms
    var color  = colors[i % colors.length];
    var tr     = Math.floor(Math.random() * 720) - 360;

    p.style.cssText = [
      'position:absolute',
      'left:' + Math.round(cx) + 'px',
      'top:' + Math.round(cy) + 'px',
      'width:' + size.toFixed(1) + 'px',
      'height:' + (isStrp ? (size * 3).toFixed(1) : size.toFixed(1)) + 'px',
      'background:' + color,
      'border-radius:' + (isCirc ? '50%' : '2px'),
      'opacity:1',
      'will-change:transform,opacity',
    ].join(';');

    container.appendChild(p);

    // Web Animations API — no CSS @keyframes dependency
    var finalX = tx;
    var finalY = ty + 380; // add gravity drop
    p.animate(
      [
        { transform: 'translate(0,0) rotate(0deg) scale(1)',     opacity: 1 },
        { transform: 'translate(0,0) rotate(0deg) scale(1)',     opacity: 1, offset: 0.01 },
        { transform: 'translate(' + finalX.toFixed(1) + 'px,' + finalY.toFixed(1) + 'px) rotate(' + tr + 'deg) scale(0.5)', opacity: 0 }
      ],
      { duration: dur, delay: delay, easing: 'ease-out', fill: 'forwards' }
    );

    if (delay + dur > maxEnd) maxEnd = delay + dur;
  }

  // Remove container shortly after the last particle finishes
  setTimeout(function () {
    if (container.parentNode) container.parentNode.removeChild(container);
  }, maxEnd + 200);
}
