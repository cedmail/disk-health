# Homebrew formula for Disk Health. This repository doubles as its own tap:
#
#   brew tap <owner>/disk-health <repo git URL>
#   brew install disk-health
#   disk-health setup --open
#
# make-release.sh --tag rewrites the url/tag/revision lines below for each release.
class DiskHealth < Formula
  desc "Keep a Mac disk healthy: growth, caches, unused apps, Homebrew & mise, in a local dashboard"
  homepage "https://github.com/cedmail/disk-health"
  url "https://github.com/cedmail/disk-health.git",
      using:    :git,
      tag:      "v1.2.0",
      revision: "5d6464aa4e2318bdb384575008ba53262499e9b8"
  license "MIT"
  head "https://github.com/cedmail/disk-health.git", branch: "main"

  depends_on :macos

  def install
    libexec.install Dir["*"] - %w[Formula tests dist docs]
    # Launcher for Full Disk Access; `disk-health setup` copies it to ~/Applications/DiskHealth.app,
    # only when launcher.c changed (a new binary would lose the Full Disk Access grant).
    system ENV.cc, "-O2", "-Wall", "-Wextra", "-o", libexec/"launcher/DiskHealth", libexec/"launcher/launcher.c"
    (libexec/"launcher/DiskHealth.sha256").write Digest::SHA256.file(libexec/"launcher/launcher.c").hexdigest
    chmod 0755, Dir[libexec/"*.sh"] + [libexec/"bin/disk-health"]
    bin.install_symlink libexec/"bin/disk-health"
  end

  def caveats
    <<~EOS
      Start the background service and open the dashboard (http://127.0.0.1:7777):
        disk-health setup --open

      Then, once:
        - Full Disk Access: System Settings > Privacy & Security > Full Disk Access > +
          > Cmd+Shift+G > ~/Applications/DiskHealth.app, then: disk-health restart
        - Chrome extension (badge + notifications): disk-health extension

      Upgrades: `brew upgrade disk-health`; the service restarts itself on the new version.
      Uninstall: `disk-health uninstall`, then `brew uninstall disk-health`.
    EOS
  end

  test do
    assert_equal version.to_s, shell_output("#{bin}/disk-health version").strip
    system "/usr/bin/python3", "-c", "import sys; sys.path.insert(0, '#{libexec}'); import diskhealth.server"
  end
end
