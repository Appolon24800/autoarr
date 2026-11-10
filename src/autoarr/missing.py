"""One task to identify aired gaps and compare their releases without grabbing."""
import datetime
import json
import re
from pathlib import Path

from .config import AutoarrError
from .results import Results
from .workflows import require


def series(client, args):
    require(client, {'sonarr'})
    if bool(args.series_id is not None) == bool(args.series):
        raise AutoarrError('Supply either a series title or --series-id')
    if args.series_id is not None:
        return client.request('GET', '/series/' + str(args.series_id))
    rows = client.request('GET', '/series')
    exact = [r for r in rows if r.get('title', '').casefold() == args.series.casefold()]
    matches = exact or [r for r in rows if args.series.casefold() in r.get('title', '').casefold()]
    if len(matches) != 1:
        raise AutoarrError('Series title must match exactly one managed show; use list series to inspect IDs')
    return matches[0]


def gaps(client, args):
    show = series(client, args)
    episodes = client.request('GET', '/episode', query=[('seriesId', show['id'])])
    now = datetime.datetime.now(datetime.timezone.utc)
    missing = []
    for e in episodes:
        if e.get('hasFile'):
            continue
        if not args.include_specials and e.get('seasonNumber', 0) == 0:
            continue
        if args.season is not None and e.get('seasonNumber') != args.season:
            continue
        if not args.include_unmonitored and not e.get('monitored'):
            continue
        try:
            aired = datetime.datetime.fromisoformat(e['airDateUtc'].replace('Z', '+00:00'))
            if aired > now:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        missing.append(e)
    return show, sorted(missing, key=lambda e: (e['seasonNumber'], e['episodeNumber']))


def missing(client, args):
    show, episodes = gaps(client, args)
    return episodes


def find_missing(client, args):
    show, episodes = gaps(client, args)
    by_season = {}
    for episode in episodes:
        by_season.setdefault(episode['seasonNumber'], []).append(episode)
    loaded = None
    if args.release_file:
        try:
            loaded = json.loads(Path(args.release_file).read_text())
        except (ValueError, OSError):
            raise AutoarrError('Cannot read release-file JSON') from None
        if not isinstance(loaded, list):
            raise AutoarrError('release-file must contain a native Sonarr release array')
    report = []
    searches = 0
    snapshots = []
    cache = Results(client.state_dir)
    for season, season_episodes in by_season.items():
        releases = loaded if loaded is not None else client.request('GET', '/release',
            query=[('seriesId', show['id']), ('seasonNumber', season)])
        searches += loaded is None
        if not isinstance(releases, list):
            raise AutoarrError('Sonarr release search returned an unexpected response')
        key = None if args.no_cache else cache.save(releases, client.app['name'], 'releases')
        if key:
            snapshots.append(key)
        for episode in season_episodes:
            candidates = []
            for index, release in enumerate(releases):
                number = episode['episodeNumber']
                release_season = release.get('mappedSeasonNumber')
                if release_season in (None, 0):
                    release_season = release.get('seasonNumber')
                mapped = release.get('mappedSeriesId')
                if mapped not in (None, 0, show['id']):
                    continue
                if release_season != season:
                    continue
                # A season pack can contain an incomplete range; do not assume
                # coverage from its name alone. Only explicit episode mappings.
                if number not in release.get('episodeNumbers', []):
                    continue
                labelled = bool(re.search(r'VOSTFR|SUBFRENCH', release.get('title', ''), re.I))
                candidates.append({**({'ref': f'{key}:{index}'} if key else {}), 'title': release.get('title'),
                    'quality': release.get('quality', {}).get('quality', {}).get('name'),
                    'seeders': release.get('seeders'), 'sizeMiB': round(release.get('size', 0) / 1024 ** 2, 1),
                    'indexer': release.get('indexer'), 'score': release.get('customFormatScore'),
                    'vostfrLabel': labelled, 'frenchSubtitlesVerified': False,
                    'rejections': release.get('rejections', [])})
            strict = [r for r in candidates if r['vostfrLabel']]
            eligible = strict if args.vostfr else candidates
            eligible.sort(key=lambda r: (not r['vostfrLabel'], bool(r['rejections']), -(r['seeders'] or 0)))
            # Deduplicate identical named releases across indexers, keeping the
            # best-seeded candidate, while counts retain all indexer results.
            seen, top = set(), []
            for candidate in eligible:
                title = candidate['title'].casefold().removesuffix('.mkv')
                if title not in seen:
                    top.append(candidate)
                    seen.add(title)
                if len(top) >= args.top:
                    break
            if args.top == 0:
                top = []
            report.append({'episode': f'S{season:02d}E{episode["episodeNumber"]:02d}',
                           'id': episode['id'], 'title': episode['title'],
                           'releases': len(candidates), 'vostfrLabelled': len(strict), 'candidates': top})
    return {'series': {'id': show['id'], 'title': show['title']}, 'missing': len(episodes),
            'mode': 'VOSTFR-labelled only' if args.vostfr else 'all languages; subtitles unverified',
            'searches': searches, 'fromFile': loaded is not None,
            'releaseResults': snapshots, 'episodes': report, 'downloadsStarted': 0}
