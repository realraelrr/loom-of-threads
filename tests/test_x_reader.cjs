const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/knowledge_archive/adapters/x/read_page.js'), 'utf8');

function read(tweets, {login = false, unresolved = false} = {}) {
  const elements = tweets.map(tweet => ({__reactFiber$test: {memoizedProps: {tweet}}}));
  if (unresolved) elements.push({});
  return JSON.parse(JSON.stringify(vm.runInNewContext(`(${source})()`, {
    document: {querySelectorAll: () => elements, querySelector: () => login ? {} : null},
    location: {href: 'https://x.com/example/status/100', pathname: '/example/status/100'},
  })));
}
const post = (id = '100', fields = {}) => ({id_str: id, user: {id_str: '10', screen_name: 'example', name: 'Example'}, created_at: '2026-11-05T00:00:00.000Z', full_text: 'text', ...fields});

test('preserves relationships and full note text instead of preview', () => {
  const result = read([post('101', {in_reply_to_status_id_str: '100', in_reply_to_user_id_str: '10', conversation_id_str: '100', note_tweet: {text: 'full note', richtext_tags: [{from_index: 0, to_index: 4, richtext_types: ['Bold']}]}})]);
  assert.equal(result.records[0].reply_to_id, '100');
  assert.equal(result.records[0].author.id, '10');
  assert.equal(result.records[0].text, 'full note');
  assert.equal(result.records[0].richtext_tags.length, 1);
});

test('quoted media and mixed media remain separate identifiable assets', () => {
  const result = read([post('100', {quoted_status: post('200', {extended_entities: {media: [{id_str: '201', type: 'photo', media_url_https: 'https://example.com/a.jpg'}]}}), extended_entities: {media: [{id_str: '102', type: 'video', media_url_https: 'https://example.com/poster.jpg', video_info: {duration_millis: 1234, variants: [{content_type: 'video/mp4', bitrate: 100, url: 'https://example.com/v.mp4'}]}}, {id_str: '103', type: 'photo', media_url_https: 'https://example.com/a.jpg'}]}})]);
  assert.equal(result.records.length, 2);
  assert.equal(result.records[0].media.length, 2);
  assert.equal(result.records[0].media[0].variants[0].bitrate, 100);
  assert.equal(result.records[1].media[0].id, '201');
  assert.equal(result.records[0].quoted_id, '200');
});

test('article source styles survive extraction unchanged', () => {
  const content_state = {blocks: [{text: 'withdrawn', inlineStyleRanges: [{style: 'Strikethrough', offset: 0, length: 9}]}], entityMap: []};
  const result = read([post('100', {article: {article_results: {result: {content_state}}}})]);
  assert.deepEqual(result.records[0].article.content_state, content_state);
});

test('missing video variants and article body are explicit gaps', () => {
  const result = read([post('100', {article: {title: 'preview'}, extended_entities: {media: [{id_str: '102', type: 'video', media_url_https: 'https://example.com/p.jpg'}]}})]);
  assert.deepEqual(result.issues.map(i => i.code), ['missing_article_body', 'missing_video_variants']);
});

test('unreadable React elements are not treated as an empty complete result', () => {
  const result = read([], {unresolved: true});
  assert.equal(result.page_state, 'unresolved');
  assert.equal(result.coverage.tweet_elements, 1);
  assert.equal(result.issues[0].code, 'unresolved_tweet_element');
});

test('login and unsafe numeric IDs do not produce valid records', () => {
  assert.equal(read([], {login: true}).page_state, 'login_required');
  const result = read([post(2117000000000000000)]);
  assert.equal(result.records.length, 0);
  assert.equal(result.issues[0].code, 'invalid_id');
});

test('a quoted node appearing later as a visible node is deduplicated', () => {
  const quoted = post('200');
  const result = read([post('100', {quoted_status: quoted}), quoted]);
  assert.deepEqual(result.visible_ids, ['100', '200']);
  assert.equal(result.records.length, 2);
});

test('visible detail replaces a shorter embedded quote', () => {
  const result = read([post('100', {quoted_status: post('200', {full_text: 'preview'})}), post('200', {note_tweet: {text: 'complete long note'}})]);
  assert.equal(result.records.find(r => r.id === '200').text, 'complete long note');
});

test('resolved visible article clears obsolete quote-preview issue', () => {
  const result = read([
    post('100', {quoted_status: post('200', {article: {title: 'preview'}})}),
    post('200', {article: {title: 'full', content_state: {blocks: [{text: 'body'}], entityMap: []}}}),
  ]);
  assert.equal(result.issues.filter(issue => issue.record_id === '200').length, 0);
});
