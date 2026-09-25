# Draft formula for wmxscott/homebrew-tap. Fill in url and sha256 once v1.0.0 is tagged:
#   curl -sL https://github.com/wmxscott/pr-tracker/archive/refs/tags/v1.0.0.tar.gz | shasum -a 256
class PrTracker < Formula
  include Language::Python::Virtualenv

  desc "Track the pull requests your coding agents open and report check changes"
  homepage "https://github.com/wmxscott/pr-tracker"
  url "https://github.com/wmxscott/pr-tracker/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"
  head "https://github.com/wmxscott/pr-tracker.git", branch: "main"

  depends_on "fzf"
  depends_on "gh"
  depends_on "python@3.14"

  def install
    virtualenv_install_with_resources
  end

  # launchd (or systemd) only supplies the tick. `pr-tracker refresh` exits at
  # once unless refresh.interval_seconds (default 300, plus jitter) has passed.
  service do
    run [opt_bin/"pr-tracker", "refresh"]
    run_type :interval
    interval 60
    process_type :background
    environment_variables PATH: std_service_path_env
    log_path var/"log/pr-tracker.log"
    error_log_path var/"log/pr-tracker.log"
  end

  test do
    assert_match "pr-tracker #{version}", shell_output("#{bin}/pr-tracker --version")
    assert_match "prs #{version}", shell_output("#{bin}/prs --version")

    ENV["XDG_STATE_HOME"] = testpath/"state"
    ENV["XDG_CONFIG_HOME"] = testpath/"config"

    hook_input = <<~JSON
      {"session_id": "t", "cwd": "#{testpath}", "hook_event_name": "PostToolUse",
       "tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"},
       "tool_response": {"stdout": "https://github.com/octo/demo/pull/7\\n"}}
    JSON
    assert_empty pipe_output("#{bin}/pr-tracker hook post-bash", hook_input, 0)
    assert_empty pipe_output("#{bin}/pr-tracker hook stop", "not json", 0)

    assert_match "octo/demo", shell_output("#{bin}/pr-tracker list --scope all")
    assert_match "1 (1 open)", shell_output("#{bin}/pr-tracker status")
    assert_match "#7", shell_output("#{bin}/prs --print --session t")
  end
end
