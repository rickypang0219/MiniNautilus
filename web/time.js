/* Shared, dependency-free display and filter rules; never used by the trading engine. */
(function (root) {
  'use strict';
  function elapsed(ms) {
    if (ms == null) return '—';
    const hours = Math.floor(ms / 3600000), minutes = Math.floor(ms / 60000) % 60;
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}.${String(ms % 1000).padStart(3, '0')}`;
  }
  function datetime(ms, zone = 'Asia/Hong_Kong', compact = false) {
    if (ms == null || !Number.isFinite(Number(ms))) return 'Unknown datetime';
    const options = { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hourCycle: 'h23' };
    const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA', options).formatToParts(new Date(Number(ms))).map(p => [p.type, p.value]));
    return compact ? `${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}` : `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}.${p.fractionalSecond}`;
  }
  function pick(row, basis) {
    if (basis === 'engine') return { ms: row.at, source: 'Engine elapsed', elapsed: true };
    if (basis === 'received') return { ms: row.received_time_ms, source: 'Received by bridge' };
    if (row.event_time_ms != null) return { ms: row.event_time_ms, source: row.time_source || 'Event time' };
    return { ms: row.received_time_ms, source: row.received_time_ms == null ? 'Datetime not recorded' : 'Received · event time unavailable' };
  }
  function parseInput(value, zone) {
    if (!value) return null;
    if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d{1,3})?)?$/.test(value)) throw Error('Enter a valid datetime');
    // UI offers fixed UTC and modern Hong Kong time only, avoiding browser-local DST ambiguity.
    const n = Date.parse(value + 'Z') - (zone === 'Asia/Hong_Kong' ? 28800000 : 0);
    if (!Number.isFinite(n) || n < 0) throw Error('Enter a datetime after the Unix epoch');
    return n;
  }
  const api = { elapsed, datetime, pick, parseInput };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.MiniTime = api;
})(globalThis);
