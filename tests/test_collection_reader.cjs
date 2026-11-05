const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/knowledge_archive/adapters/x/read_collection.js'), 'utf8');
function read(rows, {profile = 'example', account = 'Example', collection = 'likes', pathname = '/i/history/likes', login = false} = {}) {
  const elements = rows.map(props => ({__reactFiber$test: {memoizedProps: props}}));
  return JSON.parse(JSON.stringify(vm.runInNewContext(`(${source})(${JSON.stringify(profile)},${JSON.stringify(collection)})`, {
    document: {querySelectorAll: selector => {assert.ok(selector.includes('primaryColumn')); return elements;},
      querySelector: selector => selector.includes('username') ? (login ? {} : null) : account ? {getAttribute: () => '/' + account} : null},
    location: {href: 'https://x.com' + pathname, hostname: 'x.com', pathname},
  })));
}
const row = (id, fields = {}) => ({tweet: {id_str: id, user: {id_str: '10', screen_name: 'author'}, ...fields}});
test('likes require explicit viewer state and ignore promoted rows', () => {
  const result = read([row('1', {favorited: true}), row('2', {favorited: false}), {...row('3', {favorited: true}), promotedContent: {id: 'ad'}}]);
  assert.deepEqual(result.selected_ids, ['1']);
  assert.equal(result.page_state, 'content');
});
test('reposts cannot be inferred from an author or social text', () => {
  const result = read([row('1', {retweeted: true}), {...row('2', {retweeted: false}), socialContext: {text: 'You reposted'}}], {collection: 'reposts', pathname: '/example'});
  assert.deepEqual(result.selected_ids, ['1']);
});
test('wrong logged-in account, route and login fail explicitly', () => {
  assert.equal(read([row('1')], {account: 'other'}).page_state, 'wrong_account');
  assert.equal(read([row('1')], {pathname: '/home'}).page_state, 'wrong_route');
  assert.equal(read([], {login: true}).page_state, 'login_required');
  assert.equal(read([], {account: null}).page_state, 'unresolved_account');
});
test('missing collection flags remain a gap', () => {
  const result = read([row('1')]);
  assert.deepEqual(result.selected_ids, []);
  assert.deepEqual(result.issues, ['missing_liked_state']);
});
test('unreadable and empty timelines never prove an empty collection', () => {
  assert.equal(read([]).page_state, 'unresolved');
  const result = read([{}]);
  assert.equal(result.page_state, 'unresolved');
  assert.deepEqual(result.issues, ['unresolved_tweet_element']);
});
