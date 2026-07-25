# Homebrew packaging

`squawk.rb` is the formula for the `elabz/homebrew-tap` tap. It is kept in
this repo as the source of truth; the tap repo holds a copy at
`Formula/squawk.rb`.

Users install with:

```sh
brew install elabz/tap/squawk
```

The formula installs the `squawk` CLI and compiles `SquawkPTT.app` from source
during `brew install` (locally built ⇒ no Gatekeeper quarantine ⇒ no Apple
notarization or Developer account). The stateful, permission-bound steps —
placing the app, loading the LaunchAgent, TCC grants — are delegated to
`squawk install-agent` / `squawk uninstall`, which the caveats point users to.

## One-time: create the tap

The tap is a public GitHub repo named `homebrew-tap` under the `elabz` account
(so `brew install elabz/tap/squawk` resolves). Once it exists:

```sh
# in a clone of elabz/homebrew-tap
mkdir -p Formula
cp /path/to/squawk/packaging/homebrew/squawk.rb Formula/squawk.rb
git add Formula/squawk.rb && git commit -m "squawk 0.1.0" && git push
```

## Version bump procedure (each release)

1. Tag and push a release in the `elabz/squawk` repo, e.g. `v0.1.0`.
2. Compute the tarball checksum:

   ```sh
   curl -fsSL https://github.com/elabz/squawk/archive/refs/tags/v0.1.0.tar.gz \
     | shasum -a 256
   ```

3. In `squawk.rb`, update **both** `url` (the tag) and `sha256` (the value from
   step 2). These two lines are the entire bump.
4. Copy the updated `squawk.rb` into the tap's `Formula/` and push.
5. Verify: `brew update && brew upgrade squawk` on a machine with the tap tapped.

> The placeholder `sha256` (all zeros) in `squawk.rb` MUST be replaced with the
> real checksum before the formula will install — Homebrew verifies it.

Optional later automation: a GitHub Actions release workflow that computes the
sha and opens the tap PR automatically. Not built yet.

## TCC re-prompts across builds

`build.sh` ad-hoc-signs with the fixed identifier `sh.squawk.ptt`, but macOS
keys an ad-hoc app's privacy grants to its **CDHash**, which tracks the
compiled bytes. Measured on 2026-07-25:

- The build is **deterministic** within one toolchain — same source compiled
  from different directories into different output paths produced an identical
  CDHash.
- Homebrew's build differs from a plain `clang` build. The same source built
  under `brew install` and by hand produced binaries of identical size but
  9078 differing bytes, and therefore different CDHashes — Homebrew's
  `superenv` clang shim injects its own flags/SDK paths.

Practical consequences:

- A user migrating from the hand-run `helper/install.sh` to `brew install`
  **will** be re-prompted for Microphone / Accessibility / Input Monitoring
  once, even though the identifier is unchanged.
- `brew upgrade` re-prompts only when the helper source actually changes (or
  Homebrew's toolchain does); a rebuild of unchanged source under the same
  Homebrew environment yields the same CDHash and keeps the grants.

Call this out in release notes for any version that touches `squawkptt.m`.
