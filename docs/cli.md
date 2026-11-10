# AutoARR CLI

Use `autoarr` to operate each application through its native API. Version 0.5 is implemented in Rust. Build once with `cargo build --release --locked`; `./bin/autoarr` points directly to the resulting native executable. No Python interpreter is used for normal commands. `autoarr APP --help` lists the relevant shortcuts; `autoarr APP ACTION --help` explains their arguments. Existing private connection JSON, schema/result caches, and CLI syntax are compatible with the Python 0.4 reference retained under `src/autoarr/` and `bin/autoarr-python`.

Supported applications include Radarr, Sonarr, Lidarr, Prowlarr, Bazarr, Bookshelf, Jellyfin, Seerr, Kapowarr, qBittorrent, Navidrome, Autobrr, and Cleanuparr. Configure each connection with your own URL and authentication. No connection credentials ship with the repository.

## Quick start

```sh
autoarr apps
autoarr doctor
autoarr sonarr queue --all
autoarr lidarr queue --all --select id,title,status,trackedDownloadStatus,statusMessages --table
autoarr radarr wanted --all --select id,title,year --limit 20 --table
autoarr bazarr wanted --media episodes --all --limit 20
autoarr sonarr list series --filter 'title~=monogatari' --select id,title,path --table
autoarr jellyfin items --type Series --search Monogatari
```

Compact, single-line JSON is the default: relevant fields, at most **10 rows and 6,000 characters**, plus `fetched`, `matched`, `shown`, and a private `result` ID. Queue issues are deduplicated; release rows omit large download URLs and nested metadata. Truncation is explicit (`more`, `outputLimited`, or `omitted`). Full native responses remain in the private cache. Global options can appear before or after the app/action:

- `--table`: compact human-readable output.
- `--select id,title,path`: project fields, including dotted paths and array wildcards such as `languages.*.name` or `languages[].name`.
- `--filter 'title~=monogatari'`: local filter; repeat to combine with AND. `--any-filter` adds an OR group that must also match. Operators and examples follow below.
- `--sort=-seeders,size`: sort by multiple fields; prefix descending fields with `-`. Use `=` before a value starting with `-`.
- `--unique-by guid,indexerId`: deduplicate after sorting, retaining the first match.
- `--count` / `--group-by status,indexer`: filtered counts or grouped counts instead of records, computed before the display limit.
- `--limit 20`: override the displayed row limit after filtering. It does not limit server retrieval or override the character budget.
- `--full`: return native fields and all fetched rows, without the default character budget. Explicit filters, selection, sort, and limits still apply. `--pretty` adds indentation.
- `--max-output 2000`: set the character budget. With `--full`, an oversized response raises an error instead of truncating.
- `--all`: follow supported pagination in Arr, Jellyfin, Bazarr, and Seerr. It fetches all pages; output still follows the compact limits. Unpaginated arrays are already complete. Start at the beginning when combining this with query parameters.
- `-q pageSize=20`: pass a native query parameter. Repeat `-q` for additional values.
- `--timeout 300`: HTTP timeout in seconds. Timed-out writes are never automatically retried.
- `--wait --wait-timeout 3600`: wait for a supported action to finish, with progress on stderr. Use `--poll-interval 5` to control checks or `--quiet-progress` to suppress them. See [waiting and agent continuation](#waiting-and-agent-continuation).
- `--dry-run`: perform necessary read checks, then print the proposed write request without sending it. Searches and previews using GET can still contact the app or indexers; dry-run is not an offline mode.

`matched` counts the fetched records that satisfy the filters, after deduplication; it is not a server-wide count unless all records were fetched. `available`, when supplied by the API, is the server's total. A supported equality or ID-array filter can also become a native query parameter when the cached schema advertises it; local filtering still verifies the result. Otherwise filtering is entirely local. Use `-q` to explicitly control server retrieval.

### Filters and offline follow-ups

```sh
autoarr sonarr list episode --filter 'seriesId=24' --filter 'hasFile=false' \
  --filter 'seasonNumber in [12,13]' --select id,code,title

# Refine a previous response without contacting any app or indexer.
autoarr result --id RESULT_ID --filter 'episodeNumbers overlaps [6,7,8]' \
  --any-filter 'title~=VOSTFR' --any-filter 'title~=SUBFRENCH' --count
autoarr result --id RESULT_ID --filter 'seeders>=5' --sort=-seeders \
  --unique-by title --select ref,title,seeders,indexer --limit 5
autoarr result --file .local/ahs-search/season13-releases.json --group-by indexer
```

Filters support `=`, `!=`, `>`, `>=`, `<`, `<=`, case-insensitive substring `~=` and exclusion `!~=`, and case-insensitive regex `=~`. Regex patterns use Rust regex syntax, which excludes look-around and backreferences; ordinary alternation, groups, anchors, and character classes remain supported. Other examples: `id in [1,2]`, `id notin [1,2]`, `seeders between [5,100]`, `episodeNumbers contains 6`, `episodeNumbers overlaps [6,7,8]`, `languages.*.name=English`, `subtitle missing`, and `subtitle exists`. Wildcard comparisons match any array element; negative comparisons require every element to pass. Values use JSON syntax when applicable. Missing means absent/null or an empty array. No expression or shell code is evaluated.

Snapshots contain the **exact unredacted API response**, stored locally with mode 0600 under `.local/autoarr/results/`. Retention is 24 hours, at most 100 snapshots and 128 MiB; old snapshots can be evicted earlier by these bounds. Replay reports `cachedAt` and reflects that saved state. `--no-cache` disables new result snapshots (including batch children); ordinary API journals and schema caching are separate. Cached data is not proof of current queue state or release availability. Explicit `--output FILE` exports remain available for longer retention.

### Repetitive work in one command

```sh
# Parallel read checks, one bounded response.
autoarr batch --apps sonarr,radarr,lidarr -- queue --all
autoarr batch --ids 23728-23730 -- sonarr get episode '{id}'

# Preview the same native update for several explicit IDs.
autoarr --dry-run batch --ids 7,8,9 -- sonarr update series '{id}' --set monitored=true
autoarr --dry-run batch --file @routine.json
```

Reusable `routine.json` example:

```json
[
  ["sonarr", "health"],
  ["radarr", "health"],
  {
    "foreach": [{"id": 7, "monitored": true}, {"id": 8, "monitored": false}],
    "argv": ["sonarr", "update", "series", "{item.id}", "--set", "monitored={item.monitored}"]
  }
]
```

Templates accept `{app}`, `{id}`, `{item}`, and `{item.FIELD}`; JSON body placeholders preserve types and escaping. They never execute shell commands. Static syntax and connections are checked before any job starts. Independent known read commands run with four workers by default (`--workers 1`–`8`), including consecutive read groups within a batch containing writes. Each read group finishes before the next write starts. Writes and raw API calls run sequentially. Writes stop after an error unless `--keep-going` is given, and completed writes are **not rolled back**. The exit code is nonzero if any job failed. Nested batches, credential configuration, and SSH initialization are excluded. Parent `--dry-run` and `--no-cache` apply to every child.

Each job shows at most three records; use its result ID for details. The outer character budget can omit jobs from display while preserving total success/failure counts and the complete private batch snapshot. Flags after the `--` separator apply to children; place outer formatting flags before it.

### Dependent steps and checks

Use named steps when the next action is already known and only needs data from an earlier response. AutoARR transfers values and checks expectations internally, so an agent does not need another model turn to copy an ID or inspect an expected count. For example, this task file finds one Jellyfin series and refreshes that specific item:

```json
[
  {
    "name": "item",
    "argv": ["jellyfin", "items", "--type", "Series", "--search", "Monogatari"],
    "expect": ["count=1"]
  },
  {
    "argv": ["jellyfin", "refresh", "{steps.item.data.Items.0.Id}"]
  }
]
```

```sh
# Read the source and preview its already authorized follow-up in one tool call.
autoarr --dry-run batch --file @followup.json
```

Step names are unique identifiers containing letters, digits, and underscores, and cannot begin with a digit. References must point to an earlier named step. `{steps.NAME.data.FIELD}` reads an exact **native response** field; dotted array indices such as `Items.0.Id` are supported. `{steps.NAME.data}` binds the whole native response, including as a JSON request body. These values are available even with `--no-cache`. `{steps.NAME.result}` binds the private snapshot ID, so later `result` steps can filter/count the same response without another API call. It requires caching on the source step. Named steps can also use `foreach`; give each expansion a unique name such as `season_{item}`.

`expect` is an array of filter expressions evaluated against `{"data": NATIVE_RESPONSE, "count": RECORD_COUNT}`. All checks must pass. `count` measures the raw array or the API envelope's records before display limits and local filtering; a non-list response counts as one. Use `count=1` before indexing search results, `data.id=173` for an identity check, or `data.status=completed` after a wait. A missing field, a failed check, or a failed wait prevents dependent work. Even with `--keep-going`, a step referring to a failed/skipped source is skipped; independent steps may continue. Expectations on a write inspect its result **after** submission and do not undo the write. Put prerequisite checks on earlier read steps.

Static syntax, names, reference ordering, guard expressions, and connections are validated before execution. Referenced values are checked against argument types again before their own step starts. References are for argument **values**; app names, actions, option names, and choice-constrained command structure must remain explicit. A referenced value cannot introduce a new CLI flag, `@file` input, or shared stdin. JSON placeholders preserve booleans, numbers, arrays, nulls, and escaping. No shell code or expressions are executed. Display `--select`, filtering, limits, and compact output do not change the native data used by references or expectations.

To include a wait before a known follow-up, place `APP wait` or an action with `--wait` in the batch. Default error handling stops later steps on timeout or failure. Where data is needed, reference the successful wait's `data.response` fields or `data.target`; no original grab is repeated. Every write must still be authorized by the original request. Use a separate model decision when release selection, subtitles, or tracker permission needs judgment; chaining steps does not grant download permission.

### Missing episodes in one task

```sh
autoarr sonarr missing 'American Horror Story'
autoarr sonarr find-missing 'American Horror Story' --vostfr
# Reuse the existing search export: no new indexer searches.
autoarr sonarr find-missing --series-id 24 --vostfr \
  --release-file .local/ahs-search/season13-releases.json
```

These commands exclude future episodes, unknown air dates, unmonitored episodes, and specials by default. Use `--season`, `--include-unmonitored`, or `--include-specials` to change the scope. `find-missing` searches once per affected season, matches explicit episode mappings, reports per-episode counts, and returns up to two candidates (`--top 0`–`10`). It does not infer complete season-pack coverage or start downloads. `--vostfr` strictly filters VOSTFR/SUBFRENCH labels; it does **not verify embedded French subtitles**. For an intentional subsequent grab, `sonarr grab --ref RESULT_ID:ROW_NUMBER --dry-run` previews the original cached native release payload. References are tied to the source app and expire with the snapshot; upstream releases can also expire.

## Complete API access and discovery

Every native REST endpoint can be called. Features are not limited to the convenience commands, and new upstream endpoints do not require a wrapper to be implemented first.

```sh
autoarr lidarr ops --search manual --details
autoarr lidarr schema get_manualimport
autoarr lidarr call get_manualimport -q folder=/media/downloads/complete/lidarr

autoarr sonarr call get_series_id -p id=173
autoarr sonarr api GET /series/173
autoarr prowlarr api GET /applications --select id,name,syncLevel --table
autoarr radarr api GET /customformat
autoarr sonarr api GET /config/naming
autoarr seerr call get_request_count
```

`ops` searches methods, paths, operation IDs, summaries, and tags. Add **`--details`** to include compact parameter types, required flags, body fields, declared required body fields/enum choices, and schema provenance with each match, avoiding a separate schema call when those details are sufficient. `schema OPERATION_ID` defaults to a compact parameter/body summary and provenance; add `--full` for resolved request/response schemas. `schema` without an operation summarizes the document; `--full` returns the full document. `call` validates required path/query parameters and request-body presence, and serializes schema-declared query arrays. Sending a call does not expand unused response schemas; explicit full schema discovery still includes them. It is not a complete JSON Schema validator; the application's own validation remains authoritative.

Bazarr, Jellyfin, and Seerr supply live descriptions. Arr production builds often do not serve their schema; the CLI downloads it **anonymously from the app's official source repository**, preferring the installed version tag. If that tag is unavailable, it explicitly records a `develop` fallback in `x-autoarr-source`. Discovery can therefore advertise features absent from an older server. Inspect provenance with `schema OPERATION_ID --select schemaSource`; native `api` access works independently of schema availability. Schemas are cached privately for 24 hours; use `--refresh-schema` to refresh them. qBittorrent and Kapowarr currently use native `api` calls and their shortcuts without OpenAPI discovery.

API paths are relative to the configured app URL, including its URL base. The app's API prefix is inserted automatically and not duplicated if supplied. For a site endpoint outside that prefix, use `api GET /PATH --site`. Requests and redirects cannot forward app credentials to another origin.

### JSON, forms, uploads, and downloads

```sh
# Exact native JSON body from a local file; - reads JSON from stdin.
autoarr radarr api PUT /qualityprofile/1 --data @profile.json --dry-run

# qBittorrent's API uses form fields.
autoarr qbittorrent api POST /torrents/setCategory \
  --form hashes=TORRENT_HASH --form category=tv-sonarr --dry-run

# Multipart subtitle upload through Bazarr.
autoarr bazarr call post_episodes_subtitles \
  -q seriesid=173 -q episodeid=EPISODE_ID -q language=fr -q forced=false -q hi=false \
  --file file=/absolute/path/subtitle.fr.srt --dry-run

# Jellyfin's image upload expects a base64 body with the image MIME type.
autoarr jellyfin api POST /Items/ITEM_ID/Images/Logo \
  --upload /absolute/path/logo.png --base64-upload --content-type image/png --dry-run

# Exact response export, including binary content, into a new private file.
autoarr jellyfin api GET /Items/ITEM_ID/Images/Logo --output /tmp/monogatari-logo.png
```

`--data` accepts a JSON literal, `@FILE`, or `-`. `--set FIELD=VALUE` on supported commands parses JSON booleans, numbers, arrays, and objects; other values remain strings. `--form` is URL encoded. `--file FIELD=FILE` switches to multipart and can be repeated or combined with `--form`. `--upload` sends one raw file, optionally base64 encoded. Response exports contain the exact unredacted response and are created with mode 0600; existing files are never overwritten. Terminal JSON and request journals redact configured credentials and recognized secret fields.

## Arr workflows

The media managers share `status`, `health`, `list`, `get`, `create`, `update`, `delete`, `queue`, `history`, `wanted`, `lookup`, `add`, `releases`, `grab`, `profiles`, `roots`, `tags`, `indexers`, `clients`, `files`, `command`, and `manual-import`. Prowlarr exposes its applicable commands; Bookshelf uses its Readarr-compatible API.

### Add content and edit configuration

```sh
autoarr radarr lookup 'tmdb:550'
autoarr radarr roots --select id,path --table
autoarr radarr profiles --select id,name --table
autoarr radarr add 'tmdb:550' --root /media/movies --profile-id 1 --dry-run

autoarr sonarr update series 173 --set monitored=true --dry-run
autoarr radarr update qualityprofile 1 --set upgradeAllowed=true --dry-run
autoarr lidarr list metadataprofile
autoarr prowlarr profiles
```

`add` requires exactly one lookup match, an existing root, and an existing quality profile. Lidarr/Bookshelf also require `--metadata-profile-id`; use `--language-profile-id` if required by that app's API. `--search` requests a missing-media search after adding; it is off by default. Sonarr additionally accepts `--series-type` and `--monitor`. `create RESOURCE --data @file` exposes advanced add options directly.

`update RESOURCE ID --set ...` first reads the existing object, merges the specified changes, and sends the result. Dotted fields and existing array indices are supported, for example `--set fields.0.value=...`; inspect the current object to identify the correct field. Changing Radarr/Sonarr media paths with `update` is refused: use `move` so the app moves its files too.

### Moves and renames

```sh
autoarr sonarr move 173 --root /media/series --dry-run
autoarr radarr move MOVIE_ID --path '/media/movies/Exact Folder Name' --dry-run
autoarr sonarr rename 173
autoarr sonarr rename 173 --apply --dry-run
```

`move` validates the destination root and checks collisions using the app's filesystem API, then submits the native update with **`moveFiles=true`**. It refuses existing destination folders, including metadata-only collisions. It never renames files over SSH. A successful request is reported as requested, not as proof that every byte of a long move finished; inspect the app's command/history state and resulting files as appropriate. `rename ID` previews native filename changes; `--apply` submits the app's rename command.

### Release replacement and import repair

```sh
autoarr radarr releases --movie-id MOVIE_ID
autoarr sonarr releases --series-id 173 --season-number 1
autoarr lidarr releases --artist-id ARTIST_ID --album-id ALBUM_ID
autoarr prowlarr releases --term 'search terms'

autoarr radarr grab --guid 'EXACT_RELEASE_GUID' --indexer-id INDEXER_ID --dry-run
autoarr radarr grab --data @selected-release.json --dry-run
autoarr sonarr files -q seriesId=173
autoarr lidarr manual-import --folder /media/downloads/complete/lidarr
autoarr lidarr manual-import --apply --data @reviewed-import-command.json --dry-run
autoarr sonarr command RefreshSeries --set seriesId=173 --dry-run
```

Release searches use the application's indexers and can be slow. Inspect its language, quality, custom-format scores, rejection reasons, and actual file media information; the CLI does not infer VO/VF/VA from a title alone. Pass additional native fields with `grab --set FIELD=VALUE` or a complete selected release body with `--data` when required by the app. Import application requires an explicit reviewed command object containing a nonempty `files` array and the app-specific file mappings/options; discovery exposes both manual-import endpoints and the generic native command API. The CLI does not auto-select matches or force rejected imports.

`command NAME --set ...` supports any native Arr command. Add `--wait --wait-timeout 300` to poll its returned command ID. Failed/aborted commands return a failed wait and a nonzero exit code. Reaching the wait timeout leaves the job running and returns its target and a read-only resume command.

`queue-remove ID` retains the download-client item and does not blocklist by default. `--remove-from-client`, `--blocklist`, and `--skip-redownload` select those behaviors explicitly. `delete movie|series|artist|author ID` defaults to `deleteFiles=false`; deleting media files requires `--delete-files`.

### Waiting and agent continuation

When a request includes an action after a download or another long job, an agent can keep the waiting tool running and continue its original instructions as soon as the tool completes. AutoARR sleeps between native API checks, without spending model turns on repeated status requests. Progress uses flushed JSON lines on **stderr** when it changes, with an unchanged-state heartbeat every 30 seconds. **Stdout** contains one final JSON result for the agent or a script to inspect. No additional downloads, automatic searches, or follow-up writes are launched by a wait.

```sh
# Submit an already selected and authorized release, then wait through import.
autoarr sonarr grab --ref RESULT_ID:ROW_NUMBER --wait --wait-timeout 3600

# Follow work that was already submitted, without starting it again.
autoarr sonarr wait --download-id DOWNLOAD_ID --wait-timeout 3600
autoarr radarr wait --download-id DOWNLOAD_ID --wait-until downloaded
autoarr qbittorrent wait --torrent-hash TORRENT_HASH --wait-timeout 3600
autoarr sonarr wait --command-id COMMAND_ID
autoarr jellyfin wait --task-id TASK_ID

# Wait as part of command submission, rename application, import, or a task.
autoarr sonarr command RefreshSeries --set seriesId=173 --wait
autoarr sonarr rename 173 --apply --wait
autoarr lidarr manual-import --apply --data @reviewed-import-command.json --wait
autoarr jellyfin task-run TASK_ID --wait

# General read-only conditions cover other application operations.
autoarr sonarr wait --path /episode/EPISODE_ID --until 'hasFile=true'
autoarr sonarr wait --path /queue --until 'totalRecords=0'
autoarr jellyfin wait --path /ScheduledTasks/TASK_ID \
  --until 'State=Idle' --fail-if 'LastExecutionResult.Status=Failed'
```

`--download-id` is the application's download ID, usually a torrent hash or Usenet job ID; it is **not** its queue record ID. Arr waits default to `--wait-until imported`, since a completed transfer may still be importing or blocked. `--wait-until downloaded` allows continuation at transfer completion. Queue disappearance alone never proves success: the watcher requires import history for that download. For Sonarr season packs with explicit grabbed episode mappings, every grabbed episode needs an import event. A blocked import, failed download, or ignored download returns failure so the agent can inspect the native reason. A qBittorrent wait finishes after transfer and any observed checking/moving state; it does not verify an Arr import or wait for seeding to finish.

`grab --wait` requires a full release with its exact `title` and `indexerId`, from `--ref` or `--data`; bare GUID/indexer flags do not provide enough information to follow a grab reliably. Before submitting, AutoARR records a history boundary and reads the indexer's name. If the API omits the download ID, it follows only a new grab with that exact title/indexer. Multiple matching download IDs return failure rather than selecting one. These read checks never grant permission to grab a release; verify its source indexer and obey the Redacted restriction before any grab.

For other pending actions, attach **`--wait --wait-path /NATIVE/STATUS --until 'FIELD=VALUE'`** to the original application command, including `api` and `call`. The initial action runs once, then the watcher uses only GET requests. Alternatively, use `APP wait --path ...` with repeated `-q KEY=VALUE` parameters. Conditions use the same filter syntax as `--filter`, and are evaluated against the raw response before output formatting. Repeated `--until` conditions must all match; any `--fail-if` match takes priority. A root array requires every element to satisfy every completion condition and must be nonempty. On a JSON object, conditions inspect that object, including pagination envelopes. Checks of paginated endpoints see the requested page; use an item-specific path or a server-wide counter when that matters. Use an execution ID or changed timestamp to distinguish a new run from an older success. The Jellyfin `task-run --wait` shortcut does this automatically by comparing execution results captured before starting.

Actions without a reliable built-in completion adapter, including media moves, require an explicit `--wait-path` and `--until` if used with `--wait`; AutoARR rejects an uncheckable wait before sending the write. A changed managed path alone is not proof that all move bytes finished. An HTTP request returning successfully also does not prove completion of an asynchronous job.

The final response includes `status`, `app`, `target`, `elapsedSeconds`, `polls`, compact `progress`, the latest native `response`, and **`resume`**, an argv array. Targets and resume arguments are preserved without truncation in normal output. A small output budget can omit the resume argv while retaining the target; use `APP wait --resume TARGET_JSON`. If the target itself exceeds the budget, the response preserves its status and private result ID: use `result --id RESULT_ID --full` to retrieve the complete target and resume argv. An oversized uncached target is rejected before the original action is submitted. The private result snapshot retains the native response as usual. A timeout result looks like:

```json
{
  "wait": true,
  "status": "timeout",
  "app": "sonarr",
  "target": {"type": "command", "id": "123"},
  "elapsedSeconds": 300,
  "polls": 60,
  "resume": ["autoarr", "--config", "/private/config.json", "sonarr", "wait", "--resume", "{\"type\":\"command\",\"id\":\"123\"}", "--wait-timeout", "300", "--poll-interval", "5"]
}
```

Execute the returned argv directly through the process/tool interface; it is **not a shell string**. To wait longer, increase its `--wait-timeout`. Resume performs read checks only and can be used after completion to inspect the same target. It preserves grab correlation boundaries and Jellyfin execution baselines. Explicit `--select` can hide these fields, so agents that need to resume should retain the default response or retrieve them from the private result snapshot.

Exit codes are **0** for completed, **3** for timeout, **2** for failed/invalid, and **130** for interrupted. Timeout and interruption stop only the watcher, leaving the native job running. A zero-second budget returns a resumable timeout without polling. The default wait budget is 300 seconds and the polling interval is 5 seconds. Each polling request is capped by the remaining budget and the configured HTTP timeout. Temporary network failures, HTTP 429, and HTTP 5xx retry read checks within that budget; writes are never retried. `--dry-run` previews the original action and skips polling; an existing-work dry run validates the target without polling. Batch jobs treat unsuccessful waits as failures and stop later writes unless `--keep-going` was explicitly chosen.

For long waits, the agent host should run the CLI as a background tool/process and resume the agent when that tool completes, retaining the original follow-up instructions. A script can also use `&&` to perform an already authorized follow-up only on exit 0. AutoARR itself does not invoke an LLM, start a daemon, create scheduled tasks, or wake an agent session that has ended. Durable wake-ups across ended sessions require the host's scheduler/event integration. This release implements the sleeping/progress path and provides resumable JSON targets for that integration.

## Bazarr

```sh
autoarr bazarr wanted --media movies --all
autoarr bazarr profiles
autoarr bazarr subtitle-search --series-id 173 --episode-id EPISODE_ID --language fr --dry-run
autoarr bazarr subtitle-search --movie-id MOVIE_ID --language fr --forced --dry-run
autoarr bazarr subtitle-sync --type episode --id EPISODE_ID \
  --path '/media/series/Show/Season 01/Episode.fr.srt' \
  --language fr --reference a:0 --dry-run
autoarr bazarr ops --search subtitles --table
```

`subtitle-search` uses Bazarr's automatic search/download endpoint. `--hi` and `--forced` select those subtitle variants. Provider search, a specific subtitle download, blacklist operations, extraction, translation, and subtitle modifications are accessible through `ops`/`schema`/`call` or `subtitles --method ... -q ...`, using the exact arguments from the running Bazarr instance. Episode IDs are Sonarr episode IDs; movie IDs are Radarr IDs. Sync paths must be paths visible to Bazarr.

## Jellyfin

```sh
autoarr jellyfin libraries --select Name,ItemId,Locations --table
autoarr jellyfin metadata ITEM_ID --select Name,OriginalTitle,OriginalLanguage,Overview
autoarr jellyfin metadata ITEM_ID --set OriginalTitle='Monogatari Series' --dry-run
autoarr jellyfin images ITEM_ID
autoarr jellyfin image-set ITEM_ID --type Logo --url https://example.org/logo.png --dry-run
autoarr jellyfin image-set ITEM_ID --type Backdrop --url https://example.org/fanart.jpg --dry-run
autoarr jellyfin refresh ITEM_ID --dry-run
autoarr jellyfin collection Monogatari --ids SHOW_ID,MOVIE_ID_1,MOVIE_ID_2 --dry-run
autoarr jellyfin playlist 'Monogatari watch order' --ids EPISODE_ID,MOVIE_ID --user-id USER_ID --dry-run
autoarr jellyfin tasks --select Name,Id,State --table
autoarr jellyfin library-options LIBRARY_ID
```

Uses Jellyfin's current `Authorization: MediaBrowser Token="…"` header, compatible with this server's disabled legacy authorization. Metadata updates preserve editable scalar fields, including `OriginalLanguage`, and omit read-only DTO properties that break roundtripping. Only explicitly changed optional collections are reassigned. Specified changed fields are read back; `SortName` is exempt because Jellyfin normalizes it. Item reads use a configured or explicit `--user-id`, otherwise an administrator returned by the API.

`image-set` uses Jellyfin's native remote-image download. For backdrops, it replaces index 0 by default: download, check the new image count, swap to the selected index, then delete only the superseded image through the image-specific endpoint. `--append` retains all existing backdrops; `--index` selects a replacement slot. Preview/readback proves request structure and image registration, not visual correctness; inspect the actual images before deciding they are appropriate. Use `api GET /Items/ID/Images/TYPE --output FILE` to retrieve them.

`refresh` preserves artwork/metadata by default; explicit `--replace-metadata` and `--replace-images` request replacement. `library-options` reads existing options and merges only requested changes, e.g. `--set ExtractChapterImagesDuringLibraryScan=false --dry-run`. `collection` and `playlist` reference existing media, with no filesystem view or hardlink creation. Other native operations—including playback state, sessions, plugin configuration, subtitle tracks, and item deletion—remain available through `call` and `api`; use them only within the requested task.

## qBittorrent, Seerr, and Kapowarr

```sh
autoarr qbittorrent torrents -q filter=downloading --select hash,name,state,progress --table
autoarr qbittorrent transfer
autoarr qbittorrent torrent TORRENT_HASH --view files
autoarr qbittorrent torrent-action stop --hashes TORRENT_HASH --dry-run
autoarr qbittorrent torrent-action category --hashes TORRENT_HASH --category tv-sonarr --dry-run
autoarr seerr search 'Monogatari'
autoarr seerr list request -q take=20
autoarr seerr ops --search discover --table
autoarr kapowarr list volumes
```

qBittorrent supports existing LAN access or session-cookie authentication with privately configured `username` and `password_env`. Torrent actions also include start, recheck, reannounce, location, and delete. `delete` retains data unless `--delete-files` is given. Kapowarr's existing API key is supplied as required by its query authentication.

## Setup and migration

Private connections live in `.local/autoarr/config.json` in this checkout. Configuration, schema caches, request journals, and verification artifacts are git-ignored. The config file must be mode 0600. JSON writes are atomic. Normal commands use direct HTTP and never read Docker or start an SSH process.

For an existing TrueNAS deployment that matches the container names and ports in `rust/bootstrap_remote.py`, an optional read-only SSH bootstrap is available:

```sh
./bin/autoarr --config .local/autoarr/config.json init \
  --ssh user@server.example.invalid --host server.example.invalid
```

`init` is the only SSH operation. It reads existing app credentials and URL bases through already-authorized passwordless Docker access and saves them privately without printing them. It does not create API keys or change authentication settings. It reads only the containers named in the bootstrap script, not additional instances. It refuses to overwrite existing configuration unless `--overwrite` is supplied. For other deployments, configure connections explicitly.

```sh
autoarr config add sonarr --kind sonarr --url https://sonarr.example.invalid --api-key-env SONARR_API_KEY
autoarr config add music --kind navidrome --url https://navidrome.example.invalid \
  --username your-user --password-env NAVIDROME_PASSWORD
autoarr music search 'artist or album'
```

Use a private environment or `config add ... --api-key-stdin` to supply credentials, never a password/API key as a command-line argument or chat message. `config.example.json` contains environment references only. Custom instance names are supported. For other APIs use `--kind generic --api-prefix /api --auth header|bearer|query|none`, with optional `--api-key-header`, `--schema-path`, and `--status-path`.

Configuration resolution: `--config`, then `AUTOARR_CONFIG`, then this working directory's `.local/autoarr/config.json`, then the checkout's private config, then `$XDG_CONFIG_HOME/autoarr/config.json` (default `~/.config/autoarr/config.json`). Existing apps can be redirected without editing files via `AUTOARR_SONARR_URL` / `AUTOARR_SONARR_API_KEY`, using the configured instance name in uppercase. Per-app `api_key_env` and `password_env` references are also supported. Move the private config and update URLs when changing servers; the CLI creates no additional media directories and requires no daemon on the server.

Build or install the native CLI:

```sh
cargo build --release --locked
./bin/autoarr --version
cargo install --path . --locked
```

`bin/autoarr` is a relative symlink to `target/release/autoarr` and executes Rust directly after the build. `Cargo.lock` pins dependencies. Build products are git-ignored. A compiled binary can be installed independently, with `--config` or the usual configuration resolution. Python is needed only for the reference tests and benchmarks, and on the remote server when explicitly running `init`. The Rust executable does not start a local Python process. The optional `pyproject.toml` package installs only the `autoarr-python` reference command. Request journals record redacted write intentions and completion, failure, or uncertain outcomes. They are an audit trail, not an automatic rollback system. Native API writes are real changes unless `--dry-run` is present. The CLI does not ask for extra confirmation.

## Verification

```sh
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
cargo build --release --locked
python3 -m unittest discover -s tests -v
python3 scripts/benchmark_rust.py --samples 11 --output docs/benchmarks/rust-vs-python.json
```

The local HTTP integration tests cover request routing, credential redaction, redirect protection, dry-run behavior, native move semantics and collisions, metadata preservation, multipart/form requests, downloads, pagination, operation discovery, compact output bounds, advanced filters, cached replay and release references, batch preflight/error handling, JSON substitution, and missing-episode matching. They use local fixtures rather than configured applications or indexers. Successful discovery does not mean every advertised write operation has been exercised.

`tests/test_rust_cli.py` launches the actual release executable against the same local HTTP fixtures as the Python reference, including real native Ctrl-C/timeout handling and differential output checks. It requires a release build first. `scripts/benchmark_output.py` and `scripts/benchmark_latency.py` are historical Python-reference measurements. `scripts/benchmark_rust.py` compares both complete CLI processes with alternating order, warm-ups, request-count assertions, and equivalent cache state.

Version 0.5 passed **15 Rust unit tests** and **219 reference/integration tests**, including **90 tests of the actual native executable**. Strict Clippy and formatting checks passed. Read-only live checks verified a parallel Sonarr/qBittorrent batch with cached-result replay, a Sonarr condition wait, and Jellyfin task listing. No live media writes, grabs, scans, imports, or SSH operations were used. The [Rust benchmark report](benchmarks/rust-vs-python.md) includes all six process-level scenarios, methodology, and raw measurements: startup improved about **97×**, local 20-read batches about **35×**, and a three-step dependent workflow about **26×**; the batch with an added 8 ms API delay improved **4.75×**. These compare the final Rust CLI against Python 0.4; the older version-to-version figures below remain historical records.

Version 0.3 passed **105 local tests**, including 42 wait tests using a local HTTP server and virtual clock. Wait coverage includes download/import evidence, season packs, exact grab correlation, resumed execution baselines, timeout/interruption handling, API failures, progress redaction, output budgets, dry runs, and stopping dependent batch writes. Two read-only live checks verified condition waits against Sonarr's status endpoint and qBittorrent's transfer endpoint; no live downloads, scans, imports, or media writes were triggered for this feature verification.

Version 0.4 passed **129 local tests**, including dependent-step guards, missing/failed sources, typed JSON substitution, option/file-input protection, concurrent reads before writes, and operation-detail discovery. A live read-only three-step batch verified parallel Sonarr/qBittorrent reads followed by a result-ID reference into offline replay; compact operation details were also checked against Sonarr's cached native schema. No live downloads, scans, or media writes were triggered.

Version 0.4's local latency benchmark reduced median argument parsing from **49.03 ms to 4.65 ms**, process startup (`--version`) from **91.61 ms to 48.00 ms**, and a 20-read batch from **2,082.09 ms to 134.19 ms** (about **15.5× faster**). The batch still sent exactly 20 native requests; this measures a loopback HTTP fixture with result caching disabled, not application processing, internet latency, download speed, or total LLM task time. The changes build only the selected app's parsers and reuse preflight arguments/configuration. Named dependent steps and `ops --details` separately reduce how many agent tool round trips are needed. `scripts/benchmark_latency.py` uses only a temporary local HTTP server and supports `--baseline FILE --output FILE` for private comparison reports.

Protocol references: [Radarr API](https://radarr.video/docs/api/), [Sonarr API](https://sonarr.tv/docs/api/), [Prowlarr API](https://prowlarr.com/docs/api/), [Jellyfin SDK](https://typescript-sdk.jellyfin.org/), [qBittorrent Web API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29), and [OpenSubsonic](https://opensubsonic.netlify.app/docs/).
