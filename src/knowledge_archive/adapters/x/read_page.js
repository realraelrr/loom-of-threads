function readPage() {
  // This function is sent to the official Chrome DevTools MCP. No credentials,
  // network interception, hidden API calls, or browser profile data are read.
  const records = new Map();
  const visibleIds = [];
  const issues = [];
  const issue = (code, id, detail) => issues.push({code, record_id: id, detail});
  const isId = value => typeof value === 'string' && /^\d+$/.test(value);

  function extract(t, includeQuote) {
    if (!isId(t.id_str)) {
      issue('invalid_id', null, 'Expected a decimal string; numeric IDs can lose precision.');
      return null;
    }
    const id = t.id_str;
    // A visible detail node may be richer than an earlier embedded quote.
    if (!includeQuote && records.has(id)) return id;
    if (includeQuote && records.has(id)) {
      for (let i = issues.length - 1; i >= 0; i--) {
        if (issues[i].record_id === id) issues.splice(i, 1);
      }
    }
    const user = t.user || {};
    const note = t.note_tweet?.note_tweet_results?.result || t.note_tweet;
    const article = t.article?.article_results?.result || t.article;
    const entities = note?.entity_set || t.entities || {};
    const text = note?.text ?? t.full_text ?? t.text;
    if (!isId(user.id_str)) issue('missing_author_id', id, 'Cannot establish author identity from a handle alone.');
    if (typeof text !== 'string') issue('missing_text', id, 'No supported text field was returned.');
    if (!t.created_at) issue('missing_created_at', id, 'Publication time is absent.');
    if (t.truncated && !note?.text) issue('truncated_text', id, 'Detail extraction is required.');
    if (article && !article.content_state) issue('missing_article_body', id, 'Article metadata exists without content_state.');

    const media = (t.extended_entities?.media || t.entities?.media || []).map(m => {
      const variants = m.video_info?.variants || [];
      if (!isId(m.id_str)) issue('missing_media_id', id, 'Cannot safely join media by position.');
      if (!m.media_url_https) issue('missing_media_url', id, `Media ${m.id_str || '?'} has no image/poster URL.`);
      if (!['photo', 'video', 'animated_gif'].includes(m.type)) issue('unsupported_media_type', id, String(m.type));
      if (['video', 'animated_gif'].includes(m.type) && !variants.some(v => v.content_type === 'video/mp4' && v.url)) {
        issue('missing_video_variants', id, `Media ${m.id_str || '?'} needs details.`);
      }
      return {
        id: m.id_str ?? null, media_key: m.media_key ?? null,
        type: m.type, image_url: m.media_url_https ?? null,
        original_info: m.original_info ?? null,
        duration_millis: m.video_info?.duration_millis ?? null,
        variants: variants.map(v => ({content_type: v.content_type, url: v.url, bitrate: v.bitrate ?? null})),
      };
    });

    const record = {
      id, author: {id: user.id_str ?? null, handle: user.screen_name ?? null, name: user.name ?? null},
      created_at: t.created_at ?? null,
      conversation_id: t.conversation_id_str ?? null,
      reply_to_id: t.in_reply_to_status_id_str ?? null,
      reply_to_author_id: t.in_reply_to_user_id_str ?? null,
      quoted_id: t.quoted_status?.id_str ?? t.quoted_status_id_str ?? null,
      reposted_id: t.retweeted_status?.id_str ?? null,
      text: typeof text === 'string' ? text : null,
      is_note: typeof note?.text === 'string',
      // Preserve source offsets/styles; rendering is a separate responsibility.
      richtext_tags: note?.richtext_tags ?? [],
      inline_media: note?.inline_media ?? [],
      urls: entities.urls ?? [],
      display_text_range: t.display_text_range ?? null,
      media, article: article ?? null,
      edit_control: t.edit_control ?? null,
    };
    records.set(id, record);
    if (includeQuote && t.quoted_status) extract(t.quoted_status, false);
    if (includeQuote && t.retweeted_status) extract(t.retweeted_status, false);
    return id;
  }

  const elements = [...document.querySelectorAll('article[data-testid="tweet"]')];
  for (const el of elements) {
    const key = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
    let fiber = key ? el[key] : null;
    let tweet = null;
    for (let depth = 0; depth < 64 && fiber; depth++, fiber = fiber.return) {
      if (fiber.memoizedProps?.tweet?.id_str) { tweet = fiber.memoizedProps.tweet; break; }
    }
    if (!tweet) {
      issue('unresolved_tweet_element', null, 'Visible tweet element has no supported React data.');
      continue;
    }
    const id = extract(tweet, true);
    if (id) visibleIds.push(id);
  }
  const loginRequired = /\/i\/flow\/login/.test(location.pathname) || !!document.querySelector('input[autocomplete="username"]');
  return {
    schema_version: 1, observed_at: new Date().toISOString(), page_url: location.href,
    page_state: loginRequired ? 'login_required' : visibleIds.length ? 'content' : 'unresolved',
    visible_ids: visibleIds, records: [...records.values()], issues,
    coverage: {tweet_elements: elements.length, extracted_elements: visibleIds.length},
  };
}
