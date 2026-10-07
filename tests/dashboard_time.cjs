const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../web/time.js');
test('Hong Kong and UTC show the same instant with explicit milliseconds', () => {
  assert.equal(T.datetime(0, 'UTC'), '1970-01-01 00:00:00.000');
  assert.equal(T.datetime(1791280050490, 'Asia/Hong_Kong'), '2026-10-06 17:47:30.490');
  assert.equal(T.datetime(1791280050490, 'UTC'), '2026-10-06 09:47:30.490');
  assert.equal(T.parseInput('2026-10-06T17:47:30.490','Asia/Hong_Kong'),1791280050490);
  assert.equal(T.parseInput('2026-10-06T09:47:30.490','UTC'),1791280050490);
});
test('legacy unknown dates and received fallback never imply 1970', () => {
  assert.equal(T.datetime(null),'Unknown datetime');
  assert.deepEqual(T.pick({at:5,event_time_ms:null,received_time_ms:99},'event'),{ms:99,source:'Received · event time unavailable'});
  assert.equal(T.pick({at:5},'event').ms,undefined);
  assert.equal(T.pick({event_time_ms:0,received_time_ms:99},'event').ms,0);
});
test('late event time stays separate from received time', () => {
  const row={at:100,event_time_ms:1000,received_time_ms:36001000,time_source:'exchange trade'};
  assert.equal(T.pick(row,'event').ms,1000);assert.equal(T.pick(row,'received').ms,36001000);
  assert.equal(T.elapsed(36000001),'10:00:00.001');
});
test('first visit without browser history renders and saves its cursor', () => {
  const fs = require('node:fs'), vm = require('node:vm');
  const source = fs.readFileSync(require.resolve('../web/app.js'), 'utf8');
  const renderVisit = source.slice(source.indexOf('function renderVisit()'), source.indexOf('\nfunction render()'));
  const notice = {}, writes = [];
  vm.runInNewContext(renderVisit + '\nrenderVisit();', {
    visit: () => null, $: () => notice, text: () => {}, date: T.datetime,
    data: {generation:'test',seq:'100'}, document:{hidden:false},
    visitKey: () => 'test', save: (...args) => writes.push(args),
  });
  assert.equal(notice.hidden, true);
  assert.equal(JSON.parse(writes[0][1]).seq, '100');
});
