import copy
import json
import posixpath

from .config import ARR, MANAGERS, AutoarrError
from .output import pairs, records, set_path

ITEM_EDITABLE = {
    'Id', 'Name', 'OriginalTitle', 'OriginalLanguage', 'Overview', 'SortName',
    'ForcedSortName', 'IndexNumber', 'ParentIndexNumber', 'IndexNumberEnd',
    'Genres', 'Tags', 'Studios', 'People', 'ProviderIds', 'ProductionYear',
    'PremiereDate', 'EndDate', 'CommunityRating', 'OfficialRating', 'CriticRating',
    'CustomRating', 'PreferredMetadataLanguage', 'PreferredMetadataCountryCode',
    'LockData', 'LockedFields', 'RunTimeTicks', 'DisplayOrder', 'Status',
    'AirDays', 'AirTime', 'DateCreated', 'AspectRatio', 'Taglines',
    'ProductionLocations', 'AirsAfterSeasonNumber', 'AirsBeforeEpisodeNumber',
    'AirsBeforeSeasonNumber', 'Video3DFormat',
}
ALIASES = {'movies': 'movie', 'shows': 'series', 'artists': 'artist',
           'albums': 'album', 'tracks': 'track', 'authors': 'author', 'books': 'book',
           'profiles': 'qualityprofile', 'roots': 'rootfolder', 'tags': 'tag',
           'indexers': 'indexer', 'clients': 'downloadclient'}


def upsert_query(query, **values):
    return [(k, v) for k, v in query if k not in values] + list(values.items())


def require(client, kinds):
    if client.kind not in kinds:
        raise AutoarrError('This shortcut supports ' + ', '.join(sorted(kinds)) + '; use ops or api for this app')


def resource_path(client, resource, identifier=None):
    if client.kind in ARR:
        resource = ALIASES.get(resource, resource)
    if client.kind == 'jellyfin':
        resource = {'items': 'Items', 'users': 'Users', 'sessions': 'Sessions',
                    'libraries': 'Library/VirtualFolders', 'tasks': 'ScheduledTasks'}.get(resource, resource)
    if identifier is not None:
        from urllib.parse import quote
        resource = resource.rstrip('/') + '/' + quote(str(identifier), safe='')
    return '/' + resource.lstrip('/')


def paginated(client, path, query=None, all_pages=False):
    query = list(query or [])
    if not all_pages:
        return client.request('GET', path, query=query)
    if client.kind in ARR:
        mode, size = 'arr', 250
    elif client.kind == 'jellyfin':
        mode, size = 'jellyfin', 250
    elif client.kind == 'bazarr':
        mode, size = 'bazarr', 250
    elif client.kind == 'seerr':
        mode, size = 'seerr', 50
    else:
        return client.request('GET', path, query=query)
    combined = []
    first = None
    prior = None
    for page in range(10000):
        if mode == 'arr': qs = upsert_query(query, page=page + 1, pageSize=size)
        elif mode == 'jellyfin': qs = upsert_query(query, startIndex=page * size, limit=size)
        elif mode == 'bazarr': qs = upsert_query(query, start=page * size, length=size)
        else: qs = upsert_query(query, skip=page * size, take=size)
        data = client.request('GET', path, query=qs)
        rows = records(data)
        if isinstance(data, list) or rows is None:
            return data
        if first is None:
            first = data
        fingerprint = json.dumps(rows, sort_keys=True)
        if rows and fingerprint == prior:
            raise AutoarrError('Pagination repeated a page; the endpoint may ignore pagination parameters')
        prior = fingerprint
        combined.extend(rows)
        total = data.get('totalRecords', data.get('TotalRecordCount', data.get('total')))
        if mode == 'seerr':
            total = data.get('pageInfo', {}).get('results', total)
        if not rows or (total is not None and len(combined) >= int(total)) or (total is None and len(rows) < size):
            key = next(k for k in ('records', 'Items', 'data', 'results') if isinstance(first.get(k), list))
            return {**first, key: combined, 'autoarrPagination': {'pages': page + 1, 'returnedRecords': len(combined)}}
    raise AutoarrError('Pagination exceeded 10,000 pages')


def command(client, name, body, wait=False, wait_timeout=300):
    require(client, ARR)
    from .waiting import validate_options, wait_for
    if wait:
        validate_options(wait_timeout, 2)
    data = {**body, 'name': name}
    result = client.request('POST', '/command', body=data)
    if client.dry_run or not wait:
        return result
    if 'id' not in result:
        raise AutoarrError('Command accepted without an ID; inspect the app command history')
    outcome = wait_for(client, {'type': 'command', 'id': result['id']}, wait_timeout, interval=2)
    if outcome['status'] != 'completed':
        raise AutoarrError('Application command did not complete: ' + json.dumps(outcome))
    return outcome['response']


def move(client, identifier, root=None, destination=None):
    require(client, {'radarr', 'sonarr'})
    entity = 'movie' if client.kind == 'radarr' else 'series'
    item = client.request('GET', f'/{entity}/{identifier}')
    roots = client.request('GET', '/rootfolder')
    paths = {posixpath.normpath(r['path']) for r in roots}
    if root:
        root = posixpath.normpath(root)
        destination = posixpath.join(root, posixpath.basename(item['path'].rstrip('/')))
    else:
        destination = posixpath.normpath(destination)
        root = posixpath.dirname(destination)
    if root not in paths:
        raise AutoarrError('Destination parent must be an existing app root folder; inspect roots or add it through the API')
    if destination == posixpath.normpath(item['path']):
        return {'changed': False, 'path': item['path']}
    # Check actual destination contents through the application's filesystem API.
    # Refuse any existing destination, including metadata-only collisions.
    listing = client.request('GET', '/filesystem', query=[('path', root.rstrip('/') + '/'), ('includeFiles', True)])
    if not isinstance(listing, dict) or 'directories' not in listing:
        raise AutoarrError('Cannot check the destination through the app filesystem API; no move was requested')
    entries = listing.get('directories', []) + listing.get('files', [])
    expected_parent = posixpath.dirname(root.rstrip('/')) or '/'
    if posixpath.normpath(listing.get('parent', '')) != expected_parent:
        raise AutoarrError('Filesystem API did not list the requested destination root; no move was requested')
    existing = {posixpath.normpath(x.get('path', '')) for x in entries}
    if destination in existing:
        raise AutoarrError('Destination already exists; inspect it before requesting a move: ' + destination)
    updated = {**item, 'path': destination, 'rootFolderPath': root}
    response = client.request('PUT', f'/{entity}/{identifier}', query=[('moveFiles', True)], body=updated)
    if client.dry_run:
        return response
    return {'requested': True, 'mover': client.kind, 'moveFiles': True,
            'oldPath': item['path'], 'newPath': destination, 'response': response}


def jellyfin_user(client, supplied=None):
    if supplied or client.app.get('user_id'):
        return supplied or client.app['user_id']
    users = client.request('GET', '/Users')
    admins = [u for u in users if u.get('Policy', {}).get('IsAdministrator')]
    if admins:
        return admins[0]['Id']
    if len(users) == 1:
        return users[0]['Id']
    raise AutoarrError('Specify --user-id or set user_id in this Jellyfin connection')


def metadata(client, identifier, changes, user_id=None):
    require(client, {'jellyfin'})
    invalid = set(changes) - ITEM_EDITABLE
    if invalid or 'Id' in changes:
        raise AutoarrError('Unsupported metadata fields: ' + ', '.join(sorted(invalid or {'Id'})))
    uid = jellyfin_user(client, user_id)
    before = client.request('GET', '/Items/' + identifier, query=[('userId', uid)])
    if not changes:
        return before
    # Exclude read-only DTO properties (notably TrickplayInfo). Preserve all
    # editable scalars; omission of OriginalLanguage would clear it on this server.
    unchanged_collections = {'People', 'ProviderIds', 'Genres', 'Tags', 'Studios', 'ProductionLocations', 'LockedFields'}
    body = {k: v for k, v in before.items() if k in ITEM_EDITABLE and (k not in unchanged_collections or k in changes)}
    body.update(changes)
    response = client.request('POST', '/Items/' + identifier, body=body)
    if client.dry_run:
        return response
    after = client.request('GET', '/Items/' + identifier, query=[('userId', uid)])
    mismatches = {k: {'requested': v, 'actual': after.get(k)} for k, v in changes.items() if k not in ('SortName', 'ForcedSortName') and after.get(k) != v}
    if mismatches:
        raise AutoarrError('Metadata write returned but readback differs: ' + json.dumps(mismatches))
    return {'updated': identifier, 'changedFields': sorted(changes), 'item': after}


def image_set(client, identifier, image_type, url, append=False, index=0):
    require(client, {'jellyfin'})
    from urllib.parse import urlsplit
    if urlsplit(url).scheme not in ('https', 'http'):
        raise AutoarrError('Artwork URL must use HTTP or HTTPS')
    before = client.request('GET', '/Items/' + identifier + '/Images')
    old = [i for i in before if i['ImageType'] == image_type]
    if image_type == 'Backdrop' and old and not append and not any(i.get('ImageIndex') == index for i in old):
        raise AutoarrError('No backdrop exists at the requested replacement index')
    result = client.request('POST', '/Items/' + identifier + '/RemoteImages/Download',
                            query=[('type', image_type), ('imageUrl', url)])
    if client.dry_run:
        return {'download': result, 'replaceBackdropIndex': index if image_type == 'Backdrop' and old and not append else None}
    after = client.request('GET', '/Items/' + identifier + '/Images')
    if image_type == 'Backdrop' and old and not append:
        new = [i for i in after if i['ImageType'] == image_type]
        if len(new) != len(old) + 1:
            raise AutoarrError('Unexpected backdrop count after download; no prior artwork was removed')
        new_index = max(i['ImageIndex'] for i in new)
        client.request('POST', f'/Items/{identifier}/Images/Backdrop/{new_index}/Index', query=[('newIndex', index)])
        # Only the superseded image is removed; this is never a media deletion.
        client.request('DELETE', f'/Items/{identifier}/Images/Backdrop/{new_index}')
        after = client.request('GET', '/Items/' + identifier + '/Images')
    return {'itemId': identifier, 'images': after}


def updated_body(before, data, fields):
    if not isinstance(before, dict) or not isinstance(data, dict):
        raise AutoarrError('Updates need JSON objects')
    body = copy.deepcopy(before)
    body.update(data)
    for path, value in pairs(fields):
        set_path(body, path, value)
    return body


def add_media(client, term, root, profile_id, metadata_profile_id=None,
              language_profile_id=None, search=False, series_type='standard', monitor='all'):
    require(client, MANAGERS)
    entity = {'radarr': 'movie', 'sonarr': 'series', 'lidarr': 'artist',
              'bookshelf': 'author', 'readarr': 'author'}[client.kind]
    roots = client.request('GET', '/rootfolder')
    if posixpath.normpath(root) not in {posixpath.normpath(r['path']) for r in roots}:
        raise AutoarrError('Choose an existing root from roots')
    profiles = client.request('GET', '/qualityprofile')
    if profile_id not in {p['id'] for p in profiles}:
        raise AutoarrError('Choose an existing quality profile ID from profiles')
    if client.kind in ('lidarr', 'bookshelf', 'readarr') and metadata_profile_id is None:
        raise AutoarrError('This app also requires --metadata-profile-id; inspect list metadataprofile')
    lookup = client.request('GET', '/' + entity + '/lookup', query=[('term', term)])
    candidates = lookup if isinstance(lookup, list) else [lookup]
    if len(candidates) != 1:
        raise AutoarrError('Lookup returned ' + str(len(candidates)) + ' candidates; use lookup first and an exact provider ID term')
    body = copy.deepcopy(candidates[0])
    if body.get('id'):
        raise AutoarrError('This item is already managed; use get or update')
    body.pop('id', None)
    body.update({'rootFolderPath': root, 'qualityProfileId': profile_id, 'monitored': monitor != 'none'})
    if metadata_profile_id is not None:
        body['metadataProfileId'] = metadata_profile_id
    if language_profile_id is not None:
        body['languageProfileId'] = language_profile_id
    if client.kind == 'radarr':
        body['minimumAvailability'] = 'released'
        body['addOptions'] = {'searchForMovie': search}
    elif client.kind == 'sonarr':
        body.update({'seriesType': series_type, 'seasonFolder': True})
        body['addOptions'] = {'monitor': monitor, 'searchForMissingEpisodes': search}
    elif client.kind == 'lidarr':
        body['addOptions'] = {'monitor': monitor, 'searchForMissingAlbums': search}
    else:
        body['addOptions'] = {'monitor': monitor, 'searchForMissingBooks': search}
    return client.request('POST', '/' + entity, body=body)
