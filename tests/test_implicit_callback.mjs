import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../publikot/implicit_callback.html', import.meta.url), 'utf8');
const source = html.match(/<script nonce="__NONCE__">([\s\S]*?)<\/script>/)[1];

async function runCallback(hash, search = '', fetcher) {
  const events = [];
  const status = {textContent: ''};
  const location = {hash, search, pathname: '/oauth/vk/callback',
    replace(path) { events.push({kind: 'navigate', path}); }};
  const history = {replaceState(_, __, path) {
    events.push({kind: 'clean', path});
    location.hash = '';
    location.search = '';
  }};
  const fetch = async (path, options) => {
    events.push({kind: 'post', path, options});
    assert.equal(location.hash, '');
    assert.equal(location.search, '');
    return fetcher ? fetcher() : {status: 200, text: async () => 'Сообщество подключено.'};
  };
  await vm.runInNewContext(source, {URLSearchParams, location, history,
    document: {getElementById: () => status}, fetch});
  return {events, status};
}

test('fragment is removed before POST; token travels only in JSON body', async () => {
  const {events, status} = await runCallback('#access_token=private-key&state=one-use-state&expires_in=3600');
  assert.equal(events[0].kind, 'clean');
  assert.equal(events[0].path, '/oauth/vk/callback');
  assert.equal(events[1].path, '/oauth/vk/callback');
  assert.equal(events[1].options.method, 'POST');
  assert.equal(events[1].options.redirect, 'error');
  assert.equal(events[1].options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(events[1].options.body), {
    state: 'one-use-state', access_token: 'private-key', expires_in: '3600', denied: false,
  });
  assert.equal(status.textContent, 'Сообщество подключено.');
  assert.ok(!status.textContent.includes('private-key'));
});

test('duplicate credentials and missing fragment produce no request', async () => {
  for (const hash of ['', '#access_token=a&access_token=b&state=s&expires_in=0',
                      '#access_token=a&state=s&state=other&expires_in=0', '#access_token=a&state=s']) {
    const result = await runCallback(hash);
    assert.equal(result.events.length, 1);
    assert.ok(result.status.textContent.includes('ВК'));
  }
});

test('query error is posted as denial without forwarding provider text', async () => {
  const {events} = await runCallback('', '?state=one-use-state&error=access_denied&error_description=private-data');
  const payload = JSON.parse(events[1].options.body);
  assert.equal(payload.denied, true);
  assert.equal(payload.access_token, '');
  assert.ok(!events[1].options.body.includes('private-data'));
});

test('network error never echoes fragment or exception contents', async () => {
  const {status} = await runCallback('#access_token=private-key&state=s&expires_in=0', '',
    () => { throw new Error('private-key'); });
  assert.ok(status.textContent.includes('Не удалось'));
  assert.ok(!status.textContent.includes('private-key'));
});

test('API validation error is shown as plain text', async () => {
  const {status} = await runCallback('#access_token=private-key&state=s&expires_in=0', '',
    () => ({status: 400, text: async () => 'Проверка подключения ВК: account.getAppPermissions, код API 15.'}));
  assert.ok(status.textContent.includes('код API 15'));
});

test('community import navigates to the same-origin picker without credentials in URL', async () => {
  const path = '/oauth/vk/select?state=' + 'a'.repeat(43);
  const {events} = await runCallback('#access_token=private-key&state=s&expires_in=0', '', () => ({
    status: 200, headers: {get: () => 'application/json; charset=utf-8'},
    text: async () => JSON.stringify({selection_url: path})
  }));
  assert.deepEqual(events.at(-1), {kind: 'navigate', path});
  assert.ok(!events.at(-1).path.includes('private-key'));
});

test('community import rejects external redirect targets', async () => {
  const {events, status} = await runCallback('#access_token=private-key&state=s&expires_in=0', '', () => ({
    status: 200, headers: {get: () => 'application/json'},
    text: async () => JSON.stringify({selection_url: 'https://attacker.example'})
  }));
  assert.ok(!events.some(event => event.kind === 'navigate'));
  assert.ok(status.textContent.includes('Не удалось'));
});
