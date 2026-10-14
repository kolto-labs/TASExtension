#!/usr/bin/env node

// Summarize one Playwright console capture into independent runtime launches.
// This is diagnostic tooling only: it never launches, stops, or controls a game.
import { readFile } from 'node:fs/promises';

const input = process.argv[2];
if (!input || input === '--help') {
  console.log('Usage: node demo/tools/runtime-attempt-report.mjs <playwright-console.log>');
  process.exit(input ? 0 : 2);
}

const source = await readFile(input, 'utf8');
const lines = source.split(/\r?\n/);
const attempts = [];
let current = null;
const elapsed = (ms, start) => ms == null ? null : Math.max(0, ms - start);
const duration = value => value == null ? 'not observed' : `${(value / 1000).toFixed(1)}s`;

for (const line of lines) {
  const clock = line.match(/^\[\s*(\d+)ms\]/);
  const ms = clock ? Number(clock[1]) : null;
  if (/\bStarting \.\.\./.test(line)) {
    const release = line.match(/runtime-releases\/([a-f\d]{64})\//i)?.[1] || 'unknown';
    current = { ordinal: attempts.length + 1, startMs: ms, release, firstWindowMs: null,
      firstContextMs: null, firstTextureMs: null, guestFaultMs: null,
      inputRoutes: [], errors: [], lines: [] };
    attempts.push(current);
  }
  if (!current || ms == null) continue;
  current.lines.push(line);
  if (/\bShowing Window\b/.test(line) && current.firstWindowMs == null) current.firstWindowMs = ms;
  if (/KPTAS_GL_CONTEXT_CREATE/.test(line) && current.firstContextMs == null) current.firstContextMs = ms;
  if (/KPTAS_GL_COMPRESSED_UPLOAD/.test(line) && current.firstTextureMs == null) current.firstTextureMs = ms;
  if (/Unhandled page fault|unhandled exception|fatal error|\bABORTED\b/i.test(line)) {
    current.guestFaultMs ??= ms;
    current.errors.push(line.replace(/^\[.*?\]\s*\[[^\]]+\]\s*/, '').slice(0, 260));
  }
  if (/Telos X11 mouse route|Telos input route|keyboard route/i.test(line)) current.inputRoutes.push(line.slice(0, 320));
  if (/\[ERROR\]/.test(line)) current.errors.push(line.replace(/^\[.*?\]\s*\[[^\]]+\]\s*/, '').slice(0, 260));
}

for (let i = 0; i < attempts.length; i++) {
  const attempt = attempts[i];
  const endMs = attempts[i + 1]?.startMs ?? null;
  const span = endMs == null ? null : elapsed(endMs, attempt.startMs);
  console.log(`Attempt ${attempt.ordinal} · release ${attempt.release}`);
  console.log(`  start ${attempt.startMs == null ? 'unknown' : `${attempt.startMs}ms`} · next start ${span == null ? 'none in this log' : duration(span)}`);
  console.log(`  first window ${duration(elapsed(attempt.firstWindowMs, attempt.startMs))} · first GL context ${duration(elapsed(attempt.firstContextMs, attempt.startMs))} · first compressed texture ${duration(elapsed(attempt.firstTextureMs, attempt.startMs))}`);
  console.log(`  guest fault ${duration(elapsed(attempt.guestFaultMs, attempt.startMs))} · input route records ${attempt.inputRoutes.length} · errors ${attempt.errors.length}`);
  for (const error of [...new Set(attempt.errors)].slice(0, 3)) console.log(`  ! ${error}`);
  for (const route of attempt.inputRoutes.slice(-2)) console.log(`  > ${route}`);
}

if (attempts.length === 0) {
  console.error('No "Starting ..." launch markers were found; the log cannot be split into attempts.');
  process.exitCode = 1;
}
console.log('Note: present count and visible menu state are not emitted by this console log; record them with the live page receipt.');
