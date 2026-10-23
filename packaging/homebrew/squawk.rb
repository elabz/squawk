class Squawk < Formula
  desc "Voice-to-terminal dictation: hold Space to talk, transcribe, inject into iTerm"
  homepage "https://github.com/elabz/squawk"
  # Pin to a tagged release tarball. On each release, bump `url` + `sha256`
  # here AND in the helper resource below — see packaging/homebrew/README.md.
  url "https://github.com/elabz/squawk/archive/refs/tags/v0.1.5.tar.gz"
  sha256 "fcc8150f95cbbf81462519cc4c4f807c5a5957a7d5369f77669c8cd7420589ce"
  license "MIT"

  depends_on :macos
  depends_on "sox"

  # The SquawkPTT helper, prebuilt: signed with the maintainer's Developer ID,
  # notarized and stapled (packaging/release-helper.sh). Shipping it signed —
  # rather than compiling an ad-hoc-signed copy here — is what lets macOS keep
  # the Microphone / Accessibility / Input Monitoring grants across upgrades:
  # they attach to "sh.squawk.ptt from this team", which every release
  # satisfies, instead of to one build's code hash.
  resource "helper" do
    url "https://github.com/elabz/squawk/releases/download/v0.1.5/SquawkPTT-0.1.5.zip"
    sha256 "a2a3e451300c0cf78042560359aa746910098688f8c8a948f34fb25e023037f6"
  end

  def install
    # CLI: a single stdlib-only Python script (no pip, no framework).
    bin.install "squawk.py" => "squawk"

    # `squawk install-agent` finds the prebuilt app and the LaunchAgent template
    # under libexec/squawk. After the first install-agent, the running helper
    # notices each `brew upgrade` here and re-runs install-agent itself.
    helper = libexec/"squawk"
    helper.install "helper/sh.squawk.ptt.plist"
    resource("helper").stage { helper.install "SquawkPTT.app" }
  end

  def caveats
    <<~EOS
      squawk installed the CLI and the signed SquawkPTT helper. First install only:

        squawk install-agent   # place SquawkPTT.app in ~/Applications + load the agent
        squawk setup           # choose your speech-to-text backend
        squawk doctor          # verify permissions and backend

      You will be prompted to grant SquawkPTT: Microphone, Accessibility, and
      Input Monitoring — plus Automation for iTerm. `squawk doctor` prints the
      exact System Settings pane for anything still missing.

      Upgrades need nothing: the running helper picks up the new version by
      itself within a few seconds, and the grants carry over. (Coming from
      0.1.4 or older: run `squawk install-agent` once and re-grant — older
      helpers predate self-update and were signed differently.)

      Before `brew uninstall`, run `squawk uninstall` first: Homebrew cannot
      unload a launchd agent or clear TCC grants, so this removes SquawkPTT.app,
      the LaunchAgent, and lists the privacy entries you may revoke.
    EOS
  end

  test do
    # No subcommand -> argparse prints usage to stderr and exits 2.
    assert_match "usage", shell_output("#{bin}/squawk --help 2>&1")
  end
end
