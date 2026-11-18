# Lastbook first-run setup: the shared contract

One download per computer: the Mac app (dmg) or the Windows installer (Setup.exe). On first open, Lastbook finds no library and
opens **setup** in its own window. Setup shows what this computer has, recommends a library (Small, Medium or All) and whether
the optional local AI fits, then downloads, checks, unpacks and switches to it. No separate library or model download, no
manual unzip. Setup also opens later from inside the app (Library → *Library and local AI*) to upgrade, add the AI or repair.

Several sessions build this at once. This file is the interface between them. Change it only together, and say so.

## Owner decisions this follows

- **The one way out.** Setup's downloader is the only code in Lastbook that may reach the internet, and only when the person
  starts or resumes a download. It fetches over HTTPS only from hosts in `ALLOWED_HOSTS` (below), with no proxy taken from
  the system, and follows a redirect only to another allowed host. Everything else stays as it is: the windows block all web
  loads, the assistant talks to loopback or pipes only, and nothing is sent anywhere (no telemetry, no update check at
  startup). The owner authorized this route on 2026-11-12 for the first-run install. It replaces "downloads happen outside
  the app" for library packs and the model, and nothing more.
- **No blanket memory floor.** The local AI is offered when the measured fit says it fits (`llm_local.judge`, about 18 GB
  of memory for model and context, plus a 2.5 GiB reserve). It is not offered below that, and the screen says why in plain words.
- **A working library is never broken.** New data is downloaded and unpacked beside the old one, checked, then switched
  in one rename. A failure at any step leaves the old library as it was.

## Who owns what

| Part | Owner (session) | Files |
|---|---|---|
| Downloader, checks, unpacking, switching, hardware, manifest, `/api/setup/*` | **setup-backend** | `engine/setup/` (new package), `packaging/release_manifest.py`, setup mode in `app/main.py` (and the smallest shell change it needs) |
| Setup screens | **setup-ui** | `web/setup.html`, `web/setup.js`, `web/setup.css`; one link in `web/app.js` (Library page) |
| Packs: build, zip, upload, manifest entries | **publish-packs** | `derived/packs/`, `derived/release/`; writes `derived/release/manifest-v1.json` entries for each verified pack |
| Domain for downloads | **download-domain** | DNS, R2 custom domain, URLs in `website/` |
| Windows runner, Windows package and installer, Windows tests | **windows-ai** | `packaging/llm/*windows*`, `packaging/build_app_windows.sh`, `packaging/lastbook_setup.py`, `packaging/scan_app_package.py` |
| Local AI engine, assistant, Mac package, Mac tests, final report, website wording at release | **main session** | `llm_local.py`, `assistant.py`, `server.py`, `web/chat.js`, the rest of `web/app.js`, Mac packaging |

Re-read a file just before editing it. Commit nothing and switch no branch: the owner reviews the working tree.

## The manifest (`manifest-v1.json`)

The app carries a copy made at build time (`packaging/release_manifest.py` → bundled `setup/manifest-v1.json`). Setup may
also fetch `https://downloads.lastbook.org/manifest-v1.json` (one GET, only when the person opens setup online) to learn of
packs published after the app was built. A fetched manifest is used only if it parses, its `version` is 1, and every URL is
on an allowed host.

```json
{
  "version": 1,
  "published": "2026-11-12",
  "packs": [
    {"id": "small", "title": "Small", "summary": "…one plain sentence…", "records": 315,
     "installed_bytes": 8236931672, "available": true,
     "files": [{"name": "Lastbook-Small.zip", "url": "https://downloads.lastbook.org/1.0.0/Lastbook-Small.zip",
                "size": 8236931672, "sha256": "…", "kind": "zip"}]}
  ],
  "models": [
    {"id": "gemma-4-26b-a4b", "title": "Gemma 4 26B A4B", "file": "gemma-4-26B-A4B-it-UD-Q4_K_M.gguf",
     "url": "https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF/resolve/c099eb48e663fd284577b04978a94ffccb261841/gemma-4-26B-A4B-it-UD-Q4_K_M.gguf?download=true",
     "size": 16947541728, "sha256": "f2c28b3dc4776931ac6f879e11f203dec637ea0f14267a86ec8f6165f63f293f",
     "memory_bytes": 19027384256, "available": true}
  ]
}
```

`installed_bytes` is the unpacked size, measured. A pack with `"available": false` shows as *coming later* and can't be chosen.
Packs may be split into several files later; setup downloads all `files` of a pack, in order.

`ALLOWED_HOSTS`: `downloads.lastbook.org`, the bucket's current `*.r2.dev` host (until the domain is live), `huggingface.co`,
and the hosts Hugging Face redirects model downloads to (`cas-bridge.xethub.hf.co`, `*.hf.co`; on 2026-11-12 the model resolved to `us.aws.cdn.hf.co`). R2 ignores If-Range, so the downloader compares the ETag itself. The exact list is a constant in
`engine/setup/download.py`, and the package scan checks that it is the only module that opens connections.

## Where things go

- The library folder stays **LastbookData** (layout unchanged: `library-data/`, `derived/`). Default place: the person's
  Documents folder on Windows, and `~/Library/Application Support/Lastbook/LastbookData` on a Mac, or a folder they choose,
  on a disk with room. `settings.json` remembers it as before (`app/main.py`).
- Work in progress: `<parent>/LastbookData.setup/`, holding `downloads/` (partial files with a `.part` suffix and a
  `.json` beside each: url, size, sha256, bytes done, ETag) and `unpack/` (the new LastbookData being built).
- The model goes to `LastbookData/models/<file>`. After its SHA-256 matches, setup calls `llm_local.mark_checked(path)`, so
  the AI does not read 17 GB a second time.
- Space needed = download size + unpacked size + 2 GB, on the disk that holds the target. A zip is deleted once it is
  unpacked and checked. Setup refuses to start, with the numbers, when there is not enough room.
- Manual downloads are recognized: a `Lastbook-*.zip` or the model file in Downloads, Documents or Desktop, or an existing
  LastbookData anywhere `app/main.py` already looks, offered as *Use the file you already have*. A file is used only after its
  size and SHA-256 match the manifest.

## Engine interface: `/api/setup/*`

JSON over the app's bridge, so no port. POSTs carry the launch key like every other POST. In setup mode (no library yet) these
are served by `engine/setup/api.py` alone. In normal mode `server.py` hands `/api/setup/` to the same module.

| Request | Answer |
|---|---|
| `GET /api/setup/state` | `{mode: "first-run" or "manage", hardware, manifest: {source: "bundled" or "fetched", packs, models}, current: {folder, pack, model} or null, found: [...], recommend: {pack, ai, why}, job}` |
| `POST /api/setup/start` `{pack, ai: true or false, folder?}` | the new `job`, or `{error, need_bytes, free_bytes}` |
| `GET /api/setup/job` | `job` |
| `POST /api/setup/pause`, `/resume`, `/cancel`, `/retry` | `job` |
| `POST /api/setup/use` `{path}` | uses a library or file already on this computer: `job` (verify, then unpack or activate) |
| `POST /api/setup/finish` | `{ok: true}`; the app then opens the library (see below) |
| `POST /api/setup/pick` | `{path}` from the system's folder picker, or `{path: null}`: a place to install (`start {folder}`) or something to `use` (a LastbookData, a folder holding one, or a folder holding a pack zip) |
| `POST /api/setup/delete-old` `{path}` | deletes a `LastbookData.old-*` setup moved aside itself (listed in `current.old`): `{ok, deleting}` |
| `POST /api/setup/remove-ai` | manage mode: deletes `LastbookData/models/<file>` (the library stays): `{ok, freed_bytes}` |
| `POST /api/setup/start` `{repair: true, ai}` | manage mode: re-checks every file against the library's `pack-manifest.tsv` and the model's SHA-256 (items `verifying`); fetches again only what is wrong (the pack: downloaded and switched to like an upgrade; the model: replaced in place). All well: `done`, nothing downloaded |

Added by setup-backend on 2026-11-12: `pick` (answers `{path, free_bytes, disk}` or `{path: null, cancelled: true}`), `delete-old`, `remove-ai` and repair above; `hardware.disks[].name` (Macintosh HD, "Data (D:)"); `current.old` (old libraries kept after an upgrade);
`recommend.folder` (where it would go) and `recommend.need_bytes` (`{pack id: bytes}` per pack); each `found` entry is
`{kind: "library" or "pack" or "model", path, name, id, size, size_ok}`. An error answer is `{error, code}` (with HTTP 400,
409 for `busy`/`not_done`, 507 for `no_space`). In normal mode the setup page is `/ui/setup.html`, which the bridge now allows as
the window's top page; in setup mode it is also served at `/`. An upgrade or a new library is switched to when the app restarts
after `finish` (the job ends `done` with the switch staged), so nothing is renamed while it is open.

`hardware` = `{os: "macos" or "windows", arch: "arm64" or "x64", ram_bytes, unified_memory: true or false, gpus: [{name,
vram_bytes}], disks: [{path, free_bytes, total_bytes}], ai: {fits, needs_bytes, why}}`. On a Mac `ai.fits` comes from
`llm_local.judge` of a real `lastbook-llm --measure` once the model file is present. Before that, it is an estimate from
`memory_bytes` and RAM, marked `"estimate": true`. On Windows, RAM plus dedicated VRAM, from DXGI.

`job` = `{id, state, items: [{id, kind: "pack" or "model", title, state, bytes_done, bytes_total, rate_bps, eta_s,
error}], error, started, folder}`. Each item also has `source`: `"found"` when its file is already on this computer
(it is checked, not downloaded), else `"download"` (it changes to `"download"` when a found file is rejected). After a restart, an
item that stopped mid-run has `paused_from` (`downloading`, `verifying`, `unpacking` or `activating`), dropped on resume, and its
`bytes_done`/`bytes_total` describe that step: unpacked bytes of the installed size for `unpacking`, the whole file for a found item. States in order: `waiting`, `downloading`, `paused`, `verifying`, `unpacking`,
`activating`, `done`, or `failed` (with `error`: a plain sentence and a `code`: `no_space`, `network`, `checksum`, `unpack`,
`activate`, `cancelled`, and `found_mismatch`: a file already on this computer (`use`) did not match its published SHA-256; the error carries `file`, the file is left untouched and no longer offered, and `retry` downloads it instead). The job lives in the app's own settings folder (`<settings>/setup/job.json`, with a staged switch in `<settings>/setup/pending.json`; never in `LastbookData.setup`, whose contents are not obeyed: audit E3), so a restart picks it up as `paused` and resumes
with HTTP Range from the bytes already on disk. A corrupt download (wrong SHA-256) is deleted and downloaded once more
automatically, then fails with `checksum`.

## Switching to the library

`finish` writes `settings.json` and tells the shell to load `lastbook://app/` (Mac) or `https://lastbook.invalid/` (Windows)
with the engine now started on the new folder. If an in-process switch proves fragile, the app may relaunch itself instead,
with the same result for the person. Either way, an upgrade (for example Small to Medium) renames the old LastbookData to
`LastbookData.old-<date>` only after the new one is complete and opens, and offers to delete the old one afterwards.

## State maps (added 2026-11-12)

The owner wants setup to offer one US state's detailed map with any library: local streets, buildings, terrain,
crops and the resource layers (water points, help points, atlas), built by `packaging/build_packs.py map <ST>`
(rules in `derived/packs/pack-spec.json` → `map_sets.state`). A state map set is self-contained: it is the base map
plus every row inside the state's box, so it **replaces** the library's map set and never merges with it.
One state per library for now; choosing another state replaces the first.

Who owns what: **map-packages** builds, zips, uploads and lists the state maps; **setup-backend** downloads and
installs them; **setup-ui** shows the choice. The app's Map, Ask and Videos screens are unchanged, and read the new
map set the same way as the base one.

Manifest: a new top-level list, alongside `packs` and `models`. A fetched manifest merges by `id`, as packs do.

```json
"maps": [
  {"id": "CA", "title": "California", "kind": "state", "installed_bytes": 5000000000, "available": true,
   "includes": "Local streets, buildings, terrain and crops, plus water, help and resource points",
   "files": [{"name": "Lastbook-Map-CA.zip", "url": "https://downloads.lastbook.org/maps/Lastbook-Map-CA.zip",
              "size": 0, "sha256": "…", "kind": "zip"}]}
]
```

The zip unpacks to the same layout as a pack, holding only the map set's files: `derived/geo/*.sqlite`,
`derived/geo/national-status.json`, `derived/geo/socal-cells.json` and `library-data/geography/resources/atlas.sqlite`,
plus `map-set.json` naming the state. Nothing else is allowed in it. Unpacking guards are the same as for packs.

Setup: `POST /api/setup/start {pack, ai, map: "CA" | null}`. A map is a third job item, `kind: "map"`. The space
needed counts the map's download plus installed bytes. Installing it builds the new map set in
`LastbookData.setup/unpack/` and switches the listed files in one step, keeping the old map set until the library
opens with the new one. `state.current.map` reports the installed set (`base`, `us` or a state code, from
`map-set.json`). The manage screen lets a person add or change the state later. A pack that already contains a
detailed map (Medium has California; All has the whole US) shows that, and choosing a state on Medium replaces
its California map. All needs no state map. The state is chosen by the person; Lastbook does not detect location.

Built by setup-backend (2026-11-12):
- Job item `kind: "map"` comes after the pack and before the model: `waiting`, `downloading`, `unpacking` (bytes of
  `installed_bytes`), `done`. With a pack, the state's files replace the pack's own map set inside the staged library.
  On its own (manage mode, `start {map: "CA"}` with no `pack`), the job ends `done` with the swap staged. The swap happens
  when the app restarts after `finish`: the old map set's files, with their SQLite `-wal`/`-shm` files, move to
  `LastbookData.setup/map-old/`, the new ones move in, and if the result does not check out (every file `map-set.json`
  lists, at its size) the old set is put back. `map-old/` is deleted once the library has opened.
- A map zip may hold only the map-set files listed above (it fails with `unpack` otherwise), and `map-set.json`'s `name`
  must equal the entry's `id`. Its files may sit under a top `LastbookData/` folder or at the zip's top (as the published
  state zips do); a pack zip still needs the folder.
- `state.current.map` is `map-set.json`'s `name`, else `pack.json`'s `map`, else `"base"`. Asking for the state already
  there answers `{code: "nothing"}`. In first-run setup, a map needs a pack in the same `start`.
- `state.recommend.map_need_bytes` = `{map id: download + installed bytes}`; `start`'s space check adds the map.
  `state.manifest.maps` lists the entries (without URLs).
- `found[]` also lists a `Lastbook-Map-*.zip` already on this computer (`kind: "map"`); `use` on it starts the map job
  in manage mode, or is used by the next `start` in first-run setup.
- Repair skips the map files when a `map-set.json` is present; those are checked against it instead.

### Regions outside the US (added 2026-11-16)

A detailed map can also cover a place that is not a US state: a region, defined by one file in `packaging/regions/`
(`CA-ON.json` Ontario, `CA-BC.json` British Columbia). Its code is ISO 3166-2 style, so it never collides with a state
code (`CA` stays California). Everything above applies to it unchanged, with these differences:

- **Box.** From the region file (an official boundary, plus the same 0.15° margin), not from ZIP centres:
  `build_packs.state_box` returns `regions.box(code)` for a region.
- **Source rows.** A region's rows are not in the US source files. Its builder writes them to
  `derived/geo/regions/<code>/` (`national`, `water`, `terrain`, `places`, `buildings`, `crops`, each `.sqlite`, with the
  same tables as the US files), and `build_packs.py map <code>` (`region_set`) copies the rows inside its box onto the
  base map, with new ids above the base map's. For Canada: `tools/build_region_canada.py <code> all`.
  If the Statistics Canada building download is unavailable, run `python tools/build_region_canada.py CA-BC all --ms-only`
  to use Microsoft Canadian Building Footprints. Use `CA-BC buildings --ms-only` to rebuild just that layer.
- **Manifest.** The entry's `id` is the region code, `kind` is the region's kind (`province`), and `includes` comes from
  its file. The zip is `Lastbook-Map-<code>.zip`; `map-set.json`'s `name` and `state.current.map` are the code.
- **Setup.** When a listed map is not a state, the picker's words say "state or region" instead of "state", and All
  (`map: "us"`) is not counted as including it.
- **Address search.** The index is `derived/geo/addresses/<code>.sqlite` (an `address_indexes` id may be a region code).
  `tools/build_addresses.py --country CA` keeps whole postal codes and writes the region code in `state`;
  `packaging/address_indexes.py build --source derived/geo/addresses-CA.sqlite --codes CA-ON CA-BC` splits it.
  `engine/address_search.py` reads province names, their abbreviations after a comma or before a postal code, and
  postal codes written `A1A 1A1`.
- **Credits.** Each region file lists every source with its licence and attribution; the Sources and permissions page
  (`build_packs.sources_page`) prints them.

To rebuild just the crop layer offline, extract the crop classification GeoTIFF from the
[Annual Crop Inventory download](https://open.canada.ca/data/en/dataset/89073037-d2fc-4a6d-9ce9-f0b1bcb48745).
For BC in 2025, use `aci_2025_bc_v1.tif` from the BC archive. Set `--year` to match the input.
This example builds a southern Okanagan sample in a separate output folder:

```bash
python tools/build_region_canada.py CA-BC crops --year 2025 --crop-geotiff /path/to/aci_2025_bc_v1.tif \
  --bbox=-119.65,49.00,-119.51,49.19 --out /path/to/okanagan-sample
```

Build and publish a region (on the build machine):

```bash
derived/buildenv/bin/python tools/build_region_canada.py CA-ON all          # derived/geo/regions/CA-ON/*.sqlite
derived/app-build/mac-venv/bin/python3 -I -B packaging/build_packs.py map CA-ON
derived/app-build/mac-venv/bin/python3 -I -B packaging/state_maps.py CA-ON  # scan, zip, upload, list
derived/buildenv/bin/python tools/build_addresses.py --country CA --bbox=-139.22,41.52,-74.17,60.16
python3 packaging/address_indexes.py all --source derived/geo/addresses-CA.sqlite --codes CA-ON CA-BC
```
