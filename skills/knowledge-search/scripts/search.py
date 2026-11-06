"""Read-only literal keyword lookup in a published Loom archive."""
import argparse
import json
from pathlib import Path
import sqlite3
from contextlib import closing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('keywords', nargs='+')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--include-inactive', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or any(not word.strip() for word in args.keywords):
        parser.error('Use nonempty keywords and a limit between 1 and 100')
    root = args.root
    try:
        if root is None:
            config = Path.home() / '.config/loom-of-threads/config.json'
            if config.is_file():
                root = Path(json.loads(config.read_text())['default_archive'])
            elif (Path.cwd() / 'archive.toml').is_file() and (Path.cwd() / 'raw/archive.json').is_file():
                root = Path.cwd()
            else:
                raise ValueError('No default archive configured; provide --root')
        root = root.expanduser().resolve()
        view = root / 'view'
        db = view / 'archive.db'
        if not db.is_file():
            raise ValueError('Published archive.db is missing; run archive maintenance first')
        where = ' OR '.join('(instr(lower(r.title),lower(?))>0 OR instr(lower(r.body),lower(?))>0)' for _ in args.keywords)
        visibility = '' if args.include_inactive else "AND (NOT EXISTS(SELECT 1 FROM members m WHERE m.record_id=r.id) OR EXISTS(SELECT 1 FROM members m JOIN entries e ON e.id=m.entry_id WHERE m.record_id=r.id AND e.status='active'))"
        params = [word for word in args.keywords for _ in range(2)]
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f'SELECT r.id,r.title,r.author_handle,r.created_at,r.source_url,r.coverage,r.file,substr(r.body,1,700) AS excerpt FROM records r WHERE ({where}) {visibility} ORDER BY r.created_at DESC,r.id LIMIT ?', [*params, args.limit]).fetchall()
            results = []
            for row in rows:
                result = dict(row)
                result['local_file'] = str(view / result.pop('file'))
                result['entries'] = [dict(e) for e in conn.execute('SELECT e.id,e.l2_topic,e.status,e.capture_state FROM members m JOIN entries e ON e.id=m.entry_id WHERE m.record_id=? ORDER BY e.id', (row['id'],))]
                results.append(result)
        print(json.dumps({'archive': str(root), 'view': str(view), 'results': results, 'limit': args.limit}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        parser.exit(1, f'Cannot search archive: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
