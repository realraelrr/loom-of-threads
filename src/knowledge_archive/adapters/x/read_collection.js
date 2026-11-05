function readCollection(profile, collection) {
  const login = /\/i\/flow\/login/.test(location.pathname) || !!document.querySelector('input[autocomplete="username"]');
  const accountPath = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]')?.getAttribute('href');
  const account = accountPath?.replace(/^\//, '').replace(/\/$/, '') ?? null;
  const expected = profile.toLowerCase();
  const pathname = location.pathname.replace(/\/$/, '').toLowerCase();
  const route = location.hostname === 'x.com' && (collection === 'likes'
    ? [ '/' + expected + '/likes', '/i/history/likes' ].includes(pathname)
    : collection === 'reposts' && pathname === '/' + expected + '/reposts');
  const rows = [], issues = [];
  for (const el of document.querySelectorAll('[data-testid="primaryColumn"] section[role="region"] article[data-testid="tweet"]')) {
    let fiber = el[Object.keys(el).find(k => k.startsWith('__reactFiber$'))];
    let props;
    for (let depth = 0; depth < 64 && fiber; depth++, fiber = fiber.return) {
      if (fiber.memoizedProps?.tweet?.id_str) { props = fiber.memoizedProps; break; }
    }
    if (!props) { issues.push('unresolved_tweet_element'); continue; }
    const t = props.tweet;
    if (typeof t.id_str !== 'string' || !/^\d+$/.test(t.id_str)) { issues.push('invalid_id'); continue; }
    const wrapped = collection === 'reposts' && t.retweeted_status != null;
    const source = wrapped ? t.retweeted_status : t;
    if (typeof source.id_str !== 'string' || !/^\d+$/.test(source.id_str)) { issues.push('invalid_reposted_source_id'); continue; }
    const actorMatches = !wrapped || (typeof t.user?.screen_name === 'string' && t.user.screen_name.toLowerCase() === expected);
    if (!actorMatches) issues.push('repost_actor_mismatch');
    // These are the logged-in viewer's explicit action flags, not a text or
    // author-name inference. Social-context schemas vary and are not guessed.
    const promoted = !!props.promotedContent;
    const flag = collection === 'likes' ? t.favorited : t.retweeted;
    const selected = !promoted && actorMatches && flag === true;
    if (!promoted && collection === 'likes' && typeof flag !== 'boolean') issues.push('missing_liked_state');
    if (!promoted && collection === 'reposts' && typeof flag !== 'boolean') issues.push('missing_reposted_state');
    rows.push({id: source.id_str, event_id: wrapped ? t.id_str : null,
      actor_id: wrapped ? t.user?.id_str ?? null : null,
      actor_handle: wrapped ? t.user?.screen_name ?? null : null,
      author_id: source.user?.id_str ?? null,
      author_handle: source.user?.screen_name ?? null, favorited: t.favorited ?? null,
      retweeted: t.retweeted ?? null, promoted, selected,
      social_context_present: props.socialContext != null});
  }
  const pageState = login ? 'login_required' : !route ? 'wrong_route'
    : !account ? 'unresolved_account' : account.toLowerCase() !== expected ? 'wrong_account'
    : rows.length ? 'content' : 'unresolved';
  return {schema_version: 1, observed_at: new Date().toISOString(), page_url: location.href,
    profile: account, collection, page_state: pageState, rows, issues,
    visible_ids: rows.map(r => r.id), selected_ids: rows.filter(r => r.selected).map(r => r.id),
    coverage: 'observed_only'};
}
