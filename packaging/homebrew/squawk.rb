class Squawk < Formula
  desc "Voice-to-terminal dictation: hold Space to talk, transcribe, inject into iTerm"
  homepage "https://github.com/elabz/squawk"
  # Pin to a tagged release tarball. On each release, bump `url` + `sha256`
  # together — see packaging/homebrew/README.md for the procedure.
  url "https://github.com/elabz/squawk/archive/refs/tags/v0.1.1.tar.gz"
  sha256 "14b5dbe7daf8c156f754d2d4b41824ae9f61f4e07dd74e1fde43abd0e1d38081"
  license "MIT"

  # xcode (clang / Command Line Tools) compiles the helper during install.
  depends_on xcode: :build
  depends_on :macos
  depends_on "sox"

  def install
    # CLI: a single stdlib-only Python script (no pip, no framework).
    bin.install "squawk.py" => "squawk"

    # Build SquawkPTT.app from source locally. Building on the user's machine is
    # deliberate: a locally compiled binary carries no Gatekeeper quarantine, so
    # no Apple notarization, Developer account, or signing certificate is needed.
    # The shared build.sh ad-hoc-signs it with a stable identifier
    # (sh.squawk.ptt). Note the identifier is stable but the ad-hoc CDHash is
    # not: it tracks the compiled bytes, so any source change — or switching
    # between a plain-clang build and this Homebrew-superenv one — re-prompts
    # for Microphone / Accessibility once. `squawk install-agent` discovers the
    # result under libexec/squawk.
    helper = libexec/"squawk"
    helper.install "helper/squawkptt.m", "helper/Info.plist",
                   "helper/build.sh", "helper/sh.squawk.ptt.plist"
    system "/bin/sh", helper/"build.sh", helper/"SquawkPTT.app"
  end

  def caveats
    <<~EOS
      squawk installed the CLI and built the SquawkPTT helper. To finish setup:

        squawk install-agent   # place SquawkPTT.app in ~/Applications + load the agent
        squawk setup           # choose your speech-to-text backend
        squawk doctor          # verify permissions and backend

      You will be prompted to grant SquawkPTT: Microphone, Accessibility, and
      Input Monitoring — plus Automation for iTerm. `squawk doctor` prints the
      exact System Settings pane for anything still missing.

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
