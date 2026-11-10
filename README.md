# plugify-builds

Assembles ready-to-use Plugify builds from released components (host archives and conda packages) and publishes them as GitHub releases plus a conda channel on GitHub Pages. No component sources live here.

Current build: **Plugify Source 2** (`plugify-source2`, Counter-Strike 2).

## Layout

```
builds/source2/build.yaml        build definition: hosts, plugins, language to module map, extra files, channel, pins
builds/source2/build.lock.json   exact versions, asset URLs and sha256 (written by the bot)
builds/source2/data/             optional build translations (<folder>/<prefix>.translations.<ext>), copied to addons/plugify/data/
tools/pfb.py                     pipeline
.github/workflows/               build.yml, release.yml, update-lock.yml
```

Modules are selected automatically from the `language` field of the standard plugins (`modules: auto`).

`files` in `build.yaml` adds plugin configs and translations to `configs/` and `data/` of the full archive: from the component repository at the locked tag (`source: repo`, sha256 in the lock) or from the conda package (`source: package`). host-only archives do not contain them and `mamba update` does not touch them. Gamedata and settings are rejected.

## Plugin files

A plugin keeps `<prefix>.<kind>.<ext>`, where `<kind>` is `settings`, `gamedata` or `translations` and the format (`jsonc`, `yaml`) is up to the plugin author. s2sdk uses its manifest name as prefix and folder (`s2sdk/s2sdk.translations.jsonc`); the permissions plugins share the `permissions/` folder (`permissions/permissions_connector_s2.translations.yaml`). Paths are relative to `addons/plugify/`:

| Path | Holds | Written by |
|---|---|---|
| `envs/<env>/<prefix>.<kind>.<ext>` | defaults, overwritten on update | package |
| `configs/<folder>/<prefix>.<kind>.<ext>` | admin edits: settings, gamedata fixes | admin |
| `data/<folder>/<prefix>.<kind>.<ext>` | translations and persistent plugin state | plugin, build (translations only) |
| `cache/<folder>/` | anything the plugin can recreate | plugin |

A plugin reads each kind from the env, then `configs`, then `data`; later files override earlier ones (s2sdk replaces top-level keys as a whole, so a gamedata fix in `configs` carries the whole `"csgo"` section). The build adds only translations, into `data/<folder>/`, and rejects settings and gamedata of any format. s2sdk no longer reads its old root `configs/settings.jsonc`, `configs/gamedata.jsonc` and `data/translations.jsonc`; the build rejects them too. `verify.plugin_files` in `build.yaml` lists files a plugin package must ship next to its manifest.

## Outputs

Per release `plugify-source2-vX.Y.Z`:

- `plugify-source2-<launcher|runtime|metamod>-<linux|windows>-<version>.<tar.gz|zip>`: full pack, extracted into `game/`;
- `...-<version>-host-only.<tar.gz|zip>`: host only, without `envs/`;
- `SHA256SUMS`, `builds.json`, `build.lock.json`.

conda channel: `https://untrustedmodders.github.io/plugify-builds/source2/` (stable) and `.../source2-dev/` (latest dev build). It contains the component packages and the meta-packages `plugify-source2-modules` and `plugify-source2-plugins`, which pin the build composition. Installed packs update with `mamba update -n modules --all` and `mamba update -n plugins --all`.

## Local build

```bash
pip install pyyaml
python3 tools/pfb.py lock                  # refresh the lock from the latest releases
python3 tools/pfb.py fetch
python3 tools/pfb.py channel --version 1.0.0
python3 tools/pfb.py envs --version 1.0.0
python3 tools/pfb.py verify --version 1.0.0
python3 tools/pfb.py smoke --version 1.0.0 --plug <path to plug>
python3 tools/pfb.py assemble --version 1.0.0
```

Upgrade check against a previous build: put its `build.lock.json` into `work/history/<version>.json`, build the channel with `--history work/history`, then run `upgrade-test --history work/history --prev <version>`.

## CI

| Workflow | Trigger | Does |
|---|---|---|
| `update-lock.yml` | every 6 hours, `repository_dispatch: component-released`, manual | opens a PR with an updated lock |
| `build.yml` | PR, push to `main`, call from `release.yml` | builds and checks packs; `main` publishes the `plugify-source2-dev` prerelease; releases get assets, Pages and `index.json` |
| `release.yml` | push to `main` | release-please (`plugify-source2-vX.Y.Z`) |

Secrets: `BOT_TOKEN` (PAT for bot PRs so that CI runs on them), `SITE_DISPATCH_TOKEN` (`repository_dispatch` `builds-updated` to `untrustedmodders/untrustedmodders.github.io`), `DISCORD_WEBHOOK` (optional).

## index.json

The website reads `https://untrustedmodders.github.io/plugify-builds/index.json`:

```jsonc
{
  "schema": 1,
  "updated": "2026-11-10T12:00:00Z",
  "builds": {
    "plugify-source2": {
      "title": "Plugify Source 2",
      "games": ["cs2"],
      "repo": "untrustedmodders/plugify-builds",
      "releases": [                       // newest first; all stable releases and the latest dev one
        {
          "version": "1.0.0",             // dev: "1.0.1-dev.12+abc1234"
          "channel": "stable",            // stable | dev
          "tag": "plugify-source2-v1.0.0",
          "date": "2026-11-10",
          "url": "https://github.com/untrustedmodders/plugify-builds/releases/tag/plugify-source2-v1.0.0",
          "changelog": "markdown",
          "core": "3.1.0",
          "channel_url": "https://untrustedmodders.github.io/plugify-builds/source2/",
          "packages": { "modules": ["plugify-module-cpp", "..."], "plugins": ["plugify-plugin-s2sdk", "..."] },
          "components": [
            { "name": "plugify-source2-launcher", "kind": "host", "version": "2.5.4",
              "repo": "untrustedmodders/plugify-source2-launcher", "tag": "v2.5.4", "release_url": "..." }
          ],
          "files": [
            { "name": "plugify-source2-launcher-linux-1.0.0.tar.gz", "variant": "full", "host": "launcher", "os": "linux",
              "size": 39424000, "sha256": "...", "url": "..." }
          ]
        }
      ]
    }
  }
}
```

`kind`: `host`, `module` or `plugin`. `variant`: `full` or `host-only`. Each release also carries the same release object as `builds.json`.
