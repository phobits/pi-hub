(function bootstrap() {
  const boot = window.__SMART_DISPLAY__ || {};
  const config = boot.config || {};
  let state = boot.state || {};
  let stateEtag = typeof config.state_etag === "string" ? config.state_etag : "";
  let screensaverActive = false;
  let idleTimer = null;
  let pollTimer = null;
  let slideshowTimer = null;
  let resizeTimer = null;
  let midnightTimer = null;
  let activeScreen = "home";
  let screenAnimationTimer = null;
  let queueRefreshTimer = null;
  let queueRequestInFlight = false;
  let suppressClickUntil = 0;
  const SCREEN_TRANSITION_MS = 220;
  const QUEUE_REFRESH_MS = 30_000;
  const SWIPE_MIN_X = 72;
  // Generous on purpose: a deliberate swipe on a wall-mounted panel is slower
  // than a phone flick, and 800 ms rejected careful gestures outright.
  const SWIPE_MAX_MS = 1500;
  const SWIPE_AXIS_RATIO = 1.3;
  const lastRenderedSectionKeys = {
    weather: "",
    calendar: "",
    spotify: "",
    timers: "",
    system: "",
  };
  const SPOTIFY_PROGRESS_KEYS = new Set(["progress_ms", "duration_ms"]);
  // Plan B1 watchdog: while the screensaver is visible we re-POST
  // /api/screensaver/state periodically so the backend pause-TTL stays
  // fresh. If this loop stops firing (tab crash, JS exception), the TTL
  // expires and Spotify polling resumes on its own.
  let screensaverHeartbeatTimer = null;
  const SCREENSAVER_HEARTBEAT_MS = 5 * 60 * 1000;
  // #18: the state poll is the only way provider errors reach the panel, so a
  // dead backend must not look like calm, current data. A request is aborted
  // after STATE_REQUEST_TIMEOUT_MS (the chain never waits on a hung socket),
  // and once no poll has succeeded for LOCAL_STALE_POLLS intervals — at least
  // LOCAL_STALE_MIN_MS — the status rail says "Anzeige wird aktualisiert".
  // Measured with performance.now(), so an NTP step cannot fake an outage.
  const POLL_INTERVAL_MS = Math.max((config.poll_interval_seconds || 15) * 1000, 5000);
  const STATE_REQUEST_TIMEOUT_MS = 10_000;
  const LOCAL_STALE_POLLS = 3;
  const LOCAL_STALE_MIN_MS = 30_000;
  const LOCAL_STALE_AFTER_MS = Math.max(LOCAL_STALE_POLLS * POLL_INTERVAL_MS, LOCAL_STALE_MIN_MS);
  // #23: a scheduled poll first waits for one rendered frame and says so in
  // FRAME_HEADER; the backend's kiosk watchdog restarts the kiosk (then the
  // Pi) when no such poll arrives. A hung compositor delivers no frame, so the
  // poll goes out unmarked after FRAME_WAIT_MS — data keeps flowing either way.
  const FRAME_HEADER = "X-Kiosk-Frame";
  const FRAME_WAIT_MS = 2000;
  let stateRequest = null;
  // Start of the current outage window: the last good poll, or the moment
  // polling resumed after the screensaver. The page itself arrived with state.
  let stateWatchFrom = performance.now();
  let localLinkLost = false;

  // Cached Intl formatters. Re-creating them per tick burns measurable CPU
  // on a Pi Zero 2 W; see plan B5. The weekday formatter drives the client-
  // side day label computation (mirrors smart_display/calendar_layout.py).
  const LOCALE = config.locale || "de-AT";
  const TIMEZONE = config.timezone || "Europe/Vienna";
  const WEEKDAY_FMT = new Intl.DateTimeFormat(LOCALE, {
    weekday: "long",
    timeZone: TIMEZONE,
  });
  const SECTION_DATE_FMT = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: TIMEZONE,
  });
  // Time of day in TIMEZONE, for finding its midnight (#20).
  const ZONE_CLOCK_FMT = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZone: TIMEZONE,
  });
  // #20: "Stand 12. Okt. 15:10" for data older than yesterday; the year only
  // when it is not this year's.
  const AGE_DATE_FMT = new Intl.DateTimeFormat(LOCALE, {
    day: "numeric",
    month: "short",
    timeZone: TIMEZONE,
  });
  const AGE_DATE_YEAR_FMT = new Intl.DateTimeFormat(LOCALE, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: TIMEZONE,
  });
  // Plan B5: the old updateClock allocated three Intl formatters per second
  // on a Pi Zero 2 W. Cache them once and drive the tick by a
  // setTimeout-to-next-minute so the idle CPU is closer to 0 %.
  const CLOCK_TIME_FMT = new Intl.DateTimeFormat(LOCALE, {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: TIMEZONE,
  });
  const CLOCK_DATE_FMT = new Intl.DateTimeFormat(LOCALE, {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: TIMEZONE,
  });
  let clockTimer = null;

  // QLOCKTWO word-clock face. The Python module smart_display/watch_faces.py
  // owns the canonical layout; this block is a 1:1 mirror so the minute tick
  // doesn't need to round-trip to the server. Keep the two sides in sync.
  const QLOCKTWO_WORDS = {
    ES: [0, 0, 2],
    IST: [0, 3, 3],
    FUENF_MIN: [0, 7, 4],
    ZEHN_MIN: [1, 0, 4],
    ZWANZIG_MIN: [1, 4, 7],
    VIERTEL: [2, 4, 7],
    NACH: [3, 2, 4],
    VOR: [3, 6, 3],
    HALB: [4, 0, 4],
    UHR: [9, 8, 3],
  };
  // Hour 1 has two forms — see smart_display/watch_faces.py for the rule.
  const QLOCKTWO_HOUR_EIN = [5, 2, 3];   // "EIN" — only paired with UHR at the full hour.
  const QLOCKTWO_HOUR_WORDS = {
    1: [5, 2, 4],  // EINS — default for every non-"ein Uhr" context.
    2: [5, 0, 4],
    3: [6, 1, 4],
    4: [7, 7, 4],
    5: [6, 7, 4],
    6: [9, 1, 5],
    7: [5, 5, 6],
    8: [8, 1, 4],
    9: [7, 3, 4],
    10: [8, 5, 4],
    11: [7, 0, 3],
    12: [4, 5, 5],
  };
  // OÖ dialect variant — mirrors smart_display/watch_faces.QLOCKTWO_OOE_*.
  // Same 10×11 geometry, no UHR, no EIN/EINS split.
  const QLOCKTWO_OOE_WORDS = {
    ES: [0, 0, 2],
    IS: [0, 3, 2],
    FUEMF_MIN: [0, 7, 4],
    ZEHN_MIN: [1, 0, 4],
    ZWANZG_MIN: [1, 5, 6],
    VIERTL: [2, 4, 6],
    NOCH: [3, 2, 4],
    VOR: [3, 7, 3],
    HOIBE: [4, 0, 5],
  };
  const QLOCKTWO_OOE_HOUR_WORDS = {
    1: [7, 0, 4],   // OANS
    2: [7, 4, 4],   // ZWOA
    3: [5, 0, 4],   // DREI
    4: [9, 6, 5],   // VIERE
    5: [6, 7, 4],   // FÜMF (hour)
    6: [9, 0, 6],   // SECHSE
    7: [6, 0, 6],   // SIEBNE
    8: [8, 1, 4],   // OCHT
    9: [5, 5, 5],   // NEINE
    10: [8, 5, 5],  // ZEHNE
    11: [7, 8, 3],  // ELF
    12: [4, 5, 5],  // ZWÖFE
  };
  const VALID_WATCH_FACES = ["flip", "lcd", "pulse", "qlocktwo", "qlocktwo-ooe", "analog"];
  const WATCH_FACE_LABELS = {
    flip: "Flip",
    lcd: "LCD",
    pulse: "Puls",
    qlocktwo: "Wortuhr",
    "qlocktwo-ooe": "Wortuhr OÖ",
    analog: "Analog",
  };
  // Segment map for the LCD face. Mirrors watch_faces.LCD_SEGMENT_MAP —
  // server-rendered initial state uses the Python side, subsequent ticks use
  // this one, so keep them in sync.
  const LCD_SEGMENT_MAP = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
  };
  const WATCH_FACE_STORAGE_KEY = "sd.watch_face";
  // Ticket #2: night window from the server config ({from, to} or null) and
  // the user's override (auto | on | off) persisted like the watch face.
  const NIGHT_MODE_STORAGE_KEY = "sd.night_mode";
  const NIGHT_WINDOW = config.night || null;
  const VALID_NIGHT_MODES = ["auto", "on", "off"];
  // Ticket #2: hold-to-confirm for power actions.
  const HOLD_MS = 2000;
  // Ticket #3: mirrors timers.RING_TIMEOUT_SECONDS — a ringing timer nobody
  // acknowledges disappears after this, so an empty flat does not pulse all day.
  const TIMER_RING_TIMEOUT_S = 600;
  let qlocktwoLetterIndex = null;
  let lastQlocktwoKey = "";
  let qlocktwoOoeLetterIndex = null;
  let lastQlocktwoOoeKey = "";

  function qlocktwoHour12(hour24) {
    const h = ((hour24 % 12) + 12) % 12;
    return h === 0 ? 12 : h;
  }

  function qlocktwoActiveKeys(hour, minute) {
    const block = Math.floor(minute / 5) * 5;
    const thisHour = qlocktwoHour12(hour);
    const nextHour = qlocktwoHour12(hour + 1);
    const words = [QLOCKTWO_WORDS.ES, QLOCKTWO_WORDS.IST];
    switch (block) {
      case 0:
        words.push(thisHour === 1 ? QLOCKTWO_HOUR_EIN : QLOCKTWO_HOUR_WORDS[thisHour], QLOCKTWO_WORDS.UHR);
        break;
      case 5:
        words.push(QLOCKTWO_WORDS.FUENF_MIN, QLOCKTWO_WORDS.NACH, QLOCKTWO_HOUR_WORDS[thisHour]);
        break;
      case 10:
        words.push(QLOCKTWO_WORDS.ZEHN_MIN, QLOCKTWO_WORDS.NACH, QLOCKTWO_HOUR_WORDS[thisHour]);
        break;
      case 15:
        words.push(QLOCKTWO_WORDS.VIERTEL, QLOCKTWO_WORDS.NACH, QLOCKTWO_HOUR_WORDS[thisHour]);
        break;
      case 20:
        words.push(QLOCKTWO_WORDS.ZWANZIG_MIN, QLOCKTWO_WORDS.NACH, QLOCKTWO_HOUR_WORDS[thisHour]);
        break;
      case 25:
        words.push(
          QLOCKTWO_WORDS.FUENF_MIN,
          QLOCKTWO_WORDS.VOR,
          QLOCKTWO_WORDS.HALB,
          QLOCKTWO_HOUR_WORDS[nextHour],
        );
        break;
      case 30:
        words.push(QLOCKTWO_WORDS.HALB, QLOCKTWO_HOUR_WORDS[nextHour]);
        break;
      case 35:
        words.push(
          QLOCKTWO_WORDS.FUENF_MIN,
          QLOCKTWO_WORDS.NACH,
          QLOCKTWO_WORDS.HALB,
          QLOCKTWO_HOUR_WORDS[nextHour],
        );
        break;
      case 40:
        words.push(QLOCKTWO_WORDS.ZWANZIG_MIN, QLOCKTWO_WORDS.VOR, QLOCKTWO_HOUR_WORDS[nextHour]);
        break;
      case 45:
        words.push(QLOCKTWO_WORDS.VIERTEL, QLOCKTWO_WORDS.VOR, QLOCKTWO_HOUR_WORDS[nextHour]);
        break;
      case 50:
        words.push(QLOCKTWO_WORDS.ZEHN_MIN, QLOCKTWO_WORDS.VOR, QLOCKTWO_HOUR_WORDS[nextHour]);
        break;
      case 55:
        words.push(QLOCKTWO_WORDS.FUENF_MIN, QLOCKTWO_WORDS.VOR, QLOCKTWO_HOUR_WORDS[nextHour]);
        break;
      default:
        break;
    }
    const keys = new Set();
    for (const word of words) {
      const [row, col, length] = word;
      for (let i = 0; i < length; i += 1) {
        keys.add(`${row},${col + i}`);
      }
    }
    return keys;
  }

  function qlocktwoOoeActiveKeys(hour, minute) {
    const block = Math.floor(minute / 5) * 5;
    const thisHour = qlocktwoHour12(hour);
    const nextHour = qlocktwoHour12(hour + 1);
    const words = [QLOCKTWO_OOE_WORDS.ES, QLOCKTWO_OOE_WORDS.IS];
    switch (block) {
      case 0:
        words.push(QLOCKTWO_OOE_HOUR_WORDS[thisHour]);
        break;
      case 5:
        words.push(QLOCKTWO_OOE_WORDS.FUEMF_MIN, QLOCKTWO_OOE_WORDS.NOCH, QLOCKTWO_OOE_HOUR_WORDS[thisHour]);
        break;
      case 10:
        words.push(QLOCKTWO_OOE_WORDS.ZEHN_MIN, QLOCKTWO_OOE_WORDS.NOCH, QLOCKTWO_OOE_HOUR_WORDS[thisHour]);
        break;
      case 15:
        words.push(QLOCKTWO_OOE_WORDS.VIERTL, QLOCKTWO_OOE_WORDS.NOCH, QLOCKTWO_OOE_HOUR_WORDS[thisHour]);
        break;
      case 20:
        words.push(QLOCKTWO_OOE_WORDS.ZWANZG_MIN, QLOCKTWO_OOE_WORDS.NOCH, QLOCKTWO_OOE_HOUR_WORDS[thisHour]);
        break;
      case 25:
        words.push(
          QLOCKTWO_OOE_WORDS.FUEMF_MIN,
          QLOCKTWO_OOE_WORDS.VOR,
          QLOCKTWO_OOE_WORDS.HOIBE,
          QLOCKTWO_OOE_HOUR_WORDS[nextHour],
        );
        break;
      case 30:
        words.push(QLOCKTWO_OOE_WORDS.HOIBE, QLOCKTWO_OOE_HOUR_WORDS[nextHour]);
        break;
      case 35:
        words.push(
          QLOCKTWO_OOE_WORDS.FUEMF_MIN,
          QLOCKTWO_OOE_WORDS.NOCH,
          QLOCKTWO_OOE_WORDS.HOIBE,
          QLOCKTWO_OOE_HOUR_WORDS[nextHour],
        );
        break;
      case 40:
        words.push(QLOCKTWO_OOE_WORDS.ZWANZG_MIN, QLOCKTWO_OOE_WORDS.VOR, QLOCKTWO_OOE_HOUR_WORDS[nextHour]);
        break;
      case 45:
        words.push(QLOCKTWO_OOE_WORDS.VIERTL, QLOCKTWO_OOE_WORDS.VOR, QLOCKTWO_OOE_HOUR_WORDS[nextHour]);
        break;
      case 50:
        words.push(QLOCKTWO_OOE_WORDS.ZEHN_MIN, QLOCKTWO_OOE_WORDS.VOR, QLOCKTWO_OOE_HOUR_WORDS[nextHour]);
        break;
      case 55:
        words.push(QLOCKTWO_OOE_WORDS.FUEMF_MIN, QLOCKTWO_OOE_WORDS.VOR, QLOCKTWO_OOE_HOUR_WORDS[nextHour]);
        break;
      default:
        break;
    }
    const keys = new Set();
    for (const word of words) {
      const [row, col, length] = word;
      for (let i = 0; i < length; i += 1) {
        keys.add(`${row},${col + i}`);
      }
    }
    return keys;
  }

  function buildLetterIndex(container) {
    if (!container) {
      return null;
    }
    const map = new Map();
    const letters = container.querySelectorAll(".qlocktwo-letter");
    letters.forEach((letter) => {
      const row = letter.dataset.row;
      const col = letter.dataset.col;
      if (row !== undefined && col !== undefined) {
        map.set(`${row},${col}`, letter);
      }
    });
    return map;
  }

  function buildQlocktwoLetterIndex() {
    return buildLetterIndex(nodes.qlocktwo);
  }

  function buildQlocktwoOoeLetterIndex() {
    return buildLetterIndex(nodes.qlocktwoOoe);
  }

  function currentHourMinute() {
    const now = new Date();
    const parts = CLOCK_TIME_FMT.formatToParts(now);
    let hour = now.getHours();
    let minute = now.getMinutes();
    for (const part of parts) {
      if (part.type === "hour") {
        hour = Number(part.value);
      } else if (part.type === "minute") {
        minute = Number(part.value);
      }
    }
    return { hour, minute };
  }

  function updateQlocktwo(force) {
    if (!nodes.qlocktwo) {
      return;
    }
    const { hour, minute } = currentHourMinute();
    const block = Math.floor(minute / 5) * 5;
    const key = `${hour}:${block}`;
    if (!force && key === lastQlocktwoKey) {
      return;
    }
    lastQlocktwoKey = key;
    if (!qlocktwoLetterIndex) {
      qlocktwoLetterIndex = buildQlocktwoLetterIndex();
    }
    if (!qlocktwoLetterIndex) {
      return;
    }
    const activeKeys = qlocktwoActiveKeys(hour, minute);
    qlocktwoLetterIndex.forEach((letter, cellKey) => {
      const active = activeKeys.has(cellKey);
      if (active !== letter.classList.contains("is-active")) {
        letter.classList.toggle("is-active", active);
      }
    });
  }

  function updateQlocktwoOoe(force) {
    if (!nodes.qlocktwoOoe) {
      return;
    }
    const { hour, minute } = currentHourMinute();
    const block = Math.floor(minute / 5) * 5;
    const key = `${hour}:${block}`;
    if (!force && key === lastQlocktwoOoeKey) {
      return;
    }
    lastQlocktwoOoeKey = key;
    if (!qlocktwoOoeLetterIndex) {
      qlocktwoOoeLetterIndex = buildQlocktwoOoeLetterIndex();
    }
    if (!qlocktwoOoeLetterIndex) {
      return;
    }
    const activeKeys = qlocktwoOoeActiveKeys(hour, minute);
    qlocktwoOoeLetterIndex.forEach((letter, cellKey) => {
      const active = activeKeys.has(cellKey);
      if (active !== letter.classList.contains("is-active")) {
        letter.classList.toggle("is-active", active);
      }
    });
  }

  let lastAnalogKey = "";
  let analogSecondTimer = null;

  function updateAnalog(force) {
    if (!nodes.analogHour || !nodes.analogMinute || !nodes.analogSecond) {
      return;
    }
    const now = new Date();
    const parts = CLOCK_TIME_FMT.formatToParts(now);
    let hour = now.getHours();
    let minute = now.getMinutes();
    for (const part of parts) {
      if (part.type === "hour") {
        hour = Number(part.value);
      } else if (part.type === "minute") {
        minute = Number(part.value);
      }
    }
    // Seconds are the same across all timezones, so the native getter is
    // safe here (unlike hour/minute which must follow config.timezone).
    const secondDeg = ((now.getSeconds() * 6) % 360).toFixed(2);
    nodes.analogSecond.setAttribute("transform", `rotate(${secondDeg} 100 100)`);

    // Hour + minute only change on minute boundaries — cache to skip the
    // two setAttribute calls on the 1 Hz seconds tick.
    const key = `${hour}:${minute}`;
    if (!force && key === lastAnalogKey) {
      return;
    }
    lastAnalogKey = key;
    const hourDeg = (((hour % 12) * 30 + minute * 0.5) % 360).toFixed(2);
    const minuteDeg = ((minute * 6) % 360).toFixed(2);
    nodes.analogHour.setAttribute("transform", `rotate(${hourDeg} 100 100)`);
    nodes.analogMinute.setAttribute("transform", `rotate(${minuteDeg} 100 100)`);
  }

  function startAnalogSecondTick() {
    if (analogSecondTimer !== null) {
      return;
    }
    // Align the first tick to the next second boundary so the hand steps
    // in sync with real-world seconds rather than drifting off by up to 1 s.
    const msUntilNextSecond = 1000 - (Date.now() % 1000);
    analogSecondTimer = window.setTimeout(function tick() {
      updateAnalog(false);
      analogSecondTimer = window.setTimeout(tick, 1000);
    }, msUntilNextSecond + 10);
  }

  function stopAnalogSecondTick() {
    if (analogSecondTimer !== null) {
      window.clearTimeout(analogSecondTimer);
      analogSecondTimer = null;
    }
  }

  function currentWatchFace() {
    const stored = (() => {
      try {
        return window.localStorage && window.localStorage.getItem(WATCH_FACE_STORAGE_KEY);
      } catch (error) {
        return null;
      }
    })();
    if (stored && VALID_WATCH_FACES.includes(stored)) {
      return stored;
    }
    const configured = config.watch_face;
    if (configured && VALID_WATCH_FACES.includes(configured)) {
      return configured;
    }
    return "flip";
  }

  // --- Flip clock ---------------------------------------------------------
  // Track pending timeouts per digit so rapid ticks can't leave a card mid-
  // flip (e.g. if the system clock jumps).
  const flipCleanupTimers = new Map();

  function flipDigitTo(card, newValue, animate) {
    if (!card) return;
    const oldValue = card.dataset.value || "";
    if (oldValue === newValue) return;
    const staticTop = card.querySelector(".flip-digit__half--top .flip-digit__digit");
    const staticBottom = card.querySelector(".flip-digit__half--bottom .flip-digit__digit");
    const flapTop = card.querySelector(".flip-digit__flap--top .flip-digit__digit");
    const flapBottom = card.querySelector(".flip-digit__flap--bottom .flip-digit__digit");
    if (!staticTop || !staticBottom) return;
    card.dataset.value = newValue;
    // Static top shows the new digit instantly — it's covered by flap--top
    // (showing the old digit) until the flap rotates away, then revealed.
    staticTop.textContent = newValue;
    if (!animate) {
      staticBottom.textContent = newValue;
      return;
    }
    if (flapTop) flapTop.textContent = oldValue;
    if (flapBottom) flapBottom.textContent = newValue;
    const pending = flipCleanupTimers.get(card);
    if (pending) window.clearTimeout(pending);
    card.classList.remove("is-flipping");
    // Force a reflow so the animation restart actually re-runs the keyframes.
    void card.offsetWidth;
    card.classList.add("is-flipping");
    const timer = window.setTimeout(() => {
      staticBottom.textContent = newValue;
      card.classList.remove("is-flipping");
      flipCleanupTimers.delete(card);
    }, 640);
    flipCleanupTimers.set(card, timer);
  }

  function currentClockDigits() {
    const now = new Date();
    const parts = CLOCK_TIME_FMT.formatToParts(now);
    let hour = String(now.getHours()).padStart(2, "0");
    let minute = String(now.getMinutes()).padStart(2, "0");
    for (const part of parts) {
      if (part.type === "hour") hour = part.value.padStart(2, "0");
      else if (part.type === "minute") minute = part.value.padStart(2, "0");
    }
    return [hour[0], hour[1], minute[0], minute[1]];
  }

  function updateFlipCards(cardSet, digits, force) {
    if (!cardSet) return;
    const cards = [cardSet.h1, cardSet.h2, cardSet.m1, cardSet.m2];
    for (let i = 0; i < 4; i += 1) {
      flipDigitTo(cards[i], digits[i], !force);
    }
  }

  function updateFlip(force) {
    const digits = currentClockDigits();
    updateFlipCards(nodes.flipCards, digits, force);
    updateFlipCards(nodes.screensaverFlipCards, digits, force);
  }

  function updateScreensaverFlip(force) {
    updateFlipCards(nodes.screensaverFlipCards, currentClockDigits(), force);
  }

  // --- LCD ---------------------------------------------------------------
  function setLcdDigit(group, value) {
    if (!group) return;
    if (group.dataset.value === value) return;
    const active = LCD_SEGMENT_MAP[value] || "";
    const rects = group.querySelectorAll(".lcd-seg");
    rects.forEach((rect) => {
      const seg = rect.dataset.seg || "";
      const on = active.includes(seg);
      if (on !== rect.classList.contains("is-on")) {
        rect.classList.toggle("is-on", on);
      }
    });
    group.dataset.value = value;
  }

  function updateLcd(force) {
    if (!nodes.lcdGroups) return;
    const now = new Date();
    const parts = CLOCK_TIME_FMT.formatToParts(now);
    let hour = String(now.getHours()).padStart(2, "0");
    let minute = String(now.getMinutes()).padStart(2, "0");
    for (const part of parts) {
      if (part.type === "hour") hour = part.value.padStart(2, "0");
      else if (part.type === "minute") minute = part.value.padStart(2, "0");
    }
    const digits = [hour[0], hour[1], minute[0], minute[1]];
    const groups = [nodes.lcdGroups.h1, nodes.lcdGroups.h2, nodes.lcdGroups.m1, nodes.lcdGroups.m2];
    for (let i = 0; i < 4; i += 1) {
      const group = groups[i];
      if (!group) continue;
      if (force || group.dataset.value !== digits[i]) {
        setLcdDigit(group, digits[i]);
      }
    }
  }

  // --- Pulse -------------------------------------------------------------

  function updatePulse(force) {
    if (!nodes.pulseHh || !nodes.pulseMm) return;
    const now = new Date();
    const parts = CLOCK_TIME_FMT.formatToParts(now);
    let hour = String(now.getHours()).padStart(2, "0");
    let minute = String(now.getMinutes()).padStart(2, "0");
    for (const part of parts) {
      if (part.type === "hour") hour = part.value.padStart(2, "0");
      else if (part.type === "minute") minute = part.value.padStart(2, "0");
    }
    if (force || nodes.pulseHh.textContent !== hour) {
      nodes.pulseHh.textContent = hour;
    }
    if (force || nodes.pulseMm.textContent !== minute) {
      nodes.pulseMm.textContent = minute;
    }
  }

  function applyWatchFace(face) {
    const next = VALID_WATCH_FACES.includes(face) ? face : "flip";
    document.body.setAttribute("data-watch-face", next);
    const isInteractive = next === "qlocktwo" || next === "qlocktwo-ooe";
    if (nodes.watchFace) {
      nodes.watchFace.setAttribute("aria-pressed", isInteractive ? "true" : "false");
    }
    const faceNodes = {
      flip: nodes.flipFace,
      lcd: nodes.lcdFace,
      pulse: nodes.pulseFace,
      qlocktwo: nodes.qlocktwo,
      "qlocktwo-ooe": nodes.qlocktwoOoe,
      analog: document.getElementById("watch-face-analog"),
    };
    for (const [key, node] of Object.entries(faceNodes)) {
      if (node) node.setAttribute("aria-hidden", key === next ? "false" : "true");
    }
    // Each face owns its live update path; stop timers for faces that aren't
    // currently visible so a background face doesn't keep the CPU warm.
    if (next === "qlocktwo") {
      updateQlocktwo(true);
      stopAnalogSecondTick();
    } else if (next === "qlocktwo-ooe") {
      updateQlocktwoOoe(true);
      stopAnalogSecondTick();
    } else if (next === "analog") {
      updateAnalog(true);
      startAnalogSecondTick();
    } else if (next === "flip") {
      updateFlip(true);
      stopAnalogSecondTick();
    } else if (next === "lcd") {
      updateLcd(true);
      stopAnalogSecondTick();
    } else if (next === "pulse") {
      updatePulse(true);
      stopAnalogSecondTick();
    } else {
      stopAnalogSecondTick();
    }
    syncFaceChips(next);
    return next;
  }

  function cycleWatchFace() {
    const current = document.body.getAttribute("data-watch-face") || "flip";
    let idx = VALID_WATCH_FACES.indexOf(current);
    if (idx === -1) idx = 0;
    const next = VALID_WATCH_FACES[(idx + 1) % VALID_WATCH_FACES.length];
    try {
      if (window.localStorage) {
        window.localStorage.setItem(WATCH_FACE_STORAGE_KEY, next);
      }
    } catch (error) {
      /* storage disabled — fall back to session-only switch */
    }
    applyWatchFace(next);
    showToast(`Uhrzeit-Stil: ${WATCH_FACE_LABELS[next] || next}`, "info", 1600);
  }

  function selectWatchFace(face) {
    if (!VALID_WATCH_FACES.includes(face)) return;
    try {
      if (window.localStorage) {
        window.localStorage.setItem(WATCH_FACE_STORAGE_KEY, face);
      }
    } catch (error) {
      /* storage disabled — session-only */
    }
    applyWatchFace(face);
  }

  const nodes = {
    screenStage: document.getElementById("screen-stage"),
    homeScreen: document.getElementById("screen-home"),
    spotifyScreen: document.getElementById("screen-spotify"),
    weatherScreen: document.getElementById("screen-weather"),
    wxTemp: document.getElementById("wx-temp"),
    wxIcon: document.getElementById("wx-icon"),
    wxCondition: document.getElementById("wx-condition"),
    wxFacts: document.getElementById("wx-facts"),
    wxWind: document.getElementById("wx-wind"),
    wxMeta: document.getElementById("wx-meta"),
    wxWarn: document.getElementById("wx-warn"),
    wxWarnTitle: document.getElementById("wx-warn-title"),
    wxWarnSub: document.getElementById("wx-warn-sub"),
    wxWarnMore: document.getElementById("wx-warn-more"),
    wxMode: document.getElementById("wx-mode"),
    wxRange: document.getElementById("wx-range"),
    wxNote: document.getElementById("wx-note"),
    wxChart: document.getElementById("wx-chart"),
    wxWeek: document.getElementById("wx-week"),
    warnSheet: document.getElementById("warn-sheet"),
    warnSheetBackdrop: document.getElementById("warn-sheet-backdrop"),
    warnSheetClose: document.getElementById("warn-sheet-close"),
    warnSheetPlace: document.getElementById("warn-sheet-place"),
    warnSheetBody: document.getElementById("warn-sheet-body"),
    warnSheetFooter: document.getElementById("warn-sheet-footer"),
    date: document.getElementById("clock-date"),
    watchFace: document.getElementById("watch-face"),
    flipFace: document.getElementById("watch-face-flip"),
    flipCards: {
      h1: document.getElementById("flip-h1"),
      h2: document.getElementById("flip-h2"),
      m1: document.getElementById("flip-m1"),
      m2: document.getElementById("flip-m2"),
    },
    lcdFace: document.getElementById("watch-face-lcd"),
    lcdGroups: {
      h1: document.getElementById("lcd-h1"),
      h2: document.getElementById("lcd-h2"),
      m1: document.getElementById("lcd-m1"),
      m2: document.getElementById("lcd-m2"),
    },
    pulseFace: document.getElementById("watch-face-pulse"),
    pulseHh: document.getElementById("pulse-hh"),
    pulseMm: document.getElementById("pulse-mm"),
    screensaverFlipCards: {
      h1: document.getElementById("screensaver-flip-h1"),
      h2: document.getElementById("screensaver-flip-h2"),
      m1: document.getElementById("screensaver-flip-m1"),
      m2: document.getElementById("screensaver-flip-m2"),
    },
    qlocktwo: document.getElementById("watch-face-qlocktwo"),
    qlocktwoOoe: document.getElementById("watch-face-qlocktwo-ooe"),
    analogHour: document.getElementById("analog-hand-hour"),
    analogMinute: document.getElementById("analog-hand-minute"),
    analogSecond: document.getElementById("analog-hand-second"),
    weatherLocation: document.getElementById("weather-location"),
    weatherFeels: document.getElementById("weather-feels"),
    weatherStamp: document.getElementById("weather-stamp"),
    weatherTemperature: document.getElementById("weather-temperature"),
    weatherIcon: document.getElementById("weather-icon"),
    weatherCondition: document.getElementById("weather-condition"),
    weatherForecast: document.getElementById("weather-forecast"),
    calendarList: document.getElementById("calendar-list"),
    cardsColumn: document.getElementById("cards-column"),
    // Status rail: one note — local link lost, else the calendar's age.
    statusNote: document.getElementById("status-note"),
    statusDot: document.getElementById("status-dot"),
    // Home tile is display-only — it has no status pill, no device badge, no
    // transport and no volume. Every one of those lives on the Spotify screen.
    spotifyCard: document.getElementById("spotify-card"),
    spotifyTrack: document.getElementById("spotify-track"),
    spotifyArtist: document.getElementById("spotify-artist"),
    spotifyArtwork: document.getElementById("spotify-artwork"),
    spotifyTilePlayIcon: document.getElementById("spotify-tile-play-icon"),
    spotifyProgressFill: document.getElementById("spotify-progress-fill"),
    spotifyDetail: document.getElementById("spotify-detail"),
    spotifyDetailArtwork: document.getElementById("spotify-detail-artwork"),
    spotifyDetailTrack: document.getElementById("spotify-detail-track"),
    spotifyDetailArtist: document.getElementById("spotify-detail-artist"),
    spotifyDetailAlbum: document.getElementById("spotify-detail-album"),
    spotifyDetailDevice: document.getElementById("spotify-detail-device"),
    spotifyDetailDeviceIcon: document.getElementById("spotify-detail-device-icon"),
    spotifyDetailVolumeReadout: document.getElementById("spotify-detail-volume-readout"),
    spotifyDetailPrevious: document.getElementById("spotify-detail-previous"),
    spotifyDetailPreviousIcon: document.getElementById("spotify-detail-previous-icon"),
    spotifyDetailToggle: document.getElementById("spotify-detail-toggle"),
    spotifyDetailToggleIcon: document.getElementById("spotify-detail-toggle-icon"),
    spotifyDetailNext: document.getElementById("spotify-detail-next"),
    spotifyDetailNextIcon: document.getElementById("spotify-detail-next-icon"),
    spotifyDetailVolume: document.getElementById("spotify-detail-volume"),
    spotifyDetailVolumeIcon: document.getElementById("spotify-detail-volume-icon"),
    spotifyDetailSeek: document.getElementById("spotify-detail-seek"),
    spotifyDetailProgressElapsed: document.getElementById("spotify-detail-progress-elapsed"),
    spotifyDetailProgressTotal: document.getElementById("spotify-detail-progress-total"),
    spotifyDetailOpenPicker: document.getElementById("spotify-detail-open-picker"),
    spotifyDetailOpenPickerIcon: document.getElementById("spotify-detail-open-picker-icon"),
    spotifyDetailQueueStatus: document.getElementById("spotify-detail-queue-status"),
    spotifyDetailQueueList: document.getElementById("spotify-detail-queue-list"),
    picker: document.getElementById("spotify-picker"),
    pickerBackdrop: document.getElementById("spotify-picker-backdrop"),
    pickerClose: document.getElementById("spotify-picker-close"),
    pickerDevices: document.getElementById("spotify-picker-devices"),
    pickerPlaylists: document.getElementById("spotify-picker-playlists"),
    pickerTransfer: document.getElementById("spotify-picker-transfer"),
    screensaver: document.getElementById("screensaver"),
    // Plan C1: two stacked image slots for crossfade. The active one is
    // visible; the inactive one holds the next image preloaded at opacity 0.
    screensaverImageA: document.getElementById("screensaver-image-a"),
    screensaverImageB: document.getElementById("screensaver-image-b"),
    screensaverFallback: document.getElementById("screensaver-fallback"),
    screensaverClock: document.getElementById("screensaver-clock"),
    // Ticket #2: info overlay over photos / dim night clock.
    screensaverInfo: document.getElementById("screensaver-info"),
    screensaverTime: document.getElementById("screensaver-time"),
    screensaverDate: document.getElementById("screensaver-date"),
    screensaverWeather: document.getElementById("screensaver-weather"),
    screensaverWeatherSep: document.getElementById("screensaver-weather-sep"),
    screensaverTimer: document.getElementById("screensaver-timer"),
    // #22: the local-warning line, outside the clock's wake area.
    screensaverWarning: document.getElementById("screensaver-warning"),
    screensaverWarningText: document.getElementById("screensaver-warning-text"),
    // Ticket #1: NTP hint.
    clockSyncHint: document.getElementById("clock-sync-hint"),
    // Ticket #3: timer chip + alarm.
    timerChip: document.getElementById("timer-chip"),
    timerChipIcon: document.getElementById("timer-chip-icon"),
    timerChipText: document.getElementById("timer-chip-text"),
    timerAlarm: document.getElementById("timer-alarm"),
    timerAlarmTitle: document.getElementById("timer-alarm-title"),
    timerAlarmLabel: document.getElementById("timer-alarm-label"),
    timerAlarmStop: document.getElementById("timer-alarm-stop"),
    timerAlarmExtend: document.getElementById("timer-alarm-extend"),
    // Ticket #2/#3: quick sheet.
    sheetHandle: document.getElementById("sheet-handle"),
    quickSheet: document.getElementById("quick-sheet"),
    quickSheetBackdrop: document.getElementById("quick-sheet-backdrop"),
    quickSheetPanel: document.getElementById("quick-sheet-panel"),
    quickSheetClose: document.getElementById("quick-sheet-close"),
    timerPresets: document.getElementById("timer-presets"),
    timerList: document.getElementById("timer-list"),
    faceChips: document.getElementById("face-chips"),
    nightChips: document.getElementById("night-chips"),
    nightNote: document.getElementById("night-note"),
    screensaverStart: document.getElementById("screensaver-start"),
    powerReboot: document.getElementById("power-reboot"),
    powerShutdown: document.getElementById("power-shutdown"),
    powerNote: document.getElementById("power-note"),
    powerOverlay: document.getElementById("power-overlay"),
    powerOverlayText: document.getElementById("power-overlay-text"),
    toast: document.getElementById("toast"),
  };
  let volumeCommitTimer = null;
  // Volume-Slider Race-Guard (Plan A2). Snapshots arriving while the user is
  // mid-drag or within 1.5 s of the last touch must not snap the slider back.
  let volumeBusyUntil = 0;
  let volumeLastSent = null;
  const VOLUME_BUSY_MS = 1500;
  const VOLUME_TOLERANCE = 2;
  let seekDragging = false;
  let seekHoldUntil = 0;
  let seekItemKey = "";
  // seekItemKey as it was when the current drag started, so a commit can be
  // discarded if the track changed mid-drag.
  let seekDragItemKey = "";
  let seekLastCommitValue = null;
  let seekLastCommitAt = 0;
  const SEEK_HOLD_MS = 5000;

  // Spotify playback-progress baseline. Polls only arrive every 3–4 s, so the
  // visible bar is interpolated locally from the last payload. `null` hides the
  // bar (no track / not controllable / no duration). `receivedAt` is a
  // performance.now() stamp so we don't depend on Pi↔browser clock sync.
  let spotifyProgress = null;
  let spotifyProgressTimer = null;

  function formatClockMs(ms) {
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function renderSpotifyProgress() {
    if (screensaverActive) {
      return;
    }
    // Both screens stay in the DOM, so paint only the visible one — otherwise
    // this 1 Hz tick writes five properties per call into a hidden subtree that
    // still costs style recalc. setActiveScreen repaints on the way in.
    const homeVisible = activeScreen === "home";
    const detailVisible = activeScreen === "spotify";
    if (!spotifyProgress) {
      // The bar's visibility belongs to `[data-music]` in CSS. Writing an
      // inline display here would outrank that rule and silently pin it, keyed
      // on a different condition than the class it fights with.
      if (nodes.spotifyProgressFill && homeVisible) {
        nodes.spotifyProgressFill.style.transform = "scaleX(0)";
      }
      if (nodes.spotifyDetailSeek && detailVisible && !seekDragging) {
        nodes.spotifyDetailSeek.max = "0";
        nodes.spotifyDetailSeek.value = "0";
        nodes.spotifyDetailSeek.style.setProperty("--seek-percent", "0%");
        nodes.spotifyDetailProgressElapsed.textContent = "0:00";
        nodes.spotifyDetailProgressTotal.textContent = "−0:00";
      }
      return;
    }
    const { progressMs, durationMs, isPlaying, receivedAt } = spotifyProgress;
    const elapsedSincePoll = isPlaying ? performance.now() - receivedAt : 0;
    const pos = Math.min(progressMs + elapsedSincePoll, durationMs);
    const ratio = durationMs > 0 ? Math.max(0, Math.min(1, pos / durationMs)) : 0;
    if (nodes.spotifyProgressFill && homeVisible) {
      nodes.spotifyProgressFill.style.transform = `scaleX(${ratio.toFixed(4)})`;
    }
    if (nodes.spotifyDetailSeek && detailVisible && !seekDragging) {
      nodes.spotifyDetailSeek.max = String(Math.max(0, Math.round(durationMs)));
      nodes.spotifyDetailSeek.value = String(Math.max(0, Math.round(pos)));
      nodes.spotifyDetailSeek.style.setProperty(
        "--seek-percent",
        `${(ratio * 100).toFixed(2)}%`,
      );
      nodes.spotifyDetailProgressElapsed.textContent = formatClockMs(pos);
      // Remaining, not total: on a panel the useful question is "how long
      // until the next track", and the total is one glance away in the queue.
      nodes.spotifyDetailProgressTotal.textContent = `−${formatClockMs(durationMs - pos)}`;
    }
  }

  // The position the bar is *showing* right now, interpolated past the last
  // poll. Optimistic updates carry this instead of the payload's stale
  // progress_ms so the bar never rewinds. `null` when no track is playing.
  function interpolatedProgressMs() {
    if (!spotifyProgress) {
      return null;
    }
    const { progressMs, durationMs, isPlaying, receivedAt } = spotifyProgress;
    const elapsed = isPlaying ? performance.now() - receivedAt : 0;
    return Math.round(Math.min(progressMs + elapsed, durationMs));
  }

  // Drive the bar once per second, independent of the /api/state render dedup
  // so it keeps moving even when nothing else in the payload changed. Only ticks
  // while a track is actually playing — paused/idle states stay static so the
  // Pi isn't woken every second for nothing.
  function ensureProgressTimer() {
    if (spotifyProgressTimer !== null || screensaverActive) {
      return;
    }
    spotifyProgressTimer = window.setInterval(renderSpotifyProgress, 1000);
  }

  function stopProgressTimer() {
    if (spotifyProgressTimer !== null) {
      window.clearInterval(spotifyProgressTimer);
      spotifyProgressTimer = null;
    }
  }

  // Update the interpolation baseline from a fresh payload. Kept separate from
  // renderSpotify so it runs on every poll, even when render() short-circuits.
  function updateSpotifyProgress(spotify) {
    const showControls = Boolean(spotify.can_control || spotify.supports_volume);
    const durationMs =
      typeof spotify.duration_ms === "number" ? spotify.duration_ms : null;
    const progressMs =
      typeof spotify.progress_ms === "number" ? spotify.progress_ms : null;
    const itemKey = [
      spotify.track_title || "",
      spotify.artist_name || "",
      durationMs === null ? "" : String(durationMs),
    ].join("\u0000");
    if (itemKey !== seekItemKey) {
      seekItemKey = itemKey;
      seekHoldUntil = 0;
      // Deliberately does NOT clear seekDragging: a poll must not cancel a drag
      // the user's finger is still in. commitSeek discards the commit instead
      // (seekDragItemKey), and pointerdown always re-arms a fresh drag.
    }
    if (!showControls || !spotify.track_title || durationMs === null || progressMs === null) {
      spotifyProgress = null;
    } else if (
      performance.now() < seekHoldUntil &&
      spotifyProgress &&
      spotifyProgress.durationMs === durationMs
    ) {
      // Spotify's inline refresh can report the pre-seek position for several
      // seconds. Keep the optimistic local baseline briefly instead of snapping
      // the thumb back; a later poll reconciles with provider truth.
      spotifyProgress.isPlaying = Boolean(spotify.is_playing);
    } else {
      spotifyProgress = {
        progressMs,
        durationMs,
        isPlaying: Boolean(spotify.is_playing),
        receivedAt: performance.now(),
      };
    }
    if (spotifyProgress && spotifyProgress.isPlaying) {
      ensureProgressTimer();
    } else {
      stopProgressTimer();
    }
    renderSpotifyProgress();
  }

  function markVolumeBusy() {
    volumeBusyUntil = performance.now() + VOLUME_BUSY_MS;
    spotifyVolumeSliders().forEach((slider) => {
      slider.dataset.dirty = "1";
    });
  }

  let toastTimer = null;
  function showToast(message, kind = "error", ms = 4000) {
    const node = nodes.toast;
    if (!node || !message) {
      return;
    }
    window.clearTimeout(toastTimer);
    node.textContent = message;
    node.dataset.kind = kind;
    node.hidden = false;
    // Force reflow so the transition re-plays when replacing an existing toast.
    // eslint-disable-next-line no-unused-expressions
    node.offsetHeight;
    node.classList.add("is-visible");
    toastTimer = window.setTimeout(() => hideToast(), ms);
  }

  function hideToast() {
    const node = nodes.toast;
    if (!node) {
      return;
    }
    window.clearTimeout(toastTimer);
    node.classList.remove("is-visible");
    // Wait for the fade-out before hiding so the screen reader doesn't announce empty.
    window.setTimeout(() => {
      if (!node.classList.contains("is-visible")) {
        node.hidden = true;
      }
    }, 220);
  }

  const icons = {
    clear:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="16" cy="16" r="5"></circle><path d="M16 3v4M16 25v4M3 16h4M25 16h4M6.8 6.8l2.8 2.8M22.4 22.4l2.8 2.8M25.2 6.8l-2.8 2.8M9.6 22.4l-2.8 2.8"></path></svg>',
    "partly-cloudy":
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 12a6 6 0 1 1 11.3-3"></path><path d="M24 17a5 5 0 0 0-1-9.9"></path><path d="M9 25h13a5 5 0 0 0 0-10 7 7 0 0 0-13-1A5 5 0 0 0 9 25Z"></path></svg>',
    cloudy:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 25h14a5 5 0 1 0-.9-9.9A7 7 0 0 0 9 14a5 5 0 0 0 0 11Z"></path></svg>',
    fog:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 12h16"></path><path d="M5 17h22"></path><path d="M8 22h16"></path></svg>',
    rain:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 20h14a5 5 0 1 0-.9-9.9A7 7 0 0 0 9 9a5 5 0 0 0 0 11Z"></path><path d="M12 23l-1.5 3M17 23l-1.5 3M22 23l-1.5 3"></path></svg>',
    snow:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19h14a5 5 0 1 0-.9-9.9A7 7 0 0 0 9 8a5 5 0 0 0 0 11Z"></path><path d="M13 24h0M19 24h0"></path><path d="M13 21v6M10 24h6M11 22l4 4M15 22l-4 4"></path><path d="M19 21v6M16 24h6M17 22l4 4M21 22l-4 4"></path></svg>',
    storm:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19h14a5 5 0 1 0-.9-9.9A7 7 0 0 0 9 8a5 5 0 0 0 0 11Z"></path><path d="M16 20l-3 5h3l-2 4 5-7h-3l2-2"></path></svg>',
    // #22: night variants, sleet and a neutral "no data" mark for the
    // detail screen. The home screen's own WMO mapping never asks for them.
    "clear-night":
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 22.5A9.5 9.5 0 0 1 13.2 7.4 9.5 9.5 0 1 0 24.6 19a9.4 9.4 0 0 1-3.6 3.5Z"></path></svg>',
    "partly-cloudy-night":
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.5 12.5A6 6 0 0 1 17 4.6a6.5 6.5 0 0 0 7 9.6"></path><path d="M9 25h13a5 5 0 0 0 0-10 7 7 0 0 0-13-1A5 5 0 0 0 9 25Z"></path></svg>',
    sleet:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19h14a5 5 0 1 0-.9-9.9A7 7 0 0 0 9 8a5 5 0 0 0 0 11Z"></path><path d="M12 22l-1.5 3"></path><path d="M20 21v6M17 24h6M18 22l4 4M22 22l-4 4"></path></svg>',
    unknown:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-dasharray="3 3"><path d="M9 24h14a5 5 0 1 0-.9-9.9A7 7 0 0 0 9 13a5 5 0 0 0 0 11Z"></path></svg>',
    previous:
      '<svg viewBox="0 0 32 32" fill="currentColor"><rect x="7" y="7" width="3" height="18" rx="1.2"></rect><path d="M23.5 8.5v15L11 16z"></path></svg>',
    next:
      '<svg viewBox="0 0 32 32" fill="currentColor"><rect x="22" y="7" width="3" height="18" rx="1.2"></rect><path d="M8.5 8.5v15L21 16z"></path></svg>',
    play:
      '<svg viewBox="0 0 32 32" fill="currentColor"><path d="M11 8.5v15l12-7.5z"></path></svg>',
    pause:
      '<svg viewBox="0 0 32 32" fill="currentColor"><rect x="10" y="8" width="4.5" height="16" rx="1.5"></rect><rect x="17.5" y="8" width="4.5" height="16" rx="1.5"></rect></svg>',
    device:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="9" width="18" height="12" rx="2"></rect><path d="M12 25h8"></path></svg>',
    timer:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="16" cy="18" r="10"></circle><path d="M16 12v6l4 2"></path><path d="M13 4h6"></path><path d="M16 4v4"></path></svg>',
    volume:
      '<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 7l-6 5H5v8h4l6 5z"></path><path d="M20 12a5.5 5.5 0 0 1 0 8"></path><path d="M23.5 8.5a10 10 0 0 1 0 15"></path></svg>',
  };

  function weatherIconName(conditionCode) {
    if (conditionCode === 0 || conditionCode === 1) {
      return "clear";
    }
    if (conditionCode === 2) {
      return "partly-cloudy";
    }
    if (conditionCode === 3) {
      return "cloudy";
    }
    if (conditionCode === 45 || conditionCode === 48) {
      return "fog";
    }
    if ([71, 73, 75, 77, 85, 86].includes(conditionCode)) {
      return "snow";
    }
    if ([95, 96, 99].includes(conditionCode)) {
      return "storm";
    }
    if ([51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82].includes(conditionCode)) {
      return "rain";
    }
    return "cloudy";
  }

  function setIcon(node, iconName) {
    if (!node) {
      return;
    }
    const nextName = icons[iconName] ? iconName : "";
    if (node.dataset.iconName === nextName) {
      return;
    }
    node.innerHTML = icons[nextName] || "";
    node.dataset.iconName = nextName;
  }

  function updateActiveWatchFace(force) {
    const face = document.body.getAttribute("data-watch-face") || "flip";
    if (face === "flip") {
      updateFlip(force);
    } else if (face === "lcd") {
      updateLcd(force);
    } else if (face === "pulse") {
      updatePulse(force);
    } else if (face === "qlocktwo") {
      updateQlocktwo(force);
    } else if (face === "qlocktwo-ooe") {
      updateQlocktwoOoe(force);
    } else if (face === "analog") {
      updateAnalog(force);
    }
  }

  function updateClock() {
    const now = new Date();
    const dateParts = CLOCK_DATE_FMT.formatToParts(now);
    const datePart = (type) => {
      const part = dateParts.find((p) => p.type === type);
      return part ? part.value : "";
    };
    const dateLabel = `${datePart("weekday")}, ${datePart("day")}. ${datePart("month")}`;
    nodes.date.textContent = dateLabel;
    // Ticket #2: the photo/night overlay is a text clock — one write a minute.
    nodes.screensaverDate.textContent = dateLabel;
    nodes.screensaverTime.textContent = CLOCK_TIME_FMT.format(now);
    applyNight(false);
    refreshCalendarMinute();
    // #22: a warning ends on its minute even when no fetch lands.
    renderScreensaverWarning();
    renderWeatherDetail(false);
    if (screensaverActive) {
      updateScreensaverFlip(false);
      return;
    }
    if ((document.body.getAttribute("data-watch-face") || "flip") === "flip") {
      updateFlip(false);
    } else {
      updateScreensaverFlip(false);
      updateActiveWatchFace(false);
    }
  }

  function scheduleClockTick() {
    // Re-arm for the next minute boundary + 500 ms safety margin so a slow
    // tick never lands in the previous minute. Clamp to at least 1 s to
    // avoid a runaway loop if the host clock misbehaves.
    const now = Date.now();
    const msUntilNextMinute = 60_000 - (now % 60_000);
    const delay = Math.max(1_000, msUntilNextMinute + 500);
    window.clearTimeout(clockTimer);
    clockTimer = window.setTimeout(() => {
      updateClock();
      scheduleClockTick();
    }, delay);
  }

  // Age of a provider's updated_at (the last good fetch) in the panel's zone:
  // "15:10" today, "gestern 15:10", "12. Okt. 15:10", "12. Okt. 2025 15:10" in
  // another year. Day boundaries are calendar days in TIMEZONE, not 24 h, so
  // DST days stay right. "" for a missing or unparsable stamp — never a time
  // that only looks correct. Midnight re-renders through
  // scheduleMidnightRerender, so "gestern" needs no timer of its own (#20).
  // `compact` lets the year replace the time — the status rail shares its
  // 328 px with the timer chip, which leaves ~180 px for the note (measured,
  // WebKit and Chromium), and "Termine · 30. Dez. 2025 14:10" needs 214.
  function formatStamp(isoString, { compact = false } = {}) {
    const ms = parseIsoMs(isoString);
    if (ms === null) {
      return "";
    }
    const time = CLOCK_TIME_FMT.format(new Date(ms));
    const day = SECTION_DATE_FMT.format(new Date(ms));
    const today = SECTION_DATE_FMT.format(new Date());
    if (day === today) {
      return time;
    }
    const [y, m, d] = today.split("-").map(Number);
    if (day === new Date(Date.UTC(y, m - 1, d - 1)).toISOString().slice(0, 10)) {
      return `gestern ${time}`;
    }
    if (day.slice(0, 4) === today.slice(0, 4)) {
      return `${AGE_DATE_FMT.format(new Date(ms))} ${time}`;
    }
    const date = AGE_DATE_YEAR_FMT.format(new Date(ms));
    return compact ? date : `${date} ${time}`;
  }

  function isBehind(snapshot) {
    const status = snapshot && snapshot.status;
    return status === "stale" || status === "error";
  }

  // Status rail (top of the right column, 36 px). Precedence (#20): the
  // timer chip always shows — it is live and tappable — and the one note
  // beside it is the lost local link (#18) if there is one, else the
  // calendar's age ("Termine · gestern 15:10" — the source is the prefix, so
  // "Stand" is left out to fit beside the chip). Weather states its age in its
  // own line and Spotify in the
  // music bar, so one failing source never reads as "the device is offline".
  // A calendar that never loaded says so in the agenda, not here.
  function renderStatusRail() {
    let note = "";
    if (localLinkLost) {
      note = "Anzeige wird aktualisiert";
    } else {
      const snapshot = (state.calendar && state.calendar.snapshot) || {};
      const age = isBehind(snapshot) ? formatStamp(snapshot.updated_at, { compact: true }) : "";
      note = age ? `Termine · ${age}` : "";
    }
    const hidden = note === "";
    nodes.statusNote.classList.toggle("is-hidden", hidden);
    nodes.statusDot.classList.toggle("is-hidden", hidden);
    if (nodes.statusNote.textContent !== note) {
      nodes.statusNote.textContent = note;
    }
  }

  function renderWeather(weather) {
    const snapshot = weather.snapshot || {};
    const hasTemperature = typeof weather.temperature_c === "number";
    nodes.weatherTemperature.textContent = hasTemperature
      ? `${Math.round(weather.temperature_c)}°`
      : "–";
    setIcon(nodes.weatherIcon, hasTemperature ? weatherIconName(weather.condition_code) : "");
    if (snapshot.status === "disabled") {
      nodes.weatherCondition.textContent = "Wetter nicht eingerichtet";
    } else if (weather.condition) {
      nodes.weatherCondition.textContent = weather.condition;
    } else if (snapshot.status === "empty" || !snapshot.status) {
      nodes.weatherCondition.textContent = "Wetterdaten werden geladen.";
    } else {
      nodes.weatherCondition.textContent = "Wetter nicht verfügbar";
    }
    nodes.weatherFeels.textContent =
      typeof weather.apparent_temperature_c === "number"
        ? `Gefühlt ${Math.round(weather.apparent_temperature_c)}°`
        : "";
    // Age instead of a badge: the reader wants to know how old "14°" is. It
    // takes the place name's room — the place never changes, and a days-old
    // "Stand 30. Dez. 2025 14:10" would otherwise lose its time to the ellipsis.
    const stamp = isBehind(snapshot) ? formatStamp(snapshot.updated_at) : "";
    nodes.weatherStamp.textContent = stamp ? `Stand ${stamp}` : "";
    nodes.weatherLocation.textContent = stamp ? "" : weather.location_label || "";

    // Ticket #2: screensaver overlay gets temperature + condition, nothing else.
    // Behind data is left out there (#20): the overlay has no room for an age,
    // and a bare "14°" from yesterday reads as now.
    const screensaverWeather = hasTemperature && !isBehind(snapshot)
      ? `${Math.round(weather.temperature_c)}°${weather.condition ? ` · ${weather.condition}` : ""}`
      : "";
    nodes.screensaverWeather.textContent = screensaverWeather;
    nodes.screensaverWeatherSep.classList.toggle("is-hidden", screensaverWeather === "");

    const forecastItems = Array.isArray(weather.forecast) ? weather.forecast.slice(0, 3) : [];
    nodes.weatherForecast.classList.toggle("is-empty", forecastItems.length === 0);
    const fragment = document.createDocumentFragment();
    forecastItems.forEach((item) => {
      const day = document.createElement("div");
      day.className = "weather__day";

      const name = document.createElement("div");
      name.className = "weather__day-name";
      name.textContent = item.day_label || "Tag";

      const icon = document.createElement("div");
      icon.className = "weather__day-icon";
      setIcon(icon, weatherIconName(item.condition_code));

      const temp = document.createElement("div");
      temp.className = "weather__day-temp";
      temp.textContent =
        typeof item.temperature_max_c === "number" ? `${Math.round(item.temperature_max_c)}°` : "--";
      if (typeof item.temperature_min_c === "number") {
        const min = document.createElement("span");
        min.className = "weather__day-min";
        min.textContent = `${Math.round(item.temperature_min_c)}°`;
        temp.appendChild(min);
      }

      day.append(name, icon, temp);
      fragment.appendChild(day);
    });
    nodes.weatherForecast.replaceChildren(fragment);
  }

  function createCalendarRow(entry) {
    const row = document.createElement("li");
    row.className = entry.past ? "agenda__item is-past" : "agenda__item";

    const time = document.createElement("div");
    time.className = "agenda__time";
    time.textContent = entry.item.time_label;

    const title = document.createElement("div");
    title.className = "agenda__title";
    title.textContent = entry.item.title;

    row.append(time, title);
    return row;
  }

  // The next upcoming event: taller, on a surface, with the relative time
  // ("in 48 Min" / "jetzt") on the right. Counts as two rows in the budget.
  function createCalendarNextRow(entry, relativeLabel) {
    const row = document.createElement("li");
    row.className = "agenda__next";

    const head = document.createElement("div");
    head.className = "agenda__next-head";
    const time = document.createElement("span");
    time.textContent = entry.item.time_label;
    const rel = document.createElement("span");
    rel.className = "agenda__next-rel";
    rel.textContent = relativeLabel;
    head.append(time, rel);

    const title = document.createElement("div");
    title.className = "agenda__next-title";
    title.textContent = entry.item.title;

    row.append(head, title);
    return row;
  }

  // "Heute · Donnerstag" → <b>Heute</b> · Donnerstag. Plain weekday labels
  // (the day after tomorrow and later) render as one bold word.
  function createCalendarSectionLabel(label) {
    const sectionLabel = document.createElement("li");
    sectionLabel.className = "agenda__section";
    const parts = label.split(" · ");
    const strong = document.createElement("b");
    strong.textContent = parts[0];
    sectionLabel.appendChild(strong);
    if (parts.length > 1) {
      const rest = document.createElement("span");
      rest.textContent = `· ${parts.slice(1).join(" · ")}`;
      sectionLabel.appendChild(rest);
    }
    return sectionLabel;
  }

  function createCalendarEmpty(title, subtitle) {
    const empty = document.createElement("li");
    empty.className = "agenda__empty";
    empty.textContent = title;
    if (subtitle) {
      const sub = document.createElement("div");
      sub.className = "agenda__empty-sub";
      sub.textContent = subtitle;
      empty.appendChild(sub);
    }
    return empty;
  }

  // An agenda without sections (#19). Only a fetch that succeeded empty — now
  // or as the last good answer behind a stale snapshot, whose age the rail
  // shows — says "Keine Termine". Everything else says what it is.
  function createCalendarPlaceholder(snapshot, nowMs) {
    const status = snapshot.status;
    if (status === "disabled") {
      return createCalendarEmpty("Kalender nicht eingerichtet", "");
    }
    const hadGoodData = parseIsoMs(snapshot.updated_at) !== null;
    if (status === "ok" || (isBehind(snapshot) && hadGoodData)) {
      const dayAfter = new Date(nowMs + 2 * 24 * 60 * 60 * 1000);
      return createCalendarEmpty(
        "Keine Termine",
        `Auch morgen und ${WEEKDAY_FMT.format(dayAfter)} ist nichts eingetragen.`,
      );
    }
    if (isBehind(snapshot)) {
      return createCalendarEmpty("Termine nicht verfügbar", "");
    }
    return createCalendarEmpty("Termine werden geladen", "");
  }

  function parseIsoMs(value) {
    if (typeof value !== "string" || !value) {
      return null;
    }
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? ms : null;
  }

  // Walk the sections once and decide, per item, whether it is over and
  // which one is "next". All-day items are never past and never "next" — a
  // delivery window has no moment to count down to.
  function classifyCalendar(sections, nowMs) {
    let nextKey = null;
    const entries = sections.map((section, sectionIdx) =>
      section.items.map((item, itemIdx) => {
        const startMs = parseIsoMs(item.starts_at);
        const endMs = parseIsoMs(item.ends_at);
        let past = false;
        if (!item.all_day) {
          if (endMs !== null) {
            past = endMs <= nowMs;
          } else if (startMs !== null) {
            past = startMs + 60 * 60 * 1000 <= nowMs;
          }
        }
        const key = `${sectionIdx}:${itemIdx}`;
        if (nextKey === null && !past && !item.all_day && startMs !== null) {
          nextKey = key;
        }
        return { item, key, startMs, endMs, past };
      }),
    );
    return { entries, nextKey };
  }

  // "jetzt" while it runs, "in N Min" up to an hour out, otherwise nothing —
  // beyond an hour the clock time itself says more than "in 3 Std 12 Min".
  function relativeLabelFor(entry, nowMs) {
    if (!entry || entry.startMs === null) {
      return "";
    }
    if (entry.startMs <= nowMs) {
      return "jetzt";
    }
    const minutes = Math.ceil((entry.startMs - nowMs) / 60_000);
    return minutes <= 60 ? `in ${minutes} Min` : "";
  }

  function todayIsoInZone() {
    // en-CA locale gives us YYYY-MM-DD directly, honoring the configured timezone.
    return SECTION_DATE_FMT.format(new Date());
  }

  function computeDayLabel(sectionDateIso, todayIso) {
    if (!sectionDateIso) {
      return "";
    }
    const sectionParts = sectionDateIso.split("-").map((part) => Number(part));
    const todayParts = todayIso.split("-").map((part) => Number(part));
    if (sectionParts.length !== 3 || todayParts.length !== 3) {
      return "";
    }
    const sectionDate = Date.UTC(sectionParts[0], sectionParts[1] - 1, sectionParts[2]);
    const today = Date.UTC(todayParts[0], todayParts[1] - 1, todayParts[2]);
    const diffDays = Math.round((sectionDate - today) / 86_400_000);
    // Weekday name for every case — use UTC noon so the Intl formatter lands on
    // the right weekday regardless of TZ.
    const display = new Date(sectionDate + 12 * 3_600_000);
    const raw = WEEKDAY_FMT.format(display);
    const weekday = raw.charAt(0).toUpperCase() + raw.slice(1);
    if (diffDays === 0) {
      return `Heute · ${weekday}`;
    }
    if (diffDays === 1) {
      return `Morgen · ${weekday}`;
    }
    if (diffDays === 2) {
      return `Übermorgen · ${weekday}`;
    }
    // Future days > 2 and stale/offline past dates: plain weekday name.
    return weekday;
  }

  // Plan B8: after the A3 backend rewrite the server always emits
  // ``sections`` with a ``section_date``. The old flat-items fallback is
  // gone — a malformed payload yields an empty calendar + a one-shot
  // warning instead of silently labelling everything as "Heute".
  let calendarMissingSectionsWarned = false;

  function normalizeCalendarSections(calendar) {
    if (!calendar || !Array.isArray(calendar.sections)) {
      if (!calendarMissingSectionsWarned) {
        calendarMissingSectionsWarned = true;
        window.console.warn(
          "calendar payload missing 'sections' array — ignoring",
        );
      }
      return [];
    }
    return calendar.sections
      .filter(
        (section) =>
          section &&
          Array.isArray(section.items) &&
          section.items.length > 0 &&
          typeof section.section_date === "string" &&
          section.section_date.length > 0,
      )
      .map((section) => ({
        day_key: section.day_key || "",
        section_date: section.section_date,
        items: section.items,
      }));
  }

  // Plan B7: port of smart_display/calendar_layout.compute_row_budget.
  // Keep in sync — Python is the source of truth, JS is a 1:1 mirror.
  function computeRowBudget(sectionItemCounts, maxRows, sectionHasLabel, sectionExtraRows) {
    const n = sectionItemCounts.length;
    if (n === 0 || maxRows <= 0) {
      return new Array(n).fill(0);
    }
    const extras = Array.isArray(sectionExtraRows)
      ? sectionExtraRows.map((x) => Math.max(0, x | 0))
      : new Array(n).fill(0);
    const allocated = sectionItemCounts.map((c) => Math.max(0, c | 0));
    const totalRows = () => {
      let total = 0;
      for (let i = 0; i < n; i += 1) {
        if (allocated[i] > 0) {
          total += allocated[i] + extras[i];
          if (sectionHasLabel[i]) {
            total += 1;
          }
        }
      }
      return total;
    };
    let guard = allocated.reduce((a, b) => a + b, 0) + 1;
    while (totalRows() > maxRows && guard > 0) {
      guard -= 1;
      let largestIdx = -1;
      let largestValue = 0;
      // `>=` breaks ties toward the latest section — sections are
      // chronological, so trimming the first would drop today and keep the day
      // after tomorrow. Mirrors calendar_layout.compute_row_budget.
      for (let i = 0; i < n; i += 1) {
        if (allocated[i] > 0 && allocated[i] >= largestValue) {
          largestValue = allocated[i];
          largestIdx = i;
        }
      }
      if (largestIdx < 0) {
        break;
      }
      allocated[largestIdx] -= 1;
    }
    return allocated;
  }

  let calendarZeroHeightWarned = false;
  // What the minute tick compares against: which item is "next" and which
  // are past (a change means re-render), and the relative label (a change
  // means one text write).
  let calendarPhaseKey = "";
  let calendarRelativeLabel = "";

  function calendarPhase(classified) {
    const pastKeys = [];
    classified.entries.forEach((entries) => {
      entries.forEach((entry) => {
        if (entry.past) pastKeys.push(entry.key);
      });
    });
    return `${classified.nextKey || ""}|${pastKeys.join(",")}`;
  }

  function nextEntryOf(classified) {
    for (const entries of classified.entries) {
      for (const entry of entries) {
        if (entry.key === classified.nextKey) return entry;
      }
    }
    return null;
  }

  function renderCalendar(calendar) {
    const snapshot = calendar.snapshot || {};
    const sections = normalizeCalendarSections(calendar);
    const todayIso = todayIsoInZone();
    const nowMs = Date.now();
    // Measure before clearing. The grid row is a plain length today, but if it
    // ever becomes content-dependent again an emptied list collapses to 0 and the
    // zero-height fallback below silently renders every event into an
    // overflowing box — which is exactly what `fit-content(48%)` used to do.
    const availableHeight = nodes.calendarList.clientHeight;
    nodes.calendarList.innerHTML = "";
    calendarPhaseKey = "";
    calendarRelativeLabel = "";

    if (sections.length === 0) {
      nodes.calendarList.appendChild(createCalendarPlaceholder(snapshot, nowMs));
      return;
    }

    const classified = classifyCalendar(sections, nowMs);
    const nextEntry = nextEntryOf(classified);
    calendarPhaseKey = calendarPhase(classified);
    calendarRelativeLabel = relativeLabelFor(nextEntry, nowMs);

    // Per section: keep at most the most recent past item (dimmed, as context),
    // drop older ones — they would only eat rows from what is still ahead.
    const visible = classified.entries.map((entries) => {
      const upcoming = entries.filter((entry) => !entry.past);
      const past = entries.filter((entry) => entry.past);
      return past.length ? [past[past.length - 1], ...upcoming] : upcoming;
    });

    // Pre-compute per-section metadata in render order.
    const labels = sections.map((section) =>
      computeDayLabel(section.section_date, todayIso),
    );
    const sectionHasLabel = labels.map((label) => label.length > 0);
    const sectionItemCounts = visible.map((entries) => entries.length);
    const sectionExtraRows = visible.map((entries) =>
      entries.some((entry) => entry.key === classified.nextKey) ? 1 : 0,
    );

    let allocated;
    if (availableHeight <= 0) {
      // If the list hasn't been laid out yet we can't measure, so render
      // everything instead of silently trimming. Warn once.
      if (!calendarZeroHeightWarned) {
        calendarZeroHeightWarned = true;
        window.console.warn(
          "calendar list has clientHeight=0; rendering without row budget",
        );
      }
      allocated = sectionItemCounts.slice();
    } else {
      // Measure one representative row + one label against the live DOM so
      // we pick up the actual CSS dimensions. The highlighted row is counted
      // as one row plus one extra via the budget — it is just under two plain
      // rows tall by design.
      const measureRow = createCalendarRow({ item: { title: "Messung", time_label: "" }, past: false });
      const measureLabel = createCalendarSectionLabel("Messung");
      nodes.calendarList.appendChild(measureRow);
      nodes.calendarList.appendChild(measureLabel);
      const rowHeight = measureRow.offsetHeight || 1;
      const labelHeight = measureLabel.offsetHeight || rowHeight;
      const listGap =
        parseFloat(window.getComputedStyle(nodes.calendarList).rowGap) || 0;
      nodes.calendarList.removeChild(measureRow);
      nodes.calendarList.removeChild(measureLabel);

      const rowUnit = Math.max(rowHeight, labelHeight) + listGap;
      const maxRows = Math.max(
        1,
        Math.floor((availableHeight + listGap) / rowUnit),
      );
      allocated = computeRowBudget(sectionItemCounts, maxRows, sectionHasLabel, sectionExtraRows);
    }

    const fragment = document.createDocumentFragment();
    let visibleRows = 0;
    for (let i = 0; i < sections.length; i += 1) {
      const count = allocated[i];
      if (count <= 0) {
        continue;
      }
      if (sectionHasLabel[i]) {
        fragment.appendChild(createCalendarSectionLabel(labels[i]));
      }
      // Trimming drops the dimmed past item before anything still ahead.
      let shown = visible[i];
      if (count < shown.length && shown[0].past) {
        shown = shown.slice(1);
      }
      shown = shown.slice(0, count);
      for (const entry of shown) {
        if (entry.key === classified.nextKey) {
          fragment.appendChild(createCalendarNextRow(entry, calendarRelativeLabel));
        } else {
          fragment.appendChild(createCalendarRow(entry));
        }
        visibleRows += 1;
      }
    }
    nodes.calendarList.appendChild(fragment);

    if (visibleRows === 0) {
      nodes.calendarList.innerHTML = "";
      nodes.calendarList.appendChild(createCalendarEmpty("Keine Termine", ""));
    }
  }

  // Minute tick: a cheap text write while nothing structural changed, a full
  // re-render once an event ends or the next one changes.
  function refreshCalendarMinute() {
    if (!calendarPhaseKey) {
      return;
    }
    const sections = normalizeCalendarSections(state.calendar || {});
    const nowMs = Date.now();
    const classified = classifyCalendar(sections, nowMs);
    if (calendarPhase(classified) !== calendarPhaseKey) {
      lastRenderedSectionKeys.calendar = "";
      render(state);
      return;
    }
    const label = relativeLabelFor(nextEntryOf(classified), nowMs);
    if (label !== calendarRelativeLabel) {
      calendarRelativeLabel = label;
      const node = nodes.calendarList.querySelector(".agenda__next-rel");
      if (node) {
        node.textContent = label;
      }
    }
  }

  function spotifyVolumeSliders() {
    return [nodes.spotifyDetailVolume].filter(Boolean);
  }

  function setSpotifyVolumeUi(value) {
    const text = value === null ? "" : `${value} %`;
    spotifyVolumeSliders().forEach((slider) => {
      slider.value = String(value === null ? 0 : value);
      slider.style.setProperty("--vol-percent", `${value === null ? 0 : value}%`);
    });
    nodes.spotifyDetailVolumeReadout.textContent = text;
  }

  function clearSpotifyVolumeDirty() {
    spotifyVolumeSliders().forEach((slider) => {
      delete slider.dataset.dirty;
    });
    volumeLastSent = null;
  }

  function renderSpotify(spotify) {
    const snapshot = spotify.snapshot || {};
    const showControls = Boolean(spotify.can_control || spotify.supports_volume);
    const canSeek = Boolean(
      spotify.can_control &&
      typeof spotify.duration_ms === "number" &&
      spotify.duration_ms > 0 &&
      typeof spotify.progress_ms === "number",
    );
    nodes.spotifyDetail.classList.toggle("is-inactive", !showControls);
    // The home bar has four shapes, chosen here and painted by CSS through
    // data-music on the column: playing (96 px, cover + progress), idle (60 px,
    // "Musik starten"), error (60 px, "Nicht erreichbar · zuletzt HH:MM") and
    // hidden (not connected at all — the agenda takes the row). `stale` is one
    // missed refresh and keeps whatever was showing; `error` is two or more.
    let musicMode;
    if (snapshot.status === "error") {
      musicMode = "error";
    } else if (showControls && spotify.track_title) {
      musicMode = "playing";
    } else if (spotify.connected) {
      musicMode = "idle";
    } else {
      musicMode = "hidden";
    }
    if (nodes.cardsColumn) {
      nodes.cardsColumn.dataset.music = musicMode;
    }
    // The picker trigger only makes sense while Spotify is connected. It lives on
    // the Spotify screen only — the home tile leads there instead.
    if (nodes.spotifyDetailOpenPicker) {
      nodes.spotifyDetailOpenPicker.classList.toggle("is-hidden", !spotify.connected);
    }

    setIcon(nodes.spotifyDetailPreviousIcon, "previous");
    setIcon(nodes.spotifyDetailNextIcon, "next");
    const toggleIcon = spotify.is_playing ? "pause" : "play";
    const toggleLabel = spotify.is_playing
      ? "Wiedergabe pausieren"
      : "Wiedergabe starten";
    setIcon(nodes.spotifyDetailToggleIcon, toggleIcon);
    nodes.spotifyDetailToggle.setAttribute("aria-label", toggleLabel);

    const trackLabel =
      spotify.track_title || spotify.empty_message || "Keine aktive Wiedergabe";
    const artistLabel =
      spotify.artist_name ||
      (musicMode === "error" ? "Spotify nicht erreichbar." : "Spotify nicht verbunden.");
    // The home bar doubles as the "start playing" entry point, so with nothing
    // playing it invites instead of reporting. An outage says how old the last
    // good state is rather than quoting the HTTP error — on its own line, so a
    // days-old "zuletzt 15. Okt. 15:10" is never cut off mid-time (#20).
    if (musicMode === "error") {
      const stamp = formatStamp(snapshot.updated_at);
      nodes.spotifyTrack.textContent = "Spotify nicht erreichbar";
      nodes.spotifyArtist.textContent = stamp ? `zuletzt ${stamp}` : "";
    } else if (musicMode === "playing") {
      nodes.spotifyTrack.textContent = spotify.track_title;
      nodes.spotifyArtist.textContent = [spotify.artist_name, spotify.device_name]
        .filter(Boolean)
        .join(" · ");
    } else {
      nodes.spotifyTrack.textContent = "Musik starten";
      nodes.spotifyArtist.textContent = "";
    }
    nodes.spotifyDetailTrack.textContent = trackLabel;
    nodes.spotifyDetailArtist.textContent = artistLabel;
    nodes.spotifyDetailAlbum.textContent = spotify.album_name || "";

    setIcon(nodes.spotifyDetailDeviceIcon, "device");
    const deviceLabel = [spotify.device_name, spotify.device_type]
      .filter(Boolean)
      .join(" · ");
    nodes.spotifyDetailDevice.textContent = deviceLabel || "Kein aktives Gerät";

    const artwork = spotify.album_art_url
      ? `url(${JSON.stringify(spotify.album_art_url)})`
      : "";
    nodes.spotifyArtwork.style.backgroundImage = artwork;
    nodes.spotifyDetailArtwork.style.backgroundImage = artwork;
    nodes.spotifyDetailArtwork.setAttribute(
      "aria-label",
      spotify.album_name ? `Cover von ${spotify.album_name}` : "Kein Albumcover",
    );

    const transportDisabled = !spotify.can_control;
    [
      nodes.spotifyDetailPrevious,
      nodes.spotifyDetailToggle,
      nodes.spotifyDetailNext,
    ].forEach((button) => {
      button.disabled = transportDisabled;
    });
    spotifyVolumeSliders().forEach((slider) => {
      slider.disabled = !spotify.supports_volume;
    });
    nodes.spotifyDetailSeek.disabled = !canSeek;

    // Keep both volume sliders in lockstep. Polls must not overwrite either
    // while the user's value is in flight.
    const now = performance.now();
    const incoming =
      typeof spotify.volume_percent === "number" ? spotify.volume_percent : null;
    const busy = volumeBusyUntil > now;
    const sliders = spotifyVolumeSliders();
    const dirty = sliders.some((slider) => slider.dataset.dirty === "1");
    const active = sliders.some((slider) => slider.matches(":active"));

    if (incoming === null) {
      if (!dirty && !busy && !active) {
        setSpotifyVolumeUi(null);
      }
      return;
    }
    if (busy) {
      return;
    }
    if (dirty) {
      if (
        volumeLastSent !== null &&
        Math.abs(incoming - volumeLastSent) <= VOLUME_TOLERANCE
      ) {
        clearSpotifyVolumeDirty();
        setSpotifyVolumeUi(incoming);
      }
      return;
    }
    setSpotifyVolumeUi(incoming);
  }

  // The music bar's current shape. Read before and after renderSpotify so
  // render() can tell when the agenda row was resized and must re-budget.
  // ---- Weather detail, screen 3 (#22) -------------------------------------
  // Drawn only while the screen is visible, and only when its inputs change:
  // the sections, the selection, and the hour (the quarter hour in rain view)
  // — past hours drop out of the chart on their own, nothing ticks. The
  // server decides which hourly series to send (weather_detail.select_hourly)
  // and when a warning stops being shown (visible_until); this side only
  // compares those times with the clock.
  const HOUR_MS = 3_600_000;
  const QUARTER_MS = 900_000;
  const SCREENSAVER_WARNING_LOOKAHEAD_MS = 24 * HOUR_MS;
  // INCA intervals that must remain before "Kein Niederschlag erwartet" may
  // be said: two of its three hours, every one of them a real 0.
  const NOWCAST_MIN_INTERVALS_FOR_DRY = 8;
  const WX_CHART = {
    width: 968,
    height: 264,
    left: 46,
    right: 56,
    iconTop: 0,
    plotTop: 44,
    plotBottom: 204,
    timeY: 224,
    windY: 252,
  };
  const WIND_DIRECTIONS = ["Nord", "Nordost", "Ost", "Südost", "Süd", "Südwest", "West", "Nordwest"];
  // Order of "how much this matters" when one icon stands for three hours.
  const ICON_WEIGHT = {
    storm: 8,
    snow: 7,
    sleet: 6,
    rain: 5,
    fog: 4,
    cloudy: 3,
    "partly-cloudy": 2,
    "partly-cloudy-night": 2,
    clear: 1,
    "clear-night": 1,
    unknown: 0,
  };
  const HOUR_NUM_FMT = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    hourCycle: "h23",
    timeZone: TIMEZONE,
  });
  // Fixed, like _short_day_label in the backend: Intl's short weekday depends
  // on the engine's ICU ("Mi" in Chromium, not the Duden form "Mi.").
  const WEEKDAY_SHORT = ["So.", "Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa."];
  const WEEKDAY_SHORT_FMT = {
    format: (date) => WEEKDAY_SHORT[new Date(`${SECTION_DATE_FMT.format(date)}T12:00:00Z`).getUTCDay()],
  };
  const DAY_MONTH_FMT = new Intl.DateTimeFormat(LOCALE, {
    day: "2-digit",
    month: "2-digit",
    timeZone: TIMEZONE,
  });
  const LONG_DAY_FMT = new Intl.DateTimeFormat(LOCALE, {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: TIMEZONE,
  });
  const ZONE_PARTS_FMT = new Intl.DateTimeFormat("en-GB", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZone: TIMEZONE,
  });
  const MM_FMT = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const MM_FINE_FMT = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 2 });

  const weatherView = { mode: "hours", range: "next24", day: "" };
  let lastWeatherDetailKey = "";
  let warnSheetOpen = false;

  function svgIcon(name, x, y, size) {
    const markup = icons[name] || icons.unknown;
    return markup.replace("<svg ", `<svg x="${x}" y="${y}" width="${size}" height="${size}" `);
  }

  function escapeXml(text) {
    return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  function fmtTemp(value) {
    return typeof value === "number" ? `${Math.round(value)}°` : "–";
  }

  function fmtKmh(value) {
    return `${Math.round(value)} km/h`;
  }

  function windDirectionName(degrees) {
    return WIND_DIRECTIONS[Math.round((((degrees % 360) + 360) % 360) / 45) % 8];
  }

  // Offset of TIMEZONE from UTC at `ms`, so local midnights can be found
  // without trusting the browser's own zone. Two passes settle a DST day.
  function zoneOffsetMs(ms) {
    const p = {};
    ZONE_PARTS_FMT.formatToParts(new Date(ms)).forEach((part) => {
      p[part.type] = part.value;
    });
    const wall = Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day), Number(p.hour), Number(p.minute), Number(p.second));
    return wall - Math.floor(ms / 1000) * 1000;
  }

  function localMidnightMs(dateIso) {
    const [y, m, d] = dateIso.split("-").map(Number);
    const guess = Date.UTC(y, m - 1, d);
    const first = guess - zoneOffsetMs(guess);
    return guess - zoneOffsetMs(first);
  }

  function addDaysIso(dateIso, days) {
    const [y, m, d] = dateIso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
  }

  // "18:00" today, "morgen 18:00", else "Mi. 18:00".
  function dayTimeLabel(ms, nowMs) {
    const time = CLOCK_TIME_FMT.format(new Date(ms));
    const day = SECTION_DATE_FMT.format(new Date(ms));
    const today = SECTION_DATE_FMT.format(new Date(nowMs));
    if (day === today) return time;
    if (day === addDaysIso(today, 1)) return `morgen ${time}`;
    return `${WEEKDAY_SHORT_FMT.format(new Date(ms))} ${time}`;
  }

  function sectionOf(name) {
    const section = state[name];
    return section && typeof section === "object" ? section : null;
  }

  // ---- Warnings (shared by the detail screen and the screensaver) ---------
  // Mirrors weather_detail.screensaver_warnings: the list arrives sorted and
  // with its visibility end; expiry is a comparison with the clock, so a
  // warning disappears on time even when no fetch lands.
  function visibleWarnings(nowMs, lookaheadMs) {
    const section = sectionOf("weather_warnings");
    const items = section && Array.isArray(section.items) ? section.items : [];
    return items.filter((item) => {
      const until = parseIsoMs(item.visible_until);
      if (until === null || until <= nowMs) return false;
      if (lookaheadMs === null) return true;
      const begins = parseIsoMs(item.begins_at);
      return begins === null || begins < nowMs + lookaheadMs;
    });
  }

  function warningName(item) {
    // Built by the server (weather_codes.warning_title).
    return item.title || "Wetterwarnung";
  }

  function warningTimePhrase(item, nowMs) {
    const begins = parseIsoMs(item.begins_at);
    if (begins !== null && begins > nowMs) {
      return `ab ${dayTimeLabel(begins, nowMs)}`;
    }
    const ends = parseIsoMs(item.ends_at);
    return ends !== null ? `bis ${dayTimeLabel(ends, nowMs)}` : "Ende nicht angegeben";
  }

  function warningsBehindStamp() {
    const section = sectionOf("weather_warnings");
    const snapshot = (section && section.snapshot) || {};
    return isBehind(snapshot) ? formatStamp(snapshot.updated_at) : "";
  }

  // The screensaver line: the most important local warning that is active or
  // begins within 24 h, the rest as "+N". Text only, no ticker, not a target.
  function renderScreensaverWarning() {
    const nowMs = Date.now();
    const list = visibleWarnings(nowMs, SCREENSAVER_WARNING_LOOKAHEAD_MS);
    let text = "";
    let level = "";
    if (list.length) {
      const top = list[0];
      const parts = [warningName(top), warningTimePhrase(top, nowMs)];
      if (list.length > 1) parts.push(`+${list.length - 1}`);
      const stamp = warningsBehindStamp();
      if (stamp) parts.push(`Stand ${stamp}`);
      text = parts.join(" · ");
      level = String(top.level || 0);
    }
    if (nodes.screensaverWarningText.textContent !== text) {
      nodes.screensaverWarningText.textContent = text;
    }
    nodes.screensaverWarning.dataset.level = level;
    nodes.screensaverWarning.classList.toggle("is-hidden", text === "");
  }

  function renderWarningHint(nowMs) {
    const section = sectionOf("weather_warnings");
    const snapshot = (section && section.snapshot) || {};
    const status = snapshot.status || "";
    const list = visibleWarnings(nowMs, null);
    let mode = "";
    let title = "";
    let sub = "";
    let more = "";
    if (!section || status === "disabled") {
      mode = "off";
    } else if (list.length) {
      mode = "active";
      title = warningName(list[0]);
      sub = warningTimePhrase(list[0], nowMs);
      const stamp = warningsBehindStamp();
      if (stamp) sub += ` · Stand ${stamp}`;
      more = list.length > 1 ? `+${list.length - 1}` : "";
    } else if (status === "ok") {
      // Only a successful, valid answer may say this (#22).
      mode = "clear";
      title = "Keine Warnungen";
    } else if (status === "empty") {
      mode = "loading";
      title = "Warnungen werden geladen";
    } else {
      mode = "unavailable";
      title = "Warnungen derzeit nicht verfügbar";
      const stamp = warningsBehindStamp();
      sub = stamp ? `Stand ${stamp}` : "";
    }
    const node = nodes.wxWarn;
    node.dataset.state = mode;
    node.dataset.level = mode === "active" ? String(list[0].level || 0) : "";
    node.classList.toggle("is-hidden", mode === "off");
    node.disabled = mode !== "active";
    nodes.wxWarnTitle.textContent = title;
    nodes.wxWarnSub.textContent = sub;
    nodes.wxWarnSub.classList.toggle("is-hidden", sub === "");
    nodes.wxWarnMore.textContent = more;
    nodes.wxWarnMore.classList.toggle("is-hidden", more === "");
    if (warnSheetOpen) {
      if (mode === "active") renderWarnSheet(list, nowMs);
      else closeWarnSheet();
    }
  }

  function periodLabel(item) {
    const begins = parseIsoMs(item.begins_at);
    const ends = parseIsoMs(item.ends_at);
    const stamp = (ms) => `${WEEKDAY_SHORT_FMT.format(new Date(ms))} ${DAY_MONTH_FMT.format(new Date(ms))}, ${CLOCK_TIME_FMT.format(new Date(ms))}`;
    if (begins === null && ends === null) return "Zeitraum nicht angegeben";
    if (begins === null) return `bis ${stamp(ends)}`;
    if (ends === null) return `ab ${stamp(begins)} · Ende nicht angegeben`;
    const sameDay = SECTION_DATE_FMT.format(new Date(begins)) === SECTION_DATE_FMT.format(new Date(ends));
    return sameDay ? `${stamp(begins)} – ${CLOCK_TIME_FMT.format(new Date(ends))}` : `${stamp(begins)} – ${stamp(ends)}`;
  }

  function warnBlock(label, text) {
    const block = document.createElement("section");
    block.className = "warn-card__block";
    const heading = document.createElement("h4");
    heading.textContent = label;
    const body = document.createElement("p");
    body.textContent = text;
    block.append(heading, body);
    return block;
  }

  // The open sheet is rebuilt only when what it shows changes — a rebuild on
  // every poll would cut off a touch scroll in the middle of a long text.
  let warnSheetKey = "";

  function renderWarnSheet(list, nowMs, force = false) {
    const section = sectionOf("weather_warnings") || {};
    const snapshot = section.snapshot || {};
    const key = JSON.stringify([
      list,
      list.map((item) => (parseIsoMs(item.begins_at) || 0) <= nowMs),
      section.municipality_name,
      formatStamp(snapshot.updated_at),
      isBehind(snapshot),
    ]);
    if (!force && key === warnSheetKey) return;
    warnSheetKey = key;
    nodes.warnSheetPlace.textContent = section.municipality_name || "";
    const fragment = document.createDocumentFragment();
    list.forEach((item) => {
      const card = document.createElement("article");
      card.className = "warn-card";
      card.dataset.level = String(item.level || 0);
      const title = document.createElement("h3");
      title.className = "warn-card__title";
      title.textContent = warningName(item);
      const period = document.createElement("div");
      period.className = "warn-card__period";
      const active = (parseIsoMs(item.begins_at) || 0) <= nowMs;
      period.textContent = `${active ? "Aktiv · " : ""}${periodLabel(item)}`;
      card.append(title, period);
      if (item.text) card.appendChild(warnBlock("Warnung", item.text));
      if (item.impacts) card.appendChild(warnBlock("Mögliche Auswirkungen", item.impacts));
      if (item.recommendations) card.appendChild(warnBlock("Empfehlungen", item.recommendations));
      if (item.meteo_text) card.appendChild(warnBlock("Wetterlage", item.meteo_text));
      fragment.appendChild(card);
    });
    nodes.warnSheetBody.replaceChildren(fragment);
    const stamp = formatStamp(snapshot.updated_at);
    nodes.warnSheetFooter.textContent =
      `Quelle: GeoSphere Austria, CC BY 4.0${stamp ? ` · Stand ${stamp}` : ""}`;
    nodes.warnSheetFooter.classList.toggle("is-behind", isBehind(snapshot));
  }

  function openWarnSheet() {
    if (screensaverActive) return;
    const nowMs = Date.now();
    const list = visibleWarnings(nowMs, null);
    if (!list.length) return;
    renderWarnSheet(list, nowMs, true);
    nodes.warnSheetBody.scrollTop = 0;
    warnSheetOpen = true;
    nodes.warnSheet.classList.add("is-open");
    nodes.warnSheet.setAttribute("aria-hidden", "false");
  }

  function closeWarnSheet() {
    if (!warnSheetOpen) return;
    warnSheetOpen = false;
    nodes.warnSheet.classList.remove("is-open");
    nodes.warnSheet.setAttribute("aria-hidden", "true");
  }

  // ---- Current conditions ---------------------------------------------------
  function forecastForDate(dateIso) {
    const weather = state.weather || {};
    const list = Array.isArray(weather.forecast) ? weather.forecast : [];
    return list.find((item) => item.date === dateIso) || null;
  }

  function renderWeatherNow(nowMs) {
    const weather = state.weather || {};
    const snapshot = weather.snapshot || {};
    const hasTemp = typeof weather.temperature_c === "number";
    nodes.wxTemp.textContent = fmtTemp(weather.temperature_c);
    setIcon(nodes.wxIcon, hasTemp ? weather.icon || weatherIconName(weather.condition_code) : "");
    let condition = weather.condition || "";
    if (snapshot.status === "disabled") condition = "Wetter nicht eingerichtet";
    else if (!hasTemp && (snapshot.status === "empty" || !snapshot.status)) condition = "Wetterdaten werden geladen.";
    else if (!hasTemp) condition = "Wetter nicht verfügbar";
    nodes.wxCondition.textContent = condition;

    const today = forecastForDate(SECTION_DATE_FMT.format(new Date(nowMs)));
    const facts = [];
    if (typeof weather.apparent_temperature_c === "number") facts.push(`Gefühlt ${fmtTemp(weather.apparent_temperature_c)}`);
    if (today && typeof today.temperature_min_c === "number" && typeof today.temperature_max_c === "number") {
      facts.push(`Heute ${fmtTemp(today.temperature_min_c)} bis ${fmtTemp(today.temperature_max_c)}`);
    }
    nodes.wxFacts.textContent = facts.join(" · ");

    let wind = "";
    if (typeof weather.wind_speed_kmh === "number") {
      if (weather.wind_speed_kmh < 1) {
        wind = "Windstill";
      } else {
        wind = `Wind ${fmtKmh(weather.wind_speed_kmh)}`;
        if (typeof weather.wind_direction_deg === "number") wind += ` aus ${windDirectionName(weather.wind_direction_deg)}`;
      }
      if (typeof weather.wind_gusts_kmh === "number" && weather.wind_gusts_kmh >= 1) wind += ` · Böen ${fmtKmh(weather.wind_gusts_kmh)}`;
    }
    nodes.wxWind.textContent = wind;

    // Model data, not a station reading — say what it is and for when.
    const meta = [];
    const stamp = isBehind(snapshot) ? formatStamp(snapshot.updated_at) : "";
    if (stamp) {
      meta.push(`Stand ${stamp}`);
    } else if (hasTemp) {
      const at = parseIsoMs(weather.observed_at);
      meta.push(at !== null ? `Modellwert für ${CLOCK_TIME_FMT.format(new Date(at))}` : "Modellwert");
    }
    const rise = today ? parseIsoMs(today.sunrise) : null;
    const set = today ? parseIsoMs(today.sunset) : null;
    if (rise !== null && set !== null) {
      meta.push(`Sonne ${CLOCK_TIME_FMT.format(new Date(rise))} – ${CLOCK_TIME_FMT.format(new Date(set))}`);
    }
    nodes.wxMeta.textContent = meta.join(" · ");
    nodes.wxMeta.classList.toggle("is-behind", stamp !== "");
  }

  // ---- Chart ----------------------------------------------------------------
  function wxMessage(title, subtitle) {
    const box = document.createElement("div");
    box.className = "wx-message";
    const strong = document.createElement("div");
    strong.className = "wx-message__title";
    strong.textContent = title;
    box.appendChild(strong);
    if (subtitle) {
      const sub = document.createElement("div");
      sub.className = "wx-message__sub";
      sub.textContent = subtitle;
      box.appendChild(sub);
    }
    return box;
  }

  function niceStep(span, maxLines) {
    const steps = [1, 2, 5, 10, 20];
    return steps.find((step) => span / step <= maxLines) || 20;
  }

  function hourlyRange(nowMs) {
    const hourStart = Math.floor(nowMs / HOUR_MS) * HOUR_MS;
    const todayIso = SECTION_DATE_FMT.format(new Date(nowMs));
    if (weatherView.range === "today") {
      return { start: hourStart, end: localMidnightMs(addDaysIso(todayIso, 1)), label: "heute" };
    }
    if (weatherView.range === "tomorrow") {
      const tomorrow = addDaysIso(todayIso, 1);
      return { start: localMidnightMs(tomorrow), end: localMidnightMs(addDaysIso(tomorrow, 1)), label: "morgen" };
    }
    return { start: hourStart, end: hourStart + 24 * HOUR_MS, label: "die nächsten 24 Stunden" };
  }

  function hourlyMessage(series, range) {
    const snapshot = series.snapshot || {};
    const weatherStatus = ((state.weather || {}).snapshot || {}).status;
    if (weatherStatus === "disabled") return wxMessage("Wetter nicht eingerichtet", "");
    const anyPoints = Array.isArray(series.points) && series.points.length > 0;
    if (snapshot.status === "disabled") return wxMessage("Stundenwerte nicht eingerichtet", "");
    if (!anyPoints && (snapshot.status === "empty" || !snapshot.status)) {
      return wxMessage("Stundenwerte werden geladen.", "");
    }
    if (!anyPoints) {
      return wxMessage("Stundenwerte derzeit nicht verfügbar", "Weder GeoSphere noch Open-Meteo haben geantwortet.");
    }
    const until = parseIsoMs(series.valid_to);
    return wxMessage(
      `Keine Stundenwerte für ${range.label}`,
      until !== null ? `Die letzten Modellwerte reichen bis ${dayTimeLabel(until, Date.now())}.` : "",
    );
  }

  function renderHourlyChart(nowMs) {
    const series = sectionOf("weather_hourly") || {};
    const range = hourlyRange(nowMs);
    const points = (Array.isArray(series.points) ? series.points : [])
      .map((point) => ({ ...point, ms: parseIsoMs(point.time) }))
      .filter((point) => point.ms !== null && point.ms >= range.start && point.ms <= range.end);
    const temps = points.filter((p) => typeof p.temperature_c === "number");
    if (!temps.length) {
      nodes.wxChart.replaceChildren(hourlyMessage(series, range));
      return;
    }
    const C = WX_CHART;
    const plotW = C.width - C.left - C.right;
    const x = (ms) => C.left + ((ms - range.start) / (range.end - range.start)) * plotW;
    let lo = Math.min(...temps.map((p) => p.temperature_c));
    let hi = Math.max(...temps.map((p) => p.temperature_c));
    if (hi - lo < 6) {
      const mid = (hi + lo) / 2;
      lo = mid - 3;
      hi = mid + 3;
    }
    const step = niceStep(hi - lo, 4);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    const yT = (v) => C.plotBottom - ((v - lo) / (hi - lo)) * (C.plotBottom - C.plotTop);
    const precip = points.filter((p) => typeof p.precipitation_mm === "number" && p.ms > range.start);
    const pMax = Math.max(1, Math.ceil(Math.max(0, ...precip.map((p) => p.precipitation_mm))));
    const yP = (v) => C.plotBottom - (v / pMax) * (C.plotBottom - C.plotTop);

    const out = [];
    out.push(`<svg class="wx-chart" viewBox="0 0 ${C.width} ${C.height}" width="${C.width}" height="${C.height}" role="img" aria-label="Temperatur und Niederschlag für ${escapeXml(range.label)}">`);
    // Temperature grid and its scale (left), precipitation scale (right).
    for (let v = lo; v <= hi + 0.001; v += step) {
      const y = yT(v).toFixed(1);
      out.push(`<line class="wx-grid" x1="${C.left}" x2="${C.width - C.right}" y1="${y}" y2="${y}"></line>`);
      out.push(`<text class="wx-axis wx-axis--temp" x="${C.left - 8}" y="${(Number(y) + 5).toFixed(1)}" text-anchor="end">${v}°</text>`);
    }
    out.push(`<text class="wx-axis wx-axis--rain" x="${C.width - C.right + 8}" y="${C.plotTop + 5}">${pMax} mm</text>`);
    out.push(`<text class="wx-axis wx-axis--rain" x="${C.width - C.right + 8}" y="${C.plotBottom + 5}">0</text>`);
    out.push(`<text class="wx-axis wx-axis--unit" x="${C.width - C.right + 8}" y="${C.plotTop + 22}">je Std</text>`);
    // Precipitation: each bar covers the hour that ends at its point.
    precip.forEach((p) => {
      if (p.precipitation_mm <= 0) return;
      const x0 = Math.max(x(p.ms - HOUR_MS), C.left) + 1.5;
      const x1 = x(p.ms) - 1.5;
      const y = yP(Math.min(p.precipitation_mm, pMax));
      out.push(`<rect class="wx-bar" x="${x0.toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(x1 - x0, 2).toFixed(1)}" height="${(C.plotBottom - y).toFixed(1)}" rx="2"></rect>`);
    });
    // Temperature line, broken where a value is missing — never bridged.
    let run = [];
    const flush = () => {
      if (run.length > 1) out.push(`<polyline class="wx-line" points="${run.join(" ")}"></polyline>`);
      if (run.length === 1) {
        const [cx, cy] = run[0].split(",");
        out.push(`<circle class="wx-dot" cx="${cx}" cy="${cy}" r="3"></circle>`);
      }
      run = [];
    };
    points.forEach((p) => {
      if (typeof p.temperature_c === "number") run.push(`${x(p.ms).toFixed(1)},${yT(p.temperature_c).toFixed(1)}`);
      else flush();
    });
    flush();
    // Every three local hours: time, temperature, and a three-hour icon and
    // wind summary. Hours are found by the zone's clock, so a 23- or 25-hour
    // day gets the right marks.
    const byMs = new Map(points.map((p) => [p.ms, p]));
    const blockIcon = (from, to) => {
      const block = points.filter((p) => p.ms > from && p.ms <= to);
      if (!block.length) return "";
      return block.reduce(
        (best, p) => ((ICON_WEIGHT[p.icon] || 0) > (ICON_WEIGHT[best] || 0) ? p.icon : best),
        block[Math.floor(block.length / 2)].icon || "unknown",
      );
    };
    const drawBlockIcon = (from, to) => {
      const icon = blockIcon(from, to);
      const xc = (x(from) + x(Math.min(to, range.end))) / 2;
      if (icon && xc - 14 >= C.left && xc + 14 <= C.width - C.right) {
        out.push(svgIcon(icon, (xc - 14).toFixed(1), C.iconTop + 4, 28));
      }
    };
    // The hours before the first three-hour mark get their own icon when
    // there is room for one.
    let firstMark = Math.ceil(range.start / HOUR_MS) * HOUR_MS;
    while (firstMark < range.end && Number(HOUR_NUM_FMT.format(new Date(firstMark))) % 3 !== 0) {
      firstMark += HOUR_MS;
    }
    if (firstMark - range.start >= 2 * HOUR_MS) drawBlockIcon(range.start, firstMark);
    for (let t = Math.ceil(range.start / HOUR_MS) * HOUR_MS; t <= range.end; t += HOUR_MS) {
      const hour = Number(HOUR_NUM_FMT.format(new Date(t)));
      if (hour % 3 !== 0) continue;
      const xm = x(t);
      if (xm > C.left + 4 || t === range.start) {
        out.push(`<line class="wx-tick" x1="${xm.toFixed(1)}" x2="${xm.toFixed(1)}" y1="${C.plotBottom}" y2="${C.plotBottom + 6}"></line>`);
      }
      const label = hour === 0 && t !== range.start ? WEEKDAY_SHORT_FMT.format(new Date(t)) : CLOCK_TIME_FMT.format(new Date(t));
      if (xm <= C.width - C.right - 20) {
        out.push(`<text class="wx-time" x="${xm.toFixed(1)}" y="${C.timeY}" text-anchor="${xm < C.left + 20 ? "start" : "middle"}">${label}</text>`);
      }
      const point = byMs.get(t);
      if (point && typeof point.temperature_c === "number" && xm <= C.width - C.right - 12) {
        out.push(`<text class="wx-value" x="${xm.toFixed(1)}" y="${(yT(point.temperature_c) - 10).toFixed(1)}" text-anchor="middle">${fmtTemp(point.temperature_c)}</text>`);
      }
      // Hours ending inside the next three: the strongest condition stands
      // for them.
      if (t + 2 * HOUR_MS <= range.end) drawBlockIcon(t, t + 3 * HOUR_MS);
      if (point && typeof point.wind_speed_kmh === "number" && xm >= C.left && xm <= C.width - C.right - 30) {
        const anchor = xm < C.left + 30 ? xm + 24 : xm;
        let windText = `${Math.round(point.wind_speed_kmh)}`;
        if (typeof point.wind_gusts_kmh === "number") windText += `<tspan class="wx-gust"> / ${Math.round(point.wind_gusts_kmh)}</tspan>`;
        if (typeof point.wind_direction_deg === "number" && point.wind_speed_kmh >= 1) {
          // The arrow points where the wind blows to.
          out.push(`<g class="wx-arrow" transform="translate(${(anchor - 20).toFixed(1)} ${C.windY - 5}) rotate(${(point.wind_direction_deg + 180) % 360})"><path d="M0 -7 L4.5 5 L0 2.5 L-4.5 5 Z"></path></g>`);
        }
        out.push(`<text class="wx-wind" x="${(anchor - 10).toFixed(1)}" y="${C.windY}">${windText}</text>`);
      }
    }
    out.push(`<text class="wx-axis wx-axis--unit" x="${C.left - 8}" y="${C.windY}" text-anchor="end">km/h</text>`);
    // Where the model stops before the range does: say so, draw nothing.
    const last = points.length ? points[points.length - 1].ms : null;
    if (last !== null && range.end - last > HOUR_MS) {
      const xl = x(last);
      out.push(`<line class="wx-horizon" x1="${xl.toFixed(1)}" x2="${xl.toFixed(1)}" y1="${C.plotTop - 6}" y2="${C.plotBottom}"></line>`);
      if (C.width - C.right - xl > 120) {
        out.push(`<text class="wx-horizon-text" x="${(xl + 10).toFixed(1)}" y="${((C.plotTop + C.plotBottom) / 2).toFixed(1)}">Modell endet ${dayTimeLabel(last, nowMs)}</text>`);
      }
    }
    out.push("</svg>");
    nodes.wxChart.innerHTML = out.join("");
  }

  function renderRainChart(nowMs) {
    const nowcast = sectionOf("weather_nowcast") || {};
    const snapshot = nowcast.snapshot || {};
    const weatherStatus = ((state.weather || {}).snapshot || {}).status;
    const interval = (nowcast.interval_minutes || 15) * 60_000;
    const points = (Array.isArray(nowcast.points) ? nowcast.points : [])
      .map((p) => ({ ...p, ms: parseIsoMs(p.ends_at) }))
      .filter((p) => p.ms !== null && p.ms > nowMs);
    if (weatherStatus === "disabled") {
      nodes.wxChart.replaceChildren(wxMessage("Wetter nicht eingerichtet", ""));
      return;
    }
    if (!points.length) {
      let message;
      if (snapshot.status === "disabled") {
        message = wxMessage("Regenvorschau für diesen Standort nicht verfügbar", "INCA deckt nur Österreich und die Randgebiete ab.");
      } else if (snapshot.status === "empty" || !snapshot.status) {
        message = wxMessage("Regenvorschau wird geladen.", "");
      } else if (Array.isArray(nowcast.points) && nowcast.points.length) {
        message = wxMessage("Regenvorschau veraltet", `Stand ${formatStamp(snapshot.updated_at)} — die Werte sind bereits vorbei.`);
      } else {
        message = wxMessage("Regenvorschau derzeit nicht verfügbar", "");
      }
      nodes.wxChart.replaceChildren(message);
      return;
    }
    const C = WX_CHART;
    const known = points.filter((p) => typeof p.precipitation_mm === "number");
    const wet = known.filter((p) => p.precipitation_mm > 0);
    const complete = known.length === points.length;
    let summary;
    if (!wet.length && complete && snapshot.status === "ok" && points.length >= NOWCAST_MIN_INTERVALS_FOR_DRY) {
      summary = "Kein Niederschlag erwartet";
    } else if (wet.length) {
      summary = `Niederschlag erwartet · bis ${MM_FINE_FMT.format(Math.max(...wet.map((p) => p.precipitation_mm)))} mm in 15 Minuten`;
      if (complete) {
        const minutes = Math.round((points.length * interval) / 60_000);
        const span = minutes % 60 === 0 ? `${minutes / 60} ${minutes === 60 ? "Stunde" : "Stunden"}` : `${minutes} Minuten`;
        summary += ` · zusammen ${MM_FMT.format(known.reduce((sum, p) => sum + p.precipitation_mm, 0))} mm in ${span}`;
      }
    } else {
      summary = complete ? "Bisher kein Niederschlag berechnet" : "Nicht für alle Intervalle Werte vorhanden";
    }
    const start = points[0].ms - interval;
    const end = points[points.length - 1].ms;
    const plotW = C.width - C.left - C.right;
    const x = (ms) => C.left + ((ms - start) / (end - start)) * plotW;
    const max = Math.max(0.5, Math.ceil(Math.max(0, ...known.map((p) => p.precipitation_mm)) * 2) / 2);
    const plotTop = C.plotTop + 4;
    const plotBottom = C.timeY - 22;
    const y = (v) => plotBottom - (v / max) * (plotBottom - plotTop);
    const out = [];
    out.push(`<svg class="wx-chart" viewBox="0 0 ${C.width} ${C.height}" width="${C.width}" height="${C.height}" role="img" aria-label="Niederschlag der nächsten drei Stunden">`);
    out.push(`<text class="wx-summary" x="${C.left}" y="22">${escapeXml(summary)}</text>`);
    [0, max / 2, max].forEach((v) => {
      const yy = y(v).toFixed(1);
      out.push(`<line class="wx-grid" x1="${C.left}" x2="${C.width - C.right}" y1="${yy}" y2="${yy}"></line>`);
      out.push(`<text class="wx-axis wx-axis--rain" x="${C.left - 8}" y="${(Number(yy) + 5).toFixed(1)}" text-anchor="end">${MM_FINE_FMT.format(v)}</text>`);
    });
    out.push(`<text class="wx-axis wx-axis--unit" x="${C.width - C.right + 8}" y="${plotTop + 5}">mm</text>`);
    out.push(`<text class="wx-axis wx-axis--unit" x="${C.width - C.right + 8}" y="${plotTop + 22}">je 15 Min</text>`);
    points.forEach((p) => {
      const x0 = x(p.ms - interval) + 3;
      const w = x(p.ms) - x0 - 3;
      if (typeof p.precipitation_mm !== "number") {
        out.push(`<rect class="wx-bar-missing" x="${x0.toFixed(1)}" y="${plotTop}" width="${w.toFixed(1)}" height="${plotBottom - plotTop}" rx="3"></rect>`);
        out.push(`<text class="wx-value" x="${(x0 + w / 2).toFixed(1)}" y="${(plotBottom - 8).toFixed(1)}" text-anchor="middle">?</text>`);
        return;
      }
      if (p.precipitation_mm > 0) {
        const yy = y(Math.min(p.precipitation_mm, max));
        out.push(`<rect class="wx-bar" x="${x0.toFixed(1)}" y="${yy.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(plotBottom - yy, 2).toFixed(1)}" rx="3"></rect>`);
        out.push(`<text class="wx-value" x="${(x0 + w / 2).toFixed(1)}" y="${(yy - 8).toFixed(1)}" text-anchor="middle">${MM_FINE_FMT.format(p.precipitation_mm)}</text>`);
      } else {
        out.push(`<rect class="wx-bar-zero" x="${x0.toFixed(1)}" y="${(plotBottom - 2).toFixed(1)}" width="${w.toFixed(1)}" height="2"></rect>`);
      }
    });
    // Interval boundaries on the half hour, labelled with the local time.
    for (let t = start; t <= end; t += interval) {
      const label = CLOCK_TIME_FMT.format(new Date(t));
      if (!label.endsWith("00") && !label.endsWith("30")) continue;
      const xm = x(t);
      out.push(`<line class="wx-tick" x1="${xm.toFixed(1)}" x2="${xm.toFixed(1)}" y1="${plotBottom}" y2="${plotBottom + 6}"></line>`);
      out.push(`<text class="wx-time" x="${xm.toFixed(1)}" y="${C.timeY}" text-anchor="${t === start ? "start" : t === end ? "end" : "middle"}">${label}</text>`);
    }
    out.push(`<text class="wx-note" x="${C.left}" y="${C.windY}">Jeder Balken: Niederschlag in den 15 Minuten bis zur rechten Kante.</text>`);
    out.push("</svg>");
    nodes.wxChart.innerHTML = out.join("");
  }

  // A day beyond the hourly coverage: its Open-Meteo day values, and an
  // honest line instead of an empty chart.
  function renderFarDay(dateIso) {
    const item = forecastForDate(dateIso);
    if (!item) {
      nodes.wxChart.replaceChildren(wxMessage("Für diesen Tag liegen keine Werte vor", ""));
      return;
    }
    const card = document.createElement("div");
    card.className = "wx-day";
    const icon = document.createElement("div");
    icon.className = "wx-day__icon";
    setIcon(icon, item.icon || "unknown");
    const copy = document.createElement("div");
    copy.className = "wx-day__copy";
    const title = document.createElement("h2");
    title.className = "wx-day__title";
    title.textContent = LONG_DAY_FMT.format(new Date(localMidnightMs(dateIso) + 12 * HOUR_MS));
    const condition = document.createElement("div");
    condition.className = "wx-day__condition";
    condition.textContent = item.condition || "Unbekannt";
    const facts = document.createElement("div");
    facts.className = "wx-day__facts";
    const parts = [`Höchstwert ${fmtTemp(item.temperature_max_c)}`, `Tiefstwert ${fmtTemp(item.temperature_min_c)}`];
    parts.push(typeof item.precipitation_probability_max === "number" ? `Regenrisiko ${item.precipitation_probability_max} %` : "Regenrisiko –");
    parts.push(typeof item.precipitation_sum_mm === "number" ? `Niederschlag ${MM_FMT.format(item.precipitation_sum_mm)} mm` : "Niederschlag –");
    facts.textContent = parts.join(" · ");
    const sun = document.createElement("div");
    sun.className = "wx-day__sun";
    const rise = parseIsoMs(item.sunrise);
    const set = parseIsoMs(item.sunset);
    sun.textContent = rise !== null && set !== null ? `Sonne ${CLOCK_TIME_FMT.format(new Date(rise))} – ${CLOCK_TIME_FMT.format(new Date(set))}` : "";
    const hint = document.createElement("div");
    hint.className = "wx-day__hint";
    hint.textContent = "Stundenverlauf gibt es nur für heute und morgen. Für diesen Tag zeigt Open-Meteo Tageswerte.";
    copy.append(title, condition, facts, sun, hint);
    card.append(icon, copy);
    nodes.wxChart.replaceChildren(card);
  }

  function renderWeek(nowMs) {
    const weather = state.weather || {};
    const todayIso = SECTION_DATE_FMT.format(new Date(nowMs));
    const items = (Array.isArray(weather.forecast) ? weather.forecast : [])
      .filter((item) => item.date && item.date >= todayIso)
      .slice(0, 7);
    if (!items.length) {
      const snapshot = weather.snapshot || {};
      const text = snapshot.status === "disabled" ? "" : snapshot.status === "empty" || !snapshot.status ? "Tageswerte werden geladen." : "Tageswerte derzeit nicht verfügbar";
      nodes.wxWeek.replaceChildren(wxMessage(text, ""));
      return;
    }
    const selected = weatherView.range === "today" ? todayIso : weatherView.range === "tomorrow" ? addDaysIso(todayIso, 1) : weatherView.day;
    const fragment = document.createDocumentFragment();
    items.forEach((item) => {
      const cell = document.createElement("button");
      cell.type = "button";
      cell.className = "wx-week__day";
      cell.dataset.date = item.date;
      cell.classList.toggle("is-selected", weatherView.mode === "hours" && item.date === selected);
      const name = document.createElement("span");
      name.className = "wx-week__name";
      name.textContent = item.date === todayIso ? "Heute" : item.date === addDaysIso(todayIso, 1) ? "Morgen" : WEEKDAY_SHORT_FMT.format(new Date(localMidnightMs(item.date) + 12 * HOUR_MS));
      const icon = document.createElement("span");
      icon.className = "wx-week__icon";
      setIcon(icon, item.icon || "unknown");
      const temps = document.createElement("span");
      temps.className = "wx-week__temps";
      temps.textContent = fmtTemp(item.temperature_max_c);
      const min = document.createElement("span");
      min.className = "wx-week__min";
      min.textContent = fmtTemp(item.temperature_min_c);
      temps.appendChild(min);
      const rain = document.createElement("span");
      rain.className = "wx-week__rain";
      rain.textContent = typeof item.precipitation_probability_max === "number" ? `${item.precipitation_probability_max} %` : "– %";
      cell.setAttribute("aria-label", `${name.textContent}: ${item.condition || ""}`);
      cell.append(name, icon, temps, rain);
      fragment.appendChild(cell);
    });
    nodes.wxWeek.replaceChildren(fragment);
  }

  // No source line while all is well (the credit sits in the quick sheet).
  // A problem shows in amber beside the controls, where it applies; current
  // values and the week say "Stand …" in #wx-meta. The part that matters
  // comes first, so an ellipsis can only cut the rest.
  function renderWeatherNote() {
    let text = "";
    let behind = false;
    if (weatherView.mode === "rain") {
      const snapshot = (sectionOf("weather_nowcast") || {}).snapshot || {};
      const stamp = isBehind(snapshot) ? formatStamp(snapshot.updated_at) : "";
      behind = stamp !== "";
      text = behind ? `Regenvorschau · Stand ${stamp}` : "Nächste 3 Stunden · Niederschlag je 15 Minuten";
    } else if (weatherView.range !== "day") {
      const series = sectionOf("weather_hourly") || {};
      const snapshot = series.snapshot || {};
      const stamp = isBehind(snapshot) ? formatStamp(snapshot.updated_at) : "";
      if (series.fallback) text = stamp ? `Stunden von Open-Meteo · Stand ${stamp}` : "GeoSphere gestört · Stunden von Open-Meteo";
      else if (stamp) text = `Stundenwerte · Stand ${stamp}`;
      behind = text !== "";
    }
    if (nodes.wxNote.textContent !== text) nodes.wxNote.textContent = text;
    nodes.wxNote.classList.toggle("is-behind", behind);
  }

  function syncWeatherControls() {
    nodes.wxMode.querySelectorAll("[data-wx-mode]").forEach((button) => {
      button.classList.toggle("is-selected", button.dataset.wxMode === weatherView.mode);
    });
    const hours = weatherView.mode === "hours";
    nodes.wxRange.classList.toggle("is-hidden", !hours);
    nodes.wxRange.querySelectorAll("[data-wx-range]").forEach((button) => {
      button.classList.toggle("is-selected", hours && weatherView.range === button.dataset.wxRange);
    });
  }

  function weatherDetailKey(nowMs) {
    const bucket = weatherView.mode === "rain" ? Math.floor(nowMs / QUARTER_MS) : Math.floor(nowMs / HOUR_MS);
    return sectionRenderKey([
      state.weather,
      state.weather_hourly,
      weatherView.mode === "rain" ? state.weather_nowcast : null,
      weatherView,
      bucket,
      SECTION_DATE_FMT.format(new Date(nowMs)),
    ]);
  }

  // Everything except the warning hint, which the minute tick refreshes on
  // its own because a warning ends on the minute, not on the hour.
  function renderWeatherDetail(force) {
    if (activeScreen !== "weather" || screensaverActive) return;
    const nowMs = Date.now();
    renderWarningHint(nowMs);
    const key = weatherDetailKey(nowMs);
    if (!force && key === lastWeatherDetailKey) return;
    lastWeatherDetailKey = key;
    syncWeatherControls();
    renderWeatherNow(nowMs);
    if (weatherView.mode === "rain") renderRainChart(nowMs);
    else if (weatherView.range === "day") renderFarDay(weatherView.day);
    else renderHourlyChart(nowMs);
    renderWeatherNote();
    renderWeek(nowMs);
  }

  function selectWeatherView(change) {
    Object.assign(weatherView, change);
    renderWeatherDetail(true);
  }

  function resetWeatherView() {
    weatherView.mode = "hours";
    weatherView.range = "next24";
    weatherView.day = "";
    lastWeatherDetailKey = "";
  }

  function selectWeatherDay(dateIso) {
    const todayIso = SECTION_DATE_FMT.format(new Date());
    if (dateIso === todayIso) selectWeatherView({ mode: "hours", range: "today", day: "" });
    else if (dateIso === addDaysIso(todayIso, 1)) selectWeatherView({ mode: "hours", range: "tomorrow", day: "" });
    else selectWeatherView({ mode: "hours", range: "day", day: dateIso });
  }

  function spotifyColumnLayoutKey() {
    return nodes.cardsColumn ? nodes.cardsColumn.dataset.music || "" : null;
  }

  function sectionRenderKey(section, omittedKeys = null) {
    try {
      return JSON.stringify(section || {}, (innerKey, value) =>
        omittedKeys && omittedKeys.has(innerKey) ? undefined : value,
      );
    } catch (error) {
      return "";
    }
  }

  function render(nextState, options = {}) {
    const next = nextState || state;
    // Refresh the progress baseline on every poll before per-section dedup, so
    // the bar stays accurate even when nothing else in the payload changed.
    updateSpotifyProgress(next.spotify || {});
    state = next;

    // Provider jobs update independently. A Spotify timestamp must not rebuild
    // the weather forecast and calendar (including forced layout measurement),
    // and a weather refresh must not reparse all Spotify SVG controls.
    const keys = {
      weather: sectionRenderKey(state.weather),
      calendar: sectionRenderKey(state.calendar),
      spotify: sectionRenderKey(
        state.spotify,
        SPOTIFY_PROGRESS_KEYS,
      ),
    };
    if (options.force || !keys.weather || keys.weather !== lastRenderedSectionKeys.weather) {
      renderWeather(state.weather || {});
      lastRenderedSectionKeys.weather = keys.weather;
    }
    if (options.force || !keys.spotify || keys.spotify !== lastRenderedSectionKeys.spotify) {
      const spotifyLayoutBefore = spotifyColumnLayoutKey();
      renderSpotify(state.spotify || {});
      lastRenderedSectionKeys.spotify = keys.spotify;
      // renderSpotify sets `data-music`, which resizes the calendar's
      // grid row. renderCalendar budgets its visible rows from the live
      // clientHeight, so it has to re-measure or it silently clips (or
      // under-fills) against the previous height.
      if (spotifyColumnLayoutKey() !== spotifyLayoutBefore) {
        lastRenderedSectionKeys.calendar = "";
      }
    }
    if (options.force || !keys.calendar || keys.calendar !== lastRenderedSectionKeys.calendar) {
      renderCalendar(state.calendar || {});
      lastRenderedSectionKeys.calendar = keys.calendar;
    }
    renderStatusRail();
    // #22: cheap — both compare text before they write. The detail screen
    // renders only while it is the visible one.
    renderScreensaverWarning();
    renderWeatherDetail(Boolean(options.force));
    // Ticket #3: timers are keyed on identity + ends_at, not on the server's
    // remaining_seconds, which changes every poll by definition.
    const timerKey = sectionRenderKey(
      (Array.isArray(state.timers) ? state.timers : []).map((t) => [t.id, t.ends_at, t.label]),
    );
    if (options.force || timerKey !== lastRenderedSectionKeys.timers) {
      applyTimers(Array.isArray(state.timers) ? state.timers : []);
      lastRenderedSectionKeys.timers = timerKey;
    }
    const systemKey = sectionRenderKey(state.system, new Set(["generated_at"]));
    if (options.force || systemKey !== lastRenderedSectionKeys.system) {
      renderSystem(state.system || {});
      lastRenderedSectionKeys.system = systemKey;
    }
  }

  // Ticket #1: the clock is the hero. If timesyncd has not stepped it yet the
  // digits are fake-hwclock's guess — say so under the date, nothing else.
  function renderSystem(system) {
    const synced = system.time_synced !== false;
    nodes.clockSyncHint.classList.toggle("is-hidden", synced);
  }

  // Midnight in TIMEZONE, not in the browser's own zone — day labels and the
  // "gestern" of every age are computed there (#20). The wait is capped at an
  // hour and the render keyed on the date, so a 23- or 25-hour DST day cannot
  // shift it: the timer re-arms, and only a new date costs a render.
  let midnightDayKey = "";

  function scheduleMidnightRerender() {
    window.clearTimeout(midnightTimer);
    const now = new Date();
    if (!midnightDayKey) {
      midnightDayKey = todayIsoInZone();
    }
    const [h, m, s] = ZONE_CLOCK_FMT.format(now).split(":").map(Number);
    const msIntoDay = ((h * 60 + m) * 60 + s) * 1000 + now.getMilliseconds();
    // 2 s slack so we land safely past the boundary.
    const delayMs = Math.min(Math.max(86_400_000 - msIntoDay + 2000, 1000), 3_600_000);
    midnightTimer = window.setTimeout(() => {
      const today = todayIsoInZone();
      if (today !== midnightDayKey) {
        midnightDayKey = today;
        render(state, { force: true });
      }
      scheduleMidnightRerender();
    }, delayMs);
  }

  function isStatePayload(payload) {
    return Boolean(payload) && typeof payload === "object" &&
      ["weather", "calendar", "spotify", "system"].every(
        (key) => Boolean(payload[key]) && typeof payload[key] === "object",
      );
  }

  // #18: success is a 304 or a 200 with a usable payload; HTTP errors, network
  // errors, the timeout and garbage are not. The hint clears on the next
  // success — provider errors in that state are rendered as usual.
  function noteStateContact(ok) {
    const now = performance.now();
    if (ok) {
      stateWatchFrom = now;
    }
    const lost = !ok && now - stateWatchFrom >= LOCAL_STALE_AFTER_MS;
    if (lost !== localLinkLost) {
      localLinkLost = lost;
      renderStatusRail();
    }
  }

  // One request at a time: the poll and exitScreensaver share it instead of
  // racing two responses into render().
  // A caller that needs a counted result (the wake-up) does not ride on a
  // quiet screensaver poll (#22): it runs its own request right after it.
  let stateRequestQuiet = false;

  function fetchState(options = {}) {
    if (stateRequest && stateRequestQuiet && !options.quiet) {
      return stateRequest.then(() => fetchState(options));
    }
    if (!stateRequest) {
      stateRequestQuiet = Boolean(options.quiet);
      stateRequest = requestState(options).finally(() => {
        stateRequest = null;
      });
    }
    return stateRequest;
  }

  async function requestState({ quiet = false, framed = false } = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), STATE_REQUEST_TIMEOUT_MS);
    let payload = null;
    let nextEtag = "";
    try {
      const headers = stateEtag ? { "If-None-Match": stateEtag } : {};
      if (framed) headers[FRAME_HEADER] = "1";
      const response = await fetch("/api/state", {
        cache: "no-store",
        headers,
        signal: controller.signal,
      });
      if (response.status !== 304) {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        // Still under the timeout: abort() also cancels a stalled body.
        nextEtag = response.headers.get("ETag") || "";
        payload = await response.json();
        if (!isStatePayload(payload)) {
          throw new Error("invalid state payload");
        }
      }
    } catch (error) {
      window.console.debug("state refresh failed", error);
      if (!quiet) noteStateContact(false);
      return;
    } finally {
      window.clearTimeout(timeout);
    }
    if (!quiet) noteStateContact(true);
    if (payload === null) {
      return;
    }
    try {
      render(payload);
    } catch (error) {
      // A render bug is not a lost link, and must not end the poll chain. The
      // ETag is kept back, so the next poll gets a 200 and renders again
      // instead of a 304 that leaves the half-written DOM standing.
      window.console.error("state render failed", error);
      return;
    }
    stateEtag = nextEtag || stateEtag;
  }

  // #22: the screensaver shows official warnings, so it cannot go without
  // state for hours. It polls every SCREENSAVER_POLL_MS instead of every few
  // seconds, and those polls never count for the lost-link hint (#18): an
  // outage is judged on wake, as before.
  const SCREENSAVER_POLL_MS = 5 * 60 * 1000;
  let lastScreensaverPollAt = 0;

  // Runs callback(true) on the next frame, or callback(false) once
  // FRAME_WAIT_MS passed without one (#23) — exactly once either way.
  function onNextFrame(callback) {
    let done = false;
    const finish = (framed) => {
      if (done) return;
      done = true;
      window.clearTimeout(fallback);
      callback(framed);
    };
    const fallback = window.setTimeout(() => finish(false), FRAME_WAIT_MS);
    window.requestAnimationFrame(() => finish(true));
  }

  function schedulePolling() {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(function poll() {
      const quiet = screensaverActive;
      if (quiet && performance.now() - lastScreensaverPollAt < SCREENSAVER_POLL_MS) {
        schedulePolling();
        return;
      }
      if (quiet) lastScreensaverPollAt = performance.now();
      // Ask for a frame only when a request goes out (#23), not on every tick.
      onNextFrame(async (framed) => {
        await fetchState({ quiet, framed });
        schedulePolling();
      });
    }, POLL_INTERVAL_MS);
  }

  // One shape for every control endpoint ({ok, message, state?, timers?}),
  // plus the HTTP status so callers can branch on 404 instead of on message
  // text. A thrown fetch reports status 0.
  function shapeResult(response, body) {
    if (!response.ok || !body || body.ok === false) {
      return {
        ok: false,
        status: response.status,
        message: (body && body.message) || "Netzwerkfehler.",
        state: null,
        timers: null,
      };
    }
    return {
      ok: true,
      status: response.status,
      message: body.message || "ok",
      state: body.state || null,
      timers: Array.isArray(body.timers) ? body.timers : null,
    };
  }

  async function postRequest(endpoint, payload) {
    try {
      const init = { method: "POST", headers: { "Content-Type": "application/json" } };
      if (payload !== undefined) {
        init.body = JSON.stringify(payload);
      }
      const response = await fetch(endpoint, init);
      const body = await response.json().catch(() => null);
      return shapeResult(response, body);
    } catch (error) {
      return { ok: false, status: 0, message: "Netzwerkfehler.", state: null, timers: null };
    }
  }

  function postAction(endpoint) {
    return postRequest(endpoint);
  }

  function postJson(endpoint, payload) {
    return postRequest(endpoint, payload);
  }

  function applySpotifyState(spotifyState) {
    if (!spotifyState) {
      return;
    }
    state = { ...state, spotify: spotifyState };
    lastRenderedSectionKeys.spotify = "";
    render(state);
  }

  // ---- Screen navigation + on-demand Spotify queue ----------------------
  function queueMessage(text) {
    const message = document.createElement("p");
    message.className = "spotify-detail__queue-message";
    message.textContent = text;
    return message;
  }

  function renderSpotifyQueue(result) {
    const container = nodes.spotifyDetailQueueList;
    if (!container) {
      return;
    }
    const status = result && result.status ? result.status : "error";
    nodes.spotifyDetailQueueStatus.textContent = status === "stale" ? "Nicht aktuell" : "";
    const items = result && Array.isArray(result.items)
      ? result.items.slice(0, 4)
      : [];
    if (!result || result.ok === false || items.length === 0) {
      const fallback =
        (result && result.message) ||
        (status === "empty" ? "Keine weiteren Titel." : "Warteschlange nicht erreichbar.");
      container.replaceChildren(queueMessage(fallback));
      return;
    }

    const fragment = document.createDocumentFragment();
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "spotify-detail__queue-item";

      const art = document.createElement("div");
      art.className = "spotify-detail__queue-art";
      if (item.image_url) {
        const image = document.createElement("img");
        image.loading = "lazy";
        image.decoding = "async";
        image.alt = "";
        image.src = item.image_url;
        art.appendChild(image);
      }

      const copy = document.createElement("div");
      copy.className = "spotify-detail__queue-copy";
      const title = document.createElement("div");
      title.className = "spotify-detail__queue-title";
      title.textContent = item.title || "Unbekannter Inhalt";
      const subtitle = document.createElement("div");
      subtitle.className = "spotify-detail__queue-subtitle";
      subtitle.textContent = item.subtitle || (item.kind === "episode" ? "Podcast" : "Spotify");
      copy.append(title, subtitle);
      row.append(art, copy);
      if (typeof item.duration_ms === "number" && item.duration_ms > 0) {
        const duration = document.createElement("div");
        duration.className = "spotify-detail__queue-duration";
        duration.textContent = formatClockMs(item.duration_ms);
        row.appendChild(duration);
      }
      fragment.appendChild(row);
    });
    container.replaceChildren(fragment);
  }

  async function loadSpotifyQueue() {
    if (
      activeScreen !== "spotify" ||
      screensaverActive ||
      queueRequestInFlight
    ) {
      return;
    }
    queueRequestInFlight = true;
    try {
      const result = await fetchJson("/api/spotify/queue");
      // The user may have left the page while the request was in flight. The
      // result is harmless to retain, but avoid rebuilding hidden DOM then.
      if (activeScreen === "spotify" && !screensaverActive) {
        renderSpotifyQueue(result);
      }
    } finally {
      queueRequestInFlight = false;
    }
  }

  function stopQueueRefresh() {
    window.clearInterval(queueRefreshTimer);
    queueRefreshTimer = null;
  }

  function startQueueRefresh() {
    stopQueueRefresh();
    if (activeScreen !== "spotify" || screensaverActive) {
      return;
    }
    loadSpotifyQueue();
    queueRefreshTimer = window.setInterval(loadSpotifyQueue, QUEUE_REFRESH_MS);
  }

  // #22: three screens in a line. A swipe moves one step and stops at either
  // end — there is no wrap from the weather screen back to home.
  const SCREEN_ORDER = ["home", "spotify", "weather"];

  function setActiveScreen(target, options = {}) {
    if (!nodes.screenStage || !SCREEN_ORDER.includes(target)) {
      return;
    }
    const animate = options.animate !== false;
    if (target === activeScreen) {
      return;
    }

    activeScreen = target;
    nodes.screenStage.dataset.activeScreen = target;
    if (!animate) {
      nodes.screenStage.classList.add("is-instant");
    } else {
      nodes.screenStage.classList.add("is-animating");
    }

    [
      [nodes.homeScreen, "home"],
      [nodes.spotifyScreen, "spotify"],
      [nodes.weatherScreen, "weather"],
    ].forEach(([screen, name]) => {
      const selected = name === target;
      screen.classList.toggle("is-active", selected);
      screen.setAttribute("aria-hidden", selected ? "false" : "true");
      if (selected) {
        screen.removeAttribute("inert");
      } else {
        screen.setAttribute("inert", "");
      }
    });

    if (target === "spotify") {
      // The home face is hidden but still in the DOM, so its second-hand timer
      // would keep waking the Pi behind an invisible screen — same reasoning as
      // applyWatchFace and showScreensaver.
      stopAnalogSecondTick();
      startQueueRefresh();
    } else if (target === "weather") {
      // #22: the queue is Spotify's alone; the chart is drawn on arrival.
      stopAnalogSecondTick();
      stopQueueRefresh();
      renderWeatherDetail(false);
    } else {
      stopQueueRefresh();
      applyWatchFace(document.body.getAttribute("data-watch-face") || "flip");
    }
    if (target !== "weather") {
      closeWarnSheet();
    }
    // Both progress renderers are gated on the visible screen, so repaint the
    // one that just became visible instead of waiting up to a second for it.
    renderSpotifyProgress();

    window.clearTimeout(screenAnimationTimer);
    if (!animate) {
      // Force the no-transition state to apply to this class swap, then remove
      // it so the next deliberate navigation can animate normally.
      // eslint-disable-next-line no-unused-expressions
      nodes.screenStage.offsetWidth;
      nodes.screenStage.classList.remove("is-instant");
      nodes.screenStage.classList.remove("is-animating");
      return;
    }
    screenAnimationTimer = window.setTimeout(() => {
      nodes.screenStage.classList.remove("is-animating");
    }, SCREEN_TRANSITION_MS);
  }

  // Only the sliders are excluded: they own the horizontal axis, so a drag
  // there must set volume/position rather than page. Buttons stay swipeable —
  // a horizontal drag across one pages, a tap still activates it (the swipe's
  // own click suppression handles the difference). The picker and toast are
  // siblings of .screen-stage, so their events never reach this handler.
  function swipeTargetIsInteractive(target) {
    return Boolean(target && target.closest && target.closest("input, label"));
  }

  // ---- Gesture tracker (Ticket #14) -------------------------------------
  // One tracker per surface — the page stage and the screensaver — so the
  // engine handling below exists once. A gesture is tracked from whichever
  // event family fires first and stays locked to it, so engines that emit
  // both Pointer and Touch events cannot process one touch twice. WPE (the
  // kiosk engine) and Blink (what local checks run against) differ here, so
  // neither family is assumed.
  //
  //   onBegin({ source, x, y, target }) → false ignores the touch
  //   onFinish({ source, startX, startY, dx, dy, cancelled }) — only for
  //     gestures that ended within SWIPE_MAX_MS; older ones cannot qualify
  //   onAbort() — optional, a second finger came down
  // The returned reset() drops whatever is in flight.
  //
  // On cancel (the engine took the touch over for its own scrolling or
  // navigation) the last tracked position stands in for the end point, so a
  // swipe that already qualified still lands.
  function createGestureTracker(element, { onBegin, onFinish, onAbort }) {
    const opts = { passive: true };
    let gesture = null;

    function begin(source, id, x, y, target) {
      const now = performance.now();
      // The family lock expires with the gesture itself: a gesture older than
      // SWIPE_MAX_MS can no longer qualify, so an end event the engine never
      // delivered must not shut the other family out for good.
      if (gesture && gesture.source !== source && now - gesture.at <= SWIPE_MAX_MS) {
        return;
      }
      // A rejected touch still holds the lock, so the other family's start
      // event for the same finger cannot run onBegin a second time.
      const ignored = onBegin({ source, x, y, target }) === false;
      gesture = { source, id, x, y, lastX: x, lastY: y, at: now, ignored };
    }

    function owns(source, id) {
      return Boolean(gesture && gesture.source === source && gesture.id === id);
    }

    function abort() {
      gesture = null;
      if (onAbort) onAbort();
    }

    function track(source, id, x, y) {
      if (!owns(source, id) || gesture.ignored) return;
      gesture.lastX = x;
      gesture.lastY = y;
    }

    // `x`/`y` are omitted on cancel.
    function finish(source, id, x, y) {
      if (!owns(source, id)) return;
      const started = gesture;
      gesture = null;
      if (started.ignored || performance.now() - started.at > SWIPE_MAX_MS) return;
      const cancelled = typeof x !== "number";
      onFinish({
        source,
        startX: started.x,
        startY: started.y,
        dx: (cancelled ? started.lastX : x) - started.x,
        dy: (cancelled ? started.lastY : y) - started.y,
        cancelled,
      });
    }

    // The tracked finger, looked up by identifier: with a second finger on
    // the panel, changedTouches can lead with the other one.
    function trackedTouch(event) {
      const list = event.changedTouches || [];
      for (let i = 0; i < list.length; i += 1) {
        if (owns("touch", list[i].identifier)) return list[i];
      }
      return null;
    }

    element.addEventListener("pointerdown", (event) => {
      // A second finger, on engines that report touch only as pointers.
      if (event.isPrimary === false) {
        abort();
        return;
      }
      if (typeof event.button === "number" && event.button !== 0) {
        return;
      }
      begin("pointer", event.pointerId, event.clientX, event.clientY, event.target);
    }, opts);
    element.addEventListener("pointermove", (event) => {
      track("pointer", event.pointerId, event.clientX, event.clientY);
    }, opts);
    element.addEventListener("pointerup", (event) => {
      finish("pointer", event.pointerId, event.clientX, event.clientY);
    }, opts);
    element.addEventListener("pointercancel", (event) => {
      finish("pointer", event.pointerId);
    }, opts);
    // Touch fallback for engines whose Pointer Events are incomplete for
    // touch input, and so a gesture the engine cancels mid-way still lands.
    element.addEventListener("touchstart", (event) => {
      if (event.touches && event.touches.length > 1) {
        abort();
        return;
      }
      const touch = event.changedTouches && event.changedTouches[0];
      if (touch) begin("touch", touch.identifier, touch.clientX, touch.clientY, event.target);
    }, opts);
    element.addEventListener("touchmove", (event) => {
      const touch = trackedTouch(event);
      if (touch) track("touch", touch.identifier, touch.clientX, touch.clientY);
    }, opts);
    element.addEventListener("touchend", (event) => {
      const touch = trackedTouch(event);
      if (touch) finish("touch", touch.identifier, touch.clientX, touch.clientY);
    }, opts);
    element.addEventListener("touchcancel", (event) => {
      const touch = trackedTouch(event);
      if (touch) finish("touch", touch.identifier);
    }, opts);

    return {
      reset() {
        gesture = null;
      },
    };
  }

  function beginPageSwipe({ target }) {
    return !(screensaverActive || swipeTargetIsInteractive(target));
  }

  function finishPageSwipe({ dx, dy }) {
    // Ticket #2/#3: a clear upward swipe opens the quick sheet. Same length
    // and axis-ratio thresholds as the horizontal page swipe, mirrored.
    if (-dy >= SWIPE_MIN_X && Math.abs(dy) > Math.abs(dx) * SWIPE_AXIS_RATIO) {
      suppressClickUntil = performance.now() + 150;
      openQuickSheet();
      return;
    }
    if (
      Math.abs(dx) < SWIPE_MIN_X ||
      Math.abs(dx) < Math.abs(dy) * SWIPE_AXIS_RATIO
    ) {
      return;
    }
    // Suppress before the no-op guard: a swipe that lands on the screen we are
    // already on still ends in a synthetic click, which would otherwise activate
    // whatever sits under the finger — the Spotify tile covers a third of the
    // column now, so that is a real misfire, not a theoretical one.
    suppressClickUntil = performance.now() + 150;
    const index = SCREEN_ORDER.indexOf(activeScreen) + (dx < 0 ? 1 : -1);
    if (index < 0 || index >= SCREEN_ORDER.length) {
      return;
    }
    setActiveScreen(SCREEN_ORDER[index]);
  }

  // ---- Detail-screen seek ------------------------------------------------
  function updateSeekPreview(rawValue) {
    const duration = Number(nodes.spotifyDetailSeek.max || 0);
    const value = Math.max(0, Math.min(duration, Math.round(Number(rawValue) || 0)));
    const ratio = duration > 0 ? value / duration : 0;
    nodes.spotifyDetailSeek.value = String(value);
    nodes.spotifyDetailSeek.style.setProperty(
      "--seek-percent",
      `${(ratio * 100).toFixed(2)}%`,
    );
    nodes.spotifyDetailProgressElapsed.textContent = formatClockMs(value);
    nodes.spotifyDetailProgressTotal.textContent = `−${formatClockMs(duration - value)}`;
  }

  async function commitSeek(rawValue) {
    if (seekDragItemKey !== "" && seekDragItemKey !== seekItemKey) {
      // The track changed under the drag (a poll, or another Spotify client).
      // Committing now would seek the *new* item to a position the user never
      // chose — the backend only range-checks, so it cannot catch this.
      seekDragItemKey = "";
      renderSpotifyProgress();
      return;
    }
    seekDragItemKey = "";
    const duration = Number(nodes.spotifyDetailSeek.max || 0);
    const target = Math.max(0, Math.min(duration, Math.round(Number(rawValue) || 0)));
    const now = performance.now();
    if (
      !duration ||
      nodes.spotifyDetailSeek.disabled ||
      (seekLastCommitValue === target && now - seekLastCommitAt < 500)
    ) {
      renderSpotifyProgress();
      return;
    }
    seekLastCommitValue = target;
    seekLastCommitAt = now;
    seekHoldUntil = now + SEEK_HOLD_MS;
    if (spotifyProgress) {
      spotifyProgress.progressMs = target;
      spotifyProgress.receivedAt = now;
    }
    renderSpotifyProgress();

    const result = await postJson("/api/spotify/seek", { position_ms: target });
    if (result.ok) {
      if (result.state) {
        // Preserve the user's target through Spotify's occasionally lagging
        // inline GET; the five-second hold above later reconciles with truth.
        applySpotifyState({ ...result.state, progress_ms: target });
      }
      return;
    }
    seekHoldUntil = 0;
    updateSpotifyProgress(state.spotify || {});
    showToast(result.message || "Spotify nicht erreichbar.");
  }

  // ---- Spotify Connect picker (start music on a chosen speaker) ----------
  // Devices + playlists are fetched lazily when the overlay opens, never on the
  // poll loop — hitting the Spotify API only on a deliberate tap keeps the Pi
  // idle. The selected device persists across re-opens while it stays present.
  const PICKER_PLAYLIST_LIMIT = 24;
  let pickerSelectedDeviceId = null;

  async function fetchJson(endpoint) {
    try {
      const response = await fetch(endpoint, { cache: "no-store" });
      const body = await response.json().catch(() => null);
      if (!response.ok || !body || body.ok === false) {
        return { ok: false, message: (body && body.message) || "Netzwerkfehler." };
      }
      return body;
    } catch (error) {
      return { ok: false, message: "Netzwerkfehler." };
    }
  }

  function pickerMessage(text) {
    const node = document.createElement("p");
    node.className = "picker__message";
    node.textContent = text;
    return node;
  }

  function openSpotifyPicker() {
    if (!nodes.picker) {
      return;
    }
    nodes.picker.classList.add("is-open");
    nodes.picker.setAttribute("aria-hidden", "false");
    loadPickerData();
  }

  function closeSpotifyPicker() {
    if (!nodes.picker) {
      return;
    }
    nodes.picker.classList.remove("is-open");
    nodes.picker.setAttribute("aria-hidden", "true");
  }

  async function loadPickerData() {
    if (nodes.pickerDevices) {
      nodes.pickerDevices.replaceChildren(pickerMessage("Geräte werden geladen …"));
    }
    if (nodes.pickerPlaylists) {
      nodes.pickerPlaylists.replaceChildren(pickerMessage("Playlists werden geladen …"));
    }
    const [devicesResult, playlistsResult] = await Promise.all([
      fetchJson("/api/spotify/devices"),
      fetchJson("/api/spotify/sources"),
    ]);
    renderPickerDevices(devicesResult);
    renderPickerPlaylists(playlistsResult);
  }

  function updatePickerSelection() {
    if (!nodes.pickerDevices) {
      return;
    }
    nodes.pickerDevices.querySelectorAll(".picker-device").forEach((chip) => {
      chip.classList.toggle("is-selected", chip.dataset.deviceId === pickerSelectedDeviceId);
    });
    if (nodes.pickerTransfer) {
      nodes.pickerTransfer.disabled = !pickerSelectedDeviceId;
    }
  }

  function renderPickerDevices(result) {
    const container = nodes.pickerDevices;
    if (!container) {
      return;
    }
    container.replaceChildren();
    if (!result.ok) {
      container.appendChild(pickerMessage(result.message || "Geräte konnten nicht geladen werden."));
      return;
    }
    const devices = result.devices || [];
    if (!devices.length) {
      container.appendChild(
        pickerMessage("Keine Geräte gefunden. Speaker in der Spotify-App aufwecken."),
      );
      pickerSelectedDeviceId = null;
      if (nodes.pickerTransfer) {
        nodes.pickerTransfer.disabled = true;
      }
      return;
    }
    const selectable = devices.filter((device) => device.id);
    // Keep an existing selection if the device is still around; otherwise prefer
    // the currently active device, falling back to the first selectable one.
    if (!selectable.some((device) => device.id === pickerSelectedDeviceId)) {
      const active = selectable.find((device) => device.is_active);
      pickerSelectedDeviceId = (active || selectable[0] || {}).id || null;
    }
    devices.forEach((device) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "picker-device";
      if (!device.id) {
        chip.disabled = true;
      } else {
        chip.dataset.deviceId = device.id;
      }
      const icon = document.createElement("span");
      icon.className = "picker-device__icon";
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = icons.device;
      const label = document.createElement("span");
      label.className = "picker-device__label";
      label.textContent = [device.name, device.type].filter(Boolean).join(" · ");
      chip.appendChild(icon);
      chip.appendChild(label);
      if (device.id) {
        chip.addEventListener("click", () => {
          pickerSelectedDeviceId = device.id;
          updatePickerSelection();
        });
      }
      container.appendChild(chip);
    });
    updatePickerSelection();
  }

  function renderPickerPlaylists(result) {
    const container = nodes.pickerPlaylists;
    if (!container) {
      return;
    }
    container.replaceChildren();
    if (!result.ok) {
      container.appendChild(pickerMessage(result.message || "Playlists konnten nicht geladen werden."));
      return;
    }
    const playlists = (result.playlists || []).slice(0, PICKER_PLAYLIST_LIMIT);
    if (!playlists.length) {
      container.appendChild(pickerMessage("Keine Playlists gefunden."));
      return;
    }
    playlists.forEach((playlist) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "picker-playlist";
      const art = document.createElement("span");
      art.className = "picker-playlist__art";
      if (playlist.image_url) {
        const img = document.createElement("img");
        img.loading = "lazy";
        img.decoding = "async";
        img.alt = "";
        img.src = playlist.image_url;
        art.appendChild(img);
      }
      const name = document.createElement("span");
      name.className = "picker-playlist__name";
      name.textContent = playlist.name || "Playlist";
      card.appendChild(art);
      card.appendChild(name);
      card.addEventListener("click", () => startFromPicker(playlist.uri));
      container.appendChild(card);
    });
  }

  async function startFromPicker(contextUri) {
    if (!pickerSelectedDeviceId) {
      showToast("Bitte zuerst ein Gerät wählen.");
      return;
    }
    const result = await postJson("/api/spotify/play", {
      device_id: pickerSelectedDeviceId,
      context_uri: contextUri || null,
    });
    if (result.ok) {
      if (result.state) {
        applySpotifyState(result.state);
      }
      closeSpotifyPicker();
      loadSpotifyQueue();
    } else {
      showToast(result.message || "Spotify nicht erreichbar.");
    }
  }

  // Plan C1: crossfade state. ``activeSlot`` is the <img> currently visible;
  // the next fetch writes into the hidden slot, waits for its ``load`` event,
  // then toggles the ``is-active`` class to trigger the CSS opacity swap.
  // We intentionally do NOT preload "the next next image": every call to
  // /api/screensaver/next pops the server's shuffled round (each photo once,
  // no immediate repeat), so a speculative fetch would consume an entry, and
  // a third decoded 1024x600 bitmap costs WPE memory on the 512 MB Pi.
  let activeScreensaverSlot = null;

  async function fetchNextScreensaverImage() {
    try {
      const response = await fetch("/api/screensaver/next", { cache: "no-store" });
      if (!response.ok) {
        return null;
      }
      const payload = await response.json();
      return payload && payload.image ? payload.image : null;
    } catch (error) {
      window.console.debug("screensaver image fetch failed", error);
      return null;
    }
  }

  function loadImageInSlot(slot, src) {
    return new Promise((resolve) => {
      const onLoad = () => {
        slot.removeEventListener("load", onLoad);
        slot.removeEventListener("error", onError);
        resolve(true);
      };
      const onError = () => {
        slot.removeEventListener("load", onLoad);
        slot.removeEventListener("error", onError);
        resolve(false);
      };
      // A swipe back can land on the retired slot that still holds this very
      // photo; re-assigning an unchanged src is not guaranteed to fire load.
      if (slot.getAttribute("src") === src && slot.complete && slot.naturalWidth > 0) {
        resolve(true);
        return;
      }
      slot.addEventListener("load", onLoad);
      slot.addEventListener("error", onError);
      slot.src = src;
    });
  }

  // Ticket #13: swiping back needs the photos already shown, and the server
  // round cannot give them again. Only the paths are kept — re-decoding one
  // on demand is cheaper than holding a third bitmap. ``historyIndex`` is the
  // visible photo; it sits below the end only after a swipe back.
  const SCREENSAVER_HISTORY_MAX = 10;
  let screensaverHistory = [];
  let screensaverHistoryIndex = -1;
  // Every load bumps this; a load that finishes after a newer one started
  // (the slideshow tick racing a swipe, or two quick swipes) is dropped.
  let screensaverLoadSeq = 0;

  function resetScreensaverHistory() {
    screensaverHistory = [];
    screensaverHistoryIndex = -1;
  }

  // Resolves "shown", "failed" or "stale".
  async function paintScreensaverImage(src, seq) {
    if (!activeScreensaverSlot) {
      activeScreensaverSlot = nodes.screensaverImageA;
    }
    const nextSlot =
      activeScreensaverSlot === nodes.screensaverImageA
        ? nodes.screensaverImageB
        : nodes.screensaverImageA;

    const loaded = await loadImageInSlot(nextSlot, src);
    if (seq !== screensaverLoadSeq || screensaverMode === "night" || !screensaverActive) {
      // A decode that finishes after night took over must not undo it, and a
      // superseded one must not overwrite the newer photo.
      return "stale";
    }
    if (!loaded) {
      // Leave the current slot visible; a failure must not blank the panel.
      return "failed";
    }

    // Swap visibility: activate the freshly loaded slot and retire the old
    // one. CSS handles the 1.2 s crossfade.
    nextSlot.classList.add("is-active");
    activeScreensaverSlot.classList.remove("is-active");
    activeScreensaverSlot = nextSlot;
    setScreensaverMode("photo");
    return "shown";
  }

  async function showScreensaverHistoryEntry(index, seq) {
    const result = await paintScreensaverImage(screensaverHistory[index], seq);
    if (result === "shown") {
      screensaverHistoryIndex = index;
    } else if (result === "failed") {
      // Gone since it was shown (Lightroom reconciliation, a replaced local
      // photo): drop it so the next swipe reaches past it.
      screensaverHistory.splice(index, 1);
      if (index < screensaverHistoryIndex) {
        screensaverHistoryIndex -= 1;
      }
    }
  }

  async function showNextScreensaverImage() {
    const seq = ++screensaverLoadSeq;
    // After a swipe back, "next" walks forward through what was already shown
    // before it asks the server for a new photo.
    if (screensaverHistoryIndex < screensaverHistory.length - 1) {
      await showScreensaverHistoryEntry(screensaverHistoryIndex + 1, seq);
      return;
    }
    const image = await fetchNextScreensaverImage();
    // Night can take over while the fetch is in flight (minute tick or the
    // sheet). Then this result is stale: painting it would override the dark
    // clock for the rest of the night, because applyNight only re-evaluates
    // on the next night-value change.
    if (seq !== screensaverLoadSeq || screensaverMode === "night" || !screensaverActive) {
      return;
    }
    if (!image) {
      showScreensaverFallback();
      return;
    }
    if ((await paintScreensaverImage(image.public_path, seq)) !== "shown") {
      return;
    }
    screensaverHistory.push(image.public_path);
    if (screensaverHistory.length > SCREENSAVER_HISTORY_MAX) {
      screensaverHistory.shift();
    }
    screensaverHistoryIndex = screensaverHistory.length - 1;
  }

  function showPreviousScreensaverImage() {
    if (screensaverHistoryIndex <= 0) {
      return;
    }
    showScreensaverHistoryEntry(screensaverHistoryIndex - 1, ++screensaverLoadSeq);
  }

  function showScreensaverFallback() {
    [nodes.screensaverImageA, nodes.screensaverImageB].forEach((slot) => {
      slot.removeAttribute("src");
      slot.classList.remove("is-active");
    });
    activeScreensaverSlot = null;
    // History survives: one failed fetch must not cost the last ten photos.
    setScreensaverMode("fallback");
  }

  // Ticket #2: one class decides what the screensaver shows —
  //   photo    → slideshow + text clock bottom-left
  //   fallback → the big flip clock (no photos available)
  //   night    → black, dim centred text clock, no photos at all
  let screensaverMode = "";

  function setScreensaverMode(mode) {
    if (mode === screensaverMode) return;
    screensaverMode = mode;
    ["photo", "fallback", "night"].forEach((name) => {
      nodes.screensaver.classList.toggle(`screensaver--${name}`, name === mode);
    });
  }

  function scheduleSlideshow() {
    window.clearInterval(slideshowTimer);
    slideshowTimer = window.setInterval(
      showNextScreensaverImage,
      Math.max((config.image_duration_seconds || 15) * 1000, 5000),
    );
  }

  function startSlideshow() {
    showNextScreensaverImage();
    scheduleSlideshow();
  }

  // Ticket #13: a swipe pages and restarts the interval, so the photo just
  // chosen gets its full duration. ``slideshowTimer`` is null at night (and
  // when the screensaver is down), which is exactly when there is nothing to
  // page.
  function stepScreensaverImage(direction) {
    if (!screensaverActive || slideshowTimer === null) {
      return;
    }
    // Nothing older to show: leave the interval alone, or repeated swipes at
    // the start of the history would hold the current photo forever.
    if (direction < 0 && screensaverHistoryIndex <= 0) {
      return;
    }
    scheduleSlideshow();
    if (direction > 0) {
      showNextScreensaverImage();
    } else {
      showPreviousScreensaverImage();
    }
  }

  // ---- Screensaver gestures (Ticket #13) ---------------------------------
  // A tap on the photos does nothing: a horizontal swipe pages them, and a
  // touch on the clock (or a double tap anywhere) wakes the panel. Night is
  // the exception — any touch wakes there. The touch handling itself is the
  // shared createGestureTracker.
  const DOUBLE_TAP_MS = 450;
  const DOUBLE_TAP_SLOP = 48;
  const TAP_SLOP = 24;
  // The clock's hit area reaches this far past its box — the photo-mode
  // clock sits in a corner and should not need aiming on a wall panel.
  const CLOCK_HIT_SLOP = 32;
  // The wake fires on touch-down, so the click the engine synthesises after
  // the lift lands on whatever the dashboard has under the finger — the
  // fallback flip clock sits right over #watch-face. Swallowed once by a
  // window capture handler; the deadline is the backstop for engines that
  // emit no click (a long press).
  const WAKE_CLICK_SUPPRESS_MS = 700;
  let suppressWakeClickUntil = 0;
  let screensaverLastTap = null;

  function wakeFromScreensaver() {
    screensaverLastTap = null;
    suppressWakeClickUntil = performance.now() + WAKE_CLICK_SUPPRESS_MS;
    exitScreensaver();
  }

  function swallowWakeClick(event) {
    if (performance.now() < suppressWakeClickUntil) {
      suppressWakeClickUntil = 0;
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }

  // Geometry, not event.target: .screensaver__info is pointer-events: none
  // so it never sits between the finger and the gesture handler. The hidden
  // variant of the two clocks is display: none and has an empty rect.
  function pointIsOnScreensaverClock(x, y) {
    return [nodes.screensaverInfo, nodes.screensaverClock].some((el) => {
      const rect = el.getBoundingClientRect();
      return (
        rect.width > 0 &&
        x >= rect.left - CLOCK_HIT_SLOP &&
        x <= rect.right + CLOCK_HIT_SLOP &&
        y >= rect.top - CLOCK_HIT_SLOP &&
        y <= rect.bottom + CLOCK_HIT_SLOP
      );
    });
  }

  function beginScreensaverGesture({ source, x, y }) {
    if (!screensaverActive) {
      return false;
    }
    // At night there are no photos to page, so any touch wakes, as before.
    // Same for mode "" — the first photo is not painted yet (or failed to
    // decode) and neither clock is visible, so there is nothing to aim at.
    if (
      screensaverMode === "night" ||
      screensaverMode === "" ||
      pointIsOnScreensaverClock(x, y)
    ) {
      wakeFromScreensaver();
      return false;
    }
    const lastTap = screensaverLastTap;
    if (
      lastTap &&
      lastTap.source === source &&
      performance.now() - lastTap.at <= DOUBLE_TAP_MS &&
      Math.abs(x - lastTap.x) <= DOUBLE_TAP_SLOP &&
      Math.abs(y - lastTap.y) <= DOUBLE_TAP_SLOP
    ) {
      wakeFromScreensaver();
      return false;
    }
    return true;
  }

  // A cancelled touch still pages when it qualified as a swipe, but never
  // counts as the first half of a double tap.
  function finishScreensaverGesture({ source, startX, startY, dx, dy, cancelled }) {
    screensaverLastTap = null;
    if (Math.abs(dx) >= SWIPE_MIN_X && Math.abs(dx) >= Math.abs(dy) * SWIPE_AXIS_RATIO) {
      stepScreensaverImage(dx < 0 ? 1 : -1);
      return;
    }
    if (!cancelled && Math.abs(dx) <= TAP_SLOP && Math.abs(dy) <= TAP_SLOP) {
      screensaverLastTap = { source, x: startX, y: startY, at: performance.now() };
    }
  }

  // Reset on every enter and exit: a finger still down when the screensaver
  // closes lifts onto the stage, so this tracker never sees that touch end.
  let screensaverGestures = null;

  function bindScreensaverGestures() {
    window.addEventListener("click", swallowWakeClick, true);
    screensaverGestures = createGestureTracker(nodes.screensaver, {
      onBegin: beginScreensaverGesture,
      onFinish: finishScreensaverGesture,
      onAbort: () => {
        screensaverLastTap = null;
      },
    });
  }

  function stopSlideshow() {
    window.clearInterval(slideshowTimer);
    slideshowTimer = null;
  }

  // Called on enter and whenever night flips while the screensaver is up.
  function refreshScreensaverPresentation() {
    if (!screensaverActive) return;
    if (isNightActive()) {
      stopSlideshow();
      [nodes.screensaverImageA, nodes.screensaverImageB].forEach((slot) => {
        slot.removeAttribute("src");
        slot.classList.remove("is-active");
      });
      activeScreensaverSlot = null;
      resetScreensaverHistory();
      setScreensaverMode("night");
      return;
    }
    if (slideshowTimer === null) {
      startSlideshow();
    }
  }

  function notifyScreensaverState(active) {
    // Plan B1: tell the backend so it can pause/resume the Spotify poll
    // group. Fire-and-forget — a failure here must not keep the screensaver
    // from showing.
    try {
      fetch("/api/screensaver/state", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: Boolean(active) }),
        keepalive: true,
      }).catch(() => {});
    } catch (error) {
      /* ignore */
    }
  }

  function enterScreensaver() {
    if (screensaverActive) {
      return;
    }
    // A ringing timer owns the panel; never cover it with the slideshow.
    if (alarmTimerId !== null) {
      resetIdleTimer();
      return;
    }
    screensaverActive = true;
    lastScreensaverPollAt = performance.now();
    closeWarnSheet();
    setActiveScreen("home", { animate: false });
    resetWeatherView();
    stopProgressTimer();
    stopAnalogSecondTick();
    closeSpotifyPicker();
    closeQuickSheet();
    nodes.screensaver.classList.add("is-active");
    nodes.screensaver.setAttribute("aria-hidden", "false");
    // The text clock is written once a minute by updateClock; make sure the
    // first frame is current rather than up to 59 s old.
    nodes.screensaverTime.textContent = CLOCK_TIME_FMT.format(new Date());
    screensaverMode = "";
    screensaverLastTap = null;
    if (screensaverGestures) screensaverGestures.reset();
    resetScreensaverHistory();
    refreshScreensaverPresentation();
    notifyScreensaverState(true);
    window.clearInterval(screensaverHeartbeatTimer);
    screensaverHeartbeatTimer = window.setInterval(
      () => notifyScreensaverState(true),
      SCREENSAVER_HEARTBEAT_MS,
    );
  }

  function exitScreensaver() {
    if (!screensaverActive) {
      return;
    }
    screensaverActive = false;
    if (screensaverGestures) screensaverGestures.reset();
    nodes.screensaver.classList.remove("is-active");
    nodes.screensaver.setAttribute("aria-hidden", "true");
    stopSlideshow();
    window.clearInterval(screensaverHeartbeatTimer);
    screensaverHeartbeatTimer = null;
    resetIdleTimer();
    notifyScreensaverState(false);
    updateClock();
    applyWatchFace(document.body.getAttribute("data-watch-face") || "flip");
    // The pause was deliberate, not missed polls (#18): count from now. An
    // outage that was already showing keeps its hint until a poll succeeds.
    if (!localLinkLost) {
      stateWatchFrom = performance.now();
    }
    fetchState();
  }

  function resetIdleTimer() {
    window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(
      enterScreensaver,
      Math.max((config.idle_timeout_seconds || 120) * 1000, 5000),
    );
  }

  function handleActivity(event) {
    if (screensaverActive) {
      // Touches belong to the screensaver's own gestures (Ticket #13): a
      // single tap or a swipe must not wake it. A key still does.
      if (event && event.type === "keydown") {
        exitScreensaver();
      }
      return;
    }
    resetIdleTimer();
  }

  // ---- Night window (Ticket #2) ------------------------------------------
  // Mirrors smart_display/night.py. The server renders data-night for the
  // first frame; from then on the minute tick re-evaluates here.
  // 1:1 mirror of night.parse_hhmm: split on the FIRST colon only, so
  // "1:2:3" is rejected (its minutes part "2:3" is not all digits) exactly
  // as Python's split(":", 1) + isdigit() rejects it (#4).
  function parseHhmm(value) {
    if (typeof value !== "string") return null;
    const text = value.trim();
    const colon = text.indexOf(":");
    if (colon < 0) return null;
    const h = text.slice(0, colon);
    const m = text.slice(colon + 1);
    if (!/^\d+$/.test(h) || !/^\d+$/.test(m)) return null;
    const hours = Number(h);
    const minutes = Number(m);
    if (hours > 23 || minutes > 59) return null;
    return hours * 60 + minutes;
  }

  function isNightMinutes(minuteOfDay, start, end) {
    if (start === null || end === null || start === end) return false;
    if (start < end) return minuteOfDay >= start && minuteOfDay < end;
    return minuteOfDay >= start || minuteOfDay < end;
  }

  function nightModeSetting() {
    try {
      const stored = window.localStorage && window.localStorage.getItem(NIGHT_MODE_STORAGE_KEY);
      if (stored && VALID_NIGHT_MODES.includes(stored)) return stored;
    } catch (error) {
      /* storage disabled */
    }
    return "auto";
  }

  function setNightModeSetting(mode) {
    if (!VALID_NIGHT_MODES.includes(mode)) return;
    try {
      if (window.localStorage) window.localStorage.setItem(NIGHT_MODE_STORAGE_KEY, mode);
    } catch (error) {
      /* session-only */
    }
    applyNight(true);
  }

  function windowSaysNight() {
    if (!NIGHT_WINDOW) return false;
    const { hour, minute } = currentHourMinute();
    return isNightMinutes(
      hour * 60 + minute,
      parseHhmm(NIGHT_WINDOW.from),
      parseHhmm(NIGHT_WINDOW.to),
    );
  }

  function isNightActive() {
    const mode = nightModeSetting();
    if (mode === "on") return true;
    if (mode === "off") return false;
    return windowSaysNight();
  }

  let lastNightValue = null;

  function applyNight(force) {
    const active = isNightActive();
    if (!force && active === lastNightValue) return;
    lastNightValue = active;
    document.body.setAttribute("data-night", active ? "true" : "false");
    syncNightChips();
    refreshScreensaverPresentation();
  }

  function syncNightChips() {
    const mode = nightModeSetting();
    nodes.nightChips.querySelectorAll("[data-night-mode]").forEach((chip) => {
      chip.classList.toggle("is-selected", chip.dataset.nightMode === mode);
    });
    let note;
    if (!NIGHT_WINDOW) {
      note = "Kein Nachtfenster konfiguriert (APP_NIGHT_FROM / APP_NIGHT_TO).";
    } else {
      note = `Automatisch von ${NIGHT_WINDOW.from} bis ${NIGHT_WINDOW.to} Uhr.`;
    }
    if (mode !== "auto") {
      note += mode === "on" ? " Manuell eingeschaltet." : " Manuell ausgeschaltet.";
    }
    nodes.nightNote.textContent = note;
  }

  // ---- Quick sheet (Ticket #2/#3) ------------------------------------------
  let quickSheetOpen = false;

  function openQuickSheet() {
    if (quickSheetOpen || screensaverActive) return;
    quickSheetOpen = true;
    closeSpotifyPicker();
    renderFaceChips();
    syncNightChips();
    renderTimerList();
    nodes.quickSheet.classList.add("is-open");
    nodes.quickSheet.setAttribute("aria-hidden", "false");
  }

  function closeQuickSheet() {
    if (!quickSheetOpen) return;
    quickSheetOpen = false;
    nodes.quickSheet.classList.remove("is-open");
    nodes.quickSheet.setAttribute("aria-hidden", "true");
  }

  let faceChipsBuilt = false;

  function renderFaceChips() {
    if (faceChipsBuilt) {
      syncFaceChips(document.body.getAttribute("data-watch-face") || "flip");
      return;
    }
    faceChipsBuilt = true;
    const fragment = document.createDocumentFragment();
    VALID_WATCH_FACES.forEach((face) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.dataset.face = face;
      chip.textContent = WATCH_FACE_LABELS[face] || face;
      chip.addEventListener("click", () => selectWatchFace(face));
      fragment.appendChild(chip);
    });
    nodes.faceChips.replaceChildren(fragment);
    syncFaceChips(document.body.getAttribute("data-watch-face") || "flip");
  }

  function syncFaceChips(face) {
    if (!faceChipsBuilt) return;
    nodes.faceChips.querySelectorAll("[data-face]").forEach((chip) => {
      chip.classList.toggle("is-selected", chip.dataset.face === face);
    });
  }

  // Hold-to-confirm: pointerdown starts the 2 s fill; any release before that
  // cancels. One gesture family at a time, so Pointer + Touch engines cannot
  // double-start.
  function bindHoldButton(node, onConfirm) {
    let holdTimer = null;
    let holdSource = null;
    const start = (source) => (event) => {
      if (node.disabled || holdTimer !== null) return;
      if (source === "pointer" && event.isPrimary === false) return;
      holdSource = source;
      node.classList.add("is-holding");
      holdTimer = window.setTimeout(() => {
        holdTimer = null;
        holdSource = null;
        node.classList.remove("is-holding");
        onConfirm();
      }, HOLD_MS);
    };
    const cancel = (source) => () => {
      if (holdTimer === null || holdSource !== source) return;
      window.clearTimeout(holdTimer);
      holdTimer = null;
      holdSource = null;
      node.classList.remove("is-holding");
    };
    node.addEventListener("pointerdown", start("pointer"), { passive: true });
    ["pointerup", "pointercancel", "pointerleave"].forEach((name) => {
      node.addEventListener(name, cancel("pointer"), { passive: true });
    });
    node.addEventListener("touchstart", start("touch"), { passive: true });
    ["touchend", "touchcancel"].forEach((name) => {
      node.addEventListener(name, cancel("touch"), { passive: true });
    });
    // A click is the synthetic tail of a hold gesture — never a confirmation.
    node.addEventListener("click", (event) => event.preventDefault());
  }

  // ---- Power (Ticket #2) -----------------------------------------------------
  async function requestPower(action) {
    const result = await postAction(`/api/system/${action}`);
    if (!result.ok) {
      showToast(result.message || "Befehl fehlgeschlagen.");
      return;
    }
    closeQuickSheet();
    nodes.powerOverlayText.textContent =
      action === "reboot" ? "Wird neu gestartet …" : "Wird ausgeschaltet …";
    nodes.powerOverlay.classList.add("is-active");
    nodes.powerOverlay.setAttribute("aria-hidden", "false");
    // Stop waking the Pi for a page that is about to go away.
    window.clearTimeout(pollTimer);
    window.clearTimeout(idleTimer);
  }

  function syncPowerButtons() {
    const allowed = Boolean(config.allow_shutdown);
    nodes.powerReboot.disabled = !allowed;
    nodes.powerShutdown.disabled = !allowed;
    nodes.powerNote.textContent = allowed
      ? "Fährt den Pi sauber herunter. Wieder einschalten: Strom kurz trennen."
      : "Auf diesem Gerät nicht freigegeben (APP_ALLOW_SHUTDOWN=true in .env).";
  }

  // ---- Timers (Ticket #3) ----------------------------------------------------
  // Server truth is `ends_at` (epoch seconds, same clock as the browser). The
  // 1 Hz tick exists only while a timer is live; idle panels tick nothing.
  let timers = [];
  let timerTickHandle = null;
  let alarmTimerId = null;
  // Timers the user already dismissed locally — hidden even if a stale poll
  // still lists them while the POST is in flight.
  const dismissedLocally = new Set();

  function nowSeconds() {
    return Date.now() / 1000;
  }

  function timerRemaining(timer) {
    return timer.ends_at - nowSeconds();
  }

  function timerStatus(timer) {
    if (timer.dismissed || dismissedLocally.has(timer.id)) return "done";
    const remaining = timerRemaining(timer);
    if (remaining > 0) return "running";
    if (-remaining < TIMER_RING_TIMEOUT_S) return "ringing";
    return "done";
  }

  function formatRemaining(seconds) {
    const total = Math.max(0, Math.ceil(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }

  function liveTimers() {
    return timers.filter((timer) => timerStatus(timer) !== "done");
  }

  function applyTimers(list) {
    timers = list.map((timer) => ({ ...timer }));
    // Server has caught up with whatever we dismissed; forget the overrides
    // for ids it no longer lists.
    const ids = new Set(timers.map((t) => t.id));
    dismissedLocally.forEach((id) => {
      if (!ids.has(id)) dismissedLocally.delete(id);
    });
    renderTimerList();
    timerTick();
    if (liveTimers().length > 0) {
      ensureTimerTick();
    } else {
      stopTimerTick();
    }
  }

  function ensureTimerTick() {
    if (timerTickHandle !== null) return;
    const msUntilNextSecond = 1000 - (Date.now() % 1000);
    timerTickHandle = window.setTimeout(function tick() {
      timerTick();
      if (liveTimers().length > 0) {
        timerTickHandle = window.setTimeout(tick, 1000);
      } else {
        timerTickHandle = null;
      }
    }, msUntilNextSecond + 5);
  }

  function stopTimerTick() {
    if (timerTickHandle !== null) {
      window.clearTimeout(timerTickHandle);
      timerTickHandle = null;
    }
  }

  // Writes the per-second surfaces: chip, screensaver line, sheet rows, alarm.
  function timerTick() {
    const live = liveTimers();
    const ringing = live.find((timer) => timerStatus(timer) === "ringing") || null;
    // Soonest running timer is what the chip shows; a ringing one wins.
    const running = live
      .filter((timer) => timerStatus(timer) === "running")
      .sort((a, b) => a.ends_at - b.ends_at);
    const shown = ringing || running[0] || null;

    nodes.timerChip.classList.toggle("is-hidden", shown === null);
    nodes.screensaverTimer.classList.toggle("is-hidden", shown === null);
    if (shown) {
      const text = ringing ? "Abgelaufen" : formatRemaining(timerRemaining(shown));
      nodes.timerChipText.textContent = text;
      nodes.timerChip.classList.toggle("is-ringing", Boolean(ringing));
      const label = shown.label ? `${shown.label} · ` : "";
      nodes.screensaverTimer.textContent = `Timer ${label}${text}`;
    }

    if (quickSheetOpen) {
      nodes.timerList.querySelectorAll("[data-timer-id]").forEach((row) => {
        const timer = timers.find((t) => t.id === row.dataset.timerId);
        const remainingNode = row.querySelector(".timer-row__remaining");
        if (!timer || !remainingNode) return;
        const status = timerStatus(timer);
        row.classList.toggle("is-ringing", status === "ringing");
        remainingNode.textContent =
          status === "ringing" ? "Abgelaufen" : formatRemaining(timerRemaining(timer));
      });
    }

    if (ringing) {
      showAlarm(ringing);
    } else if (alarmTimerId !== null) {
      hideAlarm();
    }
  }

  function renderTimerList() {
    const live = liveTimers().sort((a, b) => a.ends_at - b.ends_at);
    if (live.length === 0) {
      const empty = document.createElement("div");
      empty.className = "timer-list__empty";
      empty.textContent = "Kein Timer aktiv.";
      nodes.timerList.replaceChildren(empty);
      return;
    }
    const fragment = document.createDocumentFragment();
    live.forEach((timer) => {
      const row = document.createElement("div");
      row.className = "timer-row";
      row.dataset.timerId = timer.id;
      const remaining = document.createElement("div");
      remaining.className = "timer-row__remaining";
      remaining.textContent = formatRemaining(timerRemaining(timer));
      const label = document.createElement("div");
      label.className = "timer-row__label";
      label.textContent = timer.label || `${Math.round(timer.duration_seconds / 60)} Min`;
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "timer-row__cancel";
      cancel.textContent = "Abbrechen";
      cancel.addEventListener("click", () => timerAction(timer.id, "cancel"));
      row.append(remaining, label, cancel);
      fragment.appendChild(row);
    });
    nodes.timerList.replaceChildren(fragment);
  }

  async function createTimer(minutes) {
    const result = await postJson("/api/timers", { duration_seconds: minutes * 60 });
    if (!result.ok) {
      showToast(result.message || "Timer konnte nicht gestartet werden.");
      return;
    }
    if (Array.isArray(result.timers)) {
      lastRenderedSectionKeys.timers = "";
      applyTimers(result.timers);
    }
  }

  async function timerAction(timerId, action, payload) {
    if (action === "dismiss" || action === "cancel") {
      dismissedLocally.add(timerId);
      timerTick();
      renderTimerList();
    } else if (action === "extend" && payload && payload.seconds > 0) {
      // Optimistic: the alarm is hidden before the POST resolves, and the next
      // 1 Hz tick would otherwise find the timer still ringing and show it
      // again. Server truth replaces this on the reply (applyTimers).
      const timer = timers.find((t) => t.id === timerId);
      if (timer) {
        timer.ends_at = Math.max(timer.ends_at, nowSeconds()) + payload.seconds;
        timerTick();
      }
    }
    const result = payload
      ? await postJson(`/api/timers/${encodeURIComponent(timerId)}/${action}`, payload)
      : await postAction(`/api/timers/${encodeURIComponent(timerId)}/${action}`);
    // 404 = the timer is already gone on the server; the local dismissal was
    // right. Anything else rolls it back.
    if (!result.ok && result.status !== 404) {
      dismissedLocally.delete(timerId);
      showToast(result.message || "Timer-Aktion fehlgeschlagen.");
    }
    if (Array.isArray(result.timers)) {
      lastRenderedSectionKeys.timers = "";
      applyTimers(result.timers);
    } else {
      fetchState();
    }
  }

  function showAlarm(timer) {
    if (alarmTimerId === timer.id) return;
    alarmTimerId = timer.id;
    if (screensaverActive) exitScreensaver();
    closeQuickSheet();
    nodes.timerAlarmTitle.textContent = "Timer abgelaufen";
    nodes.timerAlarmLabel.textContent =
      timer.label || `${Math.round(timer.duration_seconds / 60)} Minuten`;
    nodes.timerAlarm.classList.add("is-active");
    nodes.timerAlarm.setAttribute("aria-hidden", "false");
  }

  function hideAlarm() {
    alarmTimerId = null;
    nodes.timerAlarm.classList.remove("is-active");
    nodes.timerAlarm.setAttribute("aria-hidden", "true");
    resetIdleTimer();
  }

  function bindEvents() {
    // Plan B6: pointermove fired on every pixel of cursor motion during
    // debug sessions and on the kiosk itself whenever a touch was dragged,
    // which was pure CPU noise — pointerdown/touchstart already cover real
    // interaction.
    ["pointerdown", "touchstart", "keydown"].forEach((eventName) => {
      window.addEventListener(eventName, handleActivity, { passive: true });
    });

    bindScreensaverGestures();

    if (nodes.watchFace) {
      nodes.watchFace.addEventListener("click", (event) => {
        // If the tap was consumed by the screensaver-exit path, the
        // screensaver is still marked active briefly — skip cycling then.
        if (screensaverActive) {
          return;
        }
        event.preventDefault();
        cycleWatchFace();
      });
    }

    // The whole tile leads to the Spotify screen — playing or idle, where it
    // reads "Musik starten". It sits inside .screen-stage, whose capture-phase
    // handler already swallows the synthetic click a swipe emits.
    if (nodes.spotifyCard) {
      nodes.spotifyCard.addEventListener("click", () => {
        if (screensaverActive) {
          return;
        }
        setActiveScreen("spotify");
      });
    }

    if (nodes.toast) {
      nodes.toast.addEventListener("pointerdown", hideToast, { passive: true });
    }

    // #22: weather screen controls. All are plain buttons inside the stage,
    // so a horizontal drag across them still pages and its synthetic click
    // is swallowed by the stage's capture handler below.
    nodes.wxMode.querySelectorAll("[data-wx-mode]").forEach((button) => {
      button.addEventListener("click", () => selectWeatherView({ mode: button.dataset.wxMode }));
    });
    nodes.wxRange.querySelectorAll("[data-wx-range]").forEach((button) => {
      button.addEventListener("click", () => selectWeatherView({ mode: "hours", range: button.dataset.wxRange, day: "" }));
    });
    nodes.wxWeek.addEventListener("click", (event) => {
      const cell = event.target.closest && event.target.closest("[data-date]");
      if (cell) selectWeatherDay(cell.dataset.date);
    });
    nodes.wxWarn.addEventListener("click", openWarnSheet);
    nodes.warnSheetClose.addEventListener("click", closeWarnSheet);
    nodes.warnSheetBackdrop.addEventListener("click", closeWarnSheet);

    if (nodes.screenStage) {
      nodes.spotifyScreen.setAttribute("inert", "");
      nodes.weatherScreen.setAttribute("inert", "");
      createGestureTracker(nodes.screenStage, {
        onBegin: beginPageSwipe,
        onFinish: finishPageSwipe,
      });
      nodes.screenStage.addEventListener(
        "click",
        (event) => {
          if (performance.now() < suppressClickUntil) {
            // One-shot: swallow only the synthetic click the gesture itself
            // emits (a few ms after pointerup), never the user's next real tap.
            // The deadline is the backstop for engines that emit no such click.
            suppressClickUntil = 0;
            event.preventDefault();
            event.stopImmediatePropagation();
          }
        },
        true,
      );
    }

    async function toggleSpotifyPlayback() {
      const currentSpotify = state.spotify || {};
      const previousIsPlaying = Boolean(currentSpotify.is_playing);
      // Optimistic flip so the button feels alive on touch. Carry the locally
      // interpolated position: the payload's progress_ms is up to a poll
      // interval stale, and applySpotifyState now rebuilds the progress
      // baseline, so passing it through would rewind the bar on every tap.
      const optimistic = { ...currentSpotify, is_playing: !previousIsPlaying };
      const interpolated = interpolatedProgressMs();
      if (interpolated !== null) {
        optimistic.progress_ms = interpolated;
      }
      applySpotifyState(optimistic);
      const result = await postAction("/api/spotify/toggle");
      if (result.ok && result.state) {
        applySpotifyState(result.state);
      } else if (result.ok === false) {
        applySpotifyState({
          ...currentSpotify,
          is_playing: previousIsPlaying,
          ...(interpolated === null ? {} : { progress_ms: interpolated }),
        });
        showToast(result.message || "Spotify nicht erreichbar.");
      }
    }

    async function skipSpotifyNext() {
      // A skip supersedes any optimistic seek hold — "previous" often restarts
      // the *same* item, which the hold's item key cannot tell from a lagging
      // refresh, and would otherwise pin the bar at the old position.
      seekHoldUntil = 0;
      const result = await postAction("/api/spotify/next");
      if (result.ok && result.state) {
        applySpotifyState(result.state);
        loadSpotifyQueue();
      } else if (result.ok === false) {
        showToast(result.message || "Spotify nicht erreichbar.");
      }
    }

    async function skipSpotifyPrevious() {
      seekHoldUntil = 0;
      const result = await postAction("/api/spotify/previous");
      if (result.ok && result.state) {
        applySpotifyState(result.state);
        loadSpotifyQueue();
      } else if (result.ok === false) {
        showToast(result.message || "Spotify nicht erreichbar.");
      }
    }

    nodes.spotifyDetailToggle.addEventListener("click", toggleSpotifyPlayback);
    nodes.spotifyDetailNext.addEventListener("click", skipSpotifyNext);
    nodes.spotifyDetailPrevious.addEventListener("click", skipSpotifyPrevious);

    // Commit the slider value to the backend. Routed through one helper so both
    // `input` (fires continuously while dragging — the reliable signal on the
    // touch kiosk) and `change` (final value on release) drive it. `input` is
    // debounced so a drag sends only the value the finger settles on; `change`
    // commits immediately. Relying on `change` alone was the bug: on the kiosk
    // a poll mid-drag could reset the thumb, so the release committed an
    // unchanged value (a no-op) — direct API calls worked, the slider didn't.
    function commitVolume(rawValue, debounceMs) {
      window.clearTimeout(volumeCommitTimer);
      const targetValue = Math.max(0, Math.min(100, Math.round(Number(rawValue))));
      volumeLastSent = targetValue;
      markVolumeBusy();
      volumeCommitTimer = window.setTimeout(async () => {
        const result = await postJson("/api/spotify/volume", {
          volume_percent: targetValue,
        });
        if (result.ok) {
          // Hold the user's value: Spotify's GET /me/player lags several seconds
          // in reporting the new device volume, so keep the dirty/busy guard
          // (renderSpotify reconciles once a poll confirms within tolerance)
          // instead of snapping back to the stale inline-refresh value.
          markVolumeBusy();
        } else {
          clearSpotifyVolumeDirty();
          showToast(result.message || "Spotify nicht erreichbar.");
        }
      }, debounceMs);
    }

    spotifyVolumeSliders().forEach((slider) => {
      slider.addEventListener("pointerdown", markVolumeBusy, { passive: true });
      slider.addEventListener("touchstart", markVolumeBusy, { passive: true });
      slider.addEventListener("input", () => {
        const value = Math.max(0, Math.min(100, Math.round(Number(slider.value))));
        markVolumeBusy();
        setSpotifyVolumeUi(value);
        commitVolume(value, 250);
      });
      slider.addEventListener("change", () => {
        commitVolume(slider.value, 0);
      });
    });

    // `restart` is true only for pointerdown, which unambiguously begins a new
    // drag. `input` must NOT re-capture mid-drag: it fires on every finger
    // movement, and re-capturing after a track change would adopt the new
    // item's key and defeat the guard in commitSeek.
    function beginSeekDrag(restart) {
      if (restart || !seekDragging) {
        seekDragItemKey = seekItemKey;
      }
      seekDragging = true;
    }

    nodes.spotifyDetailSeek.addEventListener(
      "pointerdown",
      () => beginSeekDrag(true),
      { passive: true },
    );
    nodes.spotifyDetailSeek.addEventListener("input", () => {
      beginSeekDrag(false);
      updateSeekPreview(nodes.spotifyDetailSeek.value);
    });
    nodes.spotifyDetailSeek.addEventListener("change", () => {
      seekDragging = false;
      commitSeek(nodes.spotifyDetailSeek.value);
    });
    nodes.spotifyDetailSeek.addEventListener("pointerup", () => {
      if (!seekDragging) {
        return;
      }
      seekDragging = false;
      commitSeek(nodes.spotifyDetailSeek.value);
    }, { passive: true });
    nodes.spotifyDetailSeek.addEventListener("pointercancel", () => {
      seekDragging = false;
      seekDragItemKey = "";
      renderSpotifyProgress();
    }, { passive: true });

    setIcon(nodes.spotifyTilePlayIcon, "play");
    setIcon(nodes.spotifyDetailOpenPickerIcon, "device");
    setIcon(nodes.spotifyDetailVolumeIcon, "volume");
    if (nodes.spotifyDetailOpenPicker) {
      nodes.spotifyDetailOpenPicker.addEventListener("click", openSpotifyPicker);
    }
    if (nodes.pickerClose) {
      nodes.pickerClose.addEventListener("click", closeSpotifyPicker);
    }
    if (nodes.pickerBackdrop) {
      nodes.pickerBackdrop.addEventListener("click", closeSpotifyPicker);
    }
    if (nodes.pickerTransfer) {
      nodes.pickerTransfer.addEventListener("click", () => startFromPicker(null));
    }

    // ---- Quick sheet, timers, night, power (Tickets #2/#3) -----------------
    nodes.sheetHandle.addEventListener("click", () => {
      if (screensaverActive) return;
      if (quickSheetOpen) closeQuickSheet();
      else openQuickSheet();
    });
    nodes.quickSheetClose.addEventListener("click", closeQuickSheet);
    nodes.quickSheetBackdrop.addEventListener("click", closeQuickSheet);
    // Swipe down on the panel closes it; buttons inside still tap normally.
    let sheetSwipeStartY = null;
    nodes.quickSheetPanel.addEventListener("pointerdown", (event) => {
      sheetSwipeStartY = event.clientY;
    }, { passive: true });
    nodes.quickSheetPanel.addEventListener("pointerup", (event) => {
      if (sheetSwipeStartY !== null && event.clientY - sheetSwipeStartY >= SWIPE_MIN_X) {
        closeQuickSheet();
      }
      sheetSwipeStartY = null;
    }, { passive: true });

    nodes.timerPresets.querySelectorAll("[data-minutes]").forEach((chip) => {
      chip.addEventListener("click", () => createTimer(Number(chip.dataset.minutes)));
    });
    nodes.timerChip.addEventListener("click", () => {
      if (screensaverActive) return;
      openQuickSheet();
    });
    nodes.timerAlarmStop.addEventListener("click", () => {
      if (alarmTimerId === null) return;
      const id = alarmTimerId;
      hideAlarm();
      timerAction(id, "dismiss");
    });
    nodes.timerAlarmExtend.addEventListener("click", () => {
      if (alarmTimerId === null) return;
      const id = alarmTimerId;
      hideAlarm();
      timerAction(id, "extend", { seconds: 60 });
    });

    nodes.nightChips.querySelectorAll("[data-night-mode]").forEach((chip) => {
      chip.addEventListener("click", () => setNightModeSetting(chip.dataset.nightMode));
    });

    // Fires on click, i.e. after the pointerdown that handleActivity sees, so
    // the tap that starts the screensaver cannot also wake it again.
    nodes.screensaverStart.addEventListener("click", enterScreensaver);

    bindHoldButton(nodes.powerReboot, () => requestPower("reboot"));
    bindHoldButton(nodes.powerShutdown, () => requestPower("shutdown"));
    syncPowerButtons();

    window.addEventListener("resize", () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => render(state, { force: true }), 80);
    });
  }

  setIcon(nodes.timerChipIcon, "timer");
  applyWatchFace(currentWatchFace());
  applyNight(true);
  bindEvents();
  updateClock();
  render(state, { force: true });
  schedulePolling();
  scheduleMidnightRerender();
  resetIdleTimer();
  scheduleClockTick();
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => render(state, { force: true }));
  }
})();
