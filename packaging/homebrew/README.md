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

Needs the maintainer's **Developer ID Application** certificate in the keychain
and notarization credentials stored once with
`xcrun notarytool store-credentials squawk-notary --apple-id <id> --team-id <team>`
(an app-specific password from account.apple.com, not the Apple ID password).

1. Set `CFBundleVersion` and `CFBundleShortVersionString` in
   `helper/Info.plist` to the new version. The helper's self-update compares
   this value, and the release script refuses to run if it doesn't match.
2. Commit, tag and push the release in `elabz/squawk`, e.g. `v0.1.5`.
3. Build the signed, notarized helper:

   ```sh
   packaging/release-helper.sh 0.1.5   # → dist/SquawkPTT-0.1.5.zip + its sha256
   ```

4. Create the GitHub release for the tag with that zip attached:

   ```sh
   gh release create v0.1.5 dist/SquawkPTT-0.1.5.zip --notes-file <notes>
   ```

5. Compute the source tarball checksum:

   ```sh
   curl -fsSL https://github.com/elabz/squawk/archive/refs/tags/v0.1.5.tar.gz \
     | shasum -a 256
   ```

6. In `squawk.rb`, update the top-level `url` + `sha256` (tarball) **and** the
   `helper` resource's `url` + `sha256` (the zip from step 3).
7. Copy the updated `squawk.rb` into the tap's `Formula/` and push.
8. Verify: `brew update && brew upgrade squawk`. Within a few seconds the
   running helper logs `Homebrew installed helper … — updating` and restarts
   on the new version, with its grants intact.

## TCC re-prompts across builds

> Since v0.1.5 the formula installs a **Developer-ID-signed, prebuilt** helper,
> so grants are keyed to the team identity and survive upgrades; the notes below
> apply to ad-hoc builds (source checkouts, the curl installer, v0.1.4 and older).

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
