#!/usr/bin/env python3
"""plugify-builds: assemble ready-to-use Plugify builds from component releases.

Commands (in pipeline order):
  lock          update build.lock.json from the latest component releases
  fetch         download host archives and conda packages from the lock, verify sha256
  channel       build conda channel: component packages + meta-packages (current version and past ones from --history)
  envs          create envs/modules and envs/plugins from the channel through the meta-packages
  verify        static checks of the environments (composition, manifests, dependencies, file overlaps)
  smoke         load the environments with the core test app plug (linux only)
  upgrade-test  previous build environments + `update --all` from the channel must equal a fresh install
  assemble      build pack archives (full and host-only for every host and OS)
  meta          SHA256SUMS and builds.json for a release
  history       download build.lock.json of all stable releases
  index         index.json with all releases (read by the website)

Requires Python >= 3.12 and PyYAML. GitHub API access: GITHUB_TOKEN/GH_TOKEN or `gh auth token`.
"""
from __future__ import annotations

import argparse
import bz2
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_KINDS = ("modules", "plugins")
VARIANTS = ("full", "host-only")  # full pack, and an archive with the host only (no envs/)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def die(msg):
    log(f"error: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------- config

class Build:
    def __init__(self, name: str):
        self.dir = ROOT / "builds" / name
        self.cfg = yaml.safe_load((self.dir / "build.yaml").read_text())
        self.lock_path = self.dir / "build.lock.json"
        self.id = self.cfg["id"]
        self.platforms = self.cfg["platforms"]
        self.hosts = self.cfg["hosts"]
        # a package in plugins/modules is "name" (repository {org}/name) or "owner/repo" (name = repo)
        self.repos = {}
        for key in ("plugins", "modules"):
            if isinstance(self.cfg.get(key), list):
                self.cfg[key] = [self._pkg(e) for e in self.cfg[key]]

    def _pkg(self, entry: str) -> str:
        if "/" in entry:
            self.repos[entry.split("/")[1]] = entry
            return entry.split("/")[1]
        return entry

    @property
    def lock(self) -> dict:
        if not self.lock_path.exists():
            die(f"{self.lock_path} not found, run `pfb.py lock` first")
        return json.loads(self.lock_path.read_text())

    def host_platforms(self, host: str) -> list[str]:
        return self.hosts[host].get("platforms", list(self.platforms))

    def repo_of(self, pkg: str) -> str:
        return self.repos.get(pkg, f"{self.cfg['org']}/{pkg}")

    def component_channel(self, pkg: str) -> str:
        org, name = self.repo_of(pkg).split("/")
        return f"https://{org}.github.io/{name}/"

    def channel_name(self, channel: str) -> str:
        """Build channel name: source2 (stable) or source2-dev."""
        name = self.cfg["channel"]["name"]
        return name if channel == "stable" else f"{name}-dev"

    def channel_url(self, channel: str) -> str:
        return f"{self.cfg['channel']['base'].rstrip('/')}/{self.channel_name(channel)}"

    def matrix(self):
        """(variant, host, os) for every archive."""
        for variant in VARIANTS:
            for host in self.hosts:
                for os_ in self.host_platforms(host):
                    yield variant, host, os_

    def file_name(self, variant, host, os_, version) -> str:
        suffix = "" if variant == "full" else f"-{variant}"
        return f"{self.id}-{host}-{os_}-{version}{suffix}.{self.platforms[os_]['archive']}"

    def default_version(self) -> str:
        """Current build version from the release-please manifest."""
        m = json.loads((ROOT / ".release-please-manifest.json").read_text())
        return m[f"builds/{self.dir.name}"]


# ---------------------------------------------------------------- versions

VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_ver(s: str):
    m = VER_RE.match(s.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def ver_tuple(s: str):
    nums = [int(x) for x in re.findall(r"\d+", s)[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def conda_version(v: str) -> str:
    """Build version to meta-package version: 1.4.0 stays 1.4.0, 1.5.0-dev.12+abc1234 becomes 1.5.0.dev12."""
    m = re.match(r"^(\d+\.\d+\.\d+)(?:-dev\.(\d+))?(?:\+.*)?$", v)
    if not m:
        die(f"build version {v!r} is not X.Y.Z or X.Y.Z-dev.N+sha")
    return m.group(1) + (f".dev{m.group(2)}" if m.group(2) else "")


def satisfies(version: str, spec: str | None) -> bool:
    """Check a version against a constraint like ">=2.5.7", ">=3.3,<4" or "2.1.2"."""
    if not spec:
        return True
    v = ver_tuple(version)
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        m = re.match(r"^(>=|<=|==|!=|>|<|=|\^|~)?\s*(.+)$", part)
        op, ref = m.group(1) or "==", ver_tuple(m.group(2))
        ok = {
            ">=": v >= ref, "<=": v <= ref, ">": v > ref, "<": v < ref,
            "==": v == ref, "=": v == ref, "!=": v != ref,
            "^": v >= ref and v[0] == ref[0],
            "~": v >= ref and v[:2] == ref[:2],
        }[op]
        if not ok:
            return False
    return True


# ---------------------------------------------------------------- GitHub

_token = None


def token():
    global _token
    if _token is None:
        _token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not _token and shutil.which("gh"):
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
            _token = r.stdout.strip() if r.returncode == 0 else ""
    return _token


def http(url: str, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "plugify-builds"})
    if token() and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {token()}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
    return urllib.request.urlopen(req, timeout=120)


def api(path: str):
    with http(f"https://api.github.com/{path}") as r:
        return json.load(r)


_releases: dict[str, list] = {}


def releases(repo: str) -> list[dict]:
    """Published (not draft or prerelease) releases with a semver tag, newest first."""
    if repo not in _releases:
        out = []
        for r in api(f"repos/{repo}/releases?per_page=50"):
            if r["draft"] or r["prerelease"] or not parse_ver(r["tag_name"]):
                continue
            out.append(r)
        out.sort(key=lambda r: parse_ver(r["tag_name"]), reverse=True)
        _releases[repo] = out
    return _releases[repo]


def download(url: str, dest: Path, sha256: str | None = None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (not sha256 or file_sha256(dest) == sha256):
        return dest
    part = dest.with_name(dest.name + ".part")
    log(f"  download {url}")
    with http(url, accept="application/octet-stream") as r, open(part, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if sha256 and (got := file_sha256(part)) != sha256:
        part.unlink()
        die(f"sha256 mismatch: {url}\n  expected {sha256}\n  got      {got}")
    part.replace(dest)
    return dest


def file_sha256(p: Path) -> str:
    return file_hash(p, "sha256")


def file_hash(p: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def asset_entry(a: dict) -> dict:
    digest = a.get("digest") or ""
    sha = digest.removeprefix("sha256:") if digest.startswith("sha256:") else None
    if not sha:  # older releases have no digest, hash the file ourselves
        tmp = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "pfb-hash" / a["name"]
        sha = file_sha256(download(a["browser_download_url"], tmp))
        tmp.unlink()
    return {"name": a["name"], "url": a["browser_download_url"], "sha256": sha, "size": a["size"]}


# ---------------------------------------------------------------- lock

def cmd_lock(b: Build, args):
    old = json.loads(b.lock_path.read_text()) if b.lock_path.exists() else {}
    pins = b.cfg.get("pins") or {}

    def pick(repo, pin, need):
        for r in releases(repo):
            v = r["tag_name"].lstrip("v")
            if not satisfies(v, pin):
                continue
            files = need(r)
            if files is not None:
                return r, files
            log(f"  {repo} {r['tag_name']}: required assets missing, trying the previous release")
        die(f"{repo}: no release matching {pin or 'any version'} with the required assets")

    hosts, core_cache = {}, {}
    for hid, h in b.hosts.items():
        def need(r, h=h, hid=hid):
            files = {}
            for os_ in b.host_platforms(hid):
                pat = h["asset"].format(host=b.platforms[os_]["host"])
                m = [a for a in r["assets"] if fnmatch.fnmatch(a["name"], pat)]
                if len(m) != 1:
                    return None
                files[os_] = m[0]
            return files
        r, files = pick(h["repo"], pins.get(hid), need)
        key = (h["repo"], r["tag_name"])
        if key not in core_cache:
            core_cache[key] = core_version(b, h["repo"], r["tag_name"])
        hosts[hid] = {
            "repo": h["repo"], "version": r["tag_name"].lstrip("v"), "tag": r["tag_name"],
            "release_url": r["html_url"], "core": core_cache[key],
            "files": {os_: asset_entry(a) for os_, a in files.items()},
        }
        log(f"host {hid}: {hosts[hid]['version']} (core {hosts[hid]['core']['version']})")

    packages = {}

    def add_package(name, kind):
        repo = b.repo_of(name)

        def need(r):
            v = r["tag_name"].lstrip("v")
            files = {}
            for os_, p in b.platforms.items():
                fn = f"{name}-{v}-{p['conda'].replace('-', '_')}.tar.bz2"
                m = [a for a in r["assets"] if a["name"] == fn]
                if not m:
                    return None
                files[os_] = m[0]
            return files
        r, files = pick(repo, pins.get(name), need)
        packages[name] = {
            "kind": kind, "repo": repo, "version": r["tag_name"].lstrip("v"), "tag": r["tag_name"],
            "release_url": r["html_url"], "channel": b.component_channel(name),
            "files": {os_: asset_entry(a) for os_, a in files.items()},
        }
        log(f"{kind} {name}: {packages[name]['version']}")

    plugins = list(b.cfg["plugins"])
    for n in plugins:
        add_package(n, "plugin")
    modules = b.cfg.get("modules", "auto")
    if modules == "auto":  # modules of the languages used by the standard plugins (`language` in their manifests)
        modules = []
        for n in plugins:
            lang = package_manifest(args, packages[n]["files"]["linux"]).get("language")
            mod = (b.cfg.get("languages") or {}).get(lang)
            if not mod:
                die(f"{n}: language {lang!r} is not listed in `languages` of build.yaml")
            if mod not in modules:
                modules.append(mod)
            log(f"  {n}: language {lang} -> {mod}")
    for n in modules:
        add_package(n, "module")

    # The composition is stored in the lock too: meta-packages of past releases are rebuilt from their locks.
    new = {"build": b.id, "composition": {"modules": modules, "plugins": plugins},
           "hosts": hosts, "packages": packages}
    changes = diff_lock(old, new)
    if old and json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True):
        log("lock unchanged")
    else:
        b.lock_path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        log(f"wrote {b.lock_path.relative_to(ROOT)}: {len(changes)} version changes")
    if args.summary:
        write_summary(b, old, new, changes, Path(args.summary))


def package_manifest(args, entry: dict) -> dict:
    """Manifest (.pplugin/.pmodule) from the root of a conda package."""
    with tarfile.open(pkg_file(args, entry)) as t:
        m = next((m for m in t.getmembers() if "/" not in m.name and m.name.endswith((".pplugin", ".pmodule"))), None)
        if not m:
            die(f"{entry['name']} has no manifest")
        return json.loads(t.extractfile(m).read().decode("utf-8-sig"))


def core_version(b: Build, repo: str, tag: str) -> dict:
    core = b.cfg["core"]
    sha = api(f"repos/{repo}/contents/{core['submodule']}?ref={tag}")["sha"]
    try:
        with http(f"https://raw.githubusercontent.com/{core['repo']}/{sha}/version.txt") as r:
            ver = r.read().decode().strip()
    except urllib.error.HTTPError:
        ver = "unknown"
    return {"version": ver, "commit": sha}


def lock_versions(lock: dict) -> dict[str, tuple[str, str, str]]:
    """Component to (version, repository, kind). Hosts from one repository count as one component."""
    out = {}
    for h in lock.get("hosts", {}).values():
        out[h["repo"].split("/")[1]] = (h["version"], h["repo"], "host")
    for n, p in lock.get("packages", {}).items():
        out[n] = (p["version"], p["repo"], p["kind"])
    return out


def diff_lock(old: dict, new: dict) -> list[tuple[str, str | None, str | None, str]]:
    a, b = lock_versions(old), lock_versions(new)
    out = []
    for n in list(b) + [n for n in a if n not in b]:
        va, vb = a.get(n, (None,))[0], b.get(n, (None,))[0]
        if va != vb:
            out.append((n, va, vb, (b.get(n) or a.get(n))[1]))
    return out


def write_summary(b: Build, old, new, changes, out_dir: Path):
    """Title and body of the bot PR: title.txt, body.md, changed.txt."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scope = b.dir.name
    composition = old.get("composition") != new.get("composition")
    feat = composition or any(not va or not vb or ver_tuple(va)[:2] != ver_tuple(vb)[:2] for _, va, vb, _ in changes)
    typ = "feat" if feat else "fix"
    if len(changes) == 1:
        n, va, vb, _ = changes[0]
        title = f"{typ}({scope}): bump {n} {va or 'new'} -> {vb or 'removed'}"
    elif changes:
        title = f"{typ}({scope}): bump {len(changes)} components"
    else:
        title = f"{typ}({scope}): update lock"
    body = [f"Updates `builds/{scope}/build.lock.json` to the latest component releases.", "",
            "| Component | Old | New |", "|---|---|---|"]
    for n, va, vb, repo in changes:
        body.append(f"| [{n}](https://github.com/{repo}) | {va or '-'} | {vb or '-'} |")
    for n, va, vb, repo in changes:
        if not vb:
            continue
        notes = [r for r in releases(repo)
                 if (not va or ver_tuple(r["tag_name"]) > ver_tuple(va)) and ver_tuple(r["tag_name"]) <= ver_tuple(vb)]
        body += ["", f"<details><summary>{n} {va or ''} -> {vb}</summary>", ""]
        for r in notes:
            body += [f"### [{r['tag_name']}]({r['html_url']})", "", (r.get("body") or "").strip(), ""]
        body += ["</details>"]
    body += ["", "Merge manually after the build, smoke test and upgrade test pass."]
    (out_dir / "title.txt").write_text(title + "\n")
    (out_dir / "body.md").write_text("\n".join(body) + "\n")
    changed = json.dumps(old, sort_keys=True) != json.dumps(new, sort_keys=True)
    (out_dir / "changed.txt").write_text("1\n" if changed else "0\n")
    log(title)


# ---------------------------------------------------------------- fetch / micromamba

def cache_dir(args) -> Path:
    return Path(args.cache).resolve()


def work_dir(args) -> Path:
    return Path(args.work).resolve()


def host_archive(b: Build, args, host: str, os_: str) -> Path:
    f = b.lock["hosts"][host]["files"][os_]
    return download(f["url"], cache_dir(args) / "hosts" / f["name"], f["sha256"])


def pkg_file(args, entry: dict) -> Path:
    return download(entry["url"], cache_dir(args) / "pkgs" / entry["name"], entry["sha256"])


def wanted_platforms(b: Build, args) -> list[str]:
    return [args.platform] if getattr(args, "platform", None) else list(b.platforms)


def cmd_fetch(b: Build, args):
    for os_ in wanted_platforms(b, args):
        for host in b.hosts:
            if os_ in b.host_platforms(host):
                host_archive(b, args, host, os_)
        for p in b.lock["packages"].values():
            pkg_file(args, p["files"][os_])
    micromamba(b, args)
    log("fetch: all files downloaded, sha256 verified")


def micromamba(b: Build, args) -> Path:
    """micromamba of the same version as the host: taken from the Linux archive of host micromamba_from."""
    if getattr(args, "micromamba", None):
        return Path(args.micromamba).resolve()
    host = b.cfg.get("micromamba_from", "launcher")
    arc = host_archive(b, args, host, "linux")
    dest = cache_dir(args) / "tools" / f"micromamba-{file_sha256(arc)[:12]}"
    if not dest.exists():
        member = f"{b.cfg['layout']['base']}/bin/{b.platforms['linux']['host']}/micromamba"
        with tarfile.open(arc) as t:
            m = next((m for m in t.getmembers() if m.name.lstrip("./") == member), None)
            if not m:
                die(f"{arc.name} has no {member}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(t.extractfile(m).read())
        dest.chmod(0o755)
    return dest


def mamba_env(args) -> dict:
    work = work_dir(args)
    return dict(os.environ,
                MAMBA_ROOT_PREFIX=str(work / "mamba-root"),
                CONDA_PKGS_DIRS=str(cache_dir(args) / "pkgs"),
                HOME=str(work / "home"))  # micromamba writes ~/.conda/environments.txt, keep it out of the real home


def run_mamba(b: Build, args, *cmd, check=True) -> subprocess.CompletedProcess:
    # A meta-package of the same version can be rebuilt with other dependencies (locally, between runs), and
    # micromamba reuses a cached package by file name. Drop them and the repodata cache so the current channel is read.
    pkgs = cache_dir(args) / "pkgs"
    for d in [*pkgs.glob(f"{b.id}-*"), pkgs / "cache"]:
        shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
    r = subprocess.run([str(micromamba(b, args)), *cmd], env=mamba_env(args), capture_output=True, text=True)
    if check and r.returncode:
        log(r.stdout, r.stderr)
        die(f"micromamba {cmd[0]} exited with code {r.returncode}")
    return r


# ---------------------------------------------------------------- meta-packages and channel

def meta_packages(build_id: str, lock: dict, cver: str):
    """Build meta-packages from the lock composition: {id}-modules and {id}-plugins with exact dependencies.

    Returns (defs, names): defs is {meta-package: ["pkg ==X", ...]}, names is {environment: meta-package}.
    """
    pk = lock["packages"]
    names = {kind: f"{build_id}-{kind}" for kind in ENV_KINDS}
    defs = {names[k]: [f"{p} =={pk[p]['version']}" for p in lock["composition"][k]] for k in ENV_KINDS}
    return defs, names


def write_meta_package(path: Path, name: str, version: str, depends: list[str], license_: str):
    """A package without files, only info/. Deterministic (mtime=0) so its sha256 is stable between runs."""
    index = {"name": name, "version": version, "build": "0", "build_number": 0, "depends": depends,
             "noarch": "generic", "subdir": "noarch", "license": license_, "timestamp": 0}
    files = {"info/index.json": json.dumps(index, indent=2).encode(),
             "info/paths.json": b'{"paths": [], "paths_version": 1}',
             "info/files": b""}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
        for n, data in files.items():
            ti = tarfile.TarInfo(n)
            ti.size, ti.mtime, ti.mode = len(data), 0, 0o644
            t.addfile(ti, io.BytesIO(data))
    path.write_bytes(bz2.compress(buf.getvalue(), 9))
    return index


def channel_dir(b: Build, args, channel: str) -> Path:
    return work_dir(args) / "channel" / b.channel_name(channel)


def history_entries(args) -> list[tuple[str, dict]]:
    """Past builds for the channel: <version>.json files (their build.lock.json) in the --history folder."""
    out = []
    if getattr(args, "history", None):
        for f in sorted(Path(args.history).glob("*.json")):
            out.append((f.stem, json.loads(f.read_text())))
    return out


def cmd_channel(b: Build, args):
    version = args.version or b.default_version()
    entries = history_entries(args) + ([] if args.only_history else [(version, b.lock)])
    if not entries:
        die("empty channel: no current build and no history")
    root = channel_dir(b, args, args.channel)
    shutil.rmtree(root, ignore_errors=True)
    subdirs = ["noarch"] + [p["conda"] for p in b.platforms.values()]
    repodata = {s: {} for s in subdirs}
    for d in subdirs:
        (root / d).mkdir(parents=True)

    def add(sub, f: Path, index: dict):
        repodata[sub][f.name] = dict(index, subdir=sub, size=f.stat().st_size,
                                     sha256=file_sha256(f), md5=file_hash(f, "md5"))

    for ver, lock in entries:
        cver = conda_version(ver)
        for os_, plat in b.platforms.items():
            sub = plat["conda"]
            for p in lock["packages"].values():
                e = p["files"].get(os_)
                if not e or e["name"] in repodata[sub]:
                    continue
                dst = root / sub / e["name"]
                shutil.copy2(pkg_file(args, e), dst)
                with tarfile.open(dst) as t:
                    index = json.load(t.extractfile("info/index.json"))
                add(sub, dst, index)
        defs, _ = meta_packages(b.id, lock, cver)
        for name, deps in defs.items():
            dst = root / "noarch" / f"{name}-{cver}-0.tar.bz2"
            index = write_meta_package(dst, name, cver, deps, b.cfg.get("license", "MIT"))
            add("noarch", dst, index)
        log(f"channel {b.channel_name(args.channel)}: {ver}: {', '.join(f'{n} {cver}' for n in defs)}")
    for sub, pkgs in repodata.items():
        (root / sub / "repodata.json").write_text(json.dumps(
            {"info": {"subdir": sub}, "packages": dict(sorted(pkgs.items())), "packages.conda": {},
             "repodata_version": 1}, indent=1) + "\n")
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    log(f"channel: {root} ({size / 2**20:.1f} MiB, {len(entries)} build versions)")


# ---------------------------------------------------------------- envs

def env_dir(b: Build, args, os_: str, meta: str, cver: str, channel: str) -> Path:
    key = hashlib.sha256(json.dumps([os_, meta, cver, channel, b.lock["packages"]], sort_keys=True).encode())
    return work_dir(args) / "envs" / os_ / f"{meta}-{cver}-{key.hexdigest()[:8]}"


def build_env(b: Build, args, os_: str, meta: str, cver: str, channel: str, dest: Path | None = None) -> Path:
    """Environment from the build channel: micromamba create ... <meta-package>=<version>."""
    dest = dest or env_dir(b, args, os_, meta, cver, channel)
    done = dest.with_name(dest.name + ".ok")
    if done.exists() and dest.exists():
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    chan = channel_dir(b, args, channel)
    if not (chan / "noarch" / "repodata.json").exists():
        die(f"channel {chan} is not built, run `pfb.py channel` first")
    log(f"envs: {os_}: {meta}={cver}")
    run_mamba(b, args, "create", "-y", "--prefix", str(dest), "--no-rc", "--always-copy", "--offline",
              "--override-channels", "-c", chan.as_uri(), "--platform", b.platforms[os_]["conda"],
              f"{meta}={cver}")
    publish_channel_urls(dest, chan.as_uri(), b.channel_url(channel))
    done.touch()
    return dest


def publish_channel_urls(prefix: Path, local: str, public: str):
    """Replace the local channel URL with the public one in conda-meta and history, drop the build command line."""
    for rec in (prefix / "conda-meta").glob("*.json"):
        text = rec.read_text()
        if local in text:
            rec.write_text(text.replace(local, public))
    hist = prefix / "conda-meta" / "history"
    if hist.exists():
        lines = [("# cmd: micromamba create (plugify-builds)" if l.startswith("# cmd:") else l.replace(local, public))
                 for l in hist.read_text().splitlines()]
        hist.write_text("\n".join(lines) + "\n")


def pack_envs(b: Build, args, os_: str, version: str, channel: str, lock=None) -> dict[str, Path]:
    cver = conda_version(version)
    _, names = meta_packages(b.id, lock or b.lock, cver)
    return {k: build_env(b, args, os_, names[k], cver, channel) for k in ENV_KINDS}


def common_args(b: Build, args):
    args.version = args.version or b.default_version()
    return args.version, args.channel


def cmd_envs(b: Build, args):
    version, channel = common_args(b, args)
    for os_ in wanted_platforms(b, args):
        for k, p in pack_envs(b, args, os_, version, channel).items():
            print(f"{os_}\t{k}\t{p}")


# ---------------------------------------------------------------- verify

def read_manifest(p: Path) -> dict:
    text = p.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError:  # in case of jsonc
        return json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))


def env_manifests(envs: dict[str, Path]):
    mods, plugs = {}, {}
    for p in envs.values():
        for f in sorted(p.glob("*.pmodule")):
            m = read_manifest(f)
            mods[m["name"]] = m
        for f in sorted(p.glob("*.pplugin")):
            m = read_manifest(f)
            plugs[m["name"]] = m
    return mods, plugs


def env_records(prefix: Path) -> dict[str, dict]:
    return {d["name"]: d for d in (json.loads(f.read_text()) for f in (prefix / "conda-meta").glob("*.json"))}


def cmd_verify(b: Build, args):
    version, channel = common_args(b, args)
    cver = conda_version(version)
    _, names = meta_packages(b.id, b.lock, cver)
    comp = b.lock["composition"]
    public = b.channel_url(channel)
    errors = []
    for os_ in wanted_platforms(b, args):
        envs = pack_envs(b, args, os_, version, channel)
        for kind, p in envs.items():
            recs = env_records(p)
            want = {**{n: b.lock["packages"][n]["version"] for n in comp[kind]}, names[kind]: cver}
            got = {n: d["version"] for n, d in recs.items()}
            if got != want:
                errors.append(f"{os_}/{kind}: composition {got} != expected {want}")
            seen = {}
            for n, d in recs.items():
                if not d.get("url", "").startswith(public):
                    errors.append(f"{os_}/{kind}: {n}: url {d.get('url')} is not under {public}")
                for f in d.get("files", []):
                    if f in seen:
                        errors.append(f"{os_}/{kind}: file {f} is in both {seen[f]} and {n}")
                    seen[f] = n
        # manifests: every plugin language has a module (checks the auto selection), dependencies and constraints hold
        mods, plugs = env_manifests(envs)
        langs = {m.get("language") for m in mods.values()}
        for n, m in plugs.items():
            if m.get("language") not in langs:
                errors.append(f"{os_}: plugin {n}: no module for language {m.get('language')}")
            for d in m.get("dependencies", []) or []:
                dep = plugs.get(d["name"])
                if dep is None:
                    if not d.get("optional"):
                        errors.append(f"{os_}: plugin {n}: missing required dependency {d['name']}")
                elif not satisfies(dep["version"], d.get("constraints")):
                    errors.append(f"{os_}: plugin {n}: {d['name']} {dep['version']} does not match {d['constraints']}")
            for c in m.get("conflicts", []) or []:
                dep = plugs.get(c["name"])
                if dep and satisfies(dep["version"], c.get("constraints")):
                    errors.append(f"{os_}: plugin {n} conflicts with {c['name']} {dep['version']}")
        log(f"verify {os_}: {len(mods)} modules, {len(plugs)} plugins")
    if errors:
        for e in errors:
            log(f"  FAIL {e}")
        die(f"verify: {len(errors)} errors")
    log("verify: ok")


# ---------------------------------------------------------------- smoke

def plug_states(b: Build, args, base: Path, names: list[str]) -> tuple[dict, str, int]:
    conf = base / "smoke.pconfig"
    conf.write_text(json.dumps({"paths": {"baseDir": str(base), "extensionsDir": "envs"},
                                "logging": {"severity": "error"}}))
    plug = Path(args.plug).resolve()
    # libplugify.so next to plug (CI caches both), in case its RUNPATH points to a build tree that is gone
    ld = os.pathsep.join(filter(None, [str(plug.parent), os.environ.get("LD_LIBRARY_PATH")]))
    r = subprocess.run([str(plug), "-c", str(conf), "-i", "-p"],
                       input="init\nload\nmodules\nplugins\nexit\n", capture_output=True, text=True,
                       cwd=base, timeout=args.timeout, env=dict(os.environ, LC_ALL="C", LD_LIBRARY_PATH=ld))
    out = r.stdout + r.stderr
    (base / "plug.log").write_text(out)
    return parse_states(out, names), out, r.returncode


def cmd_smoke(b: Build, args):
    version, channel = common_args(b, args)
    expect = (b.cfg.get("smoke") or {}).get("expect_failed") or {}
    envs = pack_envs(b, args, "linux", version, channel)
    base = work_dir(args) / "smoke"
    shutil.rmtree(base, ignore_errors=True)
    (base / "envs").mkdir(parents=True)
    for k, p in envs.items():  # a copy at another path, as in the pack: this also checks relocation
        shutil.copytree(p, base / "envs" / k, symlinks=True)
    mods, plugs = env_manifests(envs)
    # plugins with a required (possibly transitive) dependency on an expected failure end up Skipped
    skipped, grew = set(), True
    while grew:
        grew = False
        for n, m in plugs.items():
            if n not in skipped and n not in expect and any(
                    not d.get("optional") and (d["name"] in expect or d["name"] in skipped)
                    for d in m.get("dependencies", []) or []):
                skipped.add(n)
                grew = True
    states, out, rc = plug_states(b, args, base, list(mods) + list(plugs))
    log(f"smoke: exit code {rc}, log {base / 'plug.log'}")
    bad = int(rc != 0)
    for n in list(mods) + list(plugs):
        st = states.get(n, "?")
        if n in skipped:
            ok, note = st == "Skipped", "expected: depends on a plugin that cannot load outside the game"
        elif n in expect:
            m = re.search(expect[n], out)
            ok = st != "Running" and m is not None
            note = f"expected failure ({m.group(0)})" if ok else f"expected a failure matching /{expect[n]}/"
        else:
            ok, note = st == "Running", ""
        bad += not ok
        log(f"  {'ok  ' if ok else 'FAIL'} {n:<14} {st:<10} {note}")
    # micromamba sees the relocated environments the same way the host does on a server (-r <base> -n <env>)
    for k in ENV_KINDS:
        lr = run_mamba(b, args, "list", "--json", "-r", str(base), "-n", k, check=False)
        got = sorted(x["name"] for x in json.loads(lr.stdout or "[]")) if lr.returncode == 0 else None
        ok = got == sorted(env_records(envs[k]))
        bad += not ok
        log(f"  {'ok  ' if ok else 'FAIL'} micromamba list -n {k}: {len(got or [])} packages")
    if bad:
        log("plug errors:")
        for line in re.sub(r"\x1b\[[0-9;]*m", "", out).splitlines():
            if re.search(r"error|fail", line, re.I):
                log(f"  {line.strip()}")
        die(f"smoke: {bad} problems")
    log("smoke: ok")


def parse_states(out: str, names: list[str]) -> dict[str, str]:
    """State of every extension from the `modules`/`plugins` tables of plug: a line with the name and a state word."""
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    states = {}
    words = r"(Running|Loaded|Failed|Corrupted|Unresolved|Disabled|Skipped|Terminated|Started|Exported|Parsed|Resolved|Initialized|Loading|Starting)"
    for line in out.splitlines():
        for n in names:
            if re.search(rf"(^|[\s│|]){re.escape(n)}([\s│|]|$)", line):
                m = re.search(words, line)
                if m:
                    states[n] = m.group(1)
    return states


# ---------------------------------------------------------------- upgrade-test

def foreign_channel(b: Build, args, os_: str, name: str) -> Path:
    """Local channel with one foreign package (latest release of name), like a component channel on a server."""
    root = work_dir(args) / "foreign-channel"
    sub = b.platforms[os_]["conda"]
    for d in ("noarch", sub):
        (root / d).mkdir(parents=True, exist_ok=True)
    fn_suffix = f"-{sub.replace('-', '_')}.tar.bz2"
    r = releases(b.repo_of(name))[0]
    a = next(a for a in r["assets"] if a["name"] == f"{name}-{r['tag_name'].lstrip('v')}{fn_suffix}")
    e = asset_entry(a)
    dst = root / sub / e["name"]
    shutil.copy2(pkg_file(args, e), dst)
    with tarfile.open(dst) as t:
        index = json.load(t.extractfile("info/index.json"))
    pkgs = {e["name"]: dict(index, subdir=sub, size=dst.stat().st_size, sha256=file_sha256(dst),
                            md5=file_hash(dst, "md5"))}
    for d, content in (("noarch", {}), (sub, pkgs)):
        (root / d / "repodata.json").write_text(json.dumps(
            {"info": {"subdir": d}, "packages": content, "packages.conda": {}, "repodata_version": 1}))
    return root


def cmd_upgrade_test(b: Build, args):
    """Environments of a previous build (from --history), then `update --all` from the channel: same as a fresh install.

    Same as on a server: environments relocated to addons/plugify/envs, .condarc in the root prefix (build channel,
    then the channel of the foreign package), commands with -r/-n. Two runs:
      plain     a foreign module (smoke.foreign) is installed into modules before the update and must stay;
                modules dropped from the composition must be removed;
      explicit  dropped modules are installed explicitly (`install <module>`) before the update and must stay.
    """
    version, channel = common_args(b, args)
    prev = dict(history_entries(args))
    if args.prev not in prev:
        die(f"no {args.prev}.json in --history")
    old_lock = prev[args.prev]
    chan = channel_dir(b, args, channel)
    foreign = (b.cfg.get("smoke") or {}).get("foreign")
    dropped = [m for m in old_lock["composition"]["modules"] if m not in b.lock["composition"]["modules"]]
    fails = 0
    for os_ in wanted_platforms(b, args):
        old_envs = pack_envs(b, args, os_, args.prev, channel, lock=old_lock)
        new_envs = pack_envs(b, args, os_, version, channel)
        fch = foreign_channel(b, args, os_, foreign) if foreign else None
        for mode in ("plain", "explicit") if dropped else ("plain",):
            base = work_dir(args) / "upgrade" / f"{os_}-{mode}"
            shutil.rmtree(base, ignore_errors=True)
            for k, p in old_envs.items():
                shutil.copytree(p, base / "envs" / k, symlinks=True)
            chans = [chan.as_uri()] + ([fch.as_uri()] if fch else [])
            (base / ".condarc").write_text("channel_priority: strict\nchannels:\n" + "".join(f"  - {c}\n" for c in chans))
            plat = ["--platform", b.platforms[os_]["conda"]]
            extra = {}
            if foreign and mode == "plain":
                run_mamba(b, args, "install", "-y", "--offline", "-r", str(base), "-n", "modules", *plat, foreign)
                extra[foreign] = env_records(base / "envs" / "modules")[foreign]["version"]
            if mode == "explicit":
                run_mamba(b, args, "install", "-y", "--offline", "-r", str(base), "-n", "modules", *plat, *dropped)
                extra.update({m: old_lock["packages"][m]["version"] for m in dropped})
            for k in ENV_KINDS:
                run_mamba(b, args, "update", "-y", "--offline", "-r", str(base), "-n", k, *plat, "--all")
            where = f"{os_}/{mode}"
            for k in ENV_KINDS:
                got = {n: d["version"] for n, d in env_records(base / "envs" / k).items()}
                want = {n: d["version"] for n, d in env_records(new_envs[k]).items()}
                if k == "modules":
                    want.update(extra)
                ok = got == want
                fails += not ok
                note = "equals a fresh install" + (f" plus {', '.join(extra)}" if k == "modules" and extra else "")
                log(f"  {'ok  ' if ok else 'FAIL'} {where}/{k}: after update --all " + (note if ok else f"{got} != {want}"))
                # no ghost records or stray files: everything listed in conda-meta exists, root manifests are owned
                recs = env_records(base / "envs" / k)
                owned = {f for d in recs.values() for f in d.get("files", [])}
                missing = [f for f in owned if not (base / "envs" / k / f).exists()]
                stray = [p.name for p in (base / "envs" / k).glob("*.p*") if p.name not in owned]
                ok = not missing and not stray
                fails += not ok
                log(f"  {'ok  ' if ok else 'FAIL'} {where}/{k}: files match conda-meta"
                    + ("" if ok else f" (missing: {missing[:5]}, stray: {stray})"))
    if fails:
        die(f"upgrade-test: {fails} problems")
    log(f"upgrade-test: {args.prev} -> {version} ok" + (f" (dropped modules: {', '.join(dropped)})" if dropped else ""))


# ---------------------------------------------------------------- assemble

def build_json(b: Build, host: str, os_: str, version: str, channel: str) -> str:
    """Host passport: which build it came with, host and core versions. The environment contents are in the meta-packages."""
    h = b.lock["hosts"][host]
    return json.dumps({
        "schema": 1,
        "build": b.id,
        "title": b.cfg["title"],
        "version": version,
        "channel": channel,
        "host": host,
        "platform": os_,
        "host_package": {"name": h["repo"].split("/")[1], "version": h["version"]},
        "core": h["core"]["version"],
        "conda_channel": b.channel_url(channel) + "/",
    }, indent=2, ensure_ascii=False) + "\n"


def condarc(b: Build, channel: str) -> str:
    chans = [b.channel_url(c) + "/" for c in (["dev", "stable"] if channel == "dev" else ["stable"])]
    chans += [b.component_channel(n) for n in b.lock["packages"]]
    return ("# Plugify channels (build " + b.id + "): the build channel first, then the component channels.\n"
            "# strict: a package present in the build channel is taken from it (the version tested in the build).\n"
            "# For a newer version use: mamba install -n <env> -c \"<component channel>\" <package>\n"
            "# This file is replaced by the host archive on update.\n"
            "channel_priority: strict\n"
            "channels:\n" + "".join(f"  - {c}\n" for c in chans))


def is_elf(p: Path) -> bool:
    with open(p, "rb") as f:
        return f.read(4) == b"\x7fELF"


def stage_pack(b: Build, args, variant, host, os_, version, channel) -> Path:
    name = b.file_name(variant, host, os_, version)
    stage = work_dir(args) / "stage" / name.removesuffix("." + b.platforms[os_]["archive"])
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    with tarfile.open(host_archive(b, args, host, os_)) as t:
        t.extractall(stage, filter="data")
    if os_ == "linux":  # host archives ship executables without +x (s2launcher, micromamba, crashpad_handler)
        for p in stage.rglob("*"):
            if p.is_file() and is_elf(p):
                p.chmod(p.stat().st_mode | 0o111)
    base = stage / b.cfg["layout"]["base"]
    if variant == "full":
        envs_root = base / b.cfg["layout"]["envs"]
        for k, p in pack_envs(b, args, os_, version, channel).items():
            shutil.copytree(p, envs_root / k, symlinks=True)
    # pkgs/ stays empty (no cache in the pack), but must exist: micromamba.exe 2.3.2 fails with
    # "Specified pkgs_dir does not exist" on install/update, while the Linux build creates it on its own
    for d in ("configs", "data", "logs", "pkgs"):
        (base / d).mkdir(parents=True, exist_ok=True)
    if variant == "full":  # build translations: full archive only, so host-only never overwrites server edits
        for f in build_data_files(b):
            dst = base / "data" / f.relative_to(b.dir / "data")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
    (base / "build.json").write_text(build_json(b, host, os_, version, channel))
    (base / ".condarc").write_text(condarc(b, channel))
    return stage


def build_data_files(b: Build) -> list[Path]:
    """Files of builds/<build>/data/ go to addons/plugify/data/ of the full archive. Gamedata and settings are
    rejected: the data/ layer overrides the package and would hide new signatures or settings of an updated plugin."""
    src = b.dir / "data"
    files = sorted(f for f in src.rglob("*") if f.is_file()) if src.is_dir() else []
    bad = [f.name for f in files if f.name.startswith(("gamedata", "settings"))]
    if bad:
        die(f"gamedata and settings are not allowed in {src}: {bad}")
    return files


def pack_archive(stage: Path, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    entries = sorted(stage.rglob("*"))
    if out.name.endswith(".tar.gz"):
        def norm(ti: tarfile.TarInfo):
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            return ti
        with tarfile.open(tmp, "w:gz", compresslevel=6) as t:
            for p in entries:
                t.add(p, arcname=p.relative_to(stage).as_posix(), recursive=False, filter=norm)
    else:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in entries:
                rel = p.relative_to(stage).as_posix()
                if p.is_dir():
                    z.writestr(zipfile.ZipInfo(rel + "/"), "")
                else:
                    z.write(p, rel)
    tmp.replace(out)


def check_tree(b: Build, stage: Path, variant: str, os_: str):
    base = b.cfg["layout"]["base"]
    mm = "micromamba.exe" if os_ == "windows" else "micromamba"
    must = [f"{base}/bin/{b.platforms[os_]['host']}/{mm}", f"{base}/build.json", f"{base}/.condarc"]
    envs = [f"{base}/{b.cfg['layout']['envs']}/{k}/conda-meta" for k in ENV_KINDS]
    missing = [m for m in must + (envs if variant == "full" else []) if not (stage / m).exists()]
    if missing:
        die(f"{stage.name} is missing: {missing}")
    if variant != "full" and (stage / base / b.cfg["layout"]["envs"]).exists():
        die(f"the {variant} archive must not contain {base}/envs")


def cmd_assemble(b: Build, args):
    version, channel = common_args(b, args)
    out = Path(args.out).resolve()
    for variant, host, os_ in b.matrix():
        if (args.variant and variant != args.variant) or (args.host and host != args.host) or \
                (args.platform and os_ != args.platform):
            continue
        name = b.file_name(variant, host, os_, version)
        stage = stage_pack(b, args, variant, host, os_, version, channel)
        check_tree(b, stage, variant, os_)
        pack_archive(stage, out / name)
        if not args.keep_stage:
            shutil.rmtree(stage)
        log(f"assemble: {name} ({(out / name).stat().st_size / 2**20:.1f} MiB)")


# ---------------------------------------------------------------- meta / index

def release_object(b: Build, args, files: list[Path]) -> dict:
    """Release object in the format read by the website (index.json, builds.<id>.releases[])."""
    lock = b.lock
    comps, seen = [], set()
    for h in lock["hosts"].values():
        n = h["repo"].split("/")[1]
        if n in seen:
            continue
        seen.add(n)
        comps.append({"name": n, "kind": "host", "version": h["version"], "repo": h["repo"],
                      "tag": h["tag"], "release_url": h["release_url"]})
    for n, p in lock["packages"].items():
        comps.append({"name": n, "kind": p["kind"], "version": p["version"], "repo": p["repo"],
                      "tag": p["tag"], "release_url": p["release_url"]})
    cores = sorted({h["core"]["version"] for h in lock["hosts"].values()})
    entries = []
    for f in files:
        m = re.match(rf"^{re.escape(b.id)}-([a-z0-9]+)-([a-z0-9]+)-.*?(-host-only)?\.(tar\.gz|zip)$", f.name)
        entries.append({"name": f.name, "variant": "host-only" if m.group(3) else "full",
                        "host": m.group(1), "os": m.group(2),
                        "size": f.stat().st_size, "sha256": file_sha256(f),
                        "url": f"https://github.com/{args.repo}/releases/download/{args.tag}/{f.name}"})
    return {
        "version": args.version, "channel": args.channel, "tag": args.tag,
        "date": args.date or datetime.now(timezone.utc).date().isoformat(),
        "url": f"https://github.com/{args.repo}/releases/tag/{args.tag}",
        "changelog": Path(args.changelog).read_text().strip() if args.changelog else "",
        "core": cores[0] if len(cores) == 1 else cores,
        "channel_url": b.channel_url(args.channel) + "/",
        "packages": {k: list(lock["composition"][k]) for k in ENV_KINDS},
        "components": comps,
        "files": entries,
    }


def cmd_meta(b: Build, args):
    out = Path(args.out).resolve()
    files = sorted(p for p in out.iterdir() if p.name.startswith(b.id + "-") and p.suffix in (".gz", ".zip"))
    if not files:
        die(f"no archives in {out}")
    (out / "SHA256SUMS").write_text("".join(f"{file_sha256(f)}  {f.name}\n" for f in files))
    rel = release_object(b, args, files)
    (out / "builds.json").write_text(json.dumps({"schema": 1, "build": b.id, "release": rel},
                                                indent=2, ensure_ascii=False) + "\n")
    shutil.copy2(b.lock_path, out / "build.lock.json")  # later releases rebuild the channel from it
    log(f"meta: SHA256SUMS, builds.json, build.lock.json ({len(files)} archives)")


def stable_releases(repo: str, build_id: str) -> list[dict]:
    """All stable releases of the build in the distribution repository."""
    out, page = [], 1
    try:
        api(f"repos/{repo}")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # no repository yet (local build), so no history
            return []
        raise
    while rels := api(f"repos/{repo}/releases?per_page=100&page={page}"):
        out += [r for r in rels if not r["draft"] and not r["prerelease"]
                and r["tag_name"].startswith(f"{build_id}-v")]
        page += 1
    return out


def cmd_history(b: Build, args):
    """Download build.lock.json of every stable release into --history as <version>.json, for the channel."""
    out = Path(args.history)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in stable_releases(args.repo, b.id):
        a = next((a for a in r["assets"] if a["name"] == "build.lock.json"), None)
        if not a:
            continue
        ver = r["tag_name"].removeprefix(f"{b.id}-v")
        with http(a["url"], accept="application/octet-stream") as f:
            (out / f"{ver}.json").write_bytes(f.read())
        n += 1
    log(f"history: {n} previous builds in {out}")
    if args.latest_file:
        vers = sorted((p.stem for p in out.glob("*.json")), key=ver_tuple)
        Path(args.latest_file).write_text((vers[-1] if vers else "") + "\n")


def cmd_index(_b, args):
    """index.json for the website: every release of every build (from the builds.json asset), only the latest dev."""
    builds = {}
    for d in sorted((ROOT / "builds").iterdir()):
        if (d / "build.yaml").exists():
            bb = Build(d.name)
            builds[bb.id] = {"title": bb.cfg["title"], "games": bb.cfg.get("games", []),
                             "repo": args.repo, "releases": []}
    page = 1
    while rels := api(f"repos/{args.repo}/releases?per_page=100&page={page}"):
        for r in rels:
            a = next((a for a in r["assets"] if a["name"] == "builds.json"), None)
            if r["draft"] or not a:
                continue
            with http(a["url"], accept="application/octet-stream") as f:
                data = json.load(f)
            if data.get("build") in builds:
                rel = data["release"]
                rel["date"] = (r.get("published_at") or rel["date"])[:10]
                builds[data["build"]]["releases"].append(rel)
        page += 1
    for bd in builds.values():
        rs = sorted(bd["releases"], key=lambda r: (r["date"], ver_tuple(r["version"])), reverse=True)
        devs = [r for r in rs if r["channel"] == "dev"]
        bd["releases"] = [r for r in rs if r["channel"] != "dev" or r is devs[0]]
    index = {"schema": 1, "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "builds": builds}
    Path(args.out).write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
    log(f"index: {args.out}")


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", default="source2", help="folder in builds/ (default: source2)")
    ap.add_argument("--cache", default=str(ROOT / ".cache"), help="download cache")
    ap.add_argument("--work", default=str(ROOT / "work"), help="work folder (channel, environments, staging)")
    ap.add_argument("--micromamba", help="use this micromamba instead of the one from the host archive")
    ap.add_argument("--lock", help="lock file instead of builds/<build>/build.lock.json (for example, a dev release lock)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def versioned(p):
        p.add_argument("--version", help="build version (default: from .release-please-manifest.json)")
        p.add_argument("--channel", default="stable", choices=["stable", "dev"])
        p.add_argument("--history", help="folder with build.lock.json of previous builds (<version>.json)")

    p = sub.add_parser("lock")
    p.add_argument("--summary", help="folder for title.txt/body.md/changed.txt (bot PR)")
    p = sub.add_parser("fetch")
    p.add_argument("--platform")
    p = sub.add_parser("channel")
    versioned(p)
    p.add_argument("--only-history", action="store_true", help="only previous builds from --history (no current lock)")
    for name in ("envs", "verify"):
        p = sub.add_parser(name)
        versioned(p)
        p.add_argument("--platform")
    p = sub.add_parser("smoke")
    versioned(p)
    p.add_argument("--plug", required=True, help="core test app plug")
    p.add_argument("--timeout", type=int, default=300)
    p = sub.add_parser("upgrade-test")
    versioned(p)
    p.add_argument("--prev", required=True, help="previous build version (<version>.json in --history)")
    p.add_argument("--platform")
    p = sub.add_parser("assemble")
    versioned(p)
    p.add_argument("--out", default=str(ROOT / "dist"))
    p.add_argument("--variant", choices=VARIANTS)
    p.add_argument("--host")
    p.add_argument("--platform")
    p.add_argument("--keep-stage", action="store_true")
    p = sub.add_parser("meta")
    p.add_argument("--version", required=True)
    p.add_argument("--channel", default="stable", choices=["stable", "dev"])
    p.add_argument("--tag", required=True)
    p.add_argument("--repo", default="untrustedmodders/plugify-builds")
    p.add_argument("--out", default=str(ROOT / "dist"))
    p.add_argument("--changelog", help="release changelog file (Markdown)")
    p.add_argument("--date")
    p = sub.add_parser("history")
    p.add_argument("--history", required=True)
    p.add_argument("--repo", default="untrustedmodders/plugify-builds")
    p.add_argument("--latest-file", help="write the latest stable build version here")
    p = sub.add_parser("index")
    p.add_argument("--repo", default="untrustedmodders/plugify-builds")
    p.add_argument("--out", default="index.json")

    args = ap.parse_args()
    b = Build(args.build)
    if args.lock:
        b.lock_path = Path(args.lock).resolve()
    globals()[f"cmd_{args.cmd.replace('-', '_')}"](b, args)


if __name__ == "__main__":
    main()
